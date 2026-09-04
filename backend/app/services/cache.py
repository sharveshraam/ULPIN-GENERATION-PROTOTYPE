"""A small bounded TTL cache for external-API results and derived values.

Why this exists
---------------
Render's free tier gives the service ~0.1 CPU and suspends it when idle, so
repeating work is expensive twice over: it burns CPU the service does not
have, and every repeat call to Overpass or Nominatim also burns the politeness
budget those services allow a hackathon prototype.

The three things worth caching here all have the same shape - a JSON-ish value
keyed by a rounded coordinate or a query string - so they share one
implementation rather than three:

* Overpass footprint queries. Two visitors scanning Kochi within 15 minutes
  get identical building data; the second one skips a multi-second external
  round trip and the whole geometry pipeline that follows it.
* Nominatim reverse geocodes. Keyed at ~110 m precision, so every building in
  a scan shares one lookup instead of paying Nominatim's 1 req/s limit.
* The ``/health`` parcel count. ``COUNT(*)`` is a full table scan, and Render
  polls the health path constantly - it must never be the expensive thing.

Design notes
------------
* Bounded by BOTH entry count and total stored bytes, because a 512 MB
  instance must not be traded for a faster second request. An Overpass
  response for a dense 5 km radius can be tens of megabytes.
* A single lock guards the whole structure. Contention is a non-issue at this
  scale and it keeps eviction exact.
* Expired entries are dropped lazily on read plus opportunistically on write,
  so there is no background thread to keep alive (and none to leak on a
  service that gets suspended mid-request).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Hashable, Optional


class TTLCache:
    """Thread-safe LRU cache whose entries expire after ``ttl`` seconds."""

    def __init__(self, name: str, ttl: float, max_entries: int = 128,
                 max_bytes: int = 64 * 1024 * 1024) -> None:
        self.name = name
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self._data: "OrderedDict[Hashable, tuple[float, Any, int]]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.ttl > 0 and self.max_entries > 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def get(self, key: Hashable) -> Optional[Any]:
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value, size = entry
            if expires_at <= now:
                del self._data[key]
                self._bytes -= size
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: Hashable, value: Any, size: Optional[int] = None) -> bool:
        """Store ``value``. Returns False when it was too large to keep."""
        if not self.enabled:
            return False
        if size is None:
            size = _rough_size(value)
        if size > self.max_bytes:
            return False
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= old[2]
            self._data[key] = (time.monotonic() + self.ttl, value, size)
            self._bytes += size
            self._evict()
        return True

    def _evict(self) -> None:
        """Drop expired, then least-recently-used, until inside both bounds."""
        now = time.monotonic()
        for key in list(self._data.keys()):
            expires_at, _value, size = self._data[key]
            if expires_at > now:
                break                      # OrderedDict is in insertion order
            del self._data[key]
            self._bytes -= size
        while len(self._data) > self.max_entries or self._bytes > self.max_bytes:
            _key, (_exp, _val, size) = self._data.popitem(last=False)
            self._bytes -= size

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "name": self.name, "entries": len(self._data),
                "bytes": self._bytes, "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
                "ttl_s": self.ttl,
            }


def _rough_size(value: Any) -> int:
    """Cheap byte estimate for a JSON-ish value, without serialising it.

    Serialising an Overpass response just to measure it would cost more than
    the cache saves. Counting containers is within a small constant factor of
    the encoded size, which is all an eviction bound needs.
    """
    if isinstance(value, str):
        return len(value) + 49
    if isinstance(value, bytes):
        return len(value) + 33
    if isinstance(value, dict):
        return 64 + sum(_rough_size(k) + _rough_size(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return 56 + sum(map(_rough_size, value))
    return 24
