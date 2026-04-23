"""
In-memory idempotency store with async locking and TTL support.

Each entry tracks:
- request_body_hash: SHA-256 of the canonical request body
- status_code: cached HTTP status code from the first successful processing
- response_body: cached response dict
- state: "processing" | "completed"
- event: asyncio.Event that waiters can block on (for in-flight race condition handling)
- created_at: timestamp for TTL expiry
"""

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# Default TTL: 24 hours (configurable via environment variable)
DEFAULT_TTL_SECONDS = 24 * 60 * 60


def get_ttl() -> int:
    """Get the TTL for idempotency keys from environment or default."""
    return int(os.getenv("IDEMPOTENCY_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))


def compute_body_hash(body: dict) -> str:
    """
    Compute a deterministic SHA-256 hash of the request body.

    Uses sorted keys to ensure the same payload always produces the same hash
    regardless of key ordering in the original JSON.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class IdempotencyEntry:
    """A single cached idempotency record."""

    request_body_hash: str
    state: str = "processing"  # "processing" or "completed"
    status_code: int = 0
    response_body: Optional[Dict[str, Any]] = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: float = field(default_factory=time.time)

    def is_expired(self, ttl: int) -> bool:
        """Check if this entry has exceeded the TTL."""
        return (time.time() - self.created_at) > ttl


class IdempotencyStore:
    """
    Thread-safe, async-compatible in-memory store for idempotency keys.

    Features:
    - Per-key locking to prevent race conditions
    - TTL-based expiry for automatic cleanup
    - Event-based waiting for in-flight requests
    """

    def __init__(self) -> None:
        self._store: Dict[str, IdempotencyEntry] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def get_lock(self, key: str) -> asyncio.Lock:
        """Get or create a per-key lock."""
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    def get(self, key: str) -> Optional[IdempotencyEntry]:
        """
        Retrieve an entry by key.

        Returns None if the key doesn't exist or has expired.
        """
        entry = self._store.get(key)
        if entry is None:
            return None

        # Check TTL expiry
        if entry.is_expired(get_ttl()):
            # Expired — remove it and treat as a new request
            del self._store[key]
            return None

        return entry

    def set(self, key: str, entry: IdempotencyEntry) -> None:
        """Store or overwrite an entry."""
        self._store[key] = entry

    def cleanup_expired(self) -> int:
        """
        Remove all expired entries from the store.

        Returns the number of entries removed.
        """
        ttl = get_ttl()
        expired_keys = [k for k, v in self._store.items() if v.is_expired(ttl)]
        for k in expired_keys:
            del self._store[k]
            if k in self._locks:
                del self._locks[k]
        return len(expired_keys)


# Singleton store instance
idempotency_store = IdempotencyStore()
