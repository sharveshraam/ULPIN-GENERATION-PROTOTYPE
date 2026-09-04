"""ULPIN Generation API.

Run locally:
    uvicorn app.main:app --reload --port 8000
Interactive docs: http://127.0.0.1:8000/docs

Performance notes (Render free tier: ~0.1 CPU, 512 MB RAM, ephemeral disk)
--------------------------------------------------------------------------
Every choice below is about CPU seconds per request, because that is the
scarce resource:

* **No blocking database call ever runs on the event loop.** Endpoints that
  only touch the database are plain ``def``, so FastAPI dispatches them to the
  worker threadpool; endpoints that also ``await`` OpenStreetMap keep their
  database work inside ``run_in_db``. The previous version declared them all
  ``async def`` and then called SQLAlchemy inline, so the 16th concurrent
  request would block the loop waiting for a pooled connection that only the
  loop could release - a hard deadlock that made the whole service, including
  ``/health``, stop answering until Render restarted it.
* **orjson** renders responses (~16x faster than ``json.dumps`` on a bulk
  payload, and slightly smaller).
* **Constant query counts.** A 600-building scan used to run ~1,200 SELECTs
  and 600 individually flushed INSERTs; ``crud.create_parcels_bulk`` does the
  same work with a handful of statements.
* **Vectorised geometry.** ``geometry_processor.measure_many`` measures a
  whole batch in one set of GEOS calls (~34x faster, bit-identical results).
* **Caching** of Overpass replies, Nominatim lookups, 3D models and the health
  probe, so repeated work - the kind a demo audience generates - is not
  repeated.
* **Pure-ASGI middleware.** ``@app.middleware("http")`` wraps handlers in
  ``BaseHTTPMiddleware``, which allocates a task group and two memory streams
  per request and re-encodes the body through them; that tax was paid twice,
  on every request including static assets.
* **Static UI served from memory**, pre-compressed, with ``Cache-Control`` so
  a repeat page view makes almost no requests at all.
"""
from __future__ import annotations

import asyncio
import logging
import os as _os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import orjson

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import crud
from .config import get_settings
from .database import SessionLocal, get_db, init_db
from .middleware import CorsFallbackMiddleware, RateLimitMiddleware, rate_limit_stats
from .schemas import (
    BBoxGenerateRequest,
    BulkGenerateRequest,
    CustomULPINRequest,
    HealthResponse,
    Model3DRequest,
    ParcelCreate,
    ULPINFromCoordsRequest,
    ULPINRequest,
)
from .services import model_3d_generator as m3d
from .services import osm_fetcher as osm
from .services import ulpin_generator as ug
from .services.cache import TTLCache
from .static_app import StaticFrontend

settings = get_settings()


class FastJSONResponse(Response):
    """JSON rendered by orjson's Rust encoder.

    FastAPI 0.141 deprecated its own ``ORJSONResponse``, and setting that as
    ``default_response_class`` also *disables* the newer fast path: FastAPI
    serialises straight to bytes with pydantic-core only while the response
    class is still its default placeholder and a response field exists.
    Defining the class here keeps both routes open - endpoints with a
    ``response_model`` take pydantic's Rust encoder, while the many endpoints
    that return plain dicts (most of this API's bulk payload) take orjson
    instead of ``json.dumps``. Measured ~16x faster and slightly smaller on a
    600-building GeoJSON response.

    ``OPT_NON_STR_KEYS`` tolerates integer keys that ORM rows can carry and
    ``OPT_SERIALIZE_NUMPY`` lets a numpy scalar reach a response without a
    manual ``float()``. Unlike stdlib ``json``, orjson rejects NaN/Infinity -
    correctly, since neither is valid JSON and both would break the browser
    client anyway.
    """

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(
            content,
            option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY,
        )


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("ulpin")


