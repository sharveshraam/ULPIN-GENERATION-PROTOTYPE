"""Database operations."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import or_
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
    """Bounding-box proximity search (indexed; good enough for a prototype)."""
    dlat = radius_km / 110.574
    dlon = radius_km / max(1e-6, 111.320 * abs(__import__("math").cos(__import__("math").radians(lat))))
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
    return {
        "type": "Feature",
        "geometry": p.geometry_json,
        "properties": {
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
            **(p.properties_json or {}),
        },
    }


def parcels_to_featurecollection(rows: list[ParcelModel]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": [parcel_to_feature(p) for p in rows]}


# --------------------------------------------------------------------------- #
# Writes
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

    admin = admin or {}
    state = str(admin.get("state_code") or "99").zfill(2)[:2]
    district = str(admin.get("district_code") or "00").zfill(2)[:2]
    sub_district = str(admin.get("sub_district_code") or "000").zfill(3)[:3]
    village = str(admin.get("village_code") or "000").zfill(3)[:3]

    plot = ug.next_plot_number(db, state, district, sub_district, village)
    ulpin = ug.generate_ulpin_code(state, district, sub_district, village, plot)

    area = geo.area_sq_m(geometry)
    lat, lon = geo.centroid_latlon(geometry)

    prof = m3d.profile_for(building_type)
    if height_m is None or height_m <= 0:
        height_m = m3d._height_for_levels(int(levels or prof["levels"]), prof)

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
        area_sq_m=round(area, 2), height_m=round(float(height_m), 2),
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
