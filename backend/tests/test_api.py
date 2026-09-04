"""Backend test suite. Network-dependent endpoints are exercised with stubs."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Point at a throwaway database BEFORE importing the app.
_TMP_DB = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["RATE_LIMIT_REQUESTS"] = "100000"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import model_3d_generator as m3d  # noqa: E402
from app.services import ulpin_generator as ug  # noqa: E402

init_db()
client = TestClient(app)


def square(lat: float, lon: float, half: float = 0.0003) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - half, lat - half], [lon + half, lat - half],
            [lon + half, lat + half], [lon - half, lat + half],
            [lon - half, lat - half],
        ]],
    }


# --------------------------------------------------------------------------- #
# ULPIN structure
# --------------------------------------------------------------------------- #
def test_ulpin_is_14_digits():
    u = ug.generate_ulpin_code("32", "07", "041", "018", 902)
    assert u == "32070410180902" and len(u) == 14


def test_floor_and_unit_ulpin_lengths():
    base = ug.generate_ulpin_code("09", "12", "105", "055", 41)
    assert len(ug.floor_ulpin(base, 1)) == 17
    assert len(ug.unit_ulpin(base, 1, 1)) == 20
    assert ug.unit_ulpin(base, 163, 12).endswith("163012")


def test_plot_number_overflow_rejected():
    with pytest.raises(ValueError):
        ug.generate_ulpin_code("09", "12", "105", "055", 10000)


def test_parse_roundtrip():
    base = ug.generate_ulpin_code("32", "07", "041", "018", 902)
    parsed = ug.parse_unit_ulpin(ug.unit_ulpin(base, 12, 3))
    assert parsed["base_ulpin"] == base
    assert parsed["floor_number"] == 12 and parsed["unit_number"] == 3


def test_state_code_lookup():
    assert ug.state_code_for("Kerala") == "32"
    assert ug.state_code_for("Uttar Pradesh") == "09"
    assert ug.state_code_for(None) == "99"


# --------------------------------------------------------------------------- #
# Floor accuracy — the headline requirement
# --------------------------------------------------------------------------- #
def test_burj_khalifa_gives_163_floors():
    """With levels tagged, the count must be exact, not height/3.5."""
    model = m3d.generate_accurate_3d_model(
        geometry=square(25.1972, 55.2744, 0.00056),
        base_ulpin="9" * 14, height_m=828.0,
        building_type="commercial", explicit_levels=163,
    )
    assert model["building"]["estimated_floors"] == 163
    assert len(model["floors"]) == 163
    assert len(model["geometry_3d"]["features"]) == 163


def test_height_only_estimate_discounts_spire():
    """828 m with no level data must not naively yield ~207 floors."""
    model = m3d.generate_accurate_3d_model(
        geometry=square(25.1972, 55.2744, 0.00056),
        base_ulpin="9" * 14, height_m=828.0, building_type="commercial",
    )
    assert 120 <= model["building"]["estimated_floors"] <= 175


def test_levels_take_precedence_over_height():
    h, levels, source = m3d.estimate_height({"building:levels": "163", "height": "828"}, "commercial")
    assert levels == 163 and "levels" in source


def test_roof_levels_added():
    _, levels, _ = m3d.estimate_height({"building:levels": "10", "roof:levels": "1"}, "residential")
    assert levels == 11


def test_mechanical_floors_have_no_units():
    model = m3d.generate_accurate_3d_model(
        geometry=square(25.1972, 55.2744, 0.0005),
        base_ulpin="9" * 14, height_m=200.0,
        building_type="commercial", explicit_levels=50,
    )
    mech = [f for f in model["floors"] if f["floor_type"] == "mechanical"]
    assert mech, "expected mechanical floors every 25 storeys"
    assert all(f["units_on_floor"] == 0 for f in mech)


def test_small_house_is_low_rise():
    model = m3d.generate_accurate_3d_model(
        geometry=square(9.9815, 76.2839, 0.00005),
        base_ulpin="9" * 14, building_type="house",
    )
    assert model["building"]["estimated_floors"] <= 3


def test_unit_ulpins_are_unique():
    model = m3d.generate_accurate_3d_model(
        geometry=square(9.9815, 76.2839, 0.0002),
        base_ulpin="32070410180902", height_m=60.0,
        building_type="apartments", explicit_levels=17,
    )
    ids = [u["unit_ulpin"] for u in model["units"]]
    assert len(ids) == len(set(ids))
    assert all(len(i) == 20 for i in ids)


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
def test_root_and_health():
    assert client.get("/").json()["success"] is True
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] == "connected"


def test_generate_ulpin_endpoint():
    r = client.post("/api/v1/generate-ulpin", json={
        "state_code": "32", "district_code": "07",
        "sub_district_code": "041", "village_code": "018", "plot_number": 902,
    })
    assert r.status_code == 200
    assert r.json()["data"]["ulpin"] == "32070410180902"


def test_generate_ulpin_validation_error():
    r = client.post("/api/v1/generate-ulpin", json={"plot_number": 99999})
    assert r.status_code == 422  # Pydantic rejects before handler


def test_create_get_and_delete_parcel():
    payload = {
        "geometry": square(9.9815, 76.2839, 0.0002),
        "name": "Test Tower", "building_type": "apartments",
        "levels": 12, "auto_detect_admin": False,
        "state_code": "32", "district_code": "07",
        "sub_district_code": "041", "village_code": "018",
    }
    r = client.post("/api/v1/parcels", json=payload)
    assert r.status_code == 201, r.text
    props = r.json()["data"]["properties"]
    ulpin = props["ulpin"]
    assert len(ulpin) == 14 and props["total_floors"] == 12

    assert client.get(f"/api/v1/parcels/{ulpin}").status_code == 200

    floors = client.get(f"/api/v1/parcels/{ulpin}/floors").json()
    assert floors["count"] == 12
    assert all(len(f["floor_ulpin"]) == 17 for f in floors["data"])

    units = client.get(f"/api/v1/parcels/{ulpin}/units?limit=5").json()
    assert units["total"] > 0 and len(units["data"]) <= 5
    assert all(len(u["unit_ulpin"]) == 20 for u in units["data"])

    assert client.delete(f"/api/v1/parcels/{ulpin}").status_code == 200
    assert client.get(f"/api/v1/parcels/{ulpin}").status_code == 404


def test_cascade_delete_removes_children():
    from app.database import FloorModel, SessionLocal, UnitModel

    r = client.post("/api/v1/parcels", json={
        "geometry": square(9.98, 76.28, 0.0002), "name": "Cascade",
        "building_type": "apartments", "levels": 4, "auto_detect_admin": False,
        "state_code": "32", "district_code": "07",
        "sub_district_code": "041", "village_code": "019",
    })
    ulpin = r.json()["data"]["properties"]["ulpin"]
    client.delete(f"/api/v1/parcels/{ulpin}")

    db = SessionLocal()
    try:
        assert db.query(FloorModel).filter(FloorModel.parent_ulpin == ulpin).count() == 0
        assert db.query(UnitModel).filter(UnitModel.parent_ulpin == ulpin).count() == 0
    finally:
        db.close()


def test_plot_numbers_auto_increment():
    base = {
        "building_type": "house", "levels": 2, "auto_detect_admin": False,
        "state_code": "32", "district_code": "07",
        "sub_district_code": "041", "village_code": "077",
    }
    a = client.post("/api/v1/parcels", json={**base, "geometry": square(9.90, 76.20)})
    b = client.post("/api/v1/parcels", json={**base, "geometry": square(9.91, 76.21)})
    pa = int(a.json()["data"]["properties"]["ulpin"][10:])
    pb = int(b.json()["data"]["properties"]["ulpin"][10:])
    assert pb == pa + 1


def test_3d_model_from_raw_geometry():
    r = client.post("/api/v1/generate-3d-model", json={
        "geometry": square(25.1972, 55.2744, 0.00056),
        "height_m": 828.0, "levels": 163,
        "building_type": "commercial", "include_unit_geometry": False,
    })
    assert r.status_code == 200
    assert r.json()["data"]["building"]["estimated_floors"] == 163


def test_3d_model_unknown_ulpin_404():
    r = client.post("/api/v1/generate-3d-model", json={"ulpin": "1" * 14})
    assert r.status_code == 404


def test_3d_model_requires_input():
    assert client.post("/api/v1/generate-3d-model", json={}).status_code == 400


def test_legacy_endpoint_still_works():
    r = client.post("/api/v1/generate-3d-parcel", json={
        "geometry": square(9.9815, 76.2839, 0.0002),
        "properties": {"floors": 8}, "height": 28.0,
    })
    assert r.status_code == 200
    assert r.json()["data"]["building"]["estimated_floors"] == 8


def test_decode_ulpin_endpoint():
    r = client.get("/api/v1/decode-ulpin/32070410180902163012")
    assert r.json()["data"]["floor_number"] == 163


def test_decode_bad_ulpin():
    assert client.get("/api/v1/decode-ulpin/123").status_code == 400


def test_search_requires_a_parameter():
    assert client.get("/api/v1/search").status_code == 400


def test_search_by_name():
    client.post("/api/v1/parcels", json={
        "geometry": square(12.97, 77.59, 0.0002), "name": "Findable Plaza",
        "building_type": "office", "levels": 5, "auto_detect_admin": False,
        "state_code": "29", "district_code": "09",
        "sub_district_code": "001", "village_code": "001",
    })
    r = client.get("/api/v1/search?q=Findable")
    assert r.json()["count"] >= 1


def test_bulk_generate_with_stubbed_osm(monkeypatch):
    """Bulk path without hitting the live Overpass API."""
    async def fake_fetch(lat, lon, radius_km):
        out = []
        for i in range(25):
            out.append({
                "type": "Feature",
                "geometry": square(9.9815 + i * 0.0006, 76.2839 + i * 0.0006, 0.00025),
                "properties": {
                    "osm_id": 900000 + i,
                    "building_type": ["apartments", "house", "office"][i % 3],
                    "name": f"Building {i}",
                    "tags": {"building:levels": str(2 + i % 15)} if i % 2 == 0 else {},
                },
            })
        return out

    async def fake_reverse(lat, lon):
        return {"state": "Kerala", "district": "Ernakulam",
                "sub_district": "Kanayannur", "village": "Kadavanthra",
                "display_name": "Kadavanthra, Ernakulam, Kerala"}

    monkeypatch.setattr("app.services.osm_fetcher.fetch_buildings_in_radius", fake_fetch)
    monkeypatch.setattr("app.services.osm_fetcher.reverse_geocode", fake_reverse)

    r = client.post("/api/v1/bulk-generate", json={
        "center_lat": 9.9815, "center_lon": 76.2839,
        "radius_km": 1.0, "persist": True, "generate_breakdown": False,
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["created"] == 25 and data["skipped"] == 0
    # Kerala must resolve to state code 32.
    assert data["admin_codes"]["state_code"] == "32"
    ulpins = [f["properties"]["ulpin"] for f in data["buildings"]["features"]]
    assert len(set(ulpins)) == 25, "ULPINs must be unique"


def test_bulk_generate_is_idempotent_on_osm_id(monkeypatch):
    async def fake_fetch(lat, lon, radius_km):
        return [{
            "type": "Feature",
            "geometry": square(8.5, 76.9, 0.00025),
            "properties": {"osm_id": 777001, "building_type": "house",
                           "name": "Repeat", "tags": {}},
        }]

    async def fake_reverse(lat, lon):
        return {"state": "Kerala", "district": "TVM",
                "sub_district": "X", "village": "Y"}

    monkeypatch.setattr("app.services.osm_fetcher.fetch_buildings_in_radius", fake_fetch)
    monkeypatch.setattr("app.services.osm_fetcher.reverse_geocode", fake_reverse)

    body = {"center_lat": 8.5, "center_lon": 76.9, "radius_km": 0.5, "persist": True}
    first = client.post("/api/v1/bulk-generate", json=body).json()["data"]
    second = client.post("/api/v1/bulk-generate", json=body).json()["data"]
    u1 = first["buildings"]["features"][0]["properties"]["ulpin"]
    u2 = second["buildings"]["features"][0]["properties"]["ulpin"]
    assert u1 == u2, "re-scanning must not mint a duplicate ULPIN"


def test_bulk_radius_limit_enforced():
    r = client.post("/api/v1/bulk-generate", json={
        "center_lat": 9.98, "center_lon": 76.28, "radius_km": 50,
    })
    assert r.status_code == 422  # schema caps at 5 km


def test_bulk_generate_empty_scan_is_a_featurecollection(monkeypatch):
    """An empty scan must speak the same shape as a populated one.

    It used to return "buildings": [], which made data.buildings.features
    undefined on every client that reads the collection - the map then showed
    a cheerful "0 buildings" with no way to tell a coverage hole from a crash.
    """
    async def fake_fetch(lat, lon, radius_km):
        return []

    async def fake_reverse(lat, lon):
        return {"display_name": "nowhere"}

    async def fake_count(lat, lon, radius_km):
        return 749

    monkeypatch.setattr("app.services.osm_fetcher.fetch_buildings_in_radius", fake_fetch)
    monkeypatch.setattr("app.services.osm_fetcher.reverse_geocode", fake_reverse)
    monkeypatch.setattr("app.services.osm_fetcher.count_buildings_in_radius", fake_count)

    r = client.post("/api/v1/bulk-generate", json={
        "center_lat": 9.9816, "center_lon": 76.2999, "radius_km": 0.1, "persist": True,
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["buildings"] == {"type": "FeatureCollection", "features": []}
    assert data["processed"] == 0
    # ...and it must explain itself: roofs are visible, footprints are not.
    assert data["diagnostics"]["wider_count"] == 749
    assert data["diagnostics"]["wider_radius_km"] >= 0.4
    assert data["diagnostics"]["source"] == "openstreetmap"


def test_bulk_generate_empty_scan_when_osm_unreachable(monkeypatch):
    async def fake_fetch(lat, lon, radius_km):
        return []

    async def fake_reverse(lat, lon):
        return {"display_name": "nowhere"}

    async def fake_count(lat, lon, radius_km):
        return -1

    monkeypatch.setattr("app.services.osm_fetcher.fetch_buildings_in_radius", fake_fetch)
    monkeypatch.setattr("app.services.osm_fetcher.reverse_geocode", fake_reverse)
    monkeypatch.setattr("app.services.osm_fetcher.count_buildings_in_radius", fake_count)

    r = client.post("/api/v1/bulk-generate", json={
        "center_lat": 9.9816, "center_lon": 76.2999, "radius_km": 0.1,
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["diagnostics"]["wider_count"] == -1


def test_empty_overpass_scan_is_not_cached(monkeypatch):
    """An empty answer must not be frozen into the cache for the full TTL.

    A half-loaded Overpass shard or a coverage hole would otherwise pin
    "0 buildings" onto an area that does have footprints.
    """
    import asyncio
    from app.services import osm_fetcher as osm

    calls = {"n": 0, "payload": {"elements": []}}

    async def fake_post(query):
        calls["n"] += 1
        return calls["payload"]

    monkeypatch.setattr("app.services.osm_fetcher._post_overpass", fake_post)
    osm.clear_caches()

    asyncio.run(osm.fetch_buildings_in_radius(9.98, 76.29, 0.1))
    asyncio.run(osm.fetch_buildings_in_radius(9.98, 76.29, 0.1))
    assert calls["n"] == 2, "an empty result must be re-queried, not served from cache"

    # A populated answer still is cached: that is the whole point of the cache.
    calls["payload"] = {"elements": [{
        "type": "way", "id": 1,
        "tags": {"building": "house"},
        "geometry": [{"lon": 76.29, "lat": 9.98}, {"lon": 76.2901, "lat": 9.98},
                     {"lon": 76.2901, "lat": 9.9801}, {"lon": 76.29, "lat": 9.98}],
    }]}
    asyncio.run(osm.fetch_buildings_in_radius(9.98, 76.29, 0.1))
    n_after = calls["n"]
    asyncio.run(osm.fetch_buildings_in_radius(9.98, 76.29, 0.1))
    assert calls["n"] == n_after, "a populated result must come from the cache"
    osm.clear_caches()


def test_osm_failure_returns_503(monkeypatch):
    from app.services.osm_fetcher import OSMError

    async def boom(*a, **k):
        raise OSMError("all mirrors down")

    monkeypatch.setattr("app.services.osm_fetcher.fetch_buildings_in_radius", boom)
    r = client.post("/api/v1/bulk-generate", json={
        "center_lat": 9.98, "center_lon": 76.28, "radius_km": 1.0,
    })
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"].lower()


def test_invalid_geometry_is_400_not_500():
    r = client.post("/api/v1/generate-3d-model", json={
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]},
        "height_m": 30,
    })
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def test_area_matches_known_square():
    from app.services.geometry_processor import area_sq_m
    # 0.0009 deg lat ~ 100 m; at the equator lon is the same.
    poly = {"type": "Polygon", "coordinates": [[
        [0.0, 0.0], [0.000904, 0.0], [0.000904, 0.000904], [0.0, 0.000904], [0.0, 0.0]]]}
    assert 9000 < area_sq_m(poly) < 11000


def test_subdivision_covers_footprint():
    from app.services.geometry_processor import area_sq_m, subdivide_polygon
    poly = square(9.9815, 76.2839, 0.0003)
    total = area_sq_m(poly)
    parts = subdivide_polygon(poly, 9)
    assert len(parts) == 9
    covered = sum(area_sq_m(p) for p in parts)
    assert abs(covered - total) / total < 0.02


def test_subdivision_handles_concave_shape():
    from app.services.geometry_processor import subdivide_polygon
    l_shape = {"type": "Polygon", "coordinates": [[
        [0, 0], [0.0004, 0], [0.0004, 0.0002], [0.0002, 0.0002],
        [0.0002, 0.0004], [0, 0.0004], [0, 0]]]}
    parts = subdivide_polygon(l_shape, 8)
    assert 1 <= len(parts) <= 8


# --------------------------------------------------------------------------- #
# Custom hyphenated ULPIN: {Country}-{State}-{District}-{City}-{Plot}-{Unit}
# --------------------------------------------------------------------------- #
def test_custom_ulpin_exact_expected_output():
    """The specified example must produce exactly IND-TN-001-CHE-F03-U301."""
    assert ug.generate_custom_ulpin(
        country="IND", state_code="TN", district_code="001",
        city_code="CHE", plot_code="F03", unit_code="U301",
    ) == "IND-TN-001-CHE-F03-U301"


def test_custom_ulpin_defaults_match_example():
    assert ug.generate_custom_ulpin() == "IND-TN-001-CHE-F03-U301"


@pytest.mark.parametrize("field,bad", [
    ("country", "IN"), ("country", "ind"), ("country", "INDIA"),
    ("state_code", "T"), ("state_code", "tn"), ("state_code", "TNX"),
    ("district_code", "01"), ("district_code", "ABC"), ("district_code", "0001"),
    ("city_code", "CH"), ("city_code", "che"),
    ("plot_code", "F3"), ("plot_code", "03F"), ("plot_code", "f03"),
    ("unit_code", "U30"), ("unit_code", "X301"), ("unit_code", "u301"),
])
def test_custom_ulpin_rejects_malformed_parts(field, bad):
    kwargs = {
        "country": "IND", "state_code": "TN", "district_code": "001",
        "city_code": "CHE", "plot_code": "F03", "unit_code": "U301",
    }
    kwargs[field] = bad
    with pytest.raises(ValueError):
        ug.generate_custom_ulpin(**kwargs)


def test_custom_ulpin_validation_can_be_skipped():
    assert ug.generate_custom_ulpin(country="xx", validate=False).startswith("xx-")


def test_custom_ulpin_roundtrip():
    parts = ug.parse_custom_ulpin("IND-TN-001-CHE-F03-U301")
    assert parts["city_code"] == "CHE"
    assert parts["floor_number"] == 3 and parts["unit_number"] == 301
    assert ug.generate_custom_ulpin(**{
        k: parts[k] for k in
        ("country", "state_code", "district_code", "city_code", "plot_code", "unit_code")
    }) == "IND-TN-001-CHE-F03-U301"


def test_custom_ulpin_endpoint_returns_exact_shape():
    r = client.post("/api/v1/generate-custom-ulpin", json={
        "country": "IND", "state_code": "TN", "district_code": "001",
        "city_code": "CHE", "plot_code": "F03", "unit_code": "U301",
    })
    assert r.status_code == 200
    # Response must be exactly {"ulpin": "..."} as specified.
    assert r.json() == {"ulpin": "IND-TN-001-CHE-F03-U301"}


def test_custom_ulpin_endpoint_normalises_lowercase():
    r = client.post("/api/v1/generate-custom-ulpin", json={
        "country": "ind", "state_code": "tn", "district_code": "001",
        "city_code": "che", "plot_code": "f03", "unit_code": "u301",
    })
    assert r.json() == {"ulpin": "IND-TN-001-CHE-F03-U301"}


def test_custom_ulpin_endpoint_bad_input_is_400_not_500():
    r = client.post("/api/v1/generate-custom-ulpin", json={
        "country": "INDIA", "state_code": "TN", "district_code": "001",
        "city_code": "CHE", "plot_code": "F03", "unit_code": "U301",
    })
    assert r.status_code == 400
    assert "country" in r.json()["message"]


def test_decode_custom_ulpin_endpoint():
    r = client.get("/api/v1/decode-custom-ulpin/IND-TN-001-CHE-F03-U301")
    assert r.status_code == 200
    assert r.json()["data"]["state_code"] == "TN"


def test_legacy_numeric_generator_untouched():
    """The original 14-digit generator must keep working unchanged."""
    assert ug.generate_ulpin_code("32", "07", "041", "018", 902) == "32070410180902"
    assert len(ug.generate_ulpin_code("09", "12", "105", "055", 41)) == 14


def test_long_ulpin_persists_in_widened_column():
    """String(50) must accept a hyphenated ULPIN end to end."""
    from app.database import ParcelModel, SessionLocal

    custom = ug.generate_custom_ulpin(plot_code="A01", unit_code="U999")
    db = SessionLocal()
    try:
        row = ParcelModel(
            ulpin=custom, name="Widened column test", building_type="residential",
            state_code="33", district_code="01", sub_district_code="001",
            village_code="001", plot_number=4321,
            centroid_lat=13.08, centroid_lon=80.27,
            area_sq_m=500.0, height_m=10.5, total_floors=3, total_units=5,
            geometry_json={}, properties_json={},
        )
        db.add(row)
        db.commit()
        stored = db.query(ParcelModel).filter(ParcelModel.ulpin == custom).one()
        assert stored.ulpin == "IND-TN-001-CHE-A01-U999"
        assert len(stored.ulpin) > 14
        db.delete(stored)
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# CORS origin handling
#
# A browser's Origin header is only ever scheme://host[:port]. Writing a
# GitHub Pages URL with its repository subpath into ALLOWED_ORIGINS used to be
# accepted verbatim and then never match, so responses came back 200 with no
# Access-Control-Allow-Origin header and the browser silently blocked them.
# --------------------------------------------------------------------------- #
from app.config import _normalise_origins  # noqa: E402


def test_origin_subpath_is_reduced_to_origin():
    assert _normalise_origins(
        "https://sharveshraam.github.io/ULPIN-GENERATION-PROTOTYPE"
    ) == ["https://sharveshraam.github.io"]


def test_origin_trailing_slash_is_stripped():
    assert _normalise_origins("https://sharveshraam.github.io/") == [
        "https://sharveshraam.github.io"
    ]


def test_wildcard_is_preserved():
    assert _normalise_origins("*") == ["*"]


def test_port_is_kept_and_duplicates_collapse():
    assert _normalise_origins("http://localhost:3000/app, http://localhost:3000") == [
        "http://localhost:3000"
    ]


def test_multiple_origins_are_each_normalised():
    assert _normalise_origins("https://a.github.io/repo , https://b.com/x/y") == [
        "https://a.github.io",
        "https://b.com",
    ]


def test_health_sends_cors_header_for_pages_origin():
    """End-to-end: the deployed frontend origin must be allowed."""
    origin = "https://sharveshraam.github.io"
    res = client.get("/health", headers={"Origin": origin})
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") in (origin, "*")


def test_preflight_allows_json_post_from_pages_origin():
    res = client.options(
        "/api/v1/generate-3d-model",
        headers={
            "Origin": "https://sharveshraam.github.io",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert res.status_code == 200
    assert "access-control-allow-origin" in res.headers


# --------------------------------------------------------------------------- #
# CORS fallback middleware
#
# The deployed Render service can carry an explicit (possibly malformed)
# ALLOWED_ORIGINS that does not include the actual frontend origin. The
# browser then throws "Failed to fetch" with no status, and the frontend
# mislabels it "API blocked" - even in a private window, because it is not an
# extension problem. The fallback guarantees CORS headers for any origin.
# --------------------------------------------------------------------------- #
def asyncio_run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _RawResponse:
    """The ASGI messages a middleware produced, as a response-like object."""

    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self.body = body


async def _cors_fallback_case(origin_header: bytes | None, pre_set: str | None = None,
                              with_credentials: bool = True, preflight: bool = False,
                              existing_vary: str | None = None):
    """Drive CorsFallbackMiddleware over a canned inner ASGI app.

    The middleware is pure ASGI now (BaseHTTPMiddleware allocated a task group
    and two memory streams per request), so it is exercised through a real
    scope/receive/send triple rather than a dispatch function.
    """
    from starlette.datastructures import Headers
    from starlette.responses import JSONResponse

    from app.middleware import CorsFallbackMiddleware

    async def inner(scope, receive, send):
        # Mimic Starlette's CORSMiddleware on a rejected request: 400 without
        # ACAO but with the credentials header it sets globally.
        resp = (JSONResponse({"detail": "Disallowed CORS origin"}, status_code=400)
                if preflight else JSONResponse({"ok": True}))
        if with_credentials:
            resp.headers["Access-Control-Allow-Credentials"] = "true"
        if pre_set:
            resp.headers["Access-Control-Allow-Origin"] = pre_set
        if existing_vary:
            resp.headers["Vary"] = existing_vary
        await resp(scope, receive, send)

    headers = [(b"host", b"api.test")]
    if origin_header is not None:
        headers.append((b"origin", origin_header))
    if preflight:
        headers += [(b"access-control-request-method", b"POST"),
                    (b"access-control-request-headers", b"content-type")]

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "OPTIONS" if preflight else "GET", "path": "/health",
             "raw_path": b"/health", "query_string": b"", "root_path": "",
             "scheme": "http", "client": ("testclient", 1), "server": ("test", 80),
             "headers": headers}

    captured = {"status": None, "headers": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = Headers(raw=list(message["headers"]))
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    await CorsFallbackMiddleware(inner)(scope, receive, send)
    return _RawResponse(captured["status"], captured["headers"], captured["body"])


def test_cors_fallback_echoes_unlisted_origin():
    """Even a totally unknown origin must get CORS headers (public API)."""
    res = asyncio_run(_cors_fallback_case(b"https://some-unknown-site.example"))
    assert res.headers["access-control-allow-origin"] == "https://some-unknown-site.example"
    assert res.headers["access-control-allow-methods"] == "*"
    assert res.headers["access-control-allow-headers"] == "*"
    assert res.headers["vary"] == "Origin"


def test_cors_fallback_never_pairs_wildcard_with_credentials():
    """'*' + credentials is rejected by browsers; never emit the pair."""
    res = asyncio_run(_cors_fallback_case(b"null", with_credentials=True))
    assert res.headers["access-control-allow-origin"] == "*"
    assert res.headers.get("access-control-allow-credentials") is None


def test_cors_fallback_converts_rejected_preflight_to_200():
    """A disallowed OPTIONS must answer 200 so the browser proceeds."""
    res = asyncio_run(_cors_fallback_case(b"https://some-unknown-site.example",
                                          preflight=True, with_credentials=True))
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "https://some-unknown-site.example"


def test_cors_fallback_drops_the_rejected_preflight_body():
    """A preflight response must not carry the 400's JSON error body."""
    res = asyncio_run(_cors_fallback_case(b"https://some-unknown-site.example",
                                          preflight=True))
    assert res.status_code == 200
    assert res.body == b""


