"""Regression tests for the performance work.

These exist because optimising this codebase is easy to get subtly wrong, and
two specific mistakes were made and caught while writing them:

* a proximity search whose bounding box was built from METRES-per-degree
  constants while the radius was in KILOMETRES, shrinking the box a
  thousandfold so the search silently returned nothing;
* every endpoint declared ``async def`` while calling SQLAlchemy inline, which
  deadlocked the whole service at 16 concurrent requests - the event loop
  blocked waiting for a pooled connection that only the event loop could
  release. ``/health`` stopped answering and Render restarted the service.

So the concurrency and numeric-parity tests below are load-bearing, not
decorative: they pin behaviour that is invisible in a single-request smoke
test.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP_DB = os.path.join(tempfile.mkdtemp(), "perf.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DB}")
os.environ["RATE_LIMIT_REQUESTS"] = "100000"

import httpx  # noqa: E402

from app import crud  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.middleware import RateLimitMiddleware  # noqa: E402
from app.services import geometry_processor as geo  # noqa: E402
from app.services import model_3d_generator as m3d  # noqa: E402
from app.services import osm_fetcher as osm  # noqa: E402
from app.services.cache import TTLCache  # noqa: E402
from app.static_app import StaticFrontend  # noqa: E402

init_db()


def square(lat: float, lon: float, half: float = 0.0003) -> dict:
    return {"type": "Polygon", "coordinates": [[
        [lon - half, lat - half], [lon + half, lat - half],
        [lon + half, lat + half], [lon - half, lat + half],
        [lon - half, lat - half]]]}


# Tricky shapes: the fast vectorised path must not silently differ from the
# general one on any of these.
ODD_GEOMETRIES = [
    square(9.9815, 76.2839, 0.0002),
    square(25.1972, 55.2744, 0.00056),
    # concave L-shape
    {"type": "Polygon", "coordinates": [[
        [0, 0], [0.0004, 0], [0.0004, 0.0002], [0.0002, 0.0002],
        [0.0002, 0.0004], [0, 0.0004], [0, 0]]]},
    # polygon with a hole
    {"type": "Polygon", "coordinates": [
        [[76.28, 9.98], [76.29, 9.98], [76.29, 9.99], [76.28, 9.99], [76.28, 9.98]],
        [[76.283, 9.983], [76.283, 9.987], [76.287, 9.987], [76.287, 9.983], [76.283, 9.983]]]},
    # multipolygon
    {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
        [[[2, 2], [2, 3], [3, 3], [3, 2], [2, 2]]]]},
    # 3D ring (must fall back, not be silently truncated to 2D)
    {"type": "Polygon", "coordinates": [[
        [76.28, 9.98, 5], [76.29, 9.98, 5], [76.29, 9.99, 5],
        [76.28, 9.99, 5], [76.28, 9.98, 5]]]},
    # self-intersecting bowtie: invalid, repaired with buffer(0)
    {"type": "Polygon", "coordinates": [[
        [0, 0], [1, 0], [0, 1], [1, 1], [0, 0]]]},
    # degenerate
    {"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]},
    # wrong type entirely
    {"type": "Point", "coordinates": [1, 2]},
]


# --------------------------------------------------------------------------- #
# The deadlock: async endpoints blocking the event loop on database I/O
# --------------------------------------------------------------------------- #
def _transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("concurrency", [16, 32, 64, 128])
def test_concurrent_requests_do_not_deadlock(concurrency):
    """128 parallel /health calls must all answer, quickly.

    Before the fix this deadlocked at 16 and never returned: the 16th request
    blocked the event loop waiting for a pooled SQLite connection that only
    the loop could release. Render's health check then failed and the service
    was restarted.
    """
    async def scenario():
        async with httpx.AsyncClient(transport=_transport(),
                                     base_url="http://perf") as client:
            return await asyncio.wait_for(
                asyncio.gather(*[client.get("/health") for _ in range(concurrency)]),
                timeout=30,
            )

    responses = _run(scenario())
    assert len(responses) == concurrency
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["status"] == "ok" for r in responses)


def test_database_endpoints_are_not_async_def():
    """A sync `def` endpoint runs in the worker threadpool, so its blocking
    SQLAlchemy call cannot stall the event loop. Any endpoint that touches the
    database and does not await anything must stay `def`."""
    from app.main import (
        delete_parcel,
        generate_3d_model,
        get_parcel,
        list_parcels,
        parcel_floors,
        parcel_units,
    )

    for fn in (list_parcels, get_parcel, delete_parcel, parcel_floors,
               parcel_units, generate_3d_model):
        assert not asyncio.iscoroutinefunction(fn), (
            f"{fn.__name__} must stay a plain def so FastAPI runs it in the "
            "threadpool; making it async def re-introduces the deadlock"
        )


def test_health_answers_while_a_bulk_scan_is_in_flight(monkeypatch):
    """/health is what Render polls to decide whether to restart the service,
    so a heavy request must never be able to starve it."""
    features = [
        {"type": "Feature", "geometry": square(9.98 + i * 1e-4, 76.28),
         "properties": {"osm_id": 900000 + i, "building_type": "apartments",
                        "name": f"Tower {i}", "tags": {"building:levels": "18"}}}
        for i in range(300)
    ]

    async def fake_fetch(lat, lon, radius_km):
        return features

    async def fake_reverse(lat, lon):
        return {"state": "Kerala", "district": "Ernakulam",
                "sub_district": "Kochi", "village": "Ernakulam"}

    monkeypatch.setattr(osm, "fetch_buildings_in_radius", fake_fetch)
    monkeypatch.setattr(osm, "reverse_geocode", fake_reverse)

    async def scenario():
        async with httpx.AsyncClient(transport=_transport(),
                                     base_url="http://perf") as client:
            bulk = asyncio.create_task(client.post("/api/v1/bulk-generate", json={
                "center_lat": 9.98, "center_lon": 76.28, "radius_km": 1.0,
                "persist": True, "generate_breakdown": False,
            }))
            probes = []
            while not bulk.done():
                probes.append(await client.get("/health"))
                await asyncio.sleep(0)
            result = await bulk
            return result, probes

    result, probes = _run(scenario())
    assert result.status_code == 200, result.text
    assert result.json()["data"]["created"] == 300
    assert probes, "expected at least one probe during the scan"
    assert all(p.status_code == 200 and p.json()["status"] == "ok" for p in probes)


# --------------------------------------------------------------------------- #
# Numeric parity: the fast paths must not change any number
# --------------------------------------------------------------------------- #
def test_measure_many_matches_per_geometry_measure():
    batch = geo.measure_many(ODD_GEOMETRIES)
    assert len(batch) == len(ODD_GEOMETRIES)
    for i, g in enumerate(ODD_GEOMETRIES):
        try:
            expected = geo.measure(g)
        except ValueError:
            assert not batch.ok[i], f"geometry {i} should have been rejected"
            continue
        assert batch.ok[i], f"geometry {i} should have been measured"
        area, lat, lon = batch[i]
        assert area == pytest.approx(expected[0], rel=1e-12, abs=1e-9)
        assert lat == pytest.approx(expected[1], abs=1e-12)
        assert lon == pytest.approx(expected[2], abs=1e-12)


def test_measure_many_on_a_large_batch_is_exact():
    geoms = [square(9.9 + i * 1e-4, 76.2 + i * 1e-4, 0.0001 + (i % 5) * 1e-5)
             for i in range(400)]
    batch = geo.measure_many(geoms)
    assert batch.ok.all()
    for i, g in enumerate(geoms):
        area, lat, lon = geo.measure(g)
        assert batch.area_sq_m[i] == pytest.approx(area, rel=1e-12)
        assert batch.lat[i] == pytest.approx(lat, abs=1e-12)
        assert batch.lon[i] == pytest.approx(lon, abs=1e-12)


def test_area_equals_the_projected_transform_result():
    """The transform-free area formula must match shapely.ops.transform.

    area_m2 == |area_deg| * m_per_deg_lon(lat) * M_PER_DEG_LAT, because the
    local projection is a translation plus an anisotropic scale.
    """
    from shapely.ops import transform

    for g in ODD_GEOMETRIES[:3]:
        shape = geo.to_shape(g)
        c = shape.centroid
        reference = abs(transform(geo._local_projection(c.y, c.x), shape).area)
        assert geo.area_sq_m(g) == pytest.approx(reference, rel=1e-9)


def test_parcels_near_box_is_in_degrees_not_metres():
    """A 1 km radius must span ~0.009 degrees of latitude.

    Building the box from metres-per-degree constants while the radius is in
    kilometres makes it ~0.000009 degrees wide - about one metre - and every
    proximity search silently returns nothing.
    """
    dlat = (1.0 * 1000.0) / geo.M_PER_DEG_LAT
    assert dlat == pytest.approx(0.00904, abs=1e-4)

    # And end to end: a parcel ~250 m away must be found by a 1 km search.
    db = SessionLocal()
    try:
        created = crud.create_parcel(
            db, geometry=square(45.0000, 45.0000, 0.0001), name="Nearby probe",
            building_type="house", levels=2,
            admin={"state_code": "99", "district_code": "99",
                   "sub_district_code": "999", "village_code": "999"},
        )
        lat, lon = created.centroid_lat, created.centroid_lon
        assert crud.parcels_near(db, lat, lon, 1.0)
        # ~2 km away, outside a 1 km box.
        assert not crud.parcels_near(db, lat + 0.02, lon, 1.0)
    finally:
        db.rollback()
        db.close()


def test_bulk_insert_matches_one_by_one_insert():
    """create_parcels_bulk must store exactly what N x create_parcel would."""
    specs = [
        {"geometry": square(30.0 + i * 0.001, 30.0 + i * 0.001, 0.0002),
         "name": f"Parity {i}", "building_type": "apartments",
         "height_m": None, "levels": 6 + (i % 4),
         "height_source": "estimated", "osm_id": 500000 + i}
        for i in range(12)
    ]
    admin = {"state_code": "32", "district_code": "07",
             "sub_district_code": "041", "village_code": "901"}

    db = SessionLocal()
    try:
        features, skipped = crud.create_parcels_bulk(db, specs, admin, commit=True)
        assert skipped == 0
        assert len(features) == len(specs)

        expected = []
        for i, spec in enumerate(specs):
            area, lat, lon = geo.measure(spec["geometry"])
            h = m3d._height_for_levels(spec["levels"], m3d.profile_for(spec["building_type"]))
            bd = m3d.calculate_floors_and_units(
                height_m=h, footprint_area_sq_m=area,
                building_type=spec["building_type"], explicit_levels=spec["levels"])
            expected.append((round(area, 2), round(lat, 7), round(lon, 7), round(h, 2),
                             bd["total_floors"], bd["total_units"]))

        for i, feature in enumerate(features):
            props = feature["properties"]
            area, lat, lon, h, floors, units = expected[i]
            assert props["area_sq_m"] == area
            assert props["centroid_lat"] == lat
            assert props["centroid_lon"] == lon
            assert props["height_m"] == h
            assert props["total_floors"] == floors
            assert props["total_units"] == units
            assert len(props["ulpin"]) == 14
            assert props["osm_id"] == specs[i]["osm_id"]

        # Plot numbers must be contiguous from the village's previous maximum.
        plots = [int(f["properties"]["ulpin"][10:]) for f in features]
        assert plots == list(range(plots[0], plots[0] + len(plots)))
        assert len(set(plots)) == len(plots)

        # And the rows really are in the database, readable by the normal path.
        ulpin = features[0]["properties"]["ulpin"]
        assert crud.get_parcel(db, ulpin) is not None
    finally:
        for f in features:
            crud.delete_parcel(db, f["properties"]["ulpin"])
        db.close()


def test_bulk_insert_is_idempotent_on_osm_id():
    specs = [{"geometry": square(31.0, 31.0, 0.0002), "name": "Repeat",
              "building_type": "house", "height_m": None, "levels": 2,
              "height_source": "manual", "osm_id": 555001}]
    admin = {"state_code": "32", "district_code": "07",
             "sub_district_code": "041", "village_code": "902"}
    db = SessionLocal()
    try:
        first, _ = crud.create_parcels_bulk(db, specs, admin, commit=True)
        second, _ = crud.create_parcels_bulk(db, specs, admin, commit=True)
        assert first[0]["properties"]["ulpin"] == second[0]["properties"]["ulpin"]
        assert crud.count_parcels(db) >= 1
    finally:
        crud.delete_parcel(db, first[0]["properties"]["ulpin"])
        db.close()


def test_bulk_insert_skips_unusable_geometry_without_failing_the_batch():
    specs = [
        {"geometry": square(32.0, 32.0, 0.0002), "name": "Good", "osm_id": 560001,
         "building_type": "house", "height_m": None, "levels": 2,
         "height_source": "manual"},
        {"geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]},
         "name": "Degenerate", "osm_id": 560002, "building_type": "house",
         "height_m": None, "levels": 2, "height_source": "manual"},
        {"geometry": square(32.001, 32.001, 0.0002), "name": "Also good",
         "osm_id": 560003, "building_type": "house", "height_m": None,
         "levels": 2, "height_source": "manual"},
    ]
    admin = {"state_code": "32", "district_code": "07",
             "sub_district_code": "041", "village_code": "903"}
    db = SessionLocal()
    try:
        features, skipped = crud.create_parcels_bulk(db, specs, admin, commit=True)
        assert skipped == 1
        assert [f["properties"]["name"] for f in features] == ["Good", "Also good"]
        # The skipped building must not consume a plot number.
        plots = [int(f["properties"]["ulpin"][10:]) for f in features]
        assert plots[1] == plots[0] + 1
    finally:
        for f in features:
            crud.delete_parcel(db, f["properties"]["ulpin"])
        db.close()


def test_bulk_insert_with_breakdown_writes_floors_and_units():
    specs = [{"geometry": square(33.0, 33.0, 0.0004), "name": "With breakdown",
              "osm_id": 570001, "building_type": "apartments",
              "height_m": None, "levels": 5, "height_source": "manual"}]
    admin = {"state_code": "32", "district_code": "07",
             "sub_district_code": "041", "village_code": "904"}
    db = SessionLocal()
    try:
        features, _ = crud.create_parcels_bulk(db, specs, admin,
                                               generate_breakdown=True, commit=True)
        ulpin = features[0]["properties"]["ulpin"]
        floors = crud.get_floors(db, ulpin)
        units, total = crud.get_units(db, ulpin, limit=5000)
        assert len(floors) == 5
        assert all(len(f.floor_ulpin) == 17 for f in floors)
        assert total > 0 and len(units) == total
        assert all(len(u.unit_ulpin) == 20 for u in units)
        assert len({u.unit_ulpin for u in units}) == total
        # Children must point at real parent rows.
        assert all(u.parcel_id is not None and u.floor_id is not None for u in units)
    finally:
        crud.delete_parcel(db, ulpin)
        db.close()


# --------------------------------------------------------------------------- #
# 3D model payload
# --------------------------------------------------------------------------- #
def test_include_units_false_keeps_everything_else_identical():
    kwargs = dict(geometry=square(25.1972, 55.2744, 0.00056), base_ulpin="9" * 14,
                  height_m=828.0, building_type="commercial", explicit_levels=163,
                  include_unit_geometry=False)
    m3d.clear_model_cache()
    full = m3d.generate_accurate_3d_model(**kwargs)
    m3d.clear_model_cache()
    lite = m3d.generate_accurate_3d_model(**kwargs, include_units=False)

    assert full["building"] == lite["building"]
    assert full["floors"] == lite["floors"]
    assert full["geometry_3d"] == lite["geometry_3d"]
    assert len(full["units"]) == 13821
    assert lite["units"] == []
    # The point of the flag: this is ~95% of the payload for a tall tower.
    assert lite["building"]["total_units"] == len(full["units"])


def test_model_cache_returns_identical_results():
    kwargs = dict(geometry=square(9.9815, 76.2839, 0.0002), base_ulpin="8" * 14,
                  building_type="apartments", explicit_levels=9)
    m3d.clear_model_cache()
    first = m3d.generate_accurate_3d_model(**kwargs)
    second = m3d.generate_accurate_3d_model(**kwargs)
    assert first == second
    stats = m3d.cache_stats()
    assert stats["hits"] >= 1 and stats["entries"] >= 1


def test_model_cache_is_bounded():
    m3d.clear_model_cache()
    for i in range(m3d._CACHE_MAX_ENTRIES + 25):
        m3d.generate_accurate_3d_model(
            geometry=square(10.0 + i * 0.001, 20.0, 0.0002),
            base_ulpin=f"{i:014d}", building_type="house", explicit_levels=2)
    assert len(m3d._model_cache) <= m3d._CACHE_MAX_ENTRIES


# --------------------------------------------------------------------------- #
# Static frontend handler
# --------------------------------------------------------------------------- #
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _static_call(frontend: StaticFrontend, path: str, root_path: str = "/app",
                 headers: dict | None = None):
    scope = {"type": "http", "method": "GET", "path": path,
             "root_path": root_path, "query_string": b"", "scheme": "http",
             "headers": [(k.lower().encode(), v.encode())
                         for k, v in (headers or {}).items()]}
    out = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        out.append(message)

    asyncio.new_event_loop().run_until_complete(frontend(scope, receive, send))
    start = next(m for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, body


@pytest.fixture(scope="module")
def frontend() -> StaticFrontend:
    return StaticFrontend(os.path.join(_REPO_ROOT, "backend", "app", "static"))


def test_static_serves_index_for_the_mount_root(frontend):
    status, headers, body = _static_call(frontend, "/app/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert b"<title>" in body


def test_static_serves_assets_with_correct_content_type(frontend):
    for path, ctype in (("/app/map.html", "text/html"),
                        ("/app/js/api.js", "javascript"),
                        ("/app/styles.css", "text/css"),
                        ("/app/tailwind.css", "text/css")):
        status, headers, _ = _static_call(frontend, path)
        assert status == 200, path
        assert ctype in headers["content-type"], (path, headers["content-type"])


def test_static_sets_cache_control(frontend):
    _, html_headers, _ = _static_call(frontend, "/app/")
    _, js_headers, _ = _static_call(frontend, "/app/js/api.js")
    assert "max-age=60" in html_headers["cache-control"]
    assert "must-revalidate" in html_headers["cache-control"]
    assert "max-age=86400" in js_headers["cache-control"]


def test_static_pre_compresses_when_the_client_accepts_gzip(frontend):
    import gzip as _gzip

    _, plain_headers, plain = _static_call(frontend, "/app/js/api.js")
    _, gz_headers, compressed = _static_call(
        frontend, "/app/js/api.js", headers={"Accept-Encoding": "gzip, deflate"})

    assert "content-encoding" not in plain_headers
    assert gz_headers["content-encoding"] == "gzip"
    assert gz_headers["vary"] == "Accept-Encoding"
    assert len(compressed) < len(plain)
    assert _gzip.decompress(compressed) == plain
    assert int(gz_headers["content-length"]) == len(compressed)


def test_static_answers_304_for_a_matching_etag(frontend):
    _, headers, body = _static_call(frontend, "/app/js/api.js")
    status, again, again_body = _static_call(
        frontend, "/app/js/api.js", headers={"If-None-Match": headers["etag"]})
    assert status == 304
    assert again_body == b""
    assert again["etag"] == headers["etag"]
    assert len(body) > 0


def test_static_blocks_path_traversal(frontend):
    for path in ("/app/../../etc/passwd", "/app/../../../etc/passwd",
                 "/app/..%2f..%2fetc%2fpasswd"):
        status, _, _ = _static_call(frontend, path)
        assert status == 404, path


def test_static_404s_a_missing_file(frontend):
    status, headers, body = _static_call(frontend, "/app/definitely-not-here.js")
    assert status == 404
    assert headers["cache-control"] == "no-store"
    assert b"Not found" in body


def test_static_caches_in_memory_after_the_first_read(frontend):
    _static_call(frontend, "/app/js/ui.js")
    before = frontend.stats()
    for _ in range(20):
        _static_call(frontend, "/app/js/ui.js")
    after = frontend.stats()
    assert after["hits"] - before["hits"] == 20
    assert after["misses"] == before["misses"], "must not touch the disk again"
    assert after["gzipped_bytes"] > 0


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
def _drive_limiter(limiter, path, client_ip="1.2.3.4"):
    scope = {"type": "http", "method": "GET", "path": path, "root_path": "",
             "client": (client_ip, 1), "headers": []}
    out = []

    async def inner_app(scope, receive, send):
        out.append("called")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limiter.app = inner_app

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        out.append(message)

    asyncio.new_event_loop().run_until_complete(limiter(scope, receive, send))
    start = next((m for m in out if isinstance(m, dict)
                  and m["type"] == "http.response.start"), None)
    return (start["status"] if start else None), out


def test_rate_limiter_exempts_the_static_prefix():
    """The UI is a dozen in-memory files; limiting them only breaks the page."""
    limiter = RateLimitMiddleware(None, requests=2, window_s=60,
                                  exempt=frozenset({"/health"}),
                                  exempt_prefixes=("/app",))
    for _ in range(10):
        status, _ = _drive_limiter(limiter, "/app/js/api.js")
        assert status == 200


def test_rate_limiter_returns_429_with_retry_after():
    limiter = RateLimitMiddleware(None, requests=2, window_s=60, exempt=frozenset())
    assert _drive_limiter(limiter, "/api/v1/search")[0] == 200
    assert _drive_limiter(limiter, "/api/v1/search")[0] == 200
    status, out = _drive_limiter(limiter, "/api/v1/search")
    assert status == 429
    headers = {k.decode(): v.decode()
               for k, v in next(m for m in out if isinstance(m, dict)
                                and m["type"] == "http.response.start")["headers"]}
    assert int(headers["retry-after"]) > 0
    body = next(m for m in out if isinstance(m, dict)
                and m["type"] == "http.response.body")["body"]
    assert b"Rate limit exceeded" in body


def test_rate_limiter_is_per_client():
    limiter = RateLimitMiddleware(None, requests=1, window_s=60, exempt=frozenset())
    assert _drive_limiter(limiter, "/x", "1.1.1.1")[0] == 200
    assert _drive_limiter(limiter, "/x", "1.1.1.1")[0] == 429
    assert _drive_limiter(limiter, "/x", "2.2.2.2")[0] == 200


def test_rate_limiter_bounds_its_client_table():
    """One deque per distinct IP kept forever is a slow memory leak."""
    limiter = RateLimitMiddleware(None, requests=100, window_s=60,
                                  exempt=frozenset(), max_clients=64)
    for i in range(400):
        _drive_limiter(limiter, "/x", f"10.0.{i // 250}.{i % 250}")
    assert len(limiter._hits) <= limiter.max_clients + 1


# --------------------------------------------------------------------------- #
# TTL cache
# --------------------------------------------------------------------------- #
def test_ttl_cache_expires():
    cache = TTLCache("t", ttl=0.05, max_entries=8)
    cache.set("k", {"v": 1})
    assert cache.get("k") == {"v": 1}
    time.sleep(0.08)
    assert cache.get("k") is None


def test_ttl_cache_is_disabled_at_zero_ttl():
    cache = TTLCache("off", ttl=0, max_entries=8)
    assert cache.set("k", 1) is False
    assert cache.get("k") is None


def test_ttl_cache_bounds_entries_and_bytes():
    cache = TTLCache("bounded", ttl=60, max_entries=4, max_bytes=10_000)
    for i in range(20):
        cache.set(i, "x" * 100)
    assert len(cache) <= 4

    tiny = TTLCache("tiny", ttl=60, max_entries=100, max_bytes=500)
    assert tiny.set("big", "y" * 5000) is False
    assert tiny.get("big") is None


def test_ttl_cache_reports_a_hit_rate():
    cache = TTLCache("stats", ttl=60, max_entries=8)
    cache.set("k", 1)
    cache.get("k")
    cache.get("missing")
    stats = cache.stats()
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert stats["hit_rate"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Overpass / Nominatim single-flight and caching
# --------------------------------------------------------------------------- #
def test_concurrent_identical_overpass_calls_collapse_into_one(monkeypatch):
    """Two visitors scanning the same neighbourhood must not both pay for a
    multi-second Overpass query, nor both burn a slot in its politeness budget."""
    calls = {"n": 0}

    async def fake_post(query):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"elements": [{
            "type": "way", "id": 1, "tags": {"building": "yes"},
            "geometry": [{"lon": 76.28, "lat": 9.98}, {"lon": 76.281, "lat": 9.98},
                         {"lon": 76.281, "lat": 9.981}, {"lon": 76.28, "lat": 9.981}]}]}

    monkeypatch.setattr(osm, "_post_overpass", fake_post)
    osm.clear_caches()

    async def scenario():
        return await asyncio.gather(*[
            osm.fetch_buildings_in_radius(9.98, 76.28, 1.0) for _ in range(8)])

    results = _run(scenario())
    assert calls["n"] == 1, f"expected one upstream call, got {calls['n']}"
    assert all(len(r) == 1 for r in results)


def test_repeat_overpass_call_is_served_from_cache(monkeypatch):
    calls = {"n": 0}

    # A populated answer: empty results are deliberately NOT cached (see
    # test_empty_overpass_scan_is_not_cached), so they cannot exercise this.
    one_building = {"elements": [{
        "type": "way", "id": 1,
        "tags": {"building": "house"},
        "geometry": [{"lon": 22.0, "lat": 11.0}, {"lon": 22.001, "lat": 11.0},
                     {"lon": 22.001, "lat": 11.001}, {"lon": 22.0, "lat": 11.0}],
    }]}

    async def fake_post(query):
        calls["n"] += 1
        return one_building

    monkeypatch.setattr(osm, "_post_overpass", fake_post)
    osm.clear_caches()

    async def scenario():
        await osm.fetch_buildings_in_radius(11.0, 22.0, 1.0)
        await osm.fetch_buildings_in_radius(11.0, 22.0, 1.0)
        # A different centre is a different query.
        await osm.fetch_buildings_in_radius(11.5, 22.0, 1.0)

    _run(scenario())
    assert calls["n"] == 2


def test_reverse_geocode_cache_key_is_coarse(monkeypatch):
    """Buildings ~110 m apart share an administrative area, so they must share
    one Nominatim lookup rather than each paying its 1 req/s politeness limit."""
    calls = []

    async def fake_get(self, url, params=None, **kwargs):
        calls.append((params["lat"], params["lon"]))
        return _FakeResponse({"display_name": "X", "address": {"state": "Kerala"}})

    class _FakeResponse:
        def __init__(self, payload):
            self.content = __import__("orjson").dumps(payload)
            self.status_code = 200

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    osm.clear_caches()

    async def scenario():
        # Three points within ~50 m of each other.
        a = await osm.reverse_geocode(9.98160, 76.29990)
        b = await osm.reverse_geocode(9.98161, 76.29991)
        c = await osm.reverse_geocode(9.98162, 76.29992)
        return a, b, c

    a, b, c = _run(scenario())
    assert len(calls) == 1, f"expected one upstream lookup, got {calls}"
    assert a == b == c
    assert a["state"] == "Kerala"


# --------------------------------------------------------------------------- #
# Response encoding
# --------------------------------------------------------------------------- #
def test_fast_json_response_renders_valid_json():
    import json as _json

    from app.main import FastJSONResponse

    payload = {"success": True, "data": {"ulpin": "32070410180902", "n": 1.5,
                                         "list": [1, 2, 3], "none": None}}
    response = FastJSONResponse(payload)
    assert response.media_type == "application/json"
    assert _json.loads(response.body) == payload


def test_fast_json_response_is_not_the_deprecated_fastapi_class():
    """FastAPI deprecated ORJSONResponse, and setting it also disables the
    newer pydantic-core fast path, so the class must stay our own."""
    import warnings

    from fastapi.responses import ORJSONResponse

    from app.main import FastJSONResponse

    assert not issubclass(FastJSONResponse, ORJSONResponse)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        FastJSONResponse({"ok": True})


def test_gzip_is_applied_above_the_threshold_only():
    async def scenario(path, accept_gzip=True):
        headers = {"Accept-Encoding": "gzip"} if accept_gzip else {}
        async with httpx.AsyncClient(transport=_transport(), base_url="http://perf",
                                     headers=headers) as client:
            return await client.get(path)

    big = _run(scenario("/app/js/map.js"))
    assert big.headers.get("content-encoding") == "gzip"

    small = _run(scenario("/health"))
    assert small.headers.get("content-encoding") is None
    assert small.status_code == 200


def test_health_is_cached_so_the_count_is_not_a_full_scan_per_probe():
    from app.main import _health_cache, _read_health

    _health_cache.clear()
    first = _run(_read_health())
    second = _run(_read_health())
    assert first == second
    assert first[0] == "connected"
    assert _health_cache.stats()["hits"] >= 1