# --------------------------------------------------------------------------- #
# Database work off the event loop
# --------------------------------------------------------------------------- #
async def run_in_db(fn, *args, **kwargs):
    """Run ``fn(db, *args)`` in a worker thread with its own session.

    An ``async def`` endpoint cannot use ``Depends(get_db)`` without blocking
    the loop on the first query, and a session must not be shared across
    threads. Opening it inside the worker satisfies both: the loop stays free
    to answer other requests (crucially ``/health``) while the database and
    the geometry work happen on a thread.
    """
    def _work():
        db = SessionLocal()
        try:
            return fn(db, *args, **kwargs)
        finally:
            db.close()

    return await run_in_threadpool(_work)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Table creation is the only startup work; it is a handful of PRAGMAs
    # against SQLite and must finish before Render's health check passes.
    await run_in_threadpool(init_db)
    logger.info("Database ready at %s", settings.database_url)
    try:
        yield
    finally:
        await osm.close_client()
        logger.info("Shutting down")


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "Generate Indian ULPINs (Bhu-Aadhaar) for building footprints and model "
        "vertical property rights as floors and units.\n\n"
        "* Parcel ULPIN — 14 digits\n"
        "* Floor ULPIN — 17 digits (parcel + floor)\n"
        "* Unit ULPIN — 20 digits (parcel + floor + unit)"
    ),
    lifespan=lifespan,
    # orjson: several times faster than the stdlib encoder on the multi-
    # megabyte GeoJSON this API produces, which is pure CPU saved per request.
    default_response_class=FastJSONResponse,
)

# --------------------------------------------------------------------------- #
# Middleware
#
# add_middleware() inserts at the front of the list, and Starlette applies the
# list outermost-first, so the LAST call here ends up outermost. The resulting
# order is:
#
#   CorsFallback -> RateLimit -> GZip -> CORSMiddleware -> router
#
# CorsFallback must be outermost so it can patch a response the inner
# CORSMiddleware refused to add headers to - including a 429 from RateLimit.
# RateLimit sits outside GZip so a rejection is never compressed.
# --------------------------------------------------------------------------- #
_EXEMPT = frozenset({
    "/", "/health", "/status", "/ulpin-status", "/api/v1/stats",
    "/docs", "/openapi.json", "/redoc", "/favicon.ico",
})
_STATIC_PREFIX = "/app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Never combine credentials with a wildcard origin: browsers reject that
    # pairing outright. settings.allow_credentials is False whenever "*" is set.
    allow_credentials=settings.allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info(
    "CORS origins=%s credentials=%s", settings.cors_origins, settings.allow_credentials
)

if settings.gzip_enabled:
    # compresslevel defaults to 4 - see GZIP_LEVEL in config.py for the
    # measured time/size curve on this API's real payloads. Starlette also
    # offloads chunks >=128 KiB to a worker thread, so a big response cannot
    # stall the loop while it compresses.
    app.add_middleware(
        GZipMiddleware,
        minimum_size=settings.gzip_minimum_size,
        compresslevel=settings.gzip_level,
    )

app.add_middleware(
    RateLimitMiddleware,
    requests=settings.rate_limit_requests,
    window_s=settings.rate_limit_window_s,
    exempt=_EXEMPT,
    # The UI is a dozen in-memory files; rate limiting them cannot protect
    # anything and would only break the page for someone reloading it.
    exempt_prefixes=(_STATIC_PREFIX,),
    max_clients=settings.rate_limit_max_clients,
)
app.add_middleware(CorsFallbackMiddleware)


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError):
    """Turn domain errors into 400s rather than 500 stack traces."""
    logger.error("ValueError: %s", exc)
    return FastJSONResponse(status_code=400,
                          content={"success": False, "message": str(exc)})


# --------------------------------------------------------------------------- #
# Health probe
#
# Render polls healthCheckPath constantly and restarts the service when it
# stops answering, so this endpoint is the one place that must never be slow
# and never wait on a busy database. COUNT(*) is a full table scan, which on a
# large registry is exactly the kind of work that made the probe time out, so
# the result is cached for a few seconds and the probe itself runs on a worker
# thread behind a timeout. Under load the last known-good answer is served
# rather than hanging - a slightly stale parcel count is a far better outcome
# than a restart.
# --------------------------------------------------------------------------- #
_health_cache = TTLCache("health", settings.cache_health_s, max_entries=4)
_health_probe_timeout_s = 5.0


