"""ULPIN generation following the Indian Bhu-Aadhaar structure.

Base parcel ULPIN (14 digits):
    [State 2][District 2][Sub-District 3][Village 3][Plot 4]

Vertical extensions used by this system:
    Floor ULPIN (17) = base(14) + floor(3)
    Unit  ULPIN (20) = base(14) + floor(3) + unit(3)
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

ULPIN_RE = re.compile(r"^\d{14}$")

# Coarse lat/lon boxes -> LGD state codes. Used only when reverse geocoding
# cannot resolve a state; deliberately conservative.
_STATE_CODES = {
    "andhra pradesh": "28", "arunachal pradesh": "12", "assam": "18", "bihar": "10",
    "chhattisgarh": "22", "goa": "30", "gujarat": "24", "haryana": "06",
    "himachal pradesh": "02", "jharkhand": "20", "karnataka": "29", "kerala": "32",
    "madhya pradesh": "23", "maharashtra": "27", "manipur": "14", "meghalaya": "17",
    "mizoram": "15", "nagaland": "13", "odisha": "21", "punjab": "03",
    "rajasthan": "08", "sikkim": "11", "tamil nadu": "33", "telangana": "36",
    "tripura": "16", "uttar pradesh": "09", "uttarakhand": "05", "west bengal": "19",
    "delhi": "07", "national capital territory of delhi": "07",
    "jammu and kashmir": "01", "ladakh": "37", "puducherry": "34",
    "chandigarh": "04", "andaman and nicobar islands": "35",
    "dadra and nagar haveli and daman and diu": "26", "lakshadweep": "31",
}


def state_code_for(state_name: Optional[str]) -> str:
    """Map a state name to its LGD code, defaulting to 99 (unknown/outside India)."""
    if not state_name:
        return "99"
    return _STATE_CODES.get(state_name.strip().lower(), "99")


def _stable_code(value: str, digits: int) -> str:
    """Deterministic numeric code derived from a name.

    Real deployments read these from the LGD registry. We need a value that is
    stable for the same input so re-running a scan does not renumber everything.
    """
    if not value:
        return "0" * digits
    h = 0
    for ch in value.strip().lower():
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return str(h % (10 ** digits)).zfill(digits)


def district_code_for(name: Optional[str]) -> str:
    return _stable_code(name or "", 2)


def sub_district_code_for(name: Optional[str]) -> str:
    return _stable_code(name or "", 3)


def village_code_for(name: Optional[str]) -> str:
    return _stable_code(name or "", 3)


def generate_ulpin_code(
    state_code: str,
    district_code: str,
    sub_district_code: str,
    village_code: str,
    plot_number: int,
) -> str:
    """Assemble the 14-digit ULPIN. Raises ValueError if the plot number overflows."""
    if not 0 <= int(plot_number) <= 9999:
        raise ValueError("plot_number must be between 0 and 9999 (4 digits)")
    ulpin = (
        str(state_code).zfill(2)[:2]
        + str(district_code).zfill(2)[:2]
        + str(sub_district_code).zfill(3)[:3]
        + str(village_code).zfill(3)[:3]
        + str(int(plot_number)).zfill(4)
    )
    if not ULPIN_RE.match(ulpin):
        raise ValueError(f"generated ULPIN is malformed: {ulpin!r}")
    return ulpin


def floor_ulpin(base_ulpin: str, floor_number: int) -> str:
    """17-digit floor identifier."""
    return f"{base_ulpin}{int(floor_number):03d}"


def unit_ulpin(base_ulpin: str, floor_number: int, unit_number: int) -> str:
    """20-digit unit identifier."""
    return f"{base_ulpin}{int(floor_number):03d}{int(unit_number):03d}"


def parse_unit_ulpin(value: str) -> dict:
    """Split a 14/17/20-digit ULPIN back into its parts."""
    v = str(value).strip()
    if len(v) not in (14, 17, 20) or not v.isdigit():
        raise ValueError("ULPIN must be 14, 17 or 20 digits")
    out = {
        "base_ulpin": v[:14],
        "state_code": v[0:2],
        "district_code": v[2:4],
        "sub_district_code": v[4:7],
        "village_code": v[7:10],
        "plot_number": int(v[10:14]),
        "floor_number": None,
        "unit_number": None,
    }
    if len(v) >= 17:
        out["floor_number"] = int(v[14:17])
    if len(v) == 20:
        out["unit_number"] = int(v[17:20])
    return out


def next_plot_number(
    db: Session,
    state_code: str,
    district_code: str,
    sub_district_code: str,
    village_code: str,
) -> int:
    """Next free plot number within a village (auto-increment, 1-9999)."""
    from ..database import ParcelModel  # local import avoids a circular dependency

    current_max = (
        db.query(func.max(ParcelModel.plot_number))
        .filter(
            ParcelModel.state_code == state_code,
            ParcelModel.district_code == district_code,
            ParcelModel.sub_district_code == sub_district_code,
            ParcelModel.village_code == village_code,
        )
        .scalar()
    )
    nxt = int(current_max or 0) + 1
    if nxt > 9999:
        raise ValueError("plot numbers exhausted for this village (max 9999)")
    return nxt
