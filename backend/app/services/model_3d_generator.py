"""Floor / unit breakdown and 3D extrusion.

Accuracy note
-------------
A naive ``floors = height / 3.5`` is wrong for real towers because the quoted
"height" usually includes architectural spires that contain no floors. The Burj
Khalifa is 828 m tall but has 163 occupied floors; 828/3.5 would claim 236.

So the rule here is:

1. If OSM (or the caller) supplies ``building:levels``, that is AUTHORITATIVE.
   Floor heights are then fitted to the occupied height.
2. Only when no level count exists do we estimate from height, and then we
   apply an occupancy factor so spires do not inflate the count.
"""
from __future__ import annotations

from typing import Any, Optional

from . import geometry_processor as geo
from .ulpin_generator import floor_ulpin, unit_ulpin

# --- Building type profiles ------------------------------------------------
# typical_floor_h, ground_floor_h, avg_unit_area_sq_m, default_levels
PROFILES: dict[str, dict[str, float]] = {
    "residential": {"typical": 3.5, "ground": 4.5, "unit_area": 85.0, "levels": 3},
    "apartments":  {"typical": 3.5, "ground": 4.5, "unit_area": 85.0, "levels": 8},
    "house":       {"typical": 3.2, "ground": 3.5, "unit_area": 110.0, "levels": 2},
    "detached":    {"typical": 3.2, "ground": 3.5, "unit_area": 120.0, "levels": 2},
    "bungalow":    {"typical": 3.2, "ground": 3.5, "unit_area": 130.0, "levels": 1},
    "commercial":  {"typical": 4.0, "ground": 4.5, "unit_area": 150.0, "levels": 5},
    "retail":      {"typical": 4.0, "ground": 4.5, "unit_area": 150.0, "levels": 3},
    "office":      {"typical": 4.0, "ground": 4.5, "unit_area": 150.0, "levels": 8},
    "hotel":       {"typical": 3.3, "ground": 5.0, "unit_area": 45.0, "levels": 9},
    "industrial":  {"typical": 6.0, "ground": 6.0, "unit_area": 400.0, "levels": 1},
    "warehouse":   {"typical": 8.0, "ground": 8.0, "unit_area": 600.0, "levels": 1},
    "school":      {"typical": 3.6, "ground": 4.0, "unit_area": 70.0, "levels": 3},
    "university":  {"typical": 3.8, "ground": 4.5, "unit_area": 90.0, "levels": 5},
    "hospital":    {"typical": 3.8, "ground": 4.5, "unit_area": 60.0, "levels": 7},
    "church":      {"typical": 6.0, "ground": 6.0, "unit_area": 200.0, "levels": 2},
    "yes":         {"typical": 3.5, "ground": 4.5, "unit_area": 85.0, "levels": 3},
}
DEFAULT_PROFILE = PROFILES["yes"]

MECHANICAL_INTERVAL = 25      # a plant floor roughly every N storeys
MECHANICAL_HEIGHT = 6.0
CORE_EFFICIENCY = 0.95        # usable floorplate after lifts/stairs/risers
MAX_FLOORS = 250              # sanity ceiling (tallest building ~163 occupied)


def profile_for(building_type: Optional[str]) -> dict[str, float]:
    return PROFILES.get((building_type or "yes").strip().lower(), DEFAULT_PROFILE)


def estimate_height(
    tags: dict[str, Any], building_type: str = "yes"
) -> tuple[float, Optional[int], str]:
    """Return (height_m, explicit_levels_or_None, human-readable source).

    Precedence: explicit levels > explicit height > type-based default.
    """
    prof = profile_for(building_type)
    tags = tags or {}

    def _f(v):
        try:
            return float(str(v).split()[0].replace(",", "."))
        except (TypeError, ValueError, IndexError):
            return None

    levels = None
    raw_levels = tags.get("building:levels")
    if raw_levels is not None:
        lv = _f(raw_levels)
        if lv and 0 < lv <= MAX_FLOORS:
            levels = int(lv)
            roof = _f(tags.get("roof:levels")) or 0
            levels += int(roof)

    explicit_h = _f(tags.get("height")) or _f(tags.get("building:height"))

    if levels:
        # Levels win. Derive a consistent height if none was tagged.
        height = explicit_h if explicit_h and explicit_h > 0 else _height_for_levels(levels, prof)
        return height, levels, "OSM building:levels"

    if explicit_h and explicit_h > 0:
        return explicit_h, None, "OSM height tag"

    default_levels = int(prof["levels"])
    return _height_for_levels(default_levels, prof), default_levels, f"estimated from building={building_type}"


def _height_for_levels(levels: int, prof: dict[str, float]) -> float:
    """Total height implied by a storey count, including mechanical floors."""
    if levels <= 0:
        return 0.0
    h = prof["ground"]
    for n in range(2, levels + 1):
        h += MECHANICAL_HEIGHT if n % MECHANICAL_INTERVAL == 0 else prof["typical"]
    return round(h, 2)