def _probe_database() -> tuple[str, int]:
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return "connected", crud.count_parcels(db)
    finally:
        db.close()


async def _read_health() -> tuple[str, int]:
    cached = _health_cache.get("h")
    if cached is not None:
        return cached
    try:
        result = await asyncio.wait_for(run_in_threadpool(_probe_database),
                                        timeout=_health_probe_timeout_s)
        _health_cache.set("h", result)
        return result
    except asyncio.TimeoutError:
        logger.warning("Health probe timed out after %.1fs", _health_probe_timeout_s)
        return "busy", 0
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check DB failure: %s", exc)
        return "error", 0


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
def _prefers_html(accept: str) -> bool:
    """
    True when a *person* navigated here in a browser, rather than code calling.

    Browsers send ``text/html,...,*/*;q=0.8``; ``fetch()`` with no Accept header
    sends ``*/*``. Comparing the q-values of text/html against application/json
    (and application/*) separates the two without guessing from User-Agent.
    """
    if not accept:
        return False
    best_html = best_json = 0.0
    for part in accept.split(","):
        media, _, rest = part.partition(";")
        media = media.strip().lower()
        q = 1.0
        for param in rest.split(";"):
            param = param.strip()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
        if media in ("text/html", "application/xhtml+xml"):
            best_html = max(best_html, q)
        elif media in ("application/json", "application/*"):
            best_json = max(best_json, q)
    return best_html > best_json


@app.get("/", tags=["meta"])
async def root(request: Request):
    # A human who lands on the bare host - a Render URL, or a preview of one -
    # wants the app, not a JSON blob. Content-negotiate: browsers are sent to
    # the frontend, and anything that asks for JSON (fetch, curl, the frontend's
    # own last-resort "/" health probe, which sends Accept: */*) still gets the
    # banner below. The banner must stay machine-readable at this exact path.
    if _FRONTEND_DIR and _prefers_html(request.headers.get("accept", "")):
        return RedirectResponse(url="/app/", status_code=302)
    return {
        "success": True,
        "data": {
            "name": settings.app_name,
            "version": settings.version,
            "docs": "/docs",
            # Same-origin frontend. Open this URL in a browser to use the app
            # with zero cross-origin requests (no CORS, no third-party block).
            "frontend": "/app/",
            "ulpin_format": {
                "parcel": "14 digits [state2][district2][subdistrict3][village3][plot4]",
                "floor": "17 digits (parcel + floor3)",
                "unit": "20 digits (parcel + floor3 + unit3)",
            },
        },
    }


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health():
    db_state, n = await _read_health()
    return HealthResponse(status="ok", version=settings.version,
                          database=db_state, parcels=n)


# Aliases for the health probe.
#
# Ad blockers and tracking-prevention lists match on URL path tokens, and
# "/health" is a common one - it is widely used by analytics and uptime
# beacons. When a filter list catches it the browser cancels the request with
# ERR_BLOCKED_BY_CLIENT before it is ever sent, so the frontend cannot tell
# the backend apart from a dead one. These aliases return exactly the same
# payload under names no filter list targets, letting the client retry a
# different path instead of giving up.
@app.get("/status", response_model=HealthResponse, include_in_schema=False)
@app.get("/ulpin-status", response_model=HealthResponse, include_in_schema=False)
async def health_alias():
    return await health()


@app.get("/api/v1/stats", include_in_schema=False)
async def runtime_stats():
    """Cache and pool state, for checking a deployed instance's behaviour."""
    from .database import engine

    pool = engine.pool
    return {
        "success": True,
        "data": {
            "uptime_s": round(time.monotonic() - _START_MONOTONIC, 1),
            "caches": osm.cache_stats() + [_health_cache.stats(), m3d.cache_stats()],
            "static": _static_stats(),
            "rate_limit": rate_limit_stats(),
            "db_pool": {
                "class": type(pool).__name__,
                "size": getattr(pool, "size", lambda: None)(),
                "checked_out": pool.checkedout(),
                "overflow": pool.overflow(),
            },
        },
    }


_START_MONOTONIC = time.monotonic()


