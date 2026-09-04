"""Fetch building footprints from OpenStreetMap (Overpass) and reverse-geocode
administrative names via Nominatim. Both are free services; we identify
ourselves with a User-Agent and fall back across mirrors.

Performance
-----------
These are the slowest calls in the system and the ones a free-tier host can
least afford to repeat, so three things are handled here rather than left to
the caller:

* **One shared ``httpx.AsyncClient``.** A client per request meant a full TLS
  handshake per request; ECDHE key agreement is milliseconds of pure CPU,
  which on ~0.1 CPU is a large fraction of the whole call. Keep-alive also
  lets a mirror fallback reuse an existing connection.
* **``orjson`` for the response body.** A dense 5 km Overpass reply is tens of
  megabytes of JSON; orjson parses it several times faster than the stdlib
  ``resp.json()`` that httpx would otherwise use.
* **A TTL cache plus single-flight.** The cache stores the *parsed features*,
  so a repeat scan skips the network round trip, the JSON parse and the
  element conversion together. Single-flight collapses two simultaneous
  identical scans into one upstream request instead of both paying for it -
  which also keeps us inside Overpass's and Nominatim's politeness limits.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx
import orjson

from ..config import get_settings
from .cache import TTLCache

logger = logging.getLogger(__name__)
settings = get_settings()


class OSMError(RuntimeError):
    """Raised when every Overpass mirror fails."""


# --------------------------------------------------------------------------- #
# Shared HTTP client
# --------------------------------------------------------------------------- #
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


def _client_kwargs() -> dict:
    return {
        # Long enough for a dense Overpass query, short enough that a hung
        # mirror fails over instead of holding a worker for the whole timeout.
        "timeout": httpx.Timeout(settings.http_timeout, connect=10.0),
        "headers": {"User-Agent": settings.user_agent, "Accept": "application/json"},
        "follow_redirects": True,
        "limits": httpx.Limits(max_connections=10, max_keepalive_connections=5,
                               keepalive_expiry=60.0),
    }


async def get_client() -> httpx.AsyncClient:
    """The process-wide client, created on first use inside the running loop."""
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(**_client_kwargs())
    return _client


async def close_client() -> None:
    """Release keep-alive sockets. Called from the app lifespan shutdown."""
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Closing the HTTP client failed: %s", exc)


# --------------------------------------------------------------------------- #
# Caches + single-flight
# --------------------------------------------------------------------------- #
_overpass_cache = TTLCache("overpass", settings.cache_osm_s,
                           max_entries=settings.cache_max_entries,
                           max_bytes=48 * 1024 * 1024)
_geocode_cache = TTLCache("geocode", settings.cache_geocode_s,
                          max_entries=settings.cache_max_entries,
                          max_bytes=8 * 1024 * 1024)
_inflight: dict[str, asyncio.Future] = {}
_inflight_lock = asyncio.Lock()


async def _single_flight(key: str, factory):
    """Run ``factory()`` once per key, letting concurrent callers share it.

    Two visitors scanning the same neighbourhood at the same moment would
    otherwise each pay for a multi-second Overpass query and each burn a slot
    in that service's politeness budget. The second caller awaits the first
    one's future instead.
    """
    async with _inflight_lock:
        existing = _inflight.get(key)
        if existing is not None and not existing.done():
            future = existing
            owner = False
        else:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            _inflight[key] = future
            owner = True

    if not owner:
        try:
            return await asyncio.shield(future)
        except Exception:  # noqa: BLE001 - the owner already logged it
            return await factory()

    try:
        result = await factory()
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
        async with _inflight_lock:
            _inflight.pop(key, None)
        raise
    if not future.done():
        future.set_result(result)
    async with _inflight_lock:
        _inflight.pop(key, None)
    return result


def cache_stats() -> list[dict]:
    return [_overpass_cache.stats(), _geocode_cache.stats()]


def clear_caches() -> None:
    _overpass_cache.clear()
    _geocode_cache.clear()


# --------------------------------------------------------------------------- #
# Overpass
# --------------------------------------------------------------------------- #
def _overpass_query(filter_clause: str) -> str:
    return (
        f"[out:json][timeout:{int(settings.http_timeout)}];"
        f"(way[\"building\"]{filter_clause};"
        f"relation[\"building\"]{filter_clause};);"
        f"out geom;"
    )


async def _post_overpass(query: str) -> dict:
    last_error: str = "no mirrors configured"
    client = await get_client()
    for url in settings.overpass_urls:
        try:
            resp = await client.post(url, data={"data": query})
            if resp.status_code == 429:
                last_error = f"{url}: rate limited (429)"
                logger.warning("Overpass rate limited: %s", url)
                continue
            if resp.status_code >= 400:
                last_error = f"{url}: HTTP {resp.status_code}"
                logger.warning("Overpass error %s from %s", resp.status_code, url)
                continue
            return orjson.loads(resp.content)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{url}: {type(exc).__name__}: {exc}"
            logger.warning("Overpass request failed: %s", last_error)
            continue
    raise OSMError(f"all Overpass mirrors failed ({last_error})")


def _element_to_feature(el: dict) -> Optional[dict]:
    """Convert one Overpass element into a GeoJSON Feature, or None if unusable."""
    tags = el.get("tags") or {}
    building = tags.get("building")
    if not building or building == "roof":  # canopies are not enclosed structures
        return None

    geometry = el.get("geometry")
    if not geometry:
        members = el.get("members")
        if members:
            for m in members:
                if m.get("role") == "outer" and m.get("geometry"):
                    geometry = m["geometry"]
                    break
    if not geometry or len(geometry) < 3:
        return None

    # float() keeps integer-valued coordinates (lon: 76) encoding as 76.0,
    # exactly as the previous implementation did.
    coords = [[float(n["lon"]), float(n["lat"])]
              for n in geometry if "lon" in n and "lat" in n]
    if len(coords) < 3:
        return None
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    if len(coords) < 4:
        return None

    housenumber = tags.get("addr:housenumber")
    street = tags.get("addr:street")
    name = (
        tags.get("name")
        or tags.get("addr:housename")
        or (f"{housenumber} {street}" if housenumber and street else None)
        or f"{str(building).replace('_', ' ').title()} (OSM {el.get('type')}/{el.get('id')})"
    )

    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [coords]},
        "properties": {
            "osm_id": el.get("id"),
            "osm_type": el.get("type"),
            "building_type": building,
            "height": tags.get("height"),
            "levels": tags.get("building:levels"),
            "name": name,
            "tags": tags,
        },
    }


def _parse_elements(data: dict) -> list[dict]:
    elements = data.get("elements")
    if not elements:
        return []
    out: list[dict] = []
    append = out.append
    for el in elements:
        feat = _element_to_feature(el)
        if feat is not None:
            append(feat)
    return out


async def fetch_buildings_in_radius(
    center_lat: float, center_lon: float, radius_km: float = 1.0
) -> list[dict]:
    """All buildings within radius_km of a point, as GeoJSON Features."""
    radius_m = int(radius_km * 1000)
    # 5 dp (~1 m) keeps two genuinely different scan centres apart while
    # collapsing the jitter of a dragged map.
    key = f"r:{center_lat:.5f}:{center_lon:.5f}:{radius_m}"
    cached = _overpass_cache.get(key)
    if cached is not None:
        logger.info("Overpass radius cache hit %s (%s buildings)", key, len(cached))
        return cached

    query = _overpass_query(f"(around:{radius_m},{center_lat},{center_lon})")

    async def _do() -> list[dict]:
        logger.info("Overpass radius query: %.5f,%.5f r=%sm", center_lat, center_lon, radius_m)
        return _parse_elements(await _post_overpass(query))

    features = await _single_flight(key, _do)
    _overpass_cache.set(key, features)
    return features


async def fetch_buildings_in_bbox(
    south: float, west: float, north: float, east: float
) -> list[dict]:
    """All buildings inside a bounding box."""
    key = f"b:{south:.5f}:{west:.5f}:{north:.5f}:{east:.5f}"
    cached = _overpass_cache.get(key)
    if cached is not None:
        logger.info("Overpass bbox cache hit %s (%s buildings)", key, len(cached))
        return cached

    query = _overpass_query(f"({south},{west},{north},{east})")

    async def _do() -> list[dict]:
        logger.info("Overpass bbox query: %s,%s,%s,%s", south, west, north, east)
        return _parse_elements(await _post_overpass(query))

    features = await _single_flight(key, _do)
    _overpass_cache.set(key, features)
    return features


# --------------------------------------------------------------------------- #
# Nominatim
# --------------------------------------------------------------------------- #
_ADMIN_FALLBACKS = (
    ("sub_district", ("city_district", "suburb", "town", "municipality", "city")),
    ("village", ("village", "hamlet", "neighbourhood", "suburb", "city")),
)


def _normalise_admin(data: dict) -> dict[str, Any]:
    addr = data.get("address") or {}
    out: dict[str, Any] = {
        "display_name": data.get("display_name"),
        "state": addr.get("state"),
        "district": addr.get("state_district") or addr.get("county") or addr.get("district"),
        "country": addr.get("country"),
        "country_code": (addr.get("country_code") or "").upper(),
    }
    for field, keys in _ADMIN_FALLBACKS:
        value = None
        for k in keys:
            value = addr.get(k)
            if value:
                break
        out[field] = value
    return out


async def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """Resolve administrative names for a coordinate (best-effort).

    Cached at ``GEOCODE_KEY_PRECISION`` decimals (~110 m at 3 dp): every
    building in a scan shares one administrative area, so a batch that used to
    make N calls makes one - which is also what keeps us inside Nominatim's
    1 request/second policy.
    """
    p = settings.geocode_key_precision
    key = f"rev:{round(lat, p)}:{round(lon, p)}"
    cached = _geocode_cache.get(key)
    if cached is not None:
        return cached

    async def _do() -> dict[str, Any]:
        url = f"{settings.nominatim_url}/reverse"
        params = {"format": "jsonv2", "lat": lat, "lon": lon,
                  "zoom": 14, "addressdetails": 1}
        try:
            client = await get_client()
            resp = await client.get(url, params=params,
                                    timeout=httpx.Timeout(20.0, connect=10.0))
            resp.raise_for_status()
            data = orjson.loads(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reverse geocode failed for %s,%s: %s", lat, lon, exc)
            return {}
        return _normalise_admin(data)

    result = await _single_flight(key, _do)
    # Cache a failure only briefly-equivalent: TTLCache has one ttl, so an
    # empty result is cached like any other. Reverse geocoding is best-effort
    # and callers already tolerate {}, so this is acceptable and prevents a
    # hammering retry loop against a rate-limited service.
    _geocode_cache.set(key, result)
    return result


async def geocode(query: str) -> list[dict[str, Any]]:
    """Forward geocoding for the search endpoint."""
    key = f"fwd:{query.strip().lower()[:120]}"
    cached = _geocode_cache.get(key)
    if cached is not None:
        return cached

    async def _do() -> list[dict[str, Any]]:
        url = f"{settings.nominatim_url}/search"
        params = {"format": "json", "q": query, "limit": 5, "addressdetails": 1}
        try:
            client = await get_client()
            resp = await client.get(url, params=params,
                                    timeout=httpx.Timeout(20.0, connect=10.0))
            resp.raise_for_status()
            return orjson.loads(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Geocode failed for %r: %s", query, exc)
            return []

    result = await _single_flight(key, _do)
    _geocode_cache.set(key, result)
    return result
