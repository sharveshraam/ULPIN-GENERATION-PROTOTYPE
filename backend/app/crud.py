"""Database operations.

Bulk paths are written to issue a *constant* number of queries per request
rather than one per building. A 600-building scan used to run ~1,200 SELECTs
(an ``osm_id`` existence check and a ``MAX(plot_number)`` per building) plus
600 individually flushed INSERTs; :func:`create_parcels_bulk` does the same
work with a handful of statements and executemany inserts. On ~0.1 CPU the
difference is seconds, not milliseconds.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, insert, or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import FloorModel, ParcelModel, UnitModel
from .services import geometry_processor as geo
from .services import model_3d_generator as m3d
from .services import ulpin_generator as ug

logger = logging.getLogger(__name__)
settings = get_settings()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_parcel(db: Session, ulpin: str) -> Optional[ParcelModel]:
    return db.query(ParcelModel).filter(ParcelModel.ulpin == ulpin).first()


def get_parcel_by_osm(db: Session, osm_id: int) -> Optional[ParcelModel]:
    return db.query(ParcelModel).filter(ParcelModel.osm_id == osm_id).first()


def get_parcels_by_osm(db: Session, osm_ids: Iterable[int]) -> dict[int, ParcelModel]:
    """Every stored parcel for a batch of OSM ids, in ONE query.

    Chunked because a 5 km scan can return more ids than SQLite is happy to
    bind in a single ``IN (...)`` (its default limit is 999 host parameters).
    """
    ids = sorted({int(i) for i in osm_ids if i is not None})
    if not ids:
        return {}
    out: dict[int, ParcelModel] = {}
    step = 500
    for start in range(0, len(ids), step):
        chunk = ids[start:start + step]
        rows = db.execute(
            select(ParcelModel).where(ParcelModel.osm_id.in_(chunk))
        ).scalars().all()
        for row in rows:
            if row.osm_id is not None:
                out.setdefault(int(row.osm_id), row)
    return out


def list_parcels(db: Session, limit: int = 1000, offset: int = 0) -> list[ParcelModel]:
    return (
        db.query(ParcelModel)
        .order_by(ParcelModel.id)
        .offset(max(0, offset))
        .limit(max(1, min(limit, settings.max_buildings_per_request)))
        .all()
    )


def count_parcels(db: Session) -> int:
    return db.query(ParcelModel).count()


def max_plot_number(db: Session, state_code: str, district_code: str,
                    sub_district_code: str, village_code: str) -> int:
    """Highest plot number already used in a village, or 0."""
    current_max = db.execute(
        select(func.max(ParcelModel.plot_number)).where(
            ParcelModel.state_code == state_code,
            ParcelModel.district_code == district_code,
            ParcelModel.sub_district_code == sub_district_code,
            ParcelModel.village_code == village_code,
        )
    ).scalar()
    return int(current_max or 0)


def get_floors(db: Session, ulpin: str) -> list[FloorModel]:
    return (
        db.query(FloorModel)
        .filter(FloorModel.parent_ulpin == ulpin)
        .order_by(FloorModel.floor_number)
        .all()
    )


def get_units(
    db: Session, ulpin: str, floor: Optional[int] = None,
    limit: int = 500, offset: int = 0,
) -> tuple[list[UnitModel], int]:
    """Paginated units; returns (rows, total_count)."""
    q = db.query(UnitModel).filter(UnitModel.parent_ulpin == ulpin)
    if floor is not None:
        q = q.filter(UnitModel.floor_number == floor)
    total = q.count()
    rows = (
        q.order_by(UnitModel.floor_number, UnitModel.unit_number)
        .offset(max(0, offset)).limit(max(1, min(limit, 5000))).all()
    )
    return rows, total


def search_parcels(db: Session, q: str, limit: int = 25) -> list[ParcelModel]:
    like = f"%{q}%"
    return (
        db.query(ParcelModel)
        .filter(or_(ParcelModel.ulpin.like(like), ParcelModel.name.ilike(like)))
        .limit(limit).all()
    )


def parcels_near(db: Session, lat: float, lon: float, radius_km: float, limit: int = 200):
    """Bounding-box proximity search (indexed; good enough for a prototype).

    The radius is in KILOMETRES while ``geo.M_PER_DEG_LAT`` and
    ``geo.m_per_deg_lon`` are METRES per degree, so the radius has to be
    scaled by 1000 first. Getting that wrong shrinks the box a thousandfold
    and the search silently returns nothing - which is why
    ``test_parcels_near_uses_a_metre_per_degree_box`` pins the box size.
    """
    radius_m = radius_km * 1000.0
    dlat = radius_m / geo.M_PER_DEG_LAT
    dlon = radius_m / max(1e-6, abs(geo.m_per_deg_lon(lat)))
    return (
        db.query(ParcelModel)
        .filter(
            ParcelModel.centroid_lat.between(lat - dlat, lat + dlat),
            ParcelModel.centroid_lon.between(lon - dlon, lon + dlon),
        )
        .limit(limit).all()
    )


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def parcel_to_feature(p: ParcelModel) -> dict[str, Any]:
    props: dict[str, Any] = {
        "ulpin": p.ulpin,
        "name": p.name,
        "building_type": p.building_type,
        "area_sq_m": p.area_sq_m,
        "height_m": p.height_m,
        "total_floors": p.total_floors,
        "total_units": p.total_units,
        "centroid_lat": p.centroid_lat,
        "centroid_lon": p.centroid_lon,
        "height_source": p.height_source,
        "osm_id": p.osm_id,
    }
    if p.properties_json:
        props.update(p.properties_json)
    return {"type": "Feature", "geometry": p.geometry_json, "properties": props}


def parcels_to_featurecollection(rows: Sequence[ParcelModel]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": [parcel_to_feature(p) for p in rows]}


# --------------------------------------------------------------------------- #
# Shared preparation
# --------------------------------------------------------------------------- #
def _admin_codes(admin: Optional[dict]) -> tuple[str, str, str, str]:
    admin = admin or {}
    return (
        str(admin.get("state_code") or "99").zfill(2)[:2],
        str(admin.get("district_code") or "00").zfill(2)[:2],
        str(admin.get("sub_district_code") or "000").zfill(3)[:3],
        str(admin.get("village_code") or "000").zfill(3)[:3],
    )


def _resolve_height(height_m: Optional[float], levels: Optional[int],
                    building_type: str) -> tuple[float, dict[str, float]]:
    prof = m3d.profile_for(building_type)
    if height_m is None or height_m <= 0:
        height_m = m3d._height_for_levels(int(levels or prof["levels"]), prof)
    return float(height_m), prof


# --------------------------------------------------------------------------- #
# Writes — single parcel
# --------------------------------------------------------------------------- #
def create_parcel(
    db: Session,
    geometry: dict,
    name: str = "Unnamed Building",
    building_type: str = "residential",
    height_m: Optional[float] = None,
    levels: Optional[int] = None,
    admin: Optional[dict] = None,
    osm_id: Optional[int] = None,
    height_source: str = "manual",
    generate_breakdown: bool = True,
    extra_properties: Optional[dict] = None,
    commit: bool = True,
) -> ParcelModel:
    """Create a building, allocate its ULPIN, and optionally persist floors/units.

    Idempotent on osm_id: re-scanning an area returns the existing row instead
    of minting a duplicate ULPIN.
    """
    if osm_id is not None:
        existing = get_parcel_by_osm(db, osm_id)
        if existing:
            return existing

    state, district, sub_district, village = _admin_codes(admin)

    plot = ug.next_plot_number(db, state, district, sub_district, village)
    ulpin = ug.generate_ulpin_code(state, district, sub_district, village, plot)

    # One parse for both measurements; this used to build the geometry twice.
    area, lat, lon = geo.measure(geometry)
    height_m, _prof = _resolve_height(height_m, levels, building_type)

    breakdown = m3d.calculate_floors_and_units(
        height_m=height_m,
        footprint_area_sq_m=area,
        building_type=building_type,
        explicit_levels=levels,
    )

    parcel = ParcelModel(
        ulpin=ulpin,
        name=name or "Unnamed Building",
        building_type=building_type,
        state_code=state, district_code=district,
        sub_district_code=sub_district, village_code=village,
        plot_number=plot,
        centroid_lat=round(lat, 7), centroid_lon=round(lon, 7),
        area_sq_m=round(area, 2), height_m=round(height_m, 2),
        total_floors=breakdown["total_floors"], total_units=breakdown["total_units"],
        osm_id=osm_id, height_source=height_source,
        geometry_json=geometry, properties_json=extra_properties or {},
    )
    db.add(parcel)
    db.flush()  # assigns parcel.id without ending the transaction

    if generate_breakdown:
        _persist_breakdown(db, parcel, geometry, breakdown)

    if commit:
        db.commit()
        db.refresh(parcel)
    return parcel


def _persist_breakdown(db: Session, parcel: ParcelModel, geometry: dict, breakdown: dict) -> None:
    """Write floor rows and (capped) unit rows."""
    floor_rows: list[FloorModel] = []
    for f in breakdown["floors"]:
        floor_rows.append(FloorModel(
            parcel_id=parcel.id,
            parent_ulpin=parcel.ulpin,
            floor_ulpin=ug.floor_ulpin(parcel.ulpin, f["floor_number"]),
            floor_number=f["floor_number"],
            floor_height_m=f["floor_height_m"],
            base_elevation_m=f["base_elevation_m"],
            floor_area_sq_m=f["floor_area_sq_m"],
            floor_type=f["floor_type"],
            units_on_floor=f["units_on_floor"],
        ))
    db.add_all(floor_rows)
    db.flush()

    # A 163-floor tower can imply >13k units; cap what we store.
    budget = settings.persist_units_limit
    unit_rows: list[UnitModel] = []
    for fr in floor_rows:
        if budget <= 0:
            break
        for u in range(1, fr.units_on_floor + 1):
            if budget <= 0:
                break
            unit_rows.append(UnitModel(
                parcel_id=parcel.id, floor_id=fr.id,
                parent_ulpin=parcel.ulpin,
                unit_ulpin=ug.unit_ulpin(parcel.ulpin, fr.floor_number, u),
                floor_number=fr.floor_number, unit_number=u,
                area_sq_m=round(fr.floor_area_sq_m / max(1, fr.units_on_floor), 2),
            ))
            budget -= 1
    if unit_rows:
        db.add_all(unit_rows)
    db.flush()


def delete_parcel(db: Session, ulpin: str) -> bool:
    p = get_parcel(db, ulpin)
    if not p:
        return False
    db.delete(p)   # cascades to floors + units
    db.commit()
    return True


# --------------------------------------------------------------------------- #
# Writes — bulk
# --------------------------------------------------------------------------- #
def create_parcels_bulk(
    db: Session,
    specs: Sequence[dict[str, Any]],
    admin: Optional[dict],
    generate_breakdown: bool = False,
    commit: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Create many parcels with a constant number of statements.

    ``specs`` entries carry ``geometry``, ``name``, ``building_type``,
    ``height_m``, ``levels``, ``height_source`` and ``osm_id``.

    Returns ``(features, skipped)``: one GeoJSON Feature per successfully
    stored or already-stored building, in input order, plus the number of
    specs that could not be used. That mirrors the per-building path, where a
    geometry error raised into the caller's ``except`` and incremented
    ``skipped``.

    Idempotency on ``osm_id`` is preserved - an already-stored building is
    returned from the registry rather than minting a second ULPIN - and so is
    the 9999-plot-per-village ceiling.
    """
    n = len(specs)
    if n == 0:
        return [], 0

    existing = get_parcels_by_osm(db, (s.get("osm_id") for s in specs))

    # Measure every footprint in ONE vectorised GEOS call instead of two
    # shapely round trips per building.
    measured = geo.measure_many([s["geometry"] for s in specs])

    features: list[Optional[dict[str, Any]]] = [None] * n
    skipped = 0

    # Already-stored buildings go straight back into their own slot.
    if existing:
        for i, spec in enumerate(specs):
            row = existing.get(int(spec["osm_id"])) if spec.get("osm_id") is not None else None
            if row is not None:
                features[i] = parcel_to_feature(row)

    # Unusable geometry is a skip, exactly as the per-building `except` was.
    for i in range(n):
        if features[i] is None and not measured.ok[i]:
            skipped += 1
    pending = [i for i in range(n) if features[i] is None and measured.ok[i]]

    if not pending:
        return [f for f in features if f is not None], skipped

    state, district, sub_district, village = _admin_codes(admin)
    plot = max_plot_number(db, state, district, sub_district, village)

    rows: list[dict[str, Any]] = []
    used: list[int] = []
    breakdowns: list[dict[str, Any]] = []

    for i in pending:
        if plot >= 9999:
            # Village numbering is exhausted; nothing further can be minted.
            skipped += 1
            continue
        spec = specs[i]
        btype = spec.get("building_type") or "residential"
        height_m, _prof = _resolve_height(spec.get("height_m"), spec.get("levels"), btype)
        area, lat, lon = measured[i]
        plot += 1
        try:
            ulpin = ug.generate_ulpin_code(state, district, sub_district, village, plot)
        except ValueError as exc:
            logger.warning("Skipping building %s: %s", spec.get("osm_id"), exc)
            skipped += 1
            plot -= 1
            continue

        breakdown = m3d.calculate_floors_and_units(
            height_m=height_m, footprint_area_sq_m=area,
            building_type=btype, explicit_levels=spec.get("levels"),
        )
        rows.append({
            "ulpin": ulpin,
            "name": spec.get("name") or "Unnamed Building",
            "building_type": btype,
            "state_code": state, "district_code": district,
            "sub_district_code": sub_district, "village_code": village,
            "plot_number": plot,
            "centroid_lat": round(lat, 7), "centroid_lon": round(lon, 7),
            "area_sq_m": round(area, 2), "height_m": round(height_m, 2),
            "total_floors": breakdown["total_floors"],
            "total_units": breakdown["total_units"],
            "osm_id": spec.get("osm_id"),
            "height_source": spec.get("height_source") or "default",
            "geometry_json": spec["geometry"],
            "properties_json": {},
        })
        used.append(i)
        breakdowns.append(breakdown)

    if rows:
        db.execute(insert(ParcelModel), rows)
        if generate_breakdown:
            _persist_breakdown_bulk(db, rows, breakdowns)

        # Build the response from the values just written - they are exactly
        # what is in the database, so the rows never need reading back.
        for i, row in zip(used, rows):
            features[i] = {
                "type": "Feature",
                "geometry": row["geometry_json"],
                "properties": {
                    "ulpin": row["ulpin"], "name": row["name"],
                    "building_type": row["building_type"],
                    "area_sq_m": row["area_sq_m"], "height_m": row["height_m"],
                    "total_floors": row["total_floors"],
                    "total_units": row["total_units"],
                    "centroid_lat": row["centroid_lat"],
                    "centroid_lon": row["centroid_lon"],
                    "height_source": row["height_source"],
                    "osm_id": row["osm_id"],
                },
            }

    if commit:
        db.commit()

    return [f for f in features if f is not None], skipped


