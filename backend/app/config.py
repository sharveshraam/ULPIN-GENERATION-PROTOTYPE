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
        self.version: str = "1.1.0"
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
        # Bound the client table so a long-lived process cannot grow it without
        # limit: one deque per distinct IP, forever, is a slow memory leak.
        self.rate_limit_max_clients: int = _env_int("RATE_LIMIT_MAX_CLIENTS", 4096)

        # Storing every unit of a 163-floor tower is a lot of rows; cap what we persist.
        self.persist_units_limit: int = _env_int("PERSIST_UNITS_LIMIT", 4000)

        # --- Database -----------------------------------------------------
        # Sized to cover Starlette's worker threadpool (40 by default) so a
        # request never has to wait for a connection, while pool_timeout keeps
        # a pathological pile-up from hanging indefinitely.
        self.db_pool_size: int = _env_int("DB_POOL_SIZE", 10)
        self.db_max_overflow: int = _env_int("DB_MAX_OVERFLOW", 20)
        self.db_pool_timeout_s: int = _env_int("DB_POOL_TIMEOUT_S", 10)

        # SQLite tuning. WAL lets readers run during a bulk write, which is
        # what keeps the health probe answering mid-scan.
        self.sqlite_busy_timeout_s: float = _env_float("SQLITE_BUSY_TIMEOUT_S", 5.0)
        self.sqlite_synchronous: str = _env("SQLITE_SYNCHRONOUS", "NORMAL")
        self.sqlite_cache_size_kib: int = _env_int("SQLITE_CACHE_SIZE_KIB", -2000)
        self.sqlite_mmap_mib: int = _env_int("SQLITE_MMAP_MIB", 64)

        # --- Caching ------------------------------------------------------
        # Every one of these trades a little staleness for CPU that Render's
        # free tier does not have. Set a TTL to 0 to disable that cache.
        self.cache_osm_s: int = _env_int("CACHE_OSM_S", 900)
        self.cache_geocode_s: int = _env_int("CACHE_GEOCODE_S", 86400)
        self.cache_health_s: float = _env_float("CACHE_HEALTH_S", 15.0)
        self.cache_max_entries: int = _env_int("CACHE_MAX_ENTRIES", 256)
        # Snap a coordinate to this many decimals before using it as a cache
        # key. 3 dp is ~110 m, well inside one administrative area, so two
        # nearby buildings share a reverse-geocode instead of each paying for
        # one (and for Nominatim's 1 req/s politeness limit).
        self.geocode_key_precision: int = _env_int("GEOCODE_KEY_PRECISION", 3)

        # --- Response encoding --------------------------------------------
        # gzip costs CPU, so it is only worth it above a threshold; below it
        # the compression burns more of Render's 0.1 CPU than it saves.
        #
        # Level 4 is the knee of the curve on this API's real payloads. For a
        # 352 KB bulk response: level 1 = 1.7 ms -> 71 KB, level 4 = 3.2 ms ->
        # 66 KB, level 6 = 5.7 ms -> 64 KB, level 9 = 13.4 ms -> 64 KB. Level
        # 9 buys 3% over level 4 for 4x the CPU, which is the wrong trade when
        # CPU is the scarce resource - and the bytes it saves still cost the
        # browser far less time to download than the level burns to produce.
        self.gzip_enabled: bool = _env("GZIP_ENABLED", "1") not in ("0", "false", "False", "")
        self.gzip_minimum_size: int = _env_int("GZIP_MINIMUM_SIZE", 1024)
        self.gzip_level: int = _env_int("GZIP_LEVEL", 4)

        # --- Static frontend ----------------------------------------------
        # The whole UI is ~200 KB across a dozen files. Holding it in memory
        # means serving a page view costs no disk I/O and no stat() calls.
        self.static_cache_enabled: bool = _env("STATIC_CACHE", "1") not in ("0", "false", "False", "")
        self.static_max_file_bytes: int = _env_int("STATIC_MAX_FILE_BYTES", 4 * 1024 * 1024)
        self.static_html_max_age: int = _env_int("STATIC_HTML_MAX_AGE", 60)
        self.static_asset_max_age: int = _env_int("STATIC_ASSET_MAX_AGE", 86400)

        self.log_level: str = _env("LOG_LEVEL", "INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
