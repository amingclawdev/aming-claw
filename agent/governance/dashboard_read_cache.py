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


@dataclass
class _TimelineWindow:
    project_id: str
    resource_id: str
    authority_generation: int
    rows: deque[dict[str, Any]]
    stored_at: float
    validated_at: float


@dataclass
class _TimelineLoadFlight:
    event: Event
    leader_token: str
    pool: str
    project_id: str
    resource_id: str


@dataclass
class _TimelinePendingAppends:
    pool: str
    project_id: str
    resource_id: str
    limit: int
    rows: dict[int, dict[str, Any]]
    stored_at: float


class DashboardTimelineReadCache:
    """Bounded Current/Playback deques kept apart from historical pages.

    Current is keyed by database scope plus project. Playback is additionally
    keyed by backlog. Timeline append notifications update warm windows and
    append buffers owned by cold single-flight loaders; first project/backlog
    activation performs one bounded indexed prewarm without letting stale
    loader output overwrite a committed append.
    """

    def __init__(
        self,
        *,
        current_window_limit: int = 50,
        playback_window_limit: int = 50,
        current_project_limit: int = 64,
        playback_resource_limit: int = 256,
    ) -> None:
        self.current_window_limit = max(1, int(current_window_limit))
        self.playback_window_limit = max(1, int(playback_window_limit))
        self.current_project_limit = max(1, int(current_project_limit))
        self.playback_resource_limit = max(1, int(playback_resource_limit))
        self._current: OrderedDict[str, _TimelineWindow] = OrderedDict()
        self._playback: OrderedDict[str, _TimelineWindow] = OrderedDict()
        self._in_flight: dict[str, _TimelineLoadFlight] = {}
        self._pending_appends: OrderedDict[
            str,
            _TimelinePendingAppends,
        ] = OrderedDict()
        self._lock = RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @staticmethod
    def _current_key(database_scope: str, project_id: str) -> str:
        return f"current:{database_scope}:{project_id}"

    @staticmethod
    def _playback_key(
        database_scope: str,
        project_id: str,
        backlog_id: str,
    ) -> str:
        return f"playback:{database_scope}:{project_id}:{backlog_id}"

    def clear(self) -> None:
        with self._lock:
            pending = [flight.event for flight in self._in_flight.values()]
            self._current.clear()
            self._playback.clear()
            self._in_flight.clear()
            self._pending_appends.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0
        for event in pending:
            event.set()

    def current_generation(
        self,
        *,
        database_scope: str,
        project_id: str,
        revalidator: Callable[[], int] | None = None,
        revalidate_after_seconds: float = 1.0,
    ) -> int | None:
        """Return the warm generation without synchronously touching SQLite.

        Committed timeline notifications advance the generation exactly.  The
        optional arguments remain accepted for compatibility with older
        callers, but a warm read never executes the revalidator: Current must
        serve process memory immediately.
        """

        del revalidator, revalidate_after_seconds
        key = self._current_key(database_scope, project_id)
        with self._lock:
            window = self._current.get(key)
            if window is None:
                return None
            return int(window.authority_generation)

    def load_current(
        self,
        *,
        database_scope: str,
        project_id: str,
        authority_generation: int,
        loader: Callable[[], list[dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._load_window(
            pool="current",
            key=self._current_key(database_scope, project_id),
            project_id=project_id,
            resource_id=project_id,
            authority_generation=authority_generation,
            limit=self.current_window_limit,
            loader=loader,
        )

    def load_playback(
        self,
        *,
        database_scope: str,
        project_id: str,
        backlog_id: str,
        authority_generation: int,
        loader: Callable[[], list[dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._load_window(
            pool="playback",
            key=self._playback_key(database_scope, project_id, backlog_id),
            project_id=project_id,
            resource_id=backlog_id,
            authority_generation=authority_generation,
            limit=self.playback_window_limit,
            loader=loader,
        )

    def playback_generation(
        self,
        *,
        database_scope: str,
        project_id: str,
        backlog_id: str,
    ) -> int | None:
        key = self._playback_key(database_scope, project_id, backlog_id)
        with self._lock:
            window = self._playback.get(key)
            return (
                int(window.authority_generation)
                if window is not None
                else None
            )

    def _load_window(
        self,
        *,
        pool: str,
        key: str,
        project_id: str,
        resource_id: str,
        authority_generation: int,
        limit: int,
        loader: Callable[[], list[dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        started = time.monotonic()
        leader_token = f"{id(Event)}:{started:.9f}"
        joined = False
        while True:
            with self._lock:
                entries = self._current if pool == "current" else self._playback
                window = entries.get(key)
                if (
                    window is not None
                    and int(window.authority_generation)
                    >= int(authority_generation)
                ):
                    entries.move_to_end(key)
                    self._hits += 1
                    age_ms = max(
                        0,
                        int((time.monotonic() - window.stored_at) * 1000),
                    )
                    return (
                        [deepcopy(row) for row in window.rows],
                        self._metrics(
                            pool=pool,
                            hit=True,
                            age_ms=age_ms,
                            joined=joined,
                            generation=window.authority_generation,
                            window_count=len(window.rows),
                        ),
                    )
                flight = self._in_flight.get(key)
                if flight is None:
                    event = Event()
                    self._in_flight[key] = _TimelineLoadFlight(
                        event=event,
                        leader_token=leader_token,
                        pool=pool,
                        project_id=str(project_id),
                        resource_id=str(resource_id),
                    )
                    self._ensure_pending_buffer_locked(
                        key=key,
                        pool=pool,
                        project_id=project_id,
                        resource_id=resource_id,
                        limit=limit,
                    )
                    self._misses += 1
                    break
                event = flight.event
                joined = True
            event.wait(timeout=5.0)

        try:
            loaded = [
                deepcopy(row)
                for row in loader()
                if isinstance(row, dict)
            ][:limit]
        except BaseException:
            self._release_leader(key, leader_token)
            raise

        result_rows = self._newest_rows(loaded, limit=limit)
        result_generation = max(
            int(authority_generation),
            max(
                (int(row.get("id") or 0) for row in result_rows),
                default=0,
            ),
        )
        merged_append_count = 0
        release_event: Event | None = None
        with self._lock:
            flight = self._in_flight.get(key)
            if flight is not None and flight.leader_token == leader_token:
                entries = self._current if pool == "current" else self._playback
                existing = entries.get(key)
                pending = self._pending_appends.get(key)
                pending_rows = list(pending.rows.values()) if pending else []
                merged_append_count = len(pending_rows)
                combined = list(loaded)
                if existing is not None:
                    combined.extend(existing.rows)
                    result_generation = max(
                        result_generation,
                        int(existing.authority_generation),
                    )
                combined.extend(pending_rows)
                result_rows = self._newest_rows(combined, limit=limit)
                result_generation = max(
                    result_generation,
                    max(
                        (
                            int(row.get("id") or 0)
                            for row in result_rows
                        ),
                        default=0,
                    ),
                )
                now = time.monotonic()
                window = _TimelineWindow(
                    project_id=str(project_id),
                    resource_id=str(resource_id),
                    authority_generation=result_generation,
                    rows=deque(result_rows, maxlen=limit),
                    stored_at=now,
                    validated_at=now,
                )
                entries[key] = window
                entries.move_to_end(key)
                pool_limit = (
                    self.current_project_limit
                    if pool == "current"
                    else self.playback_resource_limit
                )
                while len(entries) > pool_limit:
                    entries.popitem(last=False)
                    self._evictions += 1
                release_event = flight.event
                self._in_flight.pop(key, None)
                self._pending_appends.pop(key, None)
        if release_event is not None:
            release_event.set()
        return (
            [deepcopy(row) for row in result_rows],
            self._metrics(
                pool=pool,
                hit=False,
                age_ms=0,
                joined=joined,
                generation=result_generation,
                window_count=len(result_rows),
                merged_append_count=merged_append_count,
            ),
        )

    def _release_leader(self, key: str, leader_token: str) -> None:
        release_event: Event | None = None
        with self._lock:
            flight = self._in_flight.get(key)
            if flight is not None and flight.leader_token == leader_token:
                release_event = flight.event
                self._in_flight.pop(key, None)
                pending = self._pending_appends.get(key)
                if pending is not None and not pending.rows:
                    self._pending_appends.pop(key, None)
        if release_event is not None:
            release_event.set()

    def _ensure_pending_buffer_locked(
        self,
        *,
        key: str,
        pool: str,
        project_id: str,
        resource_id: str,
        limit: int,
    ) -> _TimelinePendingAppends:
        pending = self._pending_appends.get(key)
        if pending is None:
            pending = _TimelinePendingAppends(
                pool=pool,
                project_id=str(project_id),
                resource_id=str(resource_id),
                limit=max(1, int(limit)),
                rows={},
                stored_at=time.monotonic(),
            )
            self._pending_appends[key] = pending
        self._pending_appends.move_to_end(key)
        pending_limit = self.current_project_limit + self.playback_resource_limit
        while len(self._pending_appends) > pending_limit:
            victim = next(
                (
                    pending_key
                    for pending_key in self._pending_appends
                    if pending_key not in self._in_flight
                ),
                "",
            )
            if not victim:
                break
            self._pending_appends.pop(victim, None)
            self._evictions += 1
        return pending

    @staticmethod
    def _newest_rows(
        rows: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        by_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            event_id = int(row.get("id") or 0)
            if event_id <= 0:
                continue
            by_id[event_id] = deepcopy(row)
        return [
            by_id[event_id]
            for event_id in sorted(by_id, reverse=True)[: max(1, int(limit))]
        ]

    def append(self, event: dict[str, Any]) -> None:
        """Append one committed/projected event to matching warm deques."""

        project_id = str(event.get("project_id") or "")
        backlog_id = str(event.get("backlog_id") or "")
        event_id = int(event.get("id") or 0)
        if not project_id or event_id <= 0:
            return
        with self._lock:
            for entries, include in (
                (self._current, lambda window: window.project_id == project_id),
                (
                    self._playback,
                    lambda window: (
                        window.project_id == project_id
                        and window.resource_id == backlog_id
                    ),
                ),
            ):
                for key, window in list(entries.items()):
                    if not include(window):
                        continue
                    retained = [
                        row
                        for row in window.rows
                        if int(row.get("id") or 0) != event_id
                    ]
                    retained.append(deepcopy(event))
                    retained.sort(
                        key=lambda row: int(row.get("id") or 0),
                        reverse=True,
                    )
                    window.rows.clear()
                    window.rows.extend(retained[: window.rows.maxlen])
                    window.authority_generation = max(
                        window.authority_generation,
                        event_id,
                    )
                    window.stored_at = time.monotonic()
                    window.validated_at = window.stored_at
                    entries.move_to_end(key)
            for key, pending in list(self._pending_appends.items()):
                if pending.project_id != project_id:
                    continue
                if (
                    pending.pool == "playback"
                    and pending.resource_id != backlog_id
                ):
                    continue
                pending.rows[event_id] = deepcopy(event)
                while len(pending.rows) > pending.limit:
                    pending.rows.pop(min(pending.rows), None)
                pending.stored_at = time.monotonic()
                self._pending_appends.move_to_end(key)

    def _metrics(
        self,
        *,
        pool: str,
        hit: bool,
        age_ms: int,
        joined: bool,
        generation: int,
        window_count: int,
        merged_append_count: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": "dashboard.timeline_hot_window.v1",
                "pool": (
                    "current_project_deque"
                    if pool == "current"
                    else "playback_backlog_deque"
                ),
                "storage": "process_memory",
                "redis": False,
                "hit": bool(hit),
                "miss": not hit,
                "age_ms": int(age_ms),
                "authority_generation": int(generation),
                "window_count": int(window_count),
                "window_limit": (
                    self.current_window_limit
                    if pool == "current"
                    else self.playback_window_limit
                ),
                "hit_count": int(self._hits),
                "miss_count": int(self._misses),
                "eviction_count": int(self._evictions),
                "single_flight": "joined" if joined else "leader",
                "newest_first": True,
                "project_isolated": True,
                "stale_while_revalidate": True,
                "revalidate_after_ms": 0,
                "revalidation": "commit_driven_exact_invalidation",
                "warm_read_database_queries": 0,
                "cold_load_append_merge": True,
                "merged_append_count": int(merged_append_count),
                "generation_never_regresses": True,
                "prewarm_source": (
                    "process_memory"
                    if hit
                    else "sqlite_indexed_bounded_loader"
                ),
            }


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
TIMELINE_READ_CACHE = DashboardTimelineReadCache()
