"""Environment-driven configuration."""
from __future__ import annotations

import os
from functools import lru_cache


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


class Settings:
    """Runtime settings. Values come from the environment (see .env.example)."""

    def __init__(self) -> None:
        self.app_name: str = _env("APP_NAME", "ULPIN Generation API")
        self.version: str = "1.0.0"
        self.database_url: str = _env("DATABASE_URL", "sqlite:///./ulpin_database.db")

        # "*" (default) allows any origin, which keeps GitHub Pages + local files working.
        raw_origins = _env("CORS_ORIGINS", "*")
        self.cors_origins: list[str] = [o.strip() for o in raw_origins.split(",") if o.strip()]

        self.overpass_urls: list[str] = [
            u.strip()
            for u in _env(
                "OVERPASS_URLS",
                "https://overpass-api.de/api/interpreter,"
                "https://overpass.kumi.systems/api/interpreter,"
                "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            ).split(",")
            if u.strip()
        ]
        self.nominatim_url: str = _env(
            "NOMINATIM_URL", "https://nominatim.openstreetmap.org"
        )
        # OSM asks every client to identify itself.
        self.user_agent: str = _env("USER_AGENT", "ULPIN-Generator/1.0 (hackathon prototype)")

        self.http_timeout: float = _env_float("HTTP_TIMEOUT", 90.0)
        self.max_radius_km: float = _env_float("MAX_RADIUS_KM", 5.0)
        self.max_buildings_per_request: int = _env_int("MAX_BUILDINGS_PER_REQUEST", 5000)

        # Simple in-process rate limit.
        self.rate_limit_requests: int = _env_int("RATE_LIMIT_REQUESTS", 60)
        self.rate_limit_window_s: int = _env_int("RATE_LIMIT_WINDOW_S", 60)

        # Storing every unit of a 163-floor tower is a lot of rows; cap what we persist.
        self.persist_units_limit: int = _env_int("PERSIST_UNITS_LIMIT", 4000)

        self.log_level: str = _env("LOG_LEVEL", "INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
