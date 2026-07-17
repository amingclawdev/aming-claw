from __future__ import annotations

import threading
import time

from agent.governance import event_bus
from agent.governance import semantic_worker


def test_register_defers_catchup_until_core_listener_is_ready(monkeypatch):
    """Registration must return before restart catchup can consume resources."""
    semantic_worker._reset_worker_runtime_for_tests()
    listener_ready = threading.Event()
    listener_probed = threading.Event()
    catchup_started = threading.Event()
    subscriptions: list[tuple[str, object]] = []

    class _Bus:
        def subscribe(self, topic, callback):
            subscriptions.append((topic, callback))

    def _listener_ready():
        listener_probed.set()
        return listener_ready.is_set()

    monkeypatch.setattr(event_bus, "get_event_bus", lambda: _Bus())
    monkeypatch.setattr(semantic_worker, "_governance_listener_ready", _listener_ready)
    monkeypatch.setattr(
        semantic_worker,
        "on_governance_startup",
        lambda payload=None: catchup_started.set(),
    )
    monkeypatch.setattr(semantic_worker, "_STARTUP_LISTENER_POLL_SECONDS", 0.005)
    monkeypatch.setattr(semantic_worker, "_STARTUP_LISTENER_TIMEOUT_SECONDS", 1.0)

    try:
        started_at = time.monotonic()
        semantic_worker.register()
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.2
        assert listener_probed.wait(0.5)
        assert not catchup_started.wait(0.02)
        assert sorted(topic for topic, _ in subscriptions) == [
            "semantic_job.enqueued",
            "system.startup",
        ]

        listener_ready.set()
        assert catchup_started.wait(0.5)
    finally:
        semantic_worker._reset_worker_runtime_for_tests()


def test_startup_catchup_with_300_pending_jobs_submits_one_bounded_batch(
    tmp_path,
    monkeypatch,
):
    """A large durable queue starts with one bounded batch, not a full drain."""
    governance_root = tmp_path / "projects"
    project_dir = governance_root / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "governance.db").touch()

    class _Conn:
        def execute(self, sql, params=()):
            if "graph_snapshot_refs" in sql:
                row = {"snapshot_id": "snapshot-300-pending"}
            elif "graph_semantic_jobs" in sql:
                row = {"n": 300}
            else:
                row = {"n": 0}
            return type("_Cursor", (), {"fetchone": lambda self: row})()

        def close(self):
            return None

    submitted: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    class _Executor:
        def submit(self, fn, *args, **kwargs):
            submitted.append((fn, args, kwargs))
            return object()

    monkeypatch.setattr("agent.governance.db._governance_root", lambda: governance_root)
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _Conn())
    monkeypatch.setattr(semantic_worker, "_get_executor", lambda _workers: _Executor())
    monkeypatch.setattr(
        semantic_worker,
        "_worker_runtime_config",
        lambda project_id="": {"max_workers": 4},
    )

    semantic_worker.on_governance_startup({"source": "test"})

    assert submitted == [
        (
            semantic_worker._drain_node,
            ("demo", "snapshot-300-pending"),
            {"max_batches": 1},
        )
    ]


def test_bounded_startup_drain_claims_only_one_node(monkeypatch):
    captured: dict[str, int] = {}

    class _Conn:
        def commit(self):
            return None

        def close(self):
            return None

    def _claim(
        conn,
        project_id,
        snapshot_id,
        *,
        worker_id,
        statuses,
        limit,
        lease_seconds,
        actor,
    ):
        captured["limit"] = limit
        return {"claim_id": "claim-startup", "claimed_count": 0, "jobs": []}

    monkeypatch.setattr(
        semantic_worker,
        "_worker_runtime_config",
        lambda project_id="": {
            "max_workers": 4,
            "claim_batch_size": 4,
            "lease_seconds": 600,
        },
    )
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _Conn())
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_enrichment.claim_semantic_jobs",
        _claim,
    )
    monkeypatch.setattr(
        semantic_worker,
        "_count_claimable_pending_node_jobs",
        lambda *args, **kwargs: 0,
    )

    semantic_worker._drain_node("demo", "snapshot-300-pending", max_batches=1)

    assert captured == {"limit": 1}


def test_overlapping_startup_scan_is_collapsed_without_touching_durable_jobs(
    monkeypatch,
):
    calls: list[object] = []
    monkeypatch.setattr(
        semantic_worker,
        "_run_governance_startup_catchup",
        lambda payload=None: calls.append(payload),
    )

    assert semantic_worker._startup_scan_lock.acquire(blocking=False)
    try:
        semantic_worker.on_governance_startup({"source": "duplicate"})
    finally:
        semantic_worker._startup_scan_lock.release()

    assert calls == []
