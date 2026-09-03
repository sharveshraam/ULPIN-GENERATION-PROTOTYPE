"""ULPIN Generation API.

Run locally:
    uvicorn app.main:app --reload --port 8000
Interactive docs: http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import logging
import os as _os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import crud
from .config import get_settings
from .database import get_db, init_db
from .schemas import (
    APIResponse,
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

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("ulpin")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("Database ready at %s", settings.database_url)
    yield
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
)

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


@app.middleware("http")
async def cors_fallback(request: Request, call_next):
    """Guarantee CORS even if the configured allowlist rejects the origin.

    The frontend and API are deployed on different hosts (GitHub Pages ->
    Render). If ``ALLOWED_ORIGINS`` on the deployed service is missing,
    malformed (e.g. with a repository subpath or trailing slash) or otherwise
    does not match the browser's Origin, FastAPI still returns the payload but
    WITHOUT ``Access-Control-Allow-Origin``. The browser then raises a
    TypeError and the frontend reports "API blocked" — and no amount of
    incognito/private-window testing helps, because the extension/cookie
    theory is wrong; it is simply CORS.

    This is a public, credential-free demo API, so the permissive answer is
    safe: any origin may call it. Registered AFTER ``CORSMiddleware`` so it
    runs outermost - when the configured middleware already allowed the
    origin we leave its headers alone; only CORS-less responses get patched.

    Two details matter:

    * ``CORSMiddleware`` rejects a disallowed PREFLIGHT with HTTP 400, and a
      browser aborts before reading the body, so we convert that to 200.
    * ``CORSMiddleware`` sets ``Access-Control-Allow-Credentials: true``
      whenever an explicit (non-wildcard) allowlist is configured, even on a
      rejected request. ``Access-Control-Allow-Origin: *`` alongside
      credentials is invalid, so we echo the request origin instead and mark
      ``Vary: Origin``. The ``null`` origin (file://, sandboxed iframes) gets
      ``*`` with the credentials header removed.
    """
    response = await call_next(request)
    origin = request.headers.get("origin")
    if not origin or response.headers.get("access-control-allow-origin"):
        return response

    is_preflight = (
        request.method == "OPTIONS"
        and request.headers.get("access-control-request-method") is not None
    )
    if is_preflight and response.status_code in (400, 403):
        response = Response(status_code=200)

    if origin == "null":
        response.headers["Access-Control-Allow-Origin"] = "*"
        if "access-control-allow-credentials" in response.headers:
            del response.headers["access-control-allow-credentials"]
    else:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response

# --------------------------------------------------------------------------- #
# Rate limiting (in-process; sufficient for a single Cloud Run instance)
# --------------------------------------------------------------------------- #
_hits: dict[str, deque] = defaultdict(deque)
_EXEMPT = {
    "/", "/health", "/status", "/ulpin-status",
    "/docs", "/openapi.json", "/redoc", "/favicon.ico",
}


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path in _EXEMPT:
        return await call_next(request)

    client = request.client.host if request.client else "unknown"
    now = time.time()
    window = settings.rate_limit_window_s
    bucket = _hits[client]
    while bucket and bucket[0] < now - window:
        bucket.popleft()

    if len(bucket) >= settings.rate_limit_requests:
        retry = int(window - (now - bucket[0])) + 1
        logger.warning("Rate limit hit by %s", client)
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": f"Rate limit exceeded. Retry in {retry}s."},
            headers={"Retry-After": str(retry)},
        )
    bucket.append(now)
    return await call_next(request)


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError):
    """Turn domain errors into 400s rather than 500 stack traces."""
    logger.error("ValueError: %s", exc)
    return JSONResponse(status_code=400, content={"success": False, "message": str(exc)})


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
@app.get("/", tags=["meta"])
async def root():
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
async def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_state = "connected"
        n = crud.count_parcels(db)
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check DB failure: %s", exc)
        db_state, n = "error", 0
    return HealthResponse(status="ok", version=settings.version, database=db_state, parcels=n)


# --------------------------------------------------------------------------- #
# ULPIN generation
# --------------------------------------------------------------------------- #
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
async def health_alias(db: Session = Depends(get_db)):
    return await health(db)


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


@app.post("/api/v1/generate-ulpin/from-coordinates", tags=["ulpin"])
async def generate_ulpin_from_coords(payload: ULPINFromCoordsRequest, db: Session = Depends(get_db)):
    """Derive admin codes from a coordinate via reverse geocoding."""
    admin = await osm.reverse_geocode(payload.latitude, payload.longitude)
    codes = {
        "state_code": ug.state_code_for(admin.get("state")),
        "district_code": ug.district_code_for(admin.get("district")),
        "sub_district_code": ug.sub_district_code_for(admin.get("sub_district")),
        "village_code": ug.village_code_for(admin.get("village")),
    }
    plot = ug.next_plot_number(db, **codes)
    ulpin = ug.generate_ulpin_code(**codes, plot_number=plot)
    return {"success": True, "data": {"ulpin": ulpin, "codes": codes, "resolved": admin}}


@app.get("/api/v1/decode-ulpin/{ulpin}", tags=["ulpin"])
async def decode_ulpin(ulpin: str):
    return {"success": True, "data": ug.parse_unit_ulpin(ulpin)}


# --------------------------------------------------------------------------- #
# Parcels
# --------------------------------------------------------------------------- #
@app.get("/api/v1/parcels", tags=["parcels"])
async def list_parcels(
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    rows = crud.list_parcels(db, limit=limit, offset=offset)
    fc = crud.parcels_to_featurecollection(rows)
    return {"success": True, "data": fc, "count": len(rows), "total": crud.count_parcels(db)}


@app.get("/api/v1/parcels/{ulpin}", tags=["parcels"])
async def get_parcel(ulpin: str, db: Session = Depends(get_db)):
    p = crud.get_parcel(db, ulpin)
    if not p:
        raise HTTPException(404, f"No parcel with ULPIN {ulpin}")
    return {"success": True, "data": crud.parcel_to_feature(p)}


@app.post("/api/v1/parcels", status_code=201, tags=["parcels"])
async def create_parcel(payload: ParcelCreate, db: Session = Depends(get_db)):
    geometry = payload.geometry.model_dump()

    admin_codes = {
        "state_code": payload.state_code,
        "district_code": payload.district_code,
        "sub_district_code": payload.sub_district_code,
        "village_code": payload.village_code,
    }
    resolved = {}
    if payload.auto_detect_admin and not all(admin_codes.values()):
        from .services.geometry_processor import centroid_latlon
        lat, lon = centroid_latlon(geometry)
        resolved = await osm.reverse_geocode(lat, lon)
        admin_codes = {
            "state_code": payload.state_code or ug.state_code_for(resolved.get("state")),
            "district_code": payload.district_code or ug.district_code_for(resolved.get("district")),
            "sub_district_code": payload.sub_district_code or ug.sub_district_code_for(resolved.get("sub_district")),
            "village_code": payload.village_code or ug.village_code_for(resolved.get("village")),
        }

    try:
        p = crud.create_parcel(
            db, geometry=geometry, name=payload.name or "Unnamed Building",
            building_type=payload.building_type, height_m=payload.height_m,
            levels=payload.levels, admin=admin_codes, osm_id=payload.osm_id,
            height_source="manual", generate_breakdown=payload.generate_breakdown,
            extra_properties={"resolved_address": resolved.get("display_name")} if resolved else None,
        )
    except Exception as exc:
        db.rollback()
        logger.error("create_parcel failed: %s", exc)
        raise HTTPException(400, f"Could not create parcel: {exc}") from exc

    logger.info("Created parcel %s (%s floors, %s units)", p.ulpin, p.total_floors, p.total_units)
    return {"success": True, "data": crud.parcel_to_feature(p)}


@app.delete("/api/v1/parcels/{ulpin}", tags=["parcels"])
async def delete_parcel(ulpin: str, db: Session = Depends(get_db)):
    if not crud.delete_parcel(db, ulpin):
        raise HTTPException(404, f"No parcel with ULPIN {ulpin}")
    logger.info("Deleted parcel %s", ulpin)
    return {"success": True, "message": f"Parcel {ulpin} deleted"}


@app.get("/api/v1/parcels/{ulpin}/floors", tags=["parcels"])
async def parcel_floors(ulpin: str, db: Session = Depends(get_db)):
    if not crud.get_parcel(db, ulpin):
        raise HTTPException(404, f"No parcel with ULPIN {ulpin}")
    rows = crud.get_floors(db, ulpin)
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
async def parcel_units(
    ulpin: str,
    floor: Optional[int] = Query(None, ge=1),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    if not crud.get_parcel(db, ulpin):
        raise HTTPException(404, f"No parcel with ULPIN {ulpin}")
    rows, total = crud.get_units(db, ulpin, floor=floor, limit=limit, offset=offset)
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


# --------------------------------------------------------------------------- #
# 3D model
# --------------------------------------------------------------------------- #
@app.post("/api/v1/generate-3d-model", tags=["3d"])
async def generate_3d_model(payload: Model3DRequest, db: Session = Depends(get_db)):
    """Full floors + units + per-floor 3D geometry.

    Supply either a stored ``ulpin`` or a raw ``geometry``.
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
async def generate_3d_parcel_legacy(payload: dict, db: Session = Depends(get_db)):
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
    return await generate_3d_model(req, db)


# --------------------------------------------------------------------------- #
# Bulk generation
# --------------------------------------------------------------------------- #
async def _bulk_from_features(
    db: Session, features: list[dict], persist: bool, generate_breakdown: bool,
    default_type: str = "residential",
) -> dict:
    """Shared body for radius and bbox bulk generation."""
    if not features:
        return {"processed": 0, "created": 0, "skipped": 0, "buildings": []}

    # One reverse-geocode for the whole batch keeps us inside Nominatim's
    # 1 req/s policy; buildings in a 1 km radius share administrative area.
    from .services.geometry_processor import centroid_latlon
    lat0, lon0 = centroid_latlon(features[0]["geometry"])
    admin_info = await osm.reverse_geocode(lat0, lon0)
    admin = {
        "state_code": ug.state_code_for(admin_info.get("state")),
        "district_code": ug.district_code_for(admin_info.get("district")),
        "sub_district_code": ug.sub_district_code_for(admin_info.get("sub_district")),
        "village_code": ug.village_code_for(admin_info.get("village")),
    }

    created, skipped, out = 0, 0, []
    cap = settings.max_buildings_per_request

    for feat in features[:cap]:
        props = feat.get("properties", {}) or {}
        tags = props.get("tags", {}) or {}
        btype = props.get("building_type") or default_type
        height, levels, source = m3d.estimate_height(tags, btype)

        try:
            if persist:
                p = crud.create_parcel(
                    db, geometry=feat["geometry"], name=props.get("name") or "Unnamed Building",
                    building_type=btype, height_m=height, levels=levels, admin=admin,
                    osm_id=props.get("osm_id"), height_source=source,
                    generate_breakdown=generate_breakdown, commit=False,
                )
                out.append(crud.parcel_to_feature(p))
                created += 1
            else:
                # Preview only: compute without touching the database.
                from .services.geometry_processor import area_sq_m, centroid_latlon as cll
                area = area_sq_m(feat["geometry"])
                bd = m3d.calculate_floors_and_units(height, area, btype, levels)
                clat, clon = cll(feat["geometry"])
                out.append({
                    "type": "Feature", "geometry": feat["geometry"],
                    "properties": {
                        "ulpin": None, "name": props.get("name"), "building_type": btype,
                        "area_sq_m": round(area, 2), "height_m": round(height, 2),
                        "total_floors": bd["total_floors"], "total_units": bd["total_units"],
                        "centroid_lat": round(clat, 7), "centroid_lon": round(clon, 7),
                        "height_source": source, "osm_id": props.get("osm_id"),
                    },
                })
                created += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipped building %s: %s", props.get("osm_id"), exc)
            skipped += 1
            continue

    if persist:
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.error("Bulk commit failed: %s", exc)
            raise HTTPException(500, f"Database commit failed: {exc}") from exc

    return {
        "processed": len(features), "created": created, "skipped": skipped,
        "truncated": len(features) > cap, "admin_codes": admin,
        "resolved_area": admin_info.get("display_name"),
        "buildings": {"type": "FeatureCollection", "features": out},
    }


@app.post("/api/v1/bulk-generate", tags=["bulk"])
async def bulk_generate(payload: BulkGenerateRequest, db: Session = Depends(get_db)):
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
        db, features, payload.persist, payload.generate_breakdown, payload.building_type_default
    )
    logger.info(
        "bulk-generate r=%skm: %s found, %s created",
        payload.radius_km, result["processed"], result["created"],
    )
    return {"success": True, "data": result}


