"""CPU-cost benchmark for the hot paths.

Render's free tier gives ~0.1 CPU, so what matters is *CPU seconds per
request*, not wall-clock on a dev laptop. This harness measures both, plus
response payload size (bytes cost CPU to serialise and to ship).

Run:  .venv/bin/python bench/bench.py
"""
from __future__ import annotations

import asyncio
import gc
import json
import math
import os
import random
import resource
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(os.path.dirname(HERE), "backend")
sys.path.insert(0, BACKEND)

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/bench.db"
os.environ["RATE_LIMIT_REQUESTS"] = "10000000"
os.environ.setdefault("PERSIST_UNITS_LIMIT", "4000")

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import osm_fetcher as osm  # noqa: E402


# --------------------------------------------------------------------------- #
# Synthetic OSM data: realistic footprints around Kochi
# --------------------------------------------------------------------------- #
def make_buildings(n: int, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    lat0, lon0 = 9.9816, 76.2999
    out = []
    kinds = ["yes", "house", "apartments", "commercial", "residential", "school"]
    for i in range(n):
        lat = lat0 + rng.uniform(-0.008, 0.008)
        lon = lon0 + rng.uniform(-0.008, 0.008)
        w = rng.uniform(0.00006, 0.0004)
        h = rng.uniform(0.00006, 0.0004)
        # Irregular 5-9 vertex footprint, like a real building outline.
        verts = rng.randint(4, 8)
        ring = []
        for k in range(verts):
            a = 2 * math.pi * k / verts
            ring.append([lon + w * math.cos(a) * rng.uniform(0.6, 1.0),
                         lat + h * math.sin(a) * rng.uniform(0.6, 1.0)])
        ring.append(ring[0])
        bt = rng.choice(kinds)
        tags = {"building": bt}
        if rng.random() < 0.45:
            tags["building:levels"] = str(rng.randint(1, 22))
        if rng.random() < 0.15:
            tags["height"] = str(round(rng.uniform(4, 90), 1))
        if rng.random() < 0.3:
            tags["name"] = f"Building {i}"
        out.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "osm_id": 100000 + i, "osm_type": "way", "building_type": bt,
                "height": tags.get("height"), "levels": tags.get("building:levels"),
                "name": tags.get("name"), "tags": tags,
            },
        })
    return out


SQUARE_BIG = {"type": "Polygon", "coordinates": [[
    [55.27384, 25.19664], [55.27496, 25.19664],
    [55.27496, 25.19776], [55.27384, 25.19776], [55.27384, 25.19664]]]}

SQUARE_SMALL = {"type": "Polygon", "coordinates": [[
    [76.28370, 9.98130], [76.28410, 9.98130],
    [76.28410, 9.98170], [76.28370, 9.98170], [76.28370, 9.98130]]]}


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
class Meter:
    def __init__(self):
        self.rows = []

    def run(self, label, fn, *, repeat=1):
        gc.collect()
        cpu0 = time.process_time()
        t0 = time.perf_counter()
        size = 0
        for _ in range(repeat):
            r = fn()
            size = r
        wall = (time.perf_counter() - t0) * 1000 / repeat
        cpu = (time.process_time() - cpu0) * 1000 / repeat
        self.rows.append((label, wall, cpu, size))
        return wall, cpu, size

    def report(self, baseline=None):
        w = max(len(r[0]) for r in self.rows) + 2
        print()
        hdr = f"{'benchmark'.ljust(w)}{'wall ms':>10}{'cpu ms':>10}{'bytes':>12}"
        if baseline:
            hdr += f"{'cpu x':>9}"
        print(hdr)
        print("-" * len(hdr))
        for label, wall, cpu, size in self.rows:
            line = f"{label.ljust(w)}{wall:>10.1f}{cpu:>10.1f}{size:>12,}"
            if baseline:
                b = baseline.get(label)
                line += f"{(b / cpu if b and cpu else 0):>8.1f}x" if b else f"{'-':>9}"
            print(line)


def _size(resp) -> int:
    body = getattr(resp, "content", None)
    if body is not None:
        return len(body)
    return len(json.dumps(resp)) if resp is not None else 0


