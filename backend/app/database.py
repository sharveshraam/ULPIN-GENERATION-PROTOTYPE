"""SQLAlchemy models: Building -> Floor -> Unit.

Connection handling is tuned for Render's free tier (~0.1 CPU, 512 MB RAM,
ephemeral disk) where SQLite is the default store:

* **WAL** so readers never queue behind a writer. With the default rollback
  journal, one bulk insert locks out every concurrent read - including the
  health probe Render uses to decide whether to restart the service.
* **synchronous=NORMAL** + a real **busy_timeout**: SQLite's default is to
  fsync on every commit and to fail immediately with "database is locked".
  On a slow container disk that is both a CPU sink and a source of spurious
  500s during a bulk scan.
* **No pool_pre_ping** for SQLite. It issues a ``SELECT 1`` on every single
  checkout to detect a stale TCP connection - a problem SQLite, an embedded
  library, does not have.
* A pool sized to cover the worker threadpool, with a finite **pool_timeout**,
  so a saturated pool raises a clean error instead of queueing forever.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import orjson
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
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

IS_SQLITE = settings.database_url.startswith("sqlite")

_connect_args: dict = {}
_engine_kwargs: dict = {}

if IS_SQLITE:
    # FastAPI hands sessions to worker threads, so the SQLite handle has to be
    # usable from more than the thread that opened it.
    _connect_args = {"check_same_thread": False, "timeout": settings.sqlite_busy_timeout_s}
    _engine_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        # pool_pre_ping costs a SELECT 1 per checkout and only exists to notice
        # dropped TCP connections. Embedded SQLite cannot drop one.
        pool_pre_ping=False,
    )
else:
    _engine_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_pre_ping=True,
        pool_recycle=1800,
    )

def _json_dumps(value) -> str:
    """orjson encoder for JSON columns, returning str as SQLAlchemy expects.

    ``orjson.dumps`` is ~16x faster than ``json.dumps`` and ``orjson.loads``
    ~10x faster than ``json.loads``, and every parcel row carries a geometry
    document that is encoded on write and decoded on read. Measured on 600
    real-shaped footprints: writes 16.1 -> 11.9 ms, reads 6.4 -> 3.6 ms, with
    a byte-identical round trip.

    orjson returns bytes, so it must be decoded: SQLite would otherwise store
    a BLOB in a TEXT-affinity column and change how the value comes back.
    """
    return orjson.dumps(value).decode("utf-8")


engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    json_serializer=_json_dumps,
    json_deserializer=orjson.loads,
    **_engine_kwargs,
)


if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        """Per-connection SQLite tuning.

        ``foreign_keys`` must be set on every connection: SQLite ignores
        ON DELETE CASCADE without it, which would leave orphaned floor and
        unit rows behind after a parcel delete.
        """
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA foreign_keys=ON")
            # A bulk insert must not lock readers out of the database.
            journal = None
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                row = cur.fetchone()
                journal = row[0] if row else None
            except Exception as exc:  # noqa: BLE001 - filesystem without shm
                logger.warning("WAL unavailable (%s); using the default journal", exc)
            cur.execute(f"PRAGMA busy_timeout={int(settings.sqlite_busy_timeout_s * 1000)}")
            cur.execute(f"PRAGMA synchronous={settings.sqlite_synchronous}")
            cur.execute(f"PRAGMA cache_size={int(settings.sqlite_cache_size_kib)}")
            cur.execute("PRAGMA temp_store=MEMORY")
            if settings.sqlite_mmap_mib > 0:
                cur.execute(f"PRAGMA mmap_size={int(settings.sqlite_mmap_mib * 1024 * 1024)}")
            if journal and str(journal).lower() != "wal":
                logger.info("SQLite journal mode is %s", journal)
        finally:
            cur.close()

else:

    @event.listens_for(engine, "connect")
    def _noop_pragmas(dbapi_connection, _connection_record):
        """Placeholder so the event registry shape matches the SQLite branch."""


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ParcelModel(Base):
    """A building / land parcel identified by a 14-digit ULPIN."""

    __tablename__ = "parcels"

    id = Column(Integer, primary_key=True, index=True)
    # 50 chars accommodates both the 14-digit numeric ULPIN and the longer
    # hyphenated form (e.g. IND-TN-001-CHE-F03-U301).
    ulpin = Column(String(50), unique=True, index=True, nullable=False)

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
    parent_ulpin = Column(String(50), index=True, nullable=False)
    floor_ulpin = Column(String(50), unique=True, index=True, nullable=False)

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

    parent_ulpin = Column(String(50), index=True, nullable=False)
    unit_ulpin = Column(String(50), unique=True, index=True, nullable=False)

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
