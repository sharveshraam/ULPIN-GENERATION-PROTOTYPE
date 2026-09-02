"""Shapely helpers. All input geometry is WGS-84 (EPSG:4326) lon/lat."""
from __future__ import annotations

import math
from typing import Iterable

from shapely.geometry import Polygon, shape
from shapely.ops import transform

M_PER_DEG_LAT = 110_574.0


def m_per_deg_lon(lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat))


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


def centroid_latlon(geom_json: dict) -> tuple[float, float]:
    c = to_shape(geom_json).centroid
    return c.y, c.x


def _local_projection(lat0: float, lon0: float):
    """Equirectangular metre projection about a local origin.

    Accurate enough for building-scale areas and avoids a pyproj dependency.
    """
    mlon = m_per_deg_lon(lat0)

    def fwd(x, y, z=None):
        return ((x - lon0) * mlon, (y - lat0) * M_PER_DEG_LAT)

    return fwd


def area_sq_m(geom_json: dict) -> float:
    """Planimetric area in square metres."""
    g = to_shape(geom_json)
    c = g.centroid
    return abs(transform(_local_projection(c.y, c.x), g).area)


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


def subdivide_polygon(geom_json: dict, n_parts: int) -> list[dict]:
    """Split a footprint into ~n_parts pieces, returned as GeoJSON polygons.

    Uses a grid of cells intersected with the real outline, so concave
    buildings yield pieces that follow the true shape. May return fewer than
    requested when cells fall outside the outline.
    """
    if n_parts <= 1:
        return [to_shape(geom_json).__geo_interface__]

    g = to_shape(geom_json)
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

    out: list[dict] = []
    for r in range(rows):
        for col in range(cols):
            if len(out) >= n_parts:
                break
            cell = Polygon([
                (minx + (maxx - minx) * col / cols, miny + (maxy - miny) * r / rows),
                (minx + (maxx - minx) * (col + 1) / cols, miny + (maxy - miny) * r / rows),
                (minx + (maxx - minx) * (col + 1) / cols, miny + (maxy - miny) * (r + 1) / rows),
                (minx + (maxx - minx) * col / cols, miny + (maxy - miny) * (r + 1) / rows),
            ])
            piece = g.intersection(cell)
            if piece.is_empty or piece.area <= 0:
                continue
            if piece.geom_type == "MultiPolygon":
                piece = max(piece.geoms, key=lambda p: p.area)
            if piece.geom_type != "Polygon":
                continue
            out.append(piece.__geo_interface__)
    return out or [g.__geo_interface__]


def extrude_to_3d(geom_json: dict, base_m: float, top_m: float) -> dict:
    """Return the footprint ring with a constant Z, as a GeoJSON polygon."""
    g = to_shape(geom_json)
    if g.geom_type == "MultiPolygon":
        g = max(g.geoms, key=lambda p: p.area)
    ring = [[float(x), float(y), float(top_m)] for x, y in g.exterior.coords]
    return {"type": "Polygon", "coordinates": [ring]}


def ring_coords(geom_json: dict) -> list:
    g = to_shape(geom_json)
    if g.geom_type == "MultiPolygon":
        g = max(g.geoms, key=lambda p: p.area)
    return [[float(x), float(y)] for x, y in g.exterior.coords]