# --------------------------------------------------------------------------- #
# ULPIN generation — pure CPU, no I/O
# --------------------------------------------------------------------------- #
@app.post("/api/v1/generate-ulpin", tags=["ulpin"])
async def generate_ulpin(payload: ULPINRequest):
    ulpin = ug.generate_ulpin_code(
        payload.state_code, payload.district_code,
        payload.sub_district_code, payload.village_code, payload.plot_number,
    )
    return {"success": True, "data": {"ulpin": ulpin, "parts": ug.parse_unit_ulpin(ulpin)}}


@app.post("/api/v1/generate-custom-ulpin", tags=["ulpin"])
async def generate_custom_ulpin(payload: CustomULPINRequest):
    """Hyphenated ULPIN: ``{Country}-{State}-{District}-{City}-{Plot}-{Unit}``.

    Example: ``IND-TN-001-CHE-F03-U301``
    """
    ulpin = ug.generate_custom_ulpin(
        country=payload.country,
        state_code=payload.state_code,
        district_code=payload.district_code,
        city_code=payload.city_code,
        plot_code=payload.plot_code,
        unit_code=payload.unit_code,
    )
    return {"ulpin": ulpin}


@app.get("/api/v1/decode-custom-ulpin/{ulpin}", tags=["ulpin"])
async def decode_custom_ulpin(ulpin: str):
    return {"success": True, "data": ug.parse_custom_ulpin(ulpin)}


@app.get("/api/v1/decode-ulpin/{ulpin}", tags=["ulpin"])
async def decode_ulpin(ulpin: str):
    return {"success": True, "data": ug.parse_unit_ulpin(ulpin)}


def _admin_from_resolution(admin: dict) -> dict[str, str]:
    return {
        "state_code": ug.state_code_for(admin.get("state")),
        "district_code": ug.district_code_for(admin.get("district")),
        "sub_district_code": ug.sub_district_code_for(admin.get("sub_district")),
        "village_code": ug.village_code_for(admin.get("village")),
    }


@app.post("/api/v1/generate-ulpin/from-coordinates", tags=["ulpin"])
async def generate_ulpin_from_coords(payload: ULPINFromCoordsRequest):
    """Derive admin codes from a coordinate via reverse geocoding."""
    admin = await osm.reverse_geocode(payload.latitude, payload.longitude)
    codes = _admin_from_resolution(admin)

    def _mint(db: Session) -> str:
        return ug.generate_ulpin_code(
            **codes, plot_number=ug.next_plot_number(db, **codes)
        )

    ulpin = await run_in_db(_mint)
    return {"success": True,
            "data": {"ulpin": ulpin, "codes": codes, "resolved": admin}}


