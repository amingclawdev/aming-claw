"""Bounded process-memory caches for dashboard read models.

The cache deliberately keeps the per-project hot window separate from
historical/search pages.  Callers supply a durable authority generation; when
that generation advances, every entry for that project is invalidated exactly.
"""
from __future__ import annotations

from collections import Counter, OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass
from threading import Event, RLock
import time
from typing import Any, Callable


@dataclass
class _CacheEntry:
    project_id: str
    authority_generation: str
    value: dict[str, Any]
    stored_at: float
    expires_at: float


class DashboardBacklogReadCache:
    """Project-isolated hot windows plus bounded TTL/LRU historical pages."""

    def __init__(
        self,
        *,
        hot_window_limit: int = 250,
        hot_project_limit: int = 64,
        historical_project_limit: int = 32,
        historical_global_limit: int = 128,
        historical_ttl_seconds: float = 180.0,
    ) -> None:
        self.hot_window_limit = max(1, int(hot_window_limit))
        self.hot_project_limit = max(1, int(hot_project_limit))
        self.historical_project_limit = max(1, int(historical_project_limit))
        self.historical_global_limit = max(
            self.historical_project_limit,
            int(historical_global_limit),
        )
        self.historical_ttl_seconds = max(1.0, float(historical_ttl_seconds))
        self._hot: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hot_rows: dict[str, deque[dict[str, Any]]] = {}
        self._historical: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._in_flight: dict[str, tuple[Event, str]] = {}
        self._project_generations: dict[str, str] = {}
        self._lock = RLock()
        self._evictions = 0
        self._hits = 0
        self._misses = 0

    def clear(self) -> None:
        with self._lock:
            pending = [event for event, _ in self._in_flight.values()]
            self._hot.clear()
            self._hot_rows.clear()
            self._historical.clear()
            self._in_flight.clear()
            self._project_generations.clear()
            self._evictions = 0
            self._hits = 0
            self._misses = 0
        for event in pending:
            event.set()

    def observe_generation(
        self,
        project_id: str,
        authority_generation: str,
    ) -> int:
        """Invalidate only one project's entries when its authority advances."""

        pid = str(project_id)
        generation = str(authority_generation)
        removed = 0
        with self._lock:
            previous = self._project_generations.get(pid)
            self._project_generations[pid] = generation
            if previous is None or previous == generation:
                return 0
            if self._hot.pop(pid, None) is not None:
                self._hot_rows.pop(pid, None)
                removed += 1
            for key in [
                key
                for key, entry in self._historical.items()
                if entry.project_id == pid
            ]:
                self._historical.pop(key, None)
                removed += 1
            self._evictions += removed
        return removed

    def load_hot(
        self,
        *,
        project_id: str,
        authority_generation: str,
        loader: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.observe_generation(project_id, authority_generation)
        return self._load(
            pool="hot",
            key=f"hot:{project_id}",
            project_id=project_id,
            authority_generation=authority_generation,
            ttl_seconds=None,
            loader=loader,
        )

    def load_historical(
        self,
        *,
        key: str,
        project_id: str,
        authority_generation: str,
        loader: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.observe_generation(project_id, authority_generation)
        return self._load(
            pool="historical",
            key=f"history:{key}",
            project_id=project_id,
            authority_generation=authority_generation,
            ttl_seconds=self.historical_ttl_seconds,
            loader=loader,
        )

    def _load(
        self,
        *,
        pool: str,
        key: str,
        project_id: str,
        authority_generation: str,
        ttl_seconds: float | None,
        loader: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        started = time.monotonic()
        leader_token = f"{id(Event)}:{started:.9f}"
        joined = False
        while True:
            with self._lock:
                entries = self._hot if pool == "hot" else self._historical
                now = time.monotonic()
                entry = entries.get(project_id if pool == "hot" else key)
                if (
                    entry is not None
                    and entry.authority_generation == authority_generation
                    and entry.expires_at > now
                ):
                    entries.move_to_end(project_id if pool == "hot" else key)
                    self._hits += 1
                    cached_value = deepcopy(entry.value)
                    if pool == "hot" and str(project_id) in self._hot_rows:
                        cached_value["bugs"] = [
                            deepcopy(row)
                            for row in self._hot_rows.get(str(project_id), ())
                        ]
                    return cached_value, self._metrics(
                        pool=pool,
                        hit=True,
                        age_ms=max(0, int((now - entry.stored_at) * 1000)),
                        joined=joined,
                    )
                in_flight = self._in_flight.get(key)
                if in_flight is None:
                    event = Event()
                    self._in_flight[key] = (event, leader_token)
                    self._misses += 1
                    break
                event = in_flight[0]
                joined = True
            event.wait(timeout=5.0)

        try:
            value = loader()
        except BaseException:
            self._release_leader(key, leader_token)
            raise

        now = time.monotonic()
        expires_at = now + ttl_seconds if ttl_seconds is not None else float("inf")
        entry = _CacheEntry(
            project_id=str(project_id),
            authority_generation=str(authority_generation),
            value=deepcopy(value),
            stored_at=now,
            expires_at=expires_at,
        )
        release_event: Event | None = None
        with self._lock:
            in_flight = self._in_flight.get(key)
            if in_flight is not None and in_flight[1] == leader_token:
                if pool == "hot":
                    self._hot[str(project_id)] = entry
                    if "bugs" in value:
                        self._hot_rows[str(project_id)] = deque(
                            (
                                deepcopy(row)
                                for row in value.get("bugs", [])
                                if isinstance(row, dict)
                            ),
                            maxlen=self.hot_window_limit,
                        )
                    self._hot.move_to_end(str(project_id))
                    while len(self._hot) > self.hot_project_limit:
                        evicted_project_id, _ = self._hot.popitem(last=False)
                        self._hot_rows.pop(evicted_project_id, None)
                        self._evictions += 1
                else:
                    self._historical[key] = entry
                    self._historical.move_to_end(key)
                    self._evict_historical_locked()
                release_event = in_flight[0]
                self._in_flight.pop(key, None)
        if release_event is not None:
            release_event.set()
        return deepcopy(value), self._metrics(
            pool=pool,
            hit=False,
            age_ms=0,
            joined=joined,
        )

    def _release_leader(self, key: str, leader_token: str) -> None:
        release_event: Event | None = None
        with self._lock:
            in_flight = self._in_flight.get(key)
            if in_flight is not None and in_flight[1] == leader_token:
                release_event = in_flight[0]
                self._in_flight.pop(key, None)
        if release_event is not None:
            release_event.set()

    def _evict_historical_locked(self) -> None:
        counts = Counter(entry.project_id for entry in self._historical.values())
        while self._historical:
            over_project = next(
                (
                    project_id
                    for project_id, count in counts.items()
                    if count > self.historical_project_limit
                ),
                "",
            )
            if not over_project and len(self._historical) <= self.historical_global_limit:
                return
            victim = next(
                (
                    key
                    for key, entry in self._historical.items()
                    if not over_project or entry.project_id == over_project
                ),
                next(iter(self._historical)),
            )
            removed = self._historical.pop(victim)
            counts[removed.project_id] -= 1
            self._evictions += 1

    def _metrics(
        self,
        *,
        pool: str,
        hit: bool,
        age_ms: int,
        joined: bool,
    ) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": "dashboard.backlog_read_cache.v1",
                "pool": "hot_window" if pool == "hot" else "historical_ttl_lru",
                "storage": "process_memory",
                "hit": bool(hit),
                "miss": not hit,
                "age_ms": int(age_ms),
                "eviction_count": int(self._evictions),
                "hit_count": int(self._hits),
                "miss_count": int(self._misses),
                "single_flight": "joined" if joined else "leader",
                "historical_ttl_seconds": self.historical_ttl_seconds,
                "hot_project_limit": self.hot_project_limit,
                "hot_window_limit": self.hot_window_limit,
                "historical_project_limit": self.historical_project_limit,
                "historical_global_limit": self.historical_global_limit,
            }


BACKLOG_READ_CACHE = DashboardBacklogReadCache()