@app.post("/api/v1/bulk-generate/bbox", tags=["bulk"])
async def bulk_generate_bbox(payload: BBoxGenerateRequest, db: Session = Depends(get_db)):
    """Same as bulk-generate but for a map viewport."""
    try:
        features = await osm.fetch_buildings_in_bbox(
            payload.south, payload.west, payload.north, payload.east
        )
    except osm.OSMError as exc:
        raise HTTPException(503, f"OpenStreetMap unavailable: {exc}") from exc

    result = await _bulk_from_features(db, features, payload.persist, payload.generate_breakdown)
    logger.info("bulk-generate bbox: %s found, %s created", result["processed"], result["created"])
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
    db: Session = Depends(get_db),
):
    """Search stored parcels by ULPIN/name, by proximity, or geocode an address."""
    if q:
        rows = crud.search_parcels(db, q)
        return {"success": True, "mode": "ulpin_or_name", "count": len(rows),
                "data": crud.parcels_to_featurecollection(rows)}

    if lat is not None and lon is not None:
        rows = crud.parcels_near(db, lat, lon, radius_km)
        return {"success": True, "mode": "proximity", "count": len(rows),
                "data": crud.parcels_to_featurecollection(rows)}

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
# Static frontend (optional, and mounted LAST so it can never shadow a route)
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

if _FRONTEND_DIR:
    from fastapi.staticfiles import StaticFiles

    # Mounted at /app rather than / so the JSON banner at "/" stays put: it is
    # both the documented API root and the last-resort health probe. html=True
    # makes /app/ serve index.html. All asset paths in the pages are relative,
    # so they resolve correctly under the prefix.
    app.mount("/app", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    logger.info("Serving frontend at /app from %s", _FRONTEND_DIR)
else:  # pragma: no cover - only when deployed without the static files
    logger.warning("No frontend found in %s; running API-only", _FRONTEND_CANDIDATES)


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
