from sqlalchemy import create_engine, Column, String, Float, Integer, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite:///./ulpin_database.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class ParcelModel(Base):
    __tablename__ = "parcels"

    id = Column(Integer, primary_key=True, index=True)
    ulpin = Column(String(14), unique=True, index=True)
    centroid_lat = Column(Float)
    centroid_lon = Column(Float)
    area_sq_m = Column(Float)
    height_m = Column(Float, default=12.5)
    geometry_json = Column(JSON)
    properties_json = Column(JSON)


def init_db():
    Base.metadata.create_all(bind=engine)
