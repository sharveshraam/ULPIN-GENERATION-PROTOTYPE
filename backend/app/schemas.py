"""Pydantic request/response models."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- #
# Shared
# --------------------------------------------------------------------------- #
class Geometry(BaseModel):
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list

    @field_validator("coordinates")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("coordinates must not be empty")
        return v


class APIResponse(BaseModel):
    success: bool = True
    data: Any = None
    message: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    parcels: int


# --------------------------------------------------------------------------- #
# ULPIN
# --------------------------------------------------------------------------- #
class ULPINRequest(BaseModel):
    state_code: str = Field("09", max_length=2)
    district_code: str = Field("12", max_length=2)
    sub_district_code: str = Field("105", max_length=3)
    village_code: str = Field("055", max_length=3)
    plot_number: int = Field(1, ge=0, le=9999)


class CustomULPINRequest(BaseModel):
    """Hyphenated format: {Country}-{State}-{District}-{City}-{Plot}-{Unit}."""

    country: str = Field("IND", description="3 uppercase letters, e.g. IND")
    state_code: str = Field("TN", description="2 uppercase letters, e.g. TN")
    district_code: str = Field("001", description="3 digits, e.g. 001")
    city_code: str = Field("CHE", description="3 uppercase letters, e.g. CHE")
    plot_code: str = Field("F03", description="Letter + 2 digits, e.g. F03")
    unit_code: str = Field("U301", description="'U' + 3 digits, e.g. U301")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "country": "IND", "state_code": "TN", "district_code": "001",
                "city_code": "CHE", "plot_code": "F03", "unit_code": "U301",
            }
        }
    )

    @field_validator("country", "state_code", "city_code", "plot_code", "unit_code")
    @classmethod
    def _upper(cls, v: str) -> str:
        # Accept lowercase input rather than rejecting it outright.
        return str(v).strip().upper()

    @field_validator("district_code")
    @classmethod
    def _strip(cls, v: str) -> str:
        return str(v).strip()


class ULPINFromCoordsRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


# --------------------------------------------------------------------------- #
# Parcels
# --------------------------------------------------------------------------- #
class ParcelCreate(BaseModel):
    geometry: Geometry
    name: Optional[str] = "Unnamed Building"
    building_type: str = "residential"
    height_m: Optional[float] = Field(None, gt=0, le=1200)
    levels: Optional[int] = Field(None, gt=0, le=250)

    # Optional manual admin codes; omitted values are reverse-geocoded.
    state_code: Optional[str] = Field(None, max_length=2)
    district_code: Optional[str] = Field(None, max_length=2)
    sub_district_code: Optional[str] = Field(None, max_length=3)
    village_code: Optional[str] = Field(None, max_length=3)

    osm_id: Optional[int] = None
    auto_detect_admin: bool = True
    generate_breakdown: bool = True


class ParcelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ulpin: str
    name: Optional[str]
    building_type: Optional[str]
    area_sq_m: Optional[float]
    height_m: Optional[float]
    total_floors: Optional[int]
    total_units: Optional[int]
    centroid_lat: Optional[float]
    centroid_lon: Optional[float]
    height_source: Optional[str]


class FloorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    floor_ulpin: str
    parent_ulpin: str
    floor_number: int
    floor_height_m: float
    base_elevation_m: float
    floor_area_sq_m: float
    floor_type: str
    units_on_floor: int


class UnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    unit_ulpin: str
    parent_ulpin: str
    floor_number: int
    unit_number: int
    area_sq_m: float
    owner_name: Optional[str] = None
    ownership_type: Optional[str] = None


# --------------------------------------------------------------------------- #
# 3D model
# --------------------------------------------------------------------------- #
class Model3DRequest(BaseModel):
    """Either reference a stored ULPIN, or pass geometry directly."""

    ulpin: Optional[str] = None
    geometry: Optional[Geometry] = None
    height_m: Optional[float] = Field(None, gt=0, le=1200)
    levels: Optional[int] = Field(None, gt=0, le=250)
    building_type: str = "residential"
    unit_area_sq_m: Optional[float] = Field(None, gt=10, le=2000)
    include_unit_geometry: bool = True

    @field_validator("ulpin")
    @classmethod
    def _valid_ulpin(cls, v):
        if v is not None and (not v.isdigit() or len(v) != 14):
            raise ValueError("ulpin must be exactly 14 digits")
        return v


# --------------------------------------------------------------------------- #
# Bulk generation
# --------------------------------------------------------------------------- #
class BulkGenerateRequest(BaseModel):
    center_lat: float = Field(..., ge=-90, le=90)
    center_lon: float = Field(..., ge=-180, le=180)
    radius_km: float = Field(1.0, gt=0, le=5)
    building_type_default: str = "residential"
    persist: bool = True
    generate_breakdown: bool = False   # full floor/unit rows for every building is heavy


class BBoxGenerateRequest(BaseModel):
    south: float = Field(..., ge=-90, le=90)
    west: float = Field(..., ge=-180, le=180)
    north: float = Field(..., ge=-90, le=90)
    east: float = Field(..., ge=-180, le=180)
    persist: bool = True
    generate_breakdown: bool = False

    @field_validator("north")
    @classmethod
    def _lat_order(cls, v, info):
        south = info.data.get("south")
        if south is not None and v <= south:
            raise ValueError("north must be greater than south")
        return v

    @field_validator("east")
    @classmethod
    def _lon_order(cls, v, info):
        west = info.data.get("west")
        if west is not None and v <= west:
            raise ValueError("east must be greater than west")
        return v
