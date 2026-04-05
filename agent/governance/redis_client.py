"""Redis client with automatic fallback to SQLite.

Dual-write pattern:
  Write: SQLite (truth) → Redis (cache)
  Read:  Redis hit → return / Redis miss → SQLite → backfill Redis
  Redis down: degrade to pure SQLite, no service interruption
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Try to import redis; if not installed, Redis features are disabled
try:
    import redis as _redis_lib
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False
    _redis_lib = None
    log.warning("redis package not installed — Redis features disabled, using SQLite-only mode")


class RedisClient:
    """Redis client with graceful degradation."""

    def __init__(self, url: str = None):
        self._url = url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self._client = None
        self._available = False

    def connect(self) -> bool:
        """Attempt to connect to Redis. Returns True if successful."""
        if not HAS_REDIS:
            log.info("redis package not installed, running without Redis")
            return False
        try:
            self._client = _redis_lib.Redis.from_url(
                self._url, decode_responses=True, socket_timeout=3,
                socket_connect_timeout=3, retry_on_timeout=True,
            )
            self._client.ping()
            self._available = True
            log.info("Redis connected: %s", self._url)
            return True
        except Exception as e:
            self._available = False
            log.warning("Redis unavailable (%s), degrading to SQLite-only", e)
            return False

    @property
    def available(self) -> bool:
        return self._available

    def _safe(self, fn, default=None):
        """Execute a Redis operation, catching errors and degrading."""
        if not self._available:
            return default
        try:
            return fn()
        except Exception as e:
            log.warning("Redis error, degrading: %s", e)
            self._available = False
            return default

    # --- Key-Value Cache ---

    def get(self, key: str) -> Optional[str]:
        return self._safe(lambda: self._client.get(key))

    def get_json(self, key: str) -> Optional[dict]:
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def set(self, key: str, value: str, ttl_sec: int = 3600) -> bool:
        return self._safe(lambda: bool(self._client.setex(key, ttl_sec, value)), False)

    def set_json(self, key: str, value: dict, ttl_sec: int = 3600) -> bool:
        return self.set(key, json.dumps(value, ensure_ascii=False), ttl_sec)

    def delete(self, key: str) -> bool:
        return self._safe(lambda: bool(self._client.delete(key)), False)

    # --- Hash Operations (for sessions) ---

    def hset(self, name: str, mapping: dict, ttl_sec: int = 0) -> bool:
        def _do():
            self._client.hset(name, mapping=mapping)
            if ttl_sec > 0:
                self._client.expire(name, ttl_sec)
            return True
        return self._safe(_do, False)

    def hgetall(self, name: str) -> Optional[dict]:
        result = self._safe(lambda: self._client.hgetall(name))
        return result if result else None

    def hdel(self, name: str) -> bool:
        return self._safe(lambda: bool(self._client.delete(name)), False)

    # --- Idempotency (SET NX + TTL) ---

    def check_idempotency(self, key: str) -> Optional[dict]:
        return self.get_json(f"idem:{key}")

    def store_idempotency(self, key: str, response: dict, ttl_sec: int = 86400) -> bool:
        return self.set_json(f"idem:{key}", response, ttl_sec)

    # --- Distributed Lock ---

    def acquire_lock(self, name: str, ttl_sec: int = 30) -> bool:
        if not self._available:
            return True  # Degrade: no lock (single-instance SQLite WAL is enough)
        return self._safe(
            lambda: bool(self._client.set(f"lock:{name}", "1", nx=True, ex=ttl_sec)),
            True,
        )

    def release_lock(self, name: str) -> None:
        self._safe(lambda: self._client.delete(f"lock:{name}"))

    # --- Pub/Sub ---

    def publish(self, channel: str, message: dict) -> None:
        self._safe(lambda: self._client.publish(channel, json.dumps(message, ensure_ascii=False)))

    # --- Session Helpers ---

    def cache_session(self, session_id: str, session_data: dict, ttl_sec: int = 86400) -> bool:
        return self.set_json(f"session:{session_id}", session_data, ttl_sec)

    def get_cached_session(self, session_id: str) -> Optional[dict]:
        return self.get_json(f"session:{session_id}")

    def invalidate_session(self, session_id: str) -> bool:
        return self.delete(f"session:{session_id}")

    def cache_token_session(self, token_hash: str, session_id: str, ttl_sec: int = 86400) -> bool:
        return self.set(f"token:{token_hash}", session_id, ttl_sec)

    def get_session_by_token(self, token_hash: str) -> Optional[str]:
        return self.get(f"token:{token_hash}")

    def update_heartbeat(self, session_id: str, ttl_sec: int = 600) -> bool:
        """Update heartbeat by refreshing TTL on session key."""
        if not self._available:
            return False
        return self._safe(
            lambda: bool(self._client.expire(f"session:{session_id}", ttl_sec)),
            False,
        )


# Global singleton
_instance: Optional[RedisClient] = None


def get_redis() -> RedisClient:
    """Get the global Redis client, lazily connecting."""
    global _instance
    if _instance is None:
        _instance = RedisClient()
        _instance.connect()
    return _instance


def reset_redis() -> None:
    """Reset the global instance (for testing)."""
    global _instance
    _instance = None