def main() -> None:
    load = os.environ.get("BENCH_LOAD", "600")
    n_buildings = int(load)
    features = make_buildings(n_buildings)

    async def fake_radius(lat, lon, radius_km):
        return features

    async def fake_reverse(lat, lon):
        return {"display_name": "Kochi, Kerala, India", "state": "Kerala",
                "district": "Ernakulam", "sub_district": "Kochi", "village": "Ernakulam"}

    osm.fetch_buildings_in_radius = fake_radius
    osm.reverse_geocode = fake_reverse

    client = TestClient(app)
    init_db()
    m = Meter()

    # Warm-up: create tables, JIT-ish caches, connection pool.
    client.get("/health")

    m.run("GET /health", lambda: _size(client.get("/health")), repeat=20)
    m.run("GET / (banner)", lambda: _size(client.get("/")), repeat=20)
    m.run("GET /openapi.json", lambda: _size(client.get("/openapi.json")), repeat=3)
    m.run("GET /app/ (static html)", lambda: _size(client.get("/app/")), repeat=10)
    m.run("GET /app/js/map.js", lambda: _size(client.get("/app/js/map.js")), repeat=10)
    m.run("GET /app/tailwind.css", lambda: _size(client.get("/app/tailwind.css")), repeat=10)

    m.run("POST /generate-ulpin", lambda: _size(client.post("/api/v1/generate-ulpin", json={
        "state_code": "32", "district_code": "07", "sub_district_code": "041",
        "village_code": "018", "plot_number": 902})), repeat=20)

    m.run("POST /decode-ulpin/20d", lambda: _size(
        client.get("/api/v1/decode-ulpin/32070410180902163012")), repeat=20)

    m.run(f"POST /bulk-generate ({n_buildings}b persist)", lambda: _size(
        client.post("/api/v1/bulk-generate", json={
            "center_lat": 9.9816, "center_lon": 76.2999, "radius_km": 1.0,
            "persist": True, "generate_breakdown": False})))

    m.run(f"POST /bulk-generate ({n_buildings}b preview)", lambda: _size(
        client.post("/api/v1/bulk-generate", json={
            "center_lat": 9.9816, "center_lon": 76.2999, "radius_km": 1.0,
            "persist": False, "generate_breakdown": False})))

    m.run("POST /generate-3d-model (163fl)", lambda: _size(
        client.post("/api/v1/generate-3d-model", json={
            "geometry": SQUARE_BIG, "height_m": 828.0, "levels": 163,
            "building_type": "commercial", "include_unit_geometry": False})), repeat=3)

    m.run("POST /generate-3d-model (12fl+units)", lambda: _size(
        client.post("/api/v1/generate-3d-model", json={
            "geometry": SQUARE_SMALL, "height_m": 42.0, "levels": 12,
            "building_type": "apartments", "include_unit_geometry": True})), repeat=3)

    m.run("POST /parcels (create, breakdown)", lambda: _size(
        client.post("/api/v1/parcels", json={
            "geometry": SQUARE_SMALL, "name": "Bench Tower", "building_type": "apartments",
            "levels": 12, "auto_detect_admin": False, "state_code": "32",
            "district_code": "07", "sub_district_code": "041", "village_code": "555"})),
        repeat=5)

    m.run("GET /parcels?limit=1000", lambda: _size(
        client.get("/api/v1/parcels?limit=1000")), repeat=3)

    m.run("GET /search?q=Building", lambda: _size(
        client.get("/api/v1/search?q=Building")), repeat=5)

    m.run("GET /search proximity", lambda: _size(
        client.get("/api/v1/search?lat=9.9816&lon=76.2999&radius_km=1")), repeat=5)

    print(f"\nsynthetic buildings: {n_buildings}")
    print(f"peak RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MiB")
    m.report()

    if os.environ.get("BENCH_JSON"):
        with open(os.environ["BENCH_JSON"], "w") as fh:
            json.dump({r[0]: r[2] for r in m.rows}, fh, indent=2)


if __name__ == "__main__":
    main()
