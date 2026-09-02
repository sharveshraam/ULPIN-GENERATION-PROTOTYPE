"""Fetch building footprints from OpenStreetMap (Overpass) and reverse-geocode
administrative names via Nominatim. Both are free services; we identify
ourselves with a User-Agent and fall back across mirrors.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from ..config import get_settings
from .geometry_processor import close_ring

logger = logging.getLogger(__name__)
settings = get_settings()


class OSMError(RuntimeError):
    """Raised when every Overpass mirror fails."""


def _overpass_query(filter_clause: str) -> str:
    return (
        f"[out:json][timeout:{int(settings.http_timeout)}];"
        f"(way[\"building\"]{filter_clause};"
        f"relation[\"building\"]{filter_clause};);"
        f"out geom;"
    )


async def _post_overpass(query: str) -> dict:
    last_error: str = "no mirrors configured"
    async with httpx.AsyncClient(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
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
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_error = f"{url}: {type(exc).__name__}: {exc}"
                logger.warning("Overpass request failed: %s", last_error)
                continue
    raise OSMError(f"all Overpass mirrors failed ({last_error})")


def _element_to_feature(el: dict) -> Optional[dict]:
    """Convert one Overpass element into a GeoJSON Feature, or None if unusable."""
    tags = el.get("tags") or {}
    if not tags.get("building"):
        return None
    if tags.get("building") == "roof":  # canopies are not enclosed structures
        return None

    geometry = el.get("geometry")
    if not geometry and el.get("members"):
        outer = [m for m in el["members"] if m.get("role") == "outer" and m.get("geometry")]
        if outer:
            geometry = outer[0]["geometry"]
    if not geometry or len(geometry) < 3:
        return None

    coords = [[float(n["lon"]), float(n["lat"])] for n in geometry if "lon" in n and "lat" in n]
    if len(coords) < 3:
        return None
    coords = close_ring(coords)
    if len(coords) < 4:
        return None

    name = (
        tags.get("name")
        or tags.get("addr:housename")
        or (f"{tags['addr:housenumber']} {tags['addr:street']}"
            if tags.get("addr:housenumber") and tags.get("addr:street") else None)
        or f"{str(tags.get('building','building')).replace('_',' ').title()} (OSM {el.get('type')}/{el.get('id')})"
    )

    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [coords]},
        "properties": {
            "osm_id": el.get("id"),
            "osm_type": el.get("type"),
            "building_type": tags.get("building", "yes"),
            "height": tags.get("height"),
            "levels": tags.get("building:levels"),
            "name": name,
            "tags": tags,
        },
    }


def _parse_elements(data: dict) -> list[dict]:
    out: list[dict] = []
    for el in data.get("elements", []):
        feat = _element_to_feature(el)
        if feat:
            out.append(feat)
    return out


async def fetch_buildings_in_radius(
    center_lat: float, center_lon: float, radius_km: float = 1.0
) -> list[dict]:
    """All buildings within radius_km of a point, as GeoJSON Features."""
    radius_m = int(radius_km * 1000)
    query = _overpass_query(f"(around:{radius_m},{center_lat},{center_lon})")
    logger.info("Overpass radius query: %.5f,%.5f r=%sm", center_lat, center_lon, radius_m)
    return _parse_elements(await _post_overpass(query))


async def fetch_buildings_in_bbox(
    south: float, west: float, north: float, east: float
) -> list[dict]:
    """All buildings inside a bounding box."""
    query = _overpass_query(f"({south},{west},{north},{east})")
    logger.info("Overpass bbox query: %s,%s,%s,%s", south, west, north, east)
    return _parse_elements(await _post_overpass(query))


async def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """Resolve administrative names for a coordinate (best-effort)."""
    url = f"{settings.nominatim_url}/reverse"
    params = {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1}
    try:
        async with httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": settings.user_agent}, follow_redirects=True
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reverse geocode failed for %s,%s: %s", lat, lon, exc)
        return {}

    addr = data.get("address", {}) or {}
    return {
        "display_name": data.get("display_name"),
        "state": addr.get("state"),
        "district": addr.get("state_district") or addr.get("county") or addr.get("district"),
        "sub_district": (addr.get("city_district") or addr.get("suburb")
                         or addr.get("town") or addr.get("municipality") or addr.get("city")),
        "village": (addr.get("village") or addr.get("hamlet") or addr.get("neighbourhood")
                    or addr.get("suburb") or addr.get("city")),
        "country": addr.get("country"),
        "country_code": (addr.get("country_code") or "").upper(),
    }


async def geocode(query: str) -> list[dict[str, Any]]:
    """Forward geocoding for the search endpoint."""
    url = f"{settings.nominatim_url}/search"
    params = {"format": "json", "q": query, "limit": 5, "addressdetails": 1}
    try:
        async with httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": settings.user_agent}, follow_redirects=True
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Geocode failed for %r: %s", query, exc)
        return []
