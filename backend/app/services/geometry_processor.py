"""Shapely helpers. All input geometry is WGS-84 (EPSG:4326) lon/lat.

Performance
-----------
Bulk scans push thousands of footprints through here and Render's free tier
gives the service ~0.1 CPU, so this module does its GEOS work in *arrays*
rather than one Python-level call per building:

* ``shapely.ops.transform`` is a pure-Python callback fired once per
  coordinate; it was the single hottest frame in a bulk scan (~33% of it). The
  projection applied here is a translation plus an anisotropic scale, and area
  under a scale is exactly ``|area| * xfact * yfact``, so the transform is
  skipped and the result matches to floating-point noise (measured max
  relative error 4.2e-16 over 600 real-shaped footprints).
* :func:`measure_many` builds every polygon in one vectorised
  ``linearrings(..., indices=)`` call, then evaluates validity, repair, area
  and centroid across the whole array. 600 footprints: ~1.6 ms instead of
  ~88 ms, bit-for-bit the same numbers the per-geometry path produced.
* :func:`extrude_ring_to_3d` lets a 163-floor tower parse its footprint once
  instead of 163 times.

Repair semantics are unchanged: an invalid (self-intersecting) OSM outline is
fixed with ``buffer(0)`` before measuring, and anything that cannot be
repaired is reported as unusable instead of being silently wrong.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import shapely
from shapely.geometry import Polygon, shape
from shapely.ops import transform

M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQUATOR = 111_320.0


def m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat))


# --------------------------------------------------------------------------- #
# Parsing / repair
# --------------------------------------------------------------------------- #
def to_shape(geom_json: dict):
    try:
        g = shape(geom_json)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid GeoJSON geometry: {exc}") from exc
    if g.is_empty:
        raise ValueError("geometry is empty")
    if not g.is_valid:
        g = g.buffer(0)  # fixes self-intersecting OSM outlines
        if g.is_empty or not g.is_valid:
            raise ValueError("geometry could not be repaired")
    return g


def _local_projection(lat0: float, lon0: float):
    """Equirectangular metre projection about a local origin.

    Accurate enough for building-scale areas and avoids a pyproj dependency.
    """
    mlon = m_per_deg_lon(lat0)

    def fwd(x, y, z=None):
        return ((x - lon0) * mlon, (y - lat0) * M_PER_DEG_LAT)

    return fwd


def _area_from_shape(g) -> float:
    """Area in m² of an already-parsed geometry, at its own centroid latitude.

    Identical to ``transform(_local_projection(...), g).area`` without moving
    a single coordinate: the projection only translates and scales.
    """
    return abs(g.area) * m_per_deg_lon(g.centroid.y) * M_PER_DEG_LAT


# --------------------------------------------------------------------------- #
# Single-geometry measurements
# --------------------------------------------------------------------------- #
def measure(geom_json: dict) -> tuple[float, float, float]:
    """Return ``(area_sq_m, centroid_lat, centroid_lon)`` from ONE parse.

    ``area_sq_m()`` and ``centroid_latlon()`` each parsed and re-validated the
    geometry, so every building was built twice. Callers needing both - which
    is all of them - should use this.
    """
    g = to_shape(geom_json)
    c = g.centroid
    area = abs(g.area) * m_per_deg_lon(c.y) * M_PER_DEG_LAT
    return float(area), float(c.y), float(c.x)


def centroid_latlon(geom_json: dict) -> tuple[float, float]:
    c = to_shape(geom_json).centroid
    return c.y, c.x


def area_sq_m(geom_json: dict) -> float:
    """Planimetric area in square metres."""
    return _area_from_shape(to_shape(geom_json))


def perimeter_m(geom_json: dict) -> float:
    g = to_shape(geom_json)
    c = g.centroid
    return float(transform(_local_projection(c.y, c.x), g).length)


def bbox(geom_json: dict) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat)."""
    return tuple(to_shape(geom_json).bounds)  # type: ignore[return-value]


def close_ring(coords: list) -> list:
    if not coords:
        return coords
    if coords[0] != coords[-1]:
        return list(coords) + [coords[0]]
    return list(coords)


