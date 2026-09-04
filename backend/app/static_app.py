"""Serve the bundled UI from memory, pre-compressed, with real cache headers.

What this replaces
------------------
``StaticFiles(directory=..., html=True)`` is correct but pays for every
request at runtime: a ``stat()`` and an ``open()`` through the anyio
threadpool, a mimetypes lookup, and an ETag computed from the stat result -
per hit, for a set of about twelve files that never change while the process
lives. It also sends no ``Cache-Control``, so browsers revalidate every asset
on every navigation and the server answers ~10 conditional requests per page
view. On ~0.1 CPU that is the most expensive thing about serving a page that
is already in RAM.

So:

* file bytes are read once and held in memory (the whole UI is ~200 KB);
* the gzip variant is compressed once, at first request, and reused - so
  serving a compressed asset costs no CPU at all, unlike a gzip middleware
  which recompresses on every hit;
* ``Cache-Control`` lets js/css/font assets be reused for a day and HTML for a
  minute, which removes most repeat requests entirely;
* ``ETag`` + ``If-None-Match`` still answer 304 for anything that does come
  back, without touching the disk.

Gzip is only offered when it actually wins (compressed smaller than raw), and
``Vary: Accept-Encoding`` is set so a shared cache cannot hand gzipped bytes
to a client that never advertised gzip.
"""
from __future__ import annotations

import gzip
import logging
import mimetypes
import os
import threading
from typing import Optional

from starlette._utils import get_route_path
from starlette.datastructures import Headers
from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

_TEXT_TYPES = ("text/", "application/javascript", "application/json",
               "image/svg+xml", "application/xml")
_GZIP_LEVEL = 6


class _Entry:
    """One cached file: raw bytes, pre-compressed bytes, and its headers."""

    __slots__ = ("raw", "gzipped", "content_type", "etag", "cache_control")

    def __init__(self, raw: bytes, content_type: str, etag: str, cache_control: str):
        self.raw = raw
        self.content_type = content_type
        self.etag = etag
        self.cache_control = cache_control
        # Only worth offering gzip when it saves a meaningful amount.
        gz = gzip.compress(raw, _GZIP_LEVEL) if len(raw) >= 512 else None
        self.gzipped = gz if (gz is not None and len(gz) < len(raw) * 0.9) else None


class StaticFrontend:
    """ASGI app serving ``directory`` with in-memory caching."""

    def __init__(self, directory: str, *, html_max_age: int = 60,
                 asset_max_age: int = 86400,
                 max_file_bytes: int = 4 * 1024 * 1024) -> None:
        self.directory = os.path.realpath(directory)
        self.html_max_age = html_max_age
        self.asset_max_age = asset_max_age
        self.max_file_bytes = max_file_bytes
        self._cache: dict[str, Optional[_Entry]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    # -- lookup ------------------------------------------------------------ #
    def _resolve(self, rel_path: str) -> Optional[str]:
        """Map a URL path to a file inside the directory, or None.

        ``html=True`` semantics are preserved: a bare directory serves its
        ``index.html``, and a path with no extension falls back to
        ``<path>/index.html`` before 404ing.
        """
        rel = rel_path.lstrip("/")
        if not rel:
            rel = "index.html"
        candidate = os.path.realpath(os.path.join(self.directory, rel))
        # realpath collapses "..", so containment is the traversal guard.
        if candidate != self.directory and not candidate.startswith(self.directory + os.sep):
            return None
        if os.path.isfile(candidate):
            return candidate
        for alt in (os.path.join(candidate, "index.html"), candidate + ".html"):
            if os.path.isfile(alt):
                return alt
        return None

    def _load(self, rel_path: str) -> Optional[_Entry]:
        cached = self._cache.get(rel_path)
        if cached is not None or rel_path in self._cache:
            self.hits += 1
            return cached
        self.misses += 1
        path = self._resolve(rel_path)
        entry: Optional[_Entry] = None
        if path is not None:
            try:
                size = os.path.getsize(path)
                if size <= self.max_file_bytes:
                    with open(path, "rb") as fh:
                        raw = fh.read()
                    entry = self._make_entry(path, raw)
            except OSError as exc:
                logger.warning("Could not read static file %s: %s", path, exc)
        with self._lock:
            # Negative results are cached too: a missing asset is asked for
            # repeatedly by a broken page and should not re-stat every time.
            self._cache[rel_path] = entry
        return entry

    def _make_entry(self, path: str, raw: bytes) -> _Entry:
        content_type, _ = mimetypes.guess_type(path)
        if content_type is None:
            content_type = "application/octet-stream"
        if any(content_type.startswith(t) for t in _TEXT_TYPES):
            content_type += "; charset=utf-8"
        is_html = content_type.startswith("text/html")
        max_age = self.html_max_age if is_html else self.asset_max_age
        # HTML must revalidate often (it is the entry point and the only file
        # that changes meaning between deploys); hashed-by-content assets can
        # be kept for a day.
        cache_control = (
            f"public, max-age={max_age}"
            + (", must-revalidate" if is_html else "")
        )
        stat = os.stat(path)
        etag = f'W/"{stat.st_mtime_ns:x}-{len(raw):x}"'
        return _Entry(raw, content_type, etag, cache_control)

    # -- ASGI --------------------------------------------------------------- #
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        # Starlette >= 1.0 hands a mounted app the FULL request path plus a
        # root_path prefix to strip, rather than a pre-trimmed path.
        # get_route_path() is correct under both conventions.
        entry = self._load(get_route_path(scope))

        if entry is None:
            body = b'{"success": false, "message": "Not found"}'
            await send({"type": "http.response.start", "status": 404, "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ]})
            await send({"type": "http.response.body", "body": body})
            return

        headers = Headers(scope=scope)
        if headers.get("if-none-match") == entry.etag:
            await send({"type": "http.response.start", "status": 304, "headers": [
                (b"etag", entry.etag.encode()),
                (b"cache-control", entry.cache_control.encode()),
            ]})
            await send({"type": "http.response.body", "body": b""})
            return

        body = entry.raw
        raw_headers = [
            (b"content-type", entry.content_type.encode()),
            (b"etag", entry.etag.encode()),
            (b"cache-control", entry.cache_control.encode()),
            (b"accept-ranges", b"none"),
        ]
        if entry.gzipped is not None and "gzip" in headers.get("accept-encoding", ""):
            body = entry.gzipped
            raw_headers.append((b"content-encoding", b"gzip"))
            raw_headers.append((b"vary", b"Accept-Encoding"))
        raw_headers.append((b"content-length", str(len(body)).encode()))

        await send({"type": "http.response.start", "status": 200, "headers": raw_headers})
        await send({"type": "http.response.body", "body": body})

    def stats(self) -> dict:
        with self._lock:
            present = sum(1 for v in self._cache.values() if v is not None)
            raw = sum(len(v.raw) for v in self._cache.values() if v is not None)
            gz = sum(len(v.gzipped) for v in self._cache.values()
                     if v is not None and v.gzipped)
            return {"files": present, "raw_bytes": raw, "gzipped_bytes": gz,
                    "hits": self.hits, "misses": self.misses}