def test_cors_fallback_keeps_credentials_on_echoed_origin():
    """Echoing the origin + credentials is valid, unlike '*' + credentials."""
    res = asyncio_run(_cors_fallback_case(b"https://sharveshraam.github.io",
                                          with_credentials=True))
    assert res.headers["access-control-allow-origin"] == "https://sharveshraam.github.io"
    assert res.headers["access-control-allow-credentials"] == "true"


def test_cors_fallback_leaves_existing_header_alone():
    res = asyncio_run(_cors_fallback_case(b"https://sharveshraam.github.io",
                                          pre_set="https://sharveshraam.github.io"))
    assert res.headers["access-control-allow-origin"] == "https://sharveshraam.github.io"


def test_cors_fallback_ignores_requests_without_origin():
    """Server-side probes (curl, uptime checks) need no CORS header."""
    res = asyncio_run(_cors_fallback_case(None))
    assert "access-control-allow-origin" not in res.headers


def test_cors_fallback_appends_to_vary_instead_of_clobbering():
    """GZipMiddleware sets 'Vary: Accept-Encoding'; overwriting it would let a
    shared cache serve gzipped bytes to a client that never advertised gzip."""
    res = asyncio_run(_cors_fallback_case(b"https://sharveshraam.github.io",
                                          existing_vary="Accept-Encoding"))
    vary = res.headers["vary"].lower()
    assert "accept-encoding" in vary and "origin" in vary