def calculate_floors_and_units(
    height_m: float,
    footprint_area_sq_m: float,
    building_type: str = "residential",
    explicit_levels: Optional[int] = None,
    unit_area_override: Optional[float] = None,
) -> dict[str, Any]:
    """Build the floor table and per-floor unit counts."""
    prof = profile_for(building_type)
    unit_area = float(unit_area_override or prof["unit_area"])
    if unit_area <= 0:
        raise ValueError("unit area must be positive")

    if explicit_levels and explicit_levels > 0:
        n_floors = min(int(explicit_levels), MAX_FLOORS)
        # Fit storey heights to the occupied height so elevations stay plausible.
        implied = _height_for_levels(n_floors, prof)
        scale = (height_m / implied) if (height_m and implied > 0) else 1.0
        # Guard against absurd scaling from a spire-inflated height tag.
        scale = max(0.6, min(scale, 1.6))
    else:
        # No level data: estimate, discounting non-occupied superstructure.
        occupied = height_m * (0.72 if height_m > 200 else 1.0)
        n_floors, acc = 0, 0.0
        while acc < occupied and n_floors < MAX_FLOORS:
            nxt = prof["ground"] if n_floors == 0 else (
                MECHANICAL_HEIGHT if (n_floors + 1) % MECHANICAL_INTERVAL == 0 else prof["typical"]
            )
            if acc + nxt > occupied and n_floors > 0:
                break
            acc += nxt
            n_floors += 1
        n_floors = max(1, n_floors)
        scale = 1.0

    floors: list[dict[str, Any]] = []
    elevation = 0.0
    for i in range(1, n_floors + 1):
        if i == 1:
            fh, ftype = prof["ground"] * scale, "ground"
        elif i % MECHANICAL_INTERVAL == 0:
            fh, ftype = MECHANICAL_HEIGHT * scale, "mechanical"
        else:
            fh, ftype = prof["typical"] * scale, "typical"

        usable = footprint_area_sq_m * (1.0 if i == 1 else CORE_EFFICIENCY)
        # Mechanical floors hold plant, not saleable units.
        n_units = 0 if ftype == "mechanical" else max(1, int(usable // unit_area))

        floors.append({
            "floor_number": i,
            "floor_height_m": round(fh, 2),
            "base_elevation_m": round(elevation, 2),
            "floor_area_sq_m": round(usable, 2),
            "floor_type": ftype,
            "units_on_floor": n_units,
        })
        elevation += fh

    total_units = sum(f["units_on_floor"] for f in floors)
    return {
        "total_floors": len(floors),
        "total_units": total_units,
        "structural_height_m": round(elevation, 2),
        "floors": floors,
        "units_per_floor_typical": max((f["units_on_floor"] for f in floors), default=0),
    }


def generate_accurate_3d_model(
    geometry: dict,
    base_ulpin: str,
    height_m: Optional[float] = None,
    building_type: str = "residential",
    explicit_levels: Optional[int] = None,
    unit_area_override: Optional[float] = None,
    include_unit_geometry: bool = True,
    max_unit_geometry: int = 2000,
) -> dict[str, Any]:
    """Full building -> floors -> units model with 3D geometry per floor."""
    area = geo.area_sq_m(geometry)
    if area <= 0:
        raise ValueError("footprint area is zero; check the geometry")

    prof = profile_for(building_type)
    if height_m is None or height_m <= 0:
        height_m = _height_for_levels(int(explicit_levels or prof["levels"]), prof)

    breakdown = calculate_floors_and_units(
        height_m=height_m,
        footprint_area_sq_m=area,
        building_type=building_type,
        explicit_levels=explicit_levels,
        unit_area_override=unit_area_override,
    )

    lat, lon = geo.centroid_latlon(geometry)
    floors_out: list[dict[str, Any]] = []
    units_out: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []

    # Subdividing the footprint is the expensive part, so do it once and reuse
    # the same cell layout on every floor that has the same unit count.
    subdivision_cache: dict[int, list[dict]] = {}
    emitted_geometry = 0

    for f in breakdown["floors"]:
        fno = f["floor_number"]
        f_ulpin = floor_ulpin(base_ulpin, fno)
        floors_out.append({**f, "floor_ulpin": f_ulpin, "parent_ulpin": base_ulpin})

        top = f["base_elevation_m"] + f["floor_height_m"]
        features.append({
            "type": "Feature",
            "geometry": geo.extrude_to_3d(geometry, f["base_elevation_m"], top),
            "properties": {
                "floor": fno,
                "floor_ulpin": f_ulpin,
                "base_elevation_m": f["base_elevation_m"],
                "top_elevation_m": round(top, 2),
                "floor_type": f["floor_type"],
            },
        })

        n_units = f["units_on_floor"]
        if n_units <= 0:
            continue

        cells: list[dict] = []
        if include_unit_geometry and emitted_geometry < max_unit_geometry:
            if n_units not in subdivision_cache:
                subdivision_cache[n_units] = geo.subdivide_polygon(geometry, n_units)
            cells = subdivision_cache[n_units]

        for u in range(1, n_units + 1):
            rec: dict[str, Any] = {
                "unit_ulpin": unit_ulpin(base_ulpin, fno, u),
                "parent_ulpin": base_ulpin,
                "floor_number": fno,
                "unit_number": u,
                "area_sq_m": round(f["floor_area_sq_m"] / n_units, 2),
            }
            if cells and emitted_geometry < max_unit_geometry:
                rec["geometry"] = cells[(u - 1) % len(cells)]
                emitted_geometry += 1
            units_out.append(rec)

    return {
        "building": {
            "ulpin": base_ulpin,
            "total_height_m": round(float(height_m), 2),
            "structural_height_m": breakdown["structural_height_m"],
            "footprint_area_sq_m": round(area, 2),
            "estimated_floors": breakdown["total_floors"],
            "total_units": breakdown["total_units"],
            "building_type": building_type,
            "centroid_lat": round(lat, 7),
            "centroid_lon": round(lon, 7),
            "levels_source": "explicit" if explicit_levels else "estimated",
        },
        "floors": floors_out,
        "units": units_out,
        "geometry_3d": {"type": "FeatureCollection", "features": features},
    }
