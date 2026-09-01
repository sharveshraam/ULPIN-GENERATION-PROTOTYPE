"""SQLAlchemy models: Building -> Floor -> Unit."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.event import listens_for
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import get_settings

settings = get_settings()

_connect_args = {}
_engine_kwargs: dict = {"pool_pre_ping": True}

if settings.database_url.startswith("sqlite"):
    # SQLite + FastAPI's threadpool need this; pooling args below are Postgres-only.
    _connect_args = {"check_same_thread": False}
else:
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)

engine = create_engine(settings.database_url, connect_args=_connect_args, **_engine_kwargs)


@listens_for(Engine, "connect")
def _enable_sqlite_fks(dbapi_connection, _connection_record):
    """SQLite ignores ON DELETE CASCADE unless foreign keys are switched on
    per connection, which would leave orphaned floor/unit rows behind."""
    if settings.database_url.startswith("sqlite"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ParcelModel(Base):
    """A building / land parcel identified by a 14-digit ULPIN."""

    __tablename__ = "parcels"

    id = Column(Integer, primary_key=True, index=True)
    ulpin = Column(String(14), unique=True, index=True, nullable=False)

    name = Column(String(255), default="Unnamed Building")
    building_type = Column(String(64), default="residential", index=True)

    state_code = Column(String(2), nullable=False)
    district_code = Column(String(2), nullable=False)
    sub_district_code = Column(String(3), nullable=False)
    village_code = Column(String(3), nullable=False)
    plot_number = Column(Integer, nullable=False)

    centroid_lat = Column(Float, index=True)
    centroid_lon = Column(Float, index=True)
    area_sq_m = Column(Float, default=0.0)
    height_m = Column(Float, default=10.5)

    total_floors = Column(Integer, default=0)
    total_units = Column(Integer, default=0)

    osm_id = Column(Integer, index=True, nullable=True)
    height_source = Column(String(128), default="default")

    geometry_json = Column(JSON)
    properties_json = Column(JSON)

    created_at = Column(DateTime, default=_utcnow)

    floors = relationship(
        "FloorModel", back_populates="parcel",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    units = relationship(
        "UnitModel", back_populates="parcel",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    __table_args__ = (
        # Plot numbers auto-increment per village, so they must be unique there.
        UniqueConstraint(
            "state_code", "district_code", "sub_district_code", "village_code",
            "plot_number", name="uq_parcel_admin_plot",
        ),
        Index("ix_parcel_location", "centroid_lat", "centroid_lon"),
    )


class FloorModel(Base):
    """One storey. floor_ulpin = 14-digit parent + 3-digit floor = 17 digits."""

    __tablename__ = "floors"

    id = Column(Integer, primary_key=True, index=True)
    parcel_id = Column(Integer, ForeignKey("parcels.id", ondelete="CASCADE"), index=True)
    parent_ulpin = Column(String(14), index=True, nullable=False)
    floor_ulpin = Column(String(17), unique=True, index=True, nullable=False)

    floor_number = Column(Integer, nullable=False)
    floor_height_m = Column(Float, nullable=False)
    base_elevation_m = Column(Float, nullable=False)
    floor_area_sq_m = Column(Float, nullable=False)
    floor_type = Column(String(32), default="typical")  # ground | typical | mechanical
    units_on_floor = Column(Integer, default=0)

    parcel = relationship("ParcelModel", back_populates="floors")
    unit_rows = relationship(
        "UnitModel", back_populates="floor",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("parent_ulpin", "floor_number", name="uq_floor_number"),
    )


class UnitModel(Base):
    """A single apartment/office. unit_ulpin = 17-digit floor + 3-digit unit = 20 digits."""

    __tablename__ = "units"

    id = Column(Integer, primary_key=True, index=True)
    parcel_id = Column(Integer, ForeignKey("parcels.id", ondelete="CASCADE"), index=True)
    floor_id = Column(Integer, ForeignKey("floors.id", ondelete="CASCADE"), index=True)

    parent_ulpin = Column(String(14), index=True, nullable=False)
    unit_ulpin = Column(String(20), unique=True, index=True, nullable=False)

    floor_number = Column(Integer, nullable=False, index=True)
    unit_number = Column(Integer, nullable=False)
    area_sq_m = Column(Float, default=0.0)

    owner_name = Column(String(255), nullable=True)
    ownership_type = Column(String(64), default="unregistered")

    geometry_json = Column(JSON, nullable=True)

    parcel = relationship("ParcelModel", back_populates="units")
    floor = relationship("FloorModel", back_populates="unit_rows")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency yielding a session that always closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