def _persist_breakdown_bulk(db: Session, parcel_rows: list[dict[str, Any]],
                            breakdowns: list[dict[str, Any]]) -> None:
    """Floor and unit rows for a whole batch, via executemany inserts."""
    floor_rows: list[dict[str, Any]] = []
    for row, breakdown in zip(parcel_rows, breakdowns):
        ulpin = row["ulpin"]
        for f in breakdown["floors"]:
            floor_rows.append({
                "parent_ulpin": ulpin,
                "floor_ulpin": ug.floor_ulpin(ulpin, f["floor_number"]),
                "floor_number": f["floor_number"],
                "floor_height_m": f["floor_height_m"],
                "base_elevation_m": f["base_elevation_m"],
                "floor_area_sq_m": f["floor_area_sq_m"],
                "floor_type": f["floor_type"],
                "units_on_floor": f["units_on_floor"],
            })
    if not floor_rows:
        return
    db.execute(insert(FloorModel), floor_rows)
    db.flush()

    # Two lookups recover the surrogate keys the child rows need: parcels by
    # ULPIN, floors by (parent ULPIN, floor number). One query each, however
    # large the batch.
    ulpins = [r["ulpin"] for r in parcel_rows]
    parcel_ids = dict(db.execute(
        select(ParcelModel.ulpin, ParcelModel.id).where(ParcelModel.ulpin.in_(ulpins))
    ).all())
    floor_ids = {
        (parent, number): fid
        for parent, number, fid in db.execute(
            select(FloorModel.parent_ulpin, FloorModel.floor_number, FloorModel.id)
            .where(FloorModel.parent_ulpin.in_(ulpins))
        ).all()
    }

    budget = settings.persist_units_limit
    unit_rows: list[dict[str, Any]] = []
    for row, breakdown in zip(parcel_rows, breakdowns):
        if budget <= 0:
            break
        ulpin = row["ulpin"]
        parcel_id = parcel_ids.get(ulpin)
        for f in breakdown["floors"]:
            if budget <= 0:
                break
            floor_id = floor_ids.get((ulpin, f["floor_number"]))
            area = round(f["floor_area_sq_m"] / max(1, f["units_on_floor"]), 2)
            for u in range(1, f["units_on_floor"] + 1):
                if budget <= 0:
                    break
                unit_rows.append({
                    "parcel_id": parcel_id, "floor_id": floor_id,
                    "parent_ulpin": ulpin,
                    "unit_ulpin": ug.unit_ulpin(ulpin, f["floor_number"], u),
                    "floor_number": f["floor_number"], "unit_number": u,
                    "area_sq_m": area,
                    "ownership_type": "unregistered",
                })
                budget -= 1
    if unit_rows:
        db.execute(insert(UnitModel), unit_rows)