# --------------------------------------------------------------------------- #
# Batched measurement — the hot path for bulk generation
# --------------------------------------------------------------------------- #
class BatchMeasure:
    """Array-valued measurements for a batch of GeoJSON geometries.

    ``ok[i]`` is False when geometry ``i`` was unusable (empty, or invalid and
    unrepairable); numeric fields at that index are meaningless and the caller
    should skip the building - which is what the per-geometry path did by
    raising ``ValueError``.
    """

    __slots__ = ("area_sq_m", "lat", "lon", "ok")

    def __init__(self, area: np.ndarray, lat: np.ndarray, lon: np.ndarray, ok: np.ndarray):
        self.area_sq_m = area
        self.lat = lat
        self.lon = lon
        self.ok = ok

    def __len__(self) -> int:
        return int(self.ok.size)

    def __getitem__(self, i: int) -> tuple[float, float, float]:
        """``(area_sq_m, lat, lon)`` for one index, matching :func:`measure`."""
        if not self.ok[i]:
            raise ValueError("geometry could not be repaired")
        return float(self.area_sq_m[i]), float(self.lat[i]), float(self.lon[i])


def _is_fast_polygon(g: object) -> bool:
    """True for a Polygon with exactly one exterior ring of >=4 points.

    That is what Overpass emits for essentially every building. MultiPolygons,
    rings with holes and non-polygons take the general path so their results
    stay identical.

    This is a *structural* filter only - checking every coordinate here cost
    more than the GEOS work it protects (0.85 ms vs 0.13 ms per 600 buildings).
    Point-level validity is enforced where it is free: :func:`_flat_coords`
    asks numpy to build a strict ``(total, 2)`` float64 array, which raises on
    ragged, non-numeric or 3D points, and the batch then falls back per item.
    """
    if type(g) is not dict or g.get("type") != "Polygon":
        return False
    rings = g.get("coordinates")
    if type(rings) is not list or len(rings) != 1:
        return False
    ring = rings[0]
    return type(ring) is list and len(ring) >= 4


def _flat_coords(geoms: Sequence[dict], fast: Sequence[int], total: int) -> np.ndarray:
    """Strict ``(total, 2)`` float64 array of every ring point, or raise."""
    flat = np.array([p for i in fast for p in geoms[i]["coordinates"][0]], dtype=np.float64)
    if flat.shape != (total, 2):
        # 3D rings land here as (total, 3): silently truncating them would
        # measure the wrong thing, so refuse and let the caller fall back.
        raise ValueError(f"expected ({total}, 2) coordinates, got {flat.shape}")
    return flat