# --------------------------------------------------------------------------- #
# Health-probe aliases
#
# "/health" is a path token that ad-block and tracking-prevention filter lists
# commonly match, which cancels the request with ERR_BLOCKED_BY_CLIENT before
# it leaves the browser. The aliases give the client an identical endpoint
# under a name no filter list targets.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/health", "/status", "/ulpin-status"])
def test_health_aliases_return_identical_payload(path):
    res = client.get(path)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["database"] == "connected"
    assert body == client.get("/health").json()


@pytest.mark.parametrize("path", ["/status", "/ulpin-status"])
def test_health_aliases_send_cors_header(path):
    origin = "https://sharveshraam.github.io"
    res = client.get(path, headers={"Origin": origin})
    assert res.headers.get("access-control-allow-origin") in (origin, "*")


def test_health_aliases_are_hidden_from_schema():
    """They are plumbing, not public API - keep the docs clean."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/health" in paths
    assert "/status" not in paths
    assert "/ulpin-status" not in paths


def test_health_aliases_exempt_from_rate_limit():
    from app.main import _EXEMPT
    assert {"/health", "/status", "/ulpin-status"} <= _EXEMPT


# --------------------------------------------------------------------------- #
# Same-origin frontend
#
# Browser tracking prevention blocks by cross-site relationship, not by
# domain: github.io -> onrender.com is third-party and gets cancelled, while
# the identical request from a page already on onrender.com is first-party
# and passes. Serving the UI from the API origin sidesteps that entirely.
# --------------------------------------------------------------------------- #
def test_frontend_is_served_at_app():
    res = client.get("/app/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


@pytest.mark.parametrize("asset", ["map.html", "js/api.js", "js/config.js", "styles.css"])
def test_frontend_assets_are_served(asset):
    assert client.get(f"/app/{asset}").status_code == 200


def test_static_mount_does_not_shadow_api_routes():
    """The mount is added last; every API path must still resolve."""
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/v1/parcels?limit=1").json()["success"] is True


def test_root_still_returns_json_banner_not_html():
    """'/' is the documented API root and the last-resort health probe."""
    res = client.get("/")
    assert res.headers["content-type"].startswith("application/json")
    assert res.json()["data"]["name"]
    assert res.json()["data"]["frontend"] == "/app/"


# What a real browser sends when a person types the URL or a preview iframe
# loads it. Note the trailing */*;q=0.8 - the negotiation must not be fooled
# into thinking a browser wants JSON just because it accepts anything.
BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8"
)


def test_root_sends_a_browser_to_the_frontend():
    """A human landing on the bare host wants the app, not a JSON blob."""
    res = client.get("/", headers={"Accept": BROWSER_ACCEPT}, follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/app/"


def test_root_browser_redirect_actually_reaches_the_ui():
    res = client.get("/", headers={"Accept": BROWSER_ACCEPT})   # follows
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")


def test_root_keeps_json_for_api_clients():
    """fetch() sends Accept: */*, and js/api.js probes "/" as a health path."""
    for accept in ("*/*", "application/json", "", "application/json, */*;q=0.8"):
        res = client.get("/", headers={"Accept": accept}, follow_redirects=False)
        assert res.status_code == 200, accept
        assert res.headers["content-type"].startswith("application/json"), accept
        assert res.json()["success"] is True, accept


def test_root_prefers_json_when_both_are_equally_accepted():
    """A tie must not redirect: the machine-readable banner is the contract."""
    res = client.get("/", headers={"Accept": "text/html;q=0.5, application/json;q=0.5"},
                     follow_redirects=False)
    assert res.status_code == 200
    assert res.json()["data"]["frontend"] == "/app/"


def test_vendored_frontend_matches_repo_root():
    """The static copy served at /app must never drift from repo-root files.

    GitHub Pages serves the repo root; FastAPI serves backend/app/static.
    Both must stay identical - run scripts/sync_frontend.sh after frontend
    edits (or just change both). This test fails CI if they diverge.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    static = root / "backend" / "app" / "static"
    rels = ["index.html", "map.html", "styles.css", "tailwind.css"] + [
        f"js/{name}" for name in
        ("api.js", "config.js", "details.js", "flyto.js", "globe.js",
         "map.js", "ui.js", "3d-viewer.js")
    ]
    for rel in rels:
        assert (static / rel).read_bytes() == (root / rel).read_bytes(), (
            f"{rel} drifted; run scripts/sync_frontend.sh"
        )