# --------------------------------------------------------------------------- #
# Parcels — database only, so these are plain `def` and run in the threadpool
# --------------------------------------------------------------------------- #
@app.get("/api/v1/parcels", tags=["parcels"])
def list_parcels(
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    rows = crud.list_parcels(db, limit=limit, offset=offset)
    fc = crud.parcels_to_featurecollection(rows)
    return {"success": True, "data": fc, "count": len(rows),
            "total": crud.count_parcels(db)}


@app.get("/api/v1/parcels/{ulpin}", tags=["parcels"])
def get_parcel(ulpin: str, db: Session = Depends(get_db)):
    p = crud.get_parcel(db, ulpin)
    if not p:
        raise HTTPException(404, f"No parcel with ULPIN {ulpin}")
    return {"success": True, "data": crud.parcel_to_feature(p)}


@app.delete("/api/v1/parcels/{ulpin}", tags=["parcels"])
def delete_parcel(ulpin: str, db: Session = Depends(get_db)):
    if not crud.delete_parcel(db, ulpin):
        raise HTTPException(404, f"No parcel with ULPIN {ulpin}")
    logger.info("Deleted parcel %s", ulpin)
    _health_cache.clear()
    return {"success": True, "message": f"Parcel {ulpin} deleted"}


@app.get("/api/v1/parcels/{ulpin}/floors", tags=["parcels"])
def parcel_floors(ulpin: str, db: Session = Depends(get_db)):
    rows = crud.get_floors(db, ulpin)
    if not rows and not crud.get_parcel(db, ulpin):
        raise HTTPException(404, f"No parcel with ULPIN {ulpin}")
    return {
        "success": True,
        "count": len(rows),
        "data": [
            {
                "floor_ulpin": f.floor_ulpin, "parent_ulpin": f.parent_ulpin,
                "floor_number": f.floor_number, "floor_height_m": f.floor_height_m,
                "base_elevation_m": f.base_elevation_m, "floor_area_sq_m": f.floor_area_sq_m,
                "floor_type": f.floor_type, "units_on_floor": f.units_on_floor,
            } for f in rows
        ],
    }


@app.get("/api/v1/parcels/{ulpin}/units", tags=["parcels"])
def parcel_units(
    ulpin: str,
    floor: Optional[int] = Query(None, ge=1),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    rows, total = crud.get_units(db, ulpin, floor=floor, limit=limit, offset=offset)
    if not rows and total == 0 and not crud.get_parcel(db, ulpin):
        raise HTTPException(404, f"No parcel with ULPIN {ulpin}")
    return {
        "success": True, "total": total, "count": len(rows),
        "limit": limit, "offset": offset,
        "data": [
            {
                "unit_ulpin": u.unit_ulpin, "parent_ulpin": u.parent_ulpin,
                "floor_number": u.floor_number, "unit_number": u.unit_number,
                "area_sq_m": u.area_sq_m, "owner_name": u.owner_name,
                "ownership_type": u.ownership_type,
            } for u in rows
        ],
    }


@app.post("/api/v1/parcels", status_code=201, tags=["parcels"])
async def create_parcel(payload: ParcelCreate):
    geometry = payload.geometry.model_dump()

    admin_codes = {
        "state_code": payload.state_code,
        "district_code": payload.district_code,
        "sub_district_code": payload.sub_district_code,
        "village_code": payload.village_code,
    }
    resolved: dict[str, Any] = {}
    if payload.auto_detect_admin and not all(admin_codes.values()):
        from .services.geometry_processor import centroid_latlon
        lat, lon = centroid_latlon(geometry)
        resolved = await osm.reverse_geocode(lat, lon)
        admin_codes = {
            "state_code": payload.state_code or ug.state_code_for(resolved.get("state")),
            "district_code": payload.district_code or ug.district_code_for(resolved.get("district")),
            "sub_district_code": (payload.sub_district_code
                                  or ug.sub_district_code_for(resolved.get("sub_district"))),
            "village_code": payload.village_code or ug.village_code_for(resolved.get("village")),
        }

    def _create(db: Session):
        try:
            p = crud.create_parcel(
                db, geometry=geometry, name=payload.name or "Unnamed Building",
                building_type=payload.building_type, height_m=payload.height_m,
                levels=payload.levels, admin=admin_codes, osm_id=payload.osm_id,
                height_source="manual", generate_breakdown=payload.generate_breakdown,
                extra_properties=({"resolved_address": resolved.get("display_name")}
                                  if resolved else None),
            )
        except Exception as exc:
            db.rollback()
            logger.error("create_parcel failed: %s", exc)
            raise HTTPException(400, f"Could not create parcel: {exc}") from exc
        feature = crud.parcel_to_feature(p)
        logger.info("Created parcel %s (%s floors, %s units)",
                    p.ulpin, p.total_floors, p.total_units)
        return feature

    return {"success": True, "data": await run_in_db(_create)}


# --------------------------------------------------------------------------- #
# 3D model — heavy CPU, nothing awaited, so it belongs in the threadpool
# --------------------------------------------------------------------------- #
@app.post("/api/v1/generate-3d-model", tags=["3d"])
def generate_3d_model(payload: Model3DRequest, db: Session = Depends(get_db)):
    """Full floors + units + per-floor 3D geometry.

    Supply either a stored ``ulpin`` or a raw ``geometry``.

    Pass ``include_units: false`` to omit the per-unit records: the floor table
    already carries ``units_on_floor``, so a client that pages units itself
    (the bundled UI does) needs nothing else. For a 163-storey tower that is
    ~13,800 records and ~1.8 MB of JSON that no consumer reads - roughly 95%
    of this endpoint's cost.
    """
    geometry = payload.geometry.model_dump() if payload.geometry else None
    base_ulpin = payload.ulpin
    height = payload.height_m
    btype = payload.building_type
    levels = payload.levels

    if base_ulpin:
        p = crud.get_parcel(db, base_ulpin)
        if not p:
            raise HTTPException(404, f"No parcel with ULPIN {base_ulpin}")
        geometry = geometry or p.geometry_json
        height = height or p.height_m
        btype = payload.building_type or p.building_type
        levels = levels or p.total_floors

    if not geometry:
        raise HTTPException(400, "Provide either a stored 'ulpin' or a 'geometry'")
    if not base_ulpin:
        base_ulpin = "9" * 14  # unsaved preview

    try:
        model = m3d.generate_accurate_3d_model(
            geometry=geometry, base_ulpin=base_ulpin, height_m=height,
            building_type=btype or "residential", explicit_levels=levels,
            unit_area_override=payload.unit_area_sq_m,
            include_unit_geometry=payload.include_unit_geometry,
            include_units=payload.include_units,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info(
        "3D model %s: %s floors, %s units",
        base_ulpin, model["building"]["estimated_floors"], model["building"]["total_units"],
    )
    return {"success": True, "data": model}


# Backwards-compatible alias for the original endpoint name.
@app.post("/api/v1/generate-3d-parcel", include_in_schema=False)
def generate_3d_parcel_legacy(payload: dict, db: Session = Depends(get_db)):
    geometry = payload.get("geometry")
    if not geometry:
        raise HTTPException(400, "geometry is required")
    props = payload.get("properties") or {}
    req = Model3DRequest(
        geometry=geometry,
        height_m=payload.get("height"),
        levels=props.get("floors"),
        building_type=props.get("building_type", "residential"),
        include_unit_geometry=False,
    )
    return generate_3d_model(req, db)


# --------------------------------------------------------------------------- #
# Bulk generation
# --------------------------------------------------------------------------- #
def _specs_from_features(features: list[dict], default_type: str) -> list[dict]:
    """Reduce Overpass features to the fields the bulk insert actually needs."""
    from .services import model_3d_generator as _m3d

    specs: list[dict] = []
    for feat in features:
        props = feat.get("properties") or {}
        tags = props.get("tags") or {}
        btype = props.get("building_type") or default_type
        height, levels, source = _m3d.estimate_height(tags, btype)
        specs.append({
            "geometry": feat["geometry"],
            "name": props.get("name"),
            "building_type": btype,
            "height_m": height,
            "levels": levels,
            "height_source": source,
            "osm_id": props.get("osm_id"),
        })
    return specs


def _preview_features(specs: list[dict]) -> tuple[list[dict], int]:
    """Compute floors/units for a batch WITHOUT touching the database."""
    from .services import geometry_processor as geo

    measured = geo.measure_many([s["geometry"] for s in specs])
    out: list[dict] = []
    skipped = 0
    for i, spec in enumerate(specs):
        if not measured.ok[i]:
            skipped += 1
            continue
        area, clat, clon = measured[i]
        bd = m3d.calculate_floors_and_units(
            height_m=spec["height_m"], footprint_area_sq_m=area,
            building_type=spec["building_type"], explicit_levels=spec["levels"],
        )
        out.append({
            "type": "Feature", "geometry": spec["geometry"],
            "properties": {
                "ulpin": None, "name": spec["name"],
                "building_type": spec["building_type"],
                "area_sq_m": round(area, 2),
                "height_m": round(spec["height_m"], 2),
                "total_floors": bd["total_floors"], "total_units": bd["total_units"],
                "centroid_lat": round(clat, 7), "centroid_lon": round(clon, 7),
                "height_source": spec["height_source"], "osm_id": spec["osm_id"],
            },
        })
    return out, skipped


async def _bulk_from_features(
    features: list[dict], persist: bool, generate_breakdown: bool,
    default_type: str = "residential",
) -> dict:
    """Shared body for radius and bbox bulk generation.

    Every CPU- and database-bound step runs on a worker thread. For a dense
    scan that is seconds of work, and keeping it off the event loop is what
    lets ``/health`` keep answering - and Render keep the service alive -
    while a big request is in flight.
    """
    found = len(features)
    if not found:
        return {"processed": 0, "created": 0, "skipped": 0, "buildings": []}

    cap = settings.max_buildings_per_request
    features = features[:cap]

    specs = await run_in_threadpool(_specs_from_features, features, default_type)

    # One reverse-geocode for the whole batch keeps us inside Nominatim's
    # 1 req/s policy; buildings in a 1 km radius share an administrative area.
    from .services.geometry_processor import centroid_latlon
    lat0, lon0 = await run_in_threadpool(centroid_latlon, specs[0]["geometry"])
    admin_info = await osm.reverse_geocode(lat0, lon0)
    admin = _admin_from_resolution(admin_info)

    if persist:
        def _persist_work():
            # Session opened, used and closed on one thread: SQLAlchemy
            # sessions are not safe to share, and a worker thread must never
            # hand one back to the loop.
            db = SessionLocal()
            try:
                created, skipped = crud.create_parcels_bulk(
                    db, specs, admin,
                    generate_breakdown=generate_breakdown, commit=False,
                )
                db.commit()
                return created, skipped
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.error("Bulk commit failed: %s", exc)
                raise HTTPException(500, f"Database commit failed: {exc}") from exc
            finally:
                db.close()

        created, skipped = await run_in_threadpool(_persist_work)
    else:
        created, skipped = await run_in_threadpool(_preview_features, specs)

    if persist:
        _health_cache.clear()   # the stored parcel count just changed

    return {
        "processed": found, "created": len(created), "skipped": skipped,
        "truncated": found > cap, "admin_codes": admin,
        "resolved_area": admin_info.get("display_name"),
        "buildings": {"type": "FeatureCollection", "features": created},
    }


@app.post("/api/v1/bulk-generate", tags=["bulk"])
async def bulk_generate(payload: BulkGenerateRequest):
    """Generate ULPINs for every OSM building within a radius."""
    if payload.radius_km > settings.max_radius_km:
        raise HTTPException(400, f"radius_km exceeds the {settings.max_radius_km} km limit")
    try:
        features = await osm.fetch_buildings_in_radius(
            payload.center_lat, payload.center_lon, payload.radius_km
        )
    except osm.OSMError as exc:
        raise HTTPException(503, f"OpenStreetMap unavailable: {exc}") from exc

    result = await _bulk_from_features(
        features, payload.persist, payload.generate_breakdown, payload.building_type_default
    )
    logger.info(
        "bulk-generate r=%skm: %s found, %s created",
        payload.radius_km, result["processed"], result["created"],
    )
    return {"success": True, "data": result}


@app.post("/api/v1/bulk-generate/bbox", tags=["bulk"])
async def bulk_generate_bbox(payload: BBoxGenerateRequest):
    """Same as bulk-generate but for a map viewport."""
    try:
        features = await osm.fetch_buildings_in_bbox(
            payload.south, payload.west, payload.north, payload.east
        )
    except osm.OSMError as exc:
        raise HTTPException(503, f"OpenStreetMap unavailable: {exc}") from exc

    result = await _bulk_from_features(features, payload.persist, payload.generate_breakdown)
    logger.info("bulk-generate bbox: %s found, %s created",
                result["processed"], result["created"])
    return {"success": True, "data": result}


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
@app.get("/api/v1/search", tags=["search"])
async def search(
    q: Optional[str] = Query(None, description="ULPIN fragment or building name"),
    lat: Optional[float] = Query(None, ge=-90, le=90),
    lon: Optional[float] = Query(None, ge=-180, le=180),
    radius_km: float = Query(1.0, gt=0, le=25),
    address: Optional[str] = Query(None, description="Free-text place name"),
):
    """Search stored parcels by ULPIN/name, by proximity, or geocode an address."""
    if q:
        def _by_text(db: Session):
            rows = crud.search_parcels(db, q)
            return len(rows), crud.parcels_to_featurecollection(rows)

        count, fc = await run_in_db(_by_text)
        return {"success": True, "mode": "ulpin_or_name", "count": count, "data": fc}

    if lat is not None and lon is not None:
        def _nearby(db: Session):
            rows = crud.parcels_near(db, lat, lon, radius_km)
            return len(rows), crud.parcels_to_featurecollection(rows)

        count, fc = await run_in_db(_nearby)
        return {"success": True, "mode": "proximity", "count": count, "data": fc}

    if address:
        results = await osm.geocode(address)
        return {"success": True, "mode": "geocode", "count": len(results),
                "data": [
                    {"display_name": r.get("display_name"),
                     "lat": float(r["lat"]), "lon": float(r["lon"]),
                     "type": r.get("type")}
                    for r in results if r.get("lat") and r.get("lon")
                ]}

    raise HTTPException(400, "Provide one of: q, address, or lat+lon")


# --------------------------------------------------------------------------- #
# Static frontend (registered LAST so it can never shadow an API route)
#
# Serving the UI from the same origin as the API turns every request into a
# first-party one. That matters because browser tracking prevention and many
# privacy extensions block by cross-site *relationship*, not by domain: a call
# from github.io to onrender.com is third-party and gets cancelled with
# ERR_BLOCKED_BY_CLIENT, while the identical call from a page already on
# onrender.com is first-party and passes. Same-origin also means no CORS
# preflight at all.
#
# GitHub Pages keeps working exactly as before - this is an addition, not a
# replacement. js/config.js resolves to same-origin automatically when the
# page is served from here.
#
# The frontend is looked up in order of preference, so the mount works no
# matter how the service is packaged:
#   1. backend/app/static   vendored copy shipped inside the Python package
#                           (works for Docker builds, root directory =
#                           backend, and every other packaging variant)
#   2. repository root       plain git checkout on native Python (Render)
#   3. current working dir   last resort for exotic layouts
# --------------------------------------------------------------------------- #
_PKG_DIR = _os.path.dirname(_os.path.abspath(__file__))  # .../backend/app
_FRONTEND_CANDIDATES = [
    _os.path.join(_PKG_DIR, "static"),
    _os.path.dirname(_PKG_DIR),          # backend/
    _os.path.dirname(_os.path.dirname(_PKG_DIR)),  # repo root
    _os.getcwd(),
]
_FRONTEND_DIR = next(
    (d for d in _FRONTEND_CANDIDATES
     if _os.path.isfile(_os.path.join(d, "index.html"))),
    None,
)

_frontend: Optional[StaticFrontend] = None
if _FRONTEND_DIR:
    _frontend = StaticFrontend(
        _FRONTEND_DIR,
        html_max_age=settings.static_html_max_age,
        asset_max_age=settings.static_asset_max_age,
        max_file_bytes=settings.static_max_file_bytes,
    ) if settings.static_cache_enabled else None

    if _frontend is None:
        # Escape hatch: STATIC_CACHE=0 restores Starlette's disk-backed server.
        from fastapi.staticfiles import StaticFiles
        # Mounted at /app rather than / so the JSON banner at "/" stays put: it
        # is both the documented API root and the last-resort health probe.
        app.mount("/app", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
        logger.info("Serving frontend at /app from %s (disk)", _FRONTEND_DIR)
    else:
        app.mount("/app", _frontend, name="frontend")
        logger.info("Serving frontend at /app from %s (in memory)", _FRONTEND_DIR)
else:  # pragma: no cover - only when deployed without the static files
    logger.warning("No frontend found in %s; running API-only", _FRONTEND_CANDIDATES)


def _static_stats() -> dict:
    return _frontend.stats() if _frontend is not None else {"enabled": False}


# --------------------------------------------------------------------------- #
# Entry point
#
# Render normally runs this via its Start Command:
#     uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT
#
# This block is a fallback so `python backend/app/main.py` also works, binding
# to 0.0.0.0 and honouring Render's $PORT.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,          # 0.0.0.0 by default, never localhost
        port=settings.port,          # $PORT on Render, 8000 locally
        reload=False,
    )