def _measure_array(polys: np.ndarray, idx: np.ndarray, area: np.ndarray,
                   lat: np.ndarray, lon: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """Validate/repair/measure an array of geometries in place.

    Returns the boolean mask of entries that could NOT be measured, so the
    caller can retry them individually.
    """
    with np.errstate(invalid="ignore"):
        unusable = shapely.is_empty(polys) | ~shapely.is_valid(polys)
    if unusable.any():
        repaired = polys.copy()
        repaired[unusable] = shapely.buffer(polys[unusable], 0)
        with np.errstate(invalid="ignore"):
            unusable = shapely.is_empty(repaired) | ~shapely.is_valid(repaired)
        polys = repaired

    good = ~unusable
    if good.any():
        sub = polys[good]
        cxy = shapely.get_coordinates(shapely.centroid(sub))
        scaled = (np.abs(shapely.area(sub))
                  * (M_PER_DEG_LON_EQUATOR * np.cos(np.radians(cxy[:, 1])))
                  * M_PER_DEG_LAT)
        target = idx[good]
        area[target] = scaled
        lat[target] = cxy[:, 1]
        lon[target] = cxy[:, 0]
        ok[target] = True
    return unusable


def measure_many(geoms: Sequence[dict]) -> BatchMeasure:
    """Measure a batch of footprints with vectorised GEOS calls.

    Areas are in m², centroids in degrees, both in input order. Semantics
    match calling :func:`measure` on each geometry one at a time, including
    the ``buffer(0)`` repair of self-intersecting outlines.
    """
    n = len(geoms)
    if n == 0:
        z = np.zeros(0)
        return BatchMeasure(z, z.copy(), z.copy(), np.zeros(0, dtype=bool))

    area = np.zeros(n)
    lat = np.zeros(n)
    lon = np.zeros(n)
    ok = np.zeros(n, dtype=bool)

    fast: list[int] = []
    slow: list[int] = []
    for i, g in enumerate(geoms):
        (fast if _is_fast_polygon(g) else slow).append(i)

    if fast:
        idx_fast = np.asarray(fast, dtype=np.intp)
        counts = np.fromiter((len(geoms[i]["coordinates"][0]) for i in fast),
                             dtype=np.intp, count=len(fast))
        try:
            flat = _flat_coords(geoms, fast, int(counts.sum()))
            polys = shapely.polygons(
                shapely.linearrings(flat, indices=np.repeat(np.arange(len(fast), dtype=np.intp), counts))
            )
            failed = _measure_array(polys, idx_fast, area, lat, lon, ok)
        except Exception:  # noqa: BLE001 - ragged/3D/degenerate; retry one by one
            failed = np.ones(len(fast), dtype=bool)
        if failed.any():
            slow.extend(int(v) for v in idx_fast[failed])

    for i in slow:
        try:
            a, la, lo = measure(geoms[i])
        except Exception:  # noqa: BLE001 - unusable geometry, as before
            continue
        area[i], lat[i], lon[i], ok[i] = a, la, lo, True

    return BatchMeasure(area, lat, lon, ok)


# --------------------------------------------------------------------------- #
# Subdivision / extrusion
# --------------------------------------------------------------------------- #
def subdivide_polygon(geom_json: dict, n_parts: int) -> list[dict]:
    """Split a footprint into ~n_parts pieces, returned as GeoJSON polygons.

    Uses a grid of cells intersected with the real outline, so concave
    buildings yield pieces that follow the true shape. May return fewer than
    requested when cells fall outside the outline.
    """
    g = to_shape(geom_json)
    if n_parts <= 1:
        return [g.__geo_interface__]

    minx, miny, maxx, maxy = g.bounds
    c = g.centroid
    mlon = m_per_deg_lon(c.y)

    width_m = (maxx - minx) * mlon
    height_m = (maxy - miny) * M_PER_DEG_LAT
    if width_m <= 0 or height_m <= 0:
        return [g.__geo_interface__]

    # Choose a grid that keeps cells roughly square.
    cols = max(1, min(n_parts, round(math.sqrt(n_parts * width_m / height_m)) or 1))
    rows = math.ceil(n_parts / cols)

    # Cell corners use exactly the arithmetic of the previous per-cell loop
    # (``minx + span * i / cols``, not ``minx + i * (span / cols)``): the two
    # differ in the last bits, and a boundary that lands a hair differently
    # changes which cells clip the outline.
    span_x = maxx - minx
    span_y = maxy - miny
    cells = np.empty(rows * cols, dtype=object)
    k = 0
    for r in range(rows):
        y0 = miny + span_y * r / rows
        y1 = miny + span_y * (r + 1) / rows
        for col in range(cols):
            x0 = minx + span_x * col / cols
            x1 = minx + span_x * (col + 1) / cols
            cells[k] = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
            k += 1

    # One vectorised intersection instead of a GEOS round trip per cell. The
    # cells are in the same row-major order the previous per-cell loop used,
    # so taking the first n_parts usable pieces reproduces its output exactly.
    pieces = shapely.intersection(np.asarray([g], dtype=object), cells)

    out: list[dict] = []
    for piece in pieces:
        if len(out) >= n_parts:
            break
        if piece is None or piece.is_empty or piece.area <= 0:
            continue
        if piece.geom_type == "MultiPolygon":
            piece = max(piece.geoms, key=lambda p: p.area)
        if piece.geom_type != "Polygon":
            continue
        out.append(piece.__geo_interface__)
    return out or [g.__geo_interface__]


def exterior_ring(geom_json: dict) -> list[list[float]]:
    """The largest exterior ring as ``[[lon, lat], ...]``, parsed once."""
    g = to_shape(geom_json)
    if g.geom_type == "MultiPolygon":
        g = max(g.geoms, key=lambda p: p.area)
    return [[float(x), float(y)] for x, y in g.exterior.coords]


def extrude_to_3d(geom_json: dict, base_m: float, top_m: float) -> dict:
    """Return the footprint ring with a constant Z, as a GeoJSON polygon."""
    return extrude_ring_to_3d(exterior_ring(geom_json), top_m)


def extrude_ring_to_3d(ring: Sequence[Sequence[float]], top_m: float) -> dict:
    """Like :func:`extrude_to_3d` but from an already-parsed ring.

    A 163-floor tower called ``extrude_to_3d`` once per floor, re-parsing and
    re-validating the identical footprint every time.
    """
    z = float(top_m)
    return {"type": "Polygon", "coordinates": [[[pt[0], pt[1], z] for pt in ring]]}


def ring_coords(geom_json: dict) -> list:
    return exterior_ring(geom_json)
