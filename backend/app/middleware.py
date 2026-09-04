"""Pure-ASGI middleware: CORS fallback and rate limiting.

Why not ``@app.middleware("http")``
-----------------------------------
That decorator wraps the function in ``BaseHTTPMiddleware``, which per request
spawns an anyio task group, allocates two memory streams and re-encodes the
response body through them. On a service that gets ~0.1 CPU that is a
measurable tax on *every* request, including static asset hits, and it is paid
twice when two such middlewares are stacked. Both of these need nothing but
the response headers, so they are written directly against the ASGI
``send`` channel: no task group, no streams, no body copying.

The behaviours they implement are unchanged - see ``backend/tests``.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from typing import Optional

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# The live limiter, published by __init__ so /api/v1/stats can report on the
# instance Starlette actually built rather than a lookalike.
_live_limiter: Optional["RateLimitMiddleware"] = None


def rate_limit_stats() -> dict:
    if _live_limiter is None:
        return {"enabled": False}
    return _live_limiter.snapshot()


# --------------------------------------------------------------------------- #
# CORS fallback
# --------------------------------------------------------------------------- #
class CorsFallbackMiddleware:
    """Guarantee CORS headers even when the configured allowlist rejects an origin.

    The frontend and API can be deployed on different hosts (GitHub Pages ->
    Render). If ``ALLOWED_ORIGINS`` on the deployed service is missing,
    malformed (e.g. carrying a repository subpath or a trailing slash) or
    simply does not match the browser's Origin, Starlette's CORSMiddleware
    still returns the payload but WITHOUT ``Access-Control-Allow-Origin``. The
    browser then raises a TypeError and the frontend reports "API blocked" -
    and no amount of incognito testing helps, because the extension/cookie
    theory is wrong; it is simply CORS.

    This is a public, credential-free demo API, so the permissive answer is
    safe: any origin may call it. It is installed OUTSIDE ``CORSMiddleware``,
    so when that middleware already allowed the origin its headers are left
    untouched and only CORS-less responses get patched.

    Two details matter:

    * ``CORSMiddleware`` rejects a disallowed PREFLIGHT with HTTP 400, and a
      browser aborts before reading the body, so that becomes a 200 (with the
      body dropped, since a preflight response must not carry one).
    * ``CORSMiddleware`` sets ``Access-Control-Allow-Credentials: true``
      whenever an explicit (non-wildcard) allowlist is configured, even on a
      rejected request. ``Access-Control-Allow-Origin: *`` alongside
      credentials is invalid, so the request origin is echoed instead and
      ``Vary: Origin`` is added. The ``null`` origin (file://, sandboxed
      iframes) gets ``*`` with the credentials header removed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_headers = scope.get("headers") or ()
        origin: Optional[str] = None
        wants_preflight = False
        for key, value in raw_headers:
            if key == b"origin":
                origin = value.decode("latin-1")
            elif key == b"access-control-request-method":
                wants_preflight = True
        if not origin:
            # Server-side probes (curl, Render's health check) need no CORS header.
            await self.app(scope, receive, send)
            return

        is_preflight = scope.get("method") == "OPTIONS" and wants_preflight
        drop_body = False

        async def send_with_cors(message: Message) -> None:
            nonlocal drop_body
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "access-control-allow-origin" not in headers:
                    if is_preflight and message["status"] in (400, 403):
                        message["status"] = 200
                        drop_body = True
                    if origin == "null":
                        headers["Access-Control-Allow-Origin"] = "*"
                        if "access-control-allow-credentials" in headers:
                            del headers["access-control-allow-credentials"]
                    else:
                        headers["Access-Control-Allow-Origin"] = origin
                        # add_vary_header appends, so an inner middleware's
                        # "Vary: Accept-Encoding" survives. Plain assignment
                        # would clobber it and let a cache serve gzipped bytes
                        # to a client that never advertised gzip.
                        headers.add_vary_header("Origin")
                    headers["Access-Control-Allow-Methods"] = "*"
                    headers["Access-Control-Allow-Headers"] = "*"
            elif message["type"] == "http.response.body" and drop_body:
                message = {"type": "http.response.body", "body": b"",
                           "more_body": message.get("more_body", False)}
            await send(message)

        await self.app(scope, receive, send_with_cors)


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
class RateLimitMiddleware:
    """Sliding-window per-client limit, in process.

    Sufficient for a single-instance free-tier deployment, and cheap: a dict
    lookup plus a few deque pops. The client table is bounded, because one
    deque per distinct IP kept forever is a slow memory leak on a service that
    runs for days.
    """

    def __init__(self, app: ASGIApp, *, requests: int, window_s: int,
                 exempt: frozenset[str] = frozenset(),
                 exempt_prefixes: tuple[str, ...] = (),
                 max_clients: int = 4096) -> None:
        self.app = app
        self.requests = max(1, int(requests))
        self.window_s = max(1, int(window_s))
        self.exempt = exempt
        self.exempt_prefixes = tuple(exempt_prefixes)
        self.max_clients = max(64, int(max_clients))
        # client -> timestamps of requests inside the window, oldest first.
        self._hits: "OrderedDict[str, deque[float]]" = OrderedDict()
        # Starlette constructs this class itself, so there is no handle to the
        # live instance from module scope. Publish it for the /stats endpoint.
        global _live_limiter
        _live_limiter = self

    def _is_exempt(self, path: str) -> bool:
        if path in self.exempt:
            return True
        return any(path.startswith(p) for p in self.exempt_prefixes)

    # __call__ is deliberately NOT async: returning the inner app's coroutine
    # (or the 429 coroutine) lets the caller await it directly, so a request
    # that is allowed never allocates a frame for this middleware at all.
    def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or self._is_exempt(scope.get("path", "")):
            return self.app(scope, receive, send)
        if self.requests <= 0:
            return self.app(scope, receive, send)

        client = scope.get("client")
        key = client[0] if client else "unknown"
        retry = self._register(key)
        if retry is None:
            return self.app(scope, receive, send)

        logger.warning("Rate limit hit by %s", key)
        body = (b'{"success": false, "message": "Rate limit exceeded. '
                b'Retry in ' + str(retry).encode() + b's."}')
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(retry).encode()),
        ]

        async def send_429() -> None:
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        return send_429()

    def _register(self, key: str) -> Optional[int]:
        """Record a hit. Returns the Retry-After in seconds, or None if allowed."""
        now = time.monotonic()
        cutoff = now - self.window_s
        bucket = self._hits.get(key)
        if bucket is None:
            bucket = deque()
            self._hits[key] = bucket
            self._prune(now)
        else:
            self._hits.move_to_end(key)
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self.requests:
            return int(self.window_s - (now - bucket[0])) + 1
        bucket.append(now)
        return None

    def _prune(self, now: float) -> None:
        """Drop clients whose window has fully elapsed, oldest first."""
        if len(self._hits) <= self.max_clients:
            return
        cutoff = now - self.window_s
        for key in list(self._hits.keys()):
            bucket = self._hits[key]
            if not bucket or bucket[-1] < cutoff:
                del self._hits[key]
            if len(self._hits) <= self.max_clients:
                break

    def snapshot(self) -> dict[str, int]:
        return {"tracked_clients": len(self._hits),
                "limit": self.requests, "window_s": self.window_s}
