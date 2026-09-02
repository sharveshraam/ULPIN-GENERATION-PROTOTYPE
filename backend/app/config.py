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


def _normalise_origins(raw: str) -> list[str]:
    """Turn a comma-separated ALLOWED_ORIGINS value into real CORS origins.

    A browser's ``Origin`` header is only ever ``scheme://host[:port]`` - never
    a path. So the very natural-looking

        ALLOWED_ORIGINS=https://user.github.io/my-repo

    can never match anything, and a trailing slash breaks it the same way.
    Starlette compares these by exact string equality, so a mistake here fails
    silently: the request still returns 200, just without the
    ``Access-Control-Allow-Origin`` header, and the browser blocks the
    response. That is indistinguishable from "the backend is down" in the
    frontend, which makes it painful to diagnose.

    Rather than let that happen, reduce each entry to its origin. A GitHub
    Pages project URL with a repository subpath therefore still works.
    """
    origins: list[str] = []
    for chunk in raw.split(","):
        value = chunk.strip()
        if not value:
            continue
        if value == "*":
            origins.append(value)
            continue
        if "://" in value:
            scheme, _, rest = value.partition("://")
            host = rest.split("/", 1)[0]          # drop any path such as /my-repo
            value = f"{scheme}://{host}" if host else value
        else:
            value = value.rstrip("/")
        if value and value not in origins:
            origins.append(value)
    return origins


class Settings:
    """Runtime settings. Values come from the environment (see .env.example)."""

    def __init__(self) -> None:
        self.app_name: str = _env("APP_NAME", "ULPIN Generation API")
        self.version: str = "1.0.0"
        self.database_url: str = _env("DATABASE_URL", "sqlite:///./ulpin_database.db")

        # Allowed browser origins. ALLOWED_ORIGINS is the canonical name;
        # CORS_ORIGINS is still honoured for backwards compatibility.
        # Default "*" keeps GitHub Pages, file:// and local dev working.
        raw_origins = _env("ALLOWED_ORIGINS", _env("CORS_ORIGINS", "*"))
        self.cors_origins: list[str] = _normalise_origins(raw_origins)

        # A wildcard origin and credentialed requests are mutually exclusive in
        # the CORS spec: browsers reject "Access-Control-Allow-Origin: *" when
        # credentials are included. Only enable credentials for explicit origins.
        self.allow_credentials: bool = "*" not in self.cors_origins

        # Convenience origins for local frontend development.
        self.dev_origins: list[str] = [
            o.strip() for o in _env(
                "DEV_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000,"
                "http://localhost:5500,http://127.0.0.1:5500,"
                "http://localhost:8080,http://127.0.0.1:8080",
            ).split(",") if o.strip()
        ]
        # When specific origins are configured, also permit the local dev ones
        # so a developer's browser is not blocked against a deployed backend.
        if "*" not in self.cors_origins:
            for origin in self.dev_origins:
                if origin not in self.cors_origins:
                    self.cors_origins.append(origin)

        # Render provides $PORT; bind 0.0.0.0 so the service is reachable.
        self.host: str = _env("HOST", "0.0.0.0")
        self.port: int = _env_int("PORT", 8000)

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
