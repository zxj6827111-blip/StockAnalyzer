"""Cache backends used by providers and command idempotency."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class CacheStore(Protocol):
    """Minimal key-value interface with TTL."""

    def get(self, key: str) -> str | None:
        """Get value if exists and not expired."""

    def set(self, key: str, value: str, ttl_sec: int) -> None:
        """Set key with ttl."""

    def exists(self, key: str) -> bool:
        """Return True if key exists and not expired."""

    def delete(self, key: str) -> None:
        """Delete a specific key if present."""

    def delete_prefix(self, prefix: str) -> int:
        """Delete keys by prefix and return number of removed keys."""


@dataclass(slots=True)
class InMemoryCache:
    """Simple in-memory cache for local development and tests."""

    _store: dict[str, tuple[float, str]] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: str, ttl_sec: int) -> None:
        expires_at = time.monotonic() + max(1, ttl_sec)
        self._store[key] = (expires_at, value)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        keys = [key for key in self._store if key.startswith(prefix)]
        for key in keys:
            self._store.pop(key, None)
        return len(keys)


@dataclass(slots=True)
class RedisCache:
    """Redis-backed cache for multi-process runtime."""

    redis_url: str
    _client: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional extra.
            raise RuntimeError("redis package is not installed") from exc
        self._client = redis.from_url(self.redis_url, decode_responses=True)

    def get(self, key: str) -> str | None:
        raw = self._client.get(key)
        return None if raw is None else str(raw)

    def set(self, key: str, value: str, ttl_sec: int) -> None:
        self._client.set(name=key, value=value, ex=max(1, ttl_sec))

    def exists(self, key: str) -> bool:
        return bool(self._client.exists(key))

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def delete_prefix(self, prefix: str) -> int:
        keys = list(self._client.scan_iter(match=f"{prefix}*"))
        if not keys:
            return 0
        deleted = self._client.delete(*keys)
        return int(deleted or 0)
