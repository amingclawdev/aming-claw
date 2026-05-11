"""In-process event-driven semantic enrichment worker.

MF-2026-05-10-016. Replaces the missing daemon for the
`/semantic/jobs` queue. Subscribes to EventBus topics:

- `semantic_job.enqueued` — fired by `POST /semantic/jobs` after writing
  ai_pending rows. Worker drains the affected snapshot.
- `system.startup` — fired during governance startup catchup so any
  ai_pending rows that survived a restart get processed.

For each drain, the worker claims a small batch via the existing
`claim_semantic_jobs` API (lease + claim_id ensure no double-claim if a
future external daemon is added), then runs `run_semantic_enrichment`
in-process for that single node with `submit_for_review=True`. The
result lands in `graph_semantic_nodes` with `status='pending_review'`,
which `backfill_existing_semantic_events` maps to
`EVENT_STATUS_PROPOSED` — invisible to the projection until an operator
flips it via `/feedback/decision` action `accept_semantic_enrichment`.

Scope guardrail: worker only handles `operation_type IN
('node_semantic', 'edge_semantic')`. Other op types (scope_reconcile,
feedback_review) are ignored at the claim layer (`claim_semantic_jobs`
already filters node-shaped rows).

Concurrency: a per-(project, snapshot) lock prevents overlapping
drains. A small ThreadPoolExecutor caps total concurrent AI calls at 4.
SQLite WAL + the existing `sqlite_write_lock` handles cross-thread
write serialization.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_busy_locks: dict[tuple[str, str], threading.Lock] = {}
_busy_locks_guard = threading.Lock()
_registered = False
_DRAIN_BATCH_SIZE = 4
_DRAIN_LEASE_SECONDS = 600


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="semantic-worker",
        )
    return _executor


def _drain_lock_for(project_id: str, snapshot_id: str) -> threading.Lock:
    key = (project_id, snapshot_id)
    with _busy_locks_guard:
        lock = _busy_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _busy_locks[key] = lock
    return lock


def _project_root_for(project_id: str) -> Path:
    """Best-effort project root resolution. Worker runs in same process as
    governance which has its own root resolver — reuse that."""
    from .db import _governance_root

    # Project source root is the project workdir; governance root holds DB.
    # For aming-claw the workdir IS the repo root that hosts agent/.
    # When invoked from server.main(), CWD is the repo root.
    return Path.cwd()


def _drain(project_id: str, snapshot_id: str) -> None:
    """Backwards-compat shim. Pre MF-2026-05-10-017 the worker only handled
    nodes; callers expecting `_drain(project_id, snapshot_id)` still work."""
    _drain_node(project_id, snapshot_id)


def _drain_node(project_id: str, snapshot_id: str) -> None:
    """Drain ai_pending semantic jobs for one snapshot.

    Runs at most one node enrichment per call to keep worker threads
    responsive. The enqueue listener will fire again as new rows land,
    and startup catchup loops until the queue is empty.
    """
    lock = _drain_lock_for(project_id, snapshot_id)
    if not lock.acquire(blocking=False):
        log.debug("semantic_worker: drain skipped (busy) %s/%s", project_id, snapshot_id)
        return
    try:
        from . import db as governance_db
        from . import reconcile_semantic_enrichment as semantic
        from .reconcile_semantic_ai import build_semantic_ai_call
        from .reconcile_semantic_config import load_semantic_enrichment_config
        from . import reconcile_feedback

        conn = governance_db.get_connection(project_id)
        try:
            try:
                claim = semantic.claim_semantic_jobs(
                    conn,
                    project_id,
                    snapshot_id,
                    worker_id="semantic_worker_inproc",
                    statuses=["ai_pending", "pending_ai"],
                    limit=_DRAIN_BATCH_SIZE,
                    lease_seconds=_DRAIN_LEASE_SECONDS,
                    actor="semantic_worker_inproc",
                )
            except Exception as exc:  # noqa: BLE001 - claim is best-effort
                log.warning("semantic_worker: claim failed %s/%s: %s",
                            project_id, snapshot_id, exc)
                conn.commit()
                return
            claim_id = str(claim.get("claim_id") or "")
            # MF-2026-05-10-016 fix: claim_semantic_jobs returns `jobs` (list
            # of row dicts), not `node_ids`. Extract node_id per row.
            jobs = claim.get("jobs") or []
            node_ids = [str(j.get("node_id") or "").strip() for j in jobs if j.get("node_id")]
            if not node_ids:
                log.info("semantic_worker: nothing claimed %s/%s (claim_id=%s claimed_count=%d)",
                         project_id, snapshot_id, claim_id, int(claim.get("claimed_count") or 0))
                return
            log.info("semantic_worker: claim_id=%s node_ids=%s",
                     claim_id, list(node_ids)[:5])
            root = _project_root_for(project_id)
            cfg = load_semantic_enrichment_config(project_root=root)
            try:
                ai_call = build_semantic_ai_call(
                    semantic_config=cfg,
                    project_id=project_id,
                    snapshot_id=snapshot_id,
                    project_root=root,
                )
            except Exception as exc:  # noqa: BLE001 - record + leave rows for next drain
                log.error("semantic_worker: build_semantic_ai_call failed: %s", exc)
                return
            for node_id in node_ids:
                node_id_s = str(node_id or "").strip()
                if not node_id_s:
                    continue
                try:
                    result = semantic.run_semantic_enrichment(
                        conn, project_id, snapshot_id, str(root),
                        use_ai=True,
                        ai_call=ai_call,
                        semantic_node_ids=[node_id_s],
                        semantic_skip_completed=False,
                        submit_for_review=True,
                        created_by="semantic_worker_inproc",
                    )
                except Exception as exc:  # noqa: BLE001 - record + carry on
                    log.exception("semantic_worker: enrich failed for %s: %s",
                                  node_id_s, exc)
                    continue
                summary = result.get("summary") if isinstance(result, dict) else {}
                ai_complete = (summary or {}).get("ai_complete_count", 0)
                if not ai_complete:
                    log.warning("semantic_worker: enrich returned 0 ai_complete for %s",
                                node_id_s)
                    continue
                # Write a feedback item so the dashboard Review Queue surfaces it.
                # Evidence carries the linked event_id derived from feature_hash.
                feature_hash = ""
                # The most recently written graph_semantic_nodes row is the source
                # of truth for feature_hash; pull it.
                row = conn.execute(
                    "SELECT feature_hash FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id=?",
                    (project_id, snapshot_id, node_id_s),
                ).fetchone()
                if row:
                    feature_hash = str(row["feature_hash"] or "")
                # Event id is deterministic per backfill: f"semnode-{snapshot_id}-{node_id}-{feature_hash[:12]}"
                # but governance constructs it via _safe_event_id — duplicate the
                # construction here is brittle. Instead, after running enrichment,
                # trigger a backfill pass so the event row exists, then look it up.
                try:
                    from . import graph_events
                    graph_events.backfill_existing_semantic_events(
                        conn, project_id, snapshot_id, actor="semantic_worker_inproc",
                    )
                except Exception as exc:  # noqa: BLE001 - advisory
                    log.warning("semantic_worker: backfill failed for %s: %s",
                                node_id_s, exc)
                    conn.commit()
                    continue
                # Look up the just-written PROPOSED event for this node.
                event_id = ""
                try:
                    ev_row = conn.execute(
                        """
                        SELECT event_id FROM graph_events
                        WHERE project_id = ? AND snapshot_id = ?
                          AND event_type = 'semantic_node_enriched'
                          AND target_id = ?
                          AND status = 'proposed'
                        ORDER BY event_seq DESC LIMIT 1
                        """,
                        (project_id, snapshot_id, node_id_s),
                    ).fetchone()
                    if ev_row:
                        event_id = str(ev_row["event_id"] or "")
                except Exception as exc:  # noqa: BLE001
                    log.warning("semantic_worker: event lookup failed for %s: %s",
                                node_id_s, exc)
                # Submit feedback row pointing at the event for review. The
                # accept handler reads node_id from item.target_id and the
                # event id list from item.evidence.linked_event_ids; we pack
                # both into the issue dict so submit_feedback_item carries
                # them through to the persisted feedback row.
                try:
                    reconcile_feedback.submit_feedback_item(
                        project_id,
                        snapshot_id,
                        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
                        issue={
                            "issue": f"AI semantic enrichment generated for {node_id_s} — awaiting operator review",
                            "source_node_ids": [node_id_s],
                            "target_id": node_id_s,
                            "target_type": "node",
                            "priority": "P3",
                            "evidence": {
                                "source": "semantic_worker_inproc",
                                "node_id": node_id_s,
                                "feature_hash": feature_hash,
                                "linked_event_ids": [event_id] if event_id else [],
                            },
                        },
                        actor="semantic_worker_inproc",
                    )
                except Exception as exc:  # noqa: BLE001 - feedback row is advisory
                    log.warning("semantic_worker: feedback submit failed for %s: %s",
                                node_id_s, exc)
                conn.commit()
        finally:
            conn.close()
    finally:
        lock.release()


def _drain_edge(project_id: str, snapshot_id: str) -> None:
    """MF-2026-05-10-017: drain unenriched edge_semantic_requested events.

    Edges don't live in graph_semantic_jobs — the queue substrate for edges is
    graph_events. A request lands as `edge_semantic_requested status=observed`;
    once an `edge_semantic_enriched` event for the same target_id exists
    (proposed/observed/accepted/materialized), the edge is considered handled.
    This drain claims unenriched requests one batch at a time, runs AI, writes
    a PROPOSED enriched event, and submits a needs_observer_decision feedback
    row so the Review Queue picks it up — same review gate as the node path.
    """
    # Use a separate lock from the node drain so node + edge work in parallel.
    lock = _drain_lock_for(project_id, snapshot_id + ":edge")
    if not lock.acquire(blocking=False):
        log.debug("semantic_worker: edge drain skipped (busy) %s/%s",
                  project_id, snapshot_id)
        return
    try:
        import json
        from . import db as governance_db
        from . import graph_events
        from . import reconcile_feedback
        from .reconcile_semantic_ai import build_semantic_ai_call
        from .reconcile_semantic_config import load_semantic_enrichment_config

        conn = governance_db.get_connection(project_id)
        try:
            rows = conn.execute(
                """
                SELECT event_id, target_id, payload_json
                FROM graph_events
                WHERE project_id = ?
                  AND snapshot_id = ?
                  AND event_type = 'edge_semantic_requested'
                  AND status = 'observed'
                  AND target_id NOT IN (
                    SELECT target_id FROM graph_events
                    WHERE project_id = ?
                      AND snapshot_id = ?
                      AND event_type = 'edge_semantic_enriched'
                      AND status IN ('observed', 'proposed', 'accepted', 'materialized')
                  )
                ORDER BY created_at
                LIMIT ?
                """,
                (project_id, snapshot_id, project_id, snapshot_id, _DRAIN_BATCH_SIZE),
            ).fetchall()
            if not rows:
                log.info("semantic_worker: no edges to drain for %s/%s",
                         project_id, snapshot_id)
                return
            log.info("semantic_worker: edge drain %s/%s candidates=%d",
                     project_id, snapshot_id, len(rows))
            root = _project_root_for(project_id)
            cfg = load_semantic_enrichment_config(project_root=root)
            try:
                ai_call = build_semantic_ai_call(
                    semantic_config=cfg,
                    project_id=project_id,
                    snapshot_id=snapshot_id,
                    project_root=root,
                )
            except Exception as exc:  # noqa: BLE001 - record + leave events for next drain
                log.error("semantic_worker: edge build_semantic_ai_call failed: %s", exc)
                return
            if ai_call is None:
                log.warning("semantic_worker: edge AI not configured for %s", project_id)
                return
            for row in rows:
                edge_id = str(row["target_id"] or "").strip()
                if not edge_id:
                    continue
                payload = {}
                try:
                    if row["payload_json"]:
                        payload = json.loads(row["payload_json"]) or {}
                except Exception:  # noqa: BLE001 - payload is advisory
                    payload = {}
                raw_edge = payload.get("edge") or {}
                edge_context = (
                    payload.get("edge_context") if isinstance(payload.get("edge_context"), dict) else {}
                )
                operator_request = (
                    payload.get("operator_request")
                    if isinstance(payload.get("operator_request"), dict)
                    else {}
                )
                instructions = (
                    payload.get("instructions") if isinstance(payload.get("instructions"), dict) else {}
                )
                ai_payload = {
                    "schema_version": 1,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "edge": raw_edge,
                    "edge_context": edge_context,
                    "operator_request": operator_request,
                    "instructions": instructions,
                    "output_contract": {
                        "required": ["relation_purpose", "confidence", "evidence"],
                        "optional": ["risk", "directionality", "semantic_label", "open_issues"],
                    },
                }
                try:
                    ai_response = ai_call("edge", ai_payload)
                except Exception as exc:  # noqa: BLE001 - record + skip
                    log.exception("semantic_worker: edge AI failed for %s: %s",
                                  edge_id, exc)
                    continue
                semantic_payload = ai_response if isinstance(ai_response, dict) else {}
                if "_ai_error" in semantic_payload:
                    log.warning("semantic_worker: edge AI error for %s: %s",
                                edge_id, semantic_payload.get("_ai_error"))
                    continue
                enriched_payload = dict(payload)
                enriched_payload["semantic_payload"] = semantic_payload
                enriched_payload["enriched_by"] = "semantic_worker_inproc_edge"
                try:
                    enriched = graph_events.create_event(
                        conn,
                        project_id,
                        snapshot_id,
                        event_type="edge_semantic_enriched",
                        event_kind="semantic_job",
                        target_type="edge",
                        target_id=edge_id,
                        status=graph_events.EVENT_STATUS_PROPOSED,
                        payload=enriched_payload,
                        evidence={"source": "semantic_worker_inproc_edge"},
                        created_by="semantic_worker_inproc",
                    )
                except Exception as exc:  # noqa: BLE001 - record + carry on
                    log.exception("semantic_worker: create edge enriched event failed for %s: %s",
                                  edge_id, exc)
                    continue
                event_id = str(enriched.get("event_id") or "")
                try:
                    reconcile_feedback.submit_feedback_item(
                        project_id,
                        snapshot_id,
                        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
                        issue={
                            "issue": f"AI edge semantic enrichment generated for {edge_id} — awaiting operator review",
                            "target_id": edge_id,
                            "target_type": "edge",
                            "priority": "P3",
                            "evidence": {
                                "source": "semantic_worker_inproc_edge",
                                "edge_id": edge_id,
                                "linked_event_ids": [event_id] if event_id else [],
                            },
                        },
                        actor="semantic_worker_inproc",
                    )
                except Exception as exc:  # noqa: BLE001 - feedback is advisory
                    log.warning("semantic_worker: edge feedback submit failed for %s: %s",
                                edge_id, exc)
                conn.commit()
        finally:
            conn.close()
    finally:
        lock.release()


def on_semantic_job_enqueued(payload: Any) -> None:
    """EventBus listener for `semantic_job.enqueued`. Spawns a drain task.

    MF-2026-05-10-017: payload may include `target_scope` ("node" | "edge").
    Default is "node" for backwards compatibility with existing publish sites
    that don't set the field.
    """
    try:
        if not isinstance(payload, dict):
            return
        project_id = str(payload.get("project_id") or "").strip()
        snapshot_id = str(payload.get("snapshot_id") or "").strip()
        if not project_id or not snapshot_id:
            return
        target_scope = str(payload.get("target_scope") or "node").strip().lower()
        log.info(
            "semantic_worker: enqueue event %s/%s scope=%s",
            project_id, snapshot_id, target_scope,
        )
        if target_scope == "edge":
            _get_executor().submit(_drain_edge, project_id, snapshot_id)
        else:
            _get_executor().submit(_drain_node, project_id, snapshot_id)
        _get_executor().submit(_drain, project_id, snapshot_id)
    except Exception as exc:  # noqa: BLE001 - listener must not raise
        log.exception("semantic_worker: on_semantic_job_enqueued failed: %s", exc)


def on_governance_startup(payload: Any = None) -> None:
    """EventBus listener for `system.startup`. Catches up on rows
    that were enqueued before this process started.

    Scope guardrail: ONLY drains the active snapshot per project.
    Superseded snapshots may have ai_pending rows from old reconcile
    cycles — those are irrelevant to the live dashboard and would
    waste AI calls. Operators wanting to backfill old snapshots can
    manually re-fire enrichment.
    """
    try:
        from . import db as governance_db
        gov_root = governance_db._governance_root()
        if not gov_root.exists():
            return
        for pdir in gov_root.iterdir():
            if not pdir.is_dir():
                continue
            db_path = pdir / "governance.db"
            if not db_path.exists():
                continue
            project_id = pdir.name
            try:
                conn = governance_db.get_connection(project_id)
                try:
                    # Active snapshot id only.
                    active_row = conn.execute(
                        "SELECT snapshot_id FROM graph_snapshot_refs WHERE project_id = ? AND ref_name = 'active'",
                        (project_id,),
                    ).fetchone()
                    if not active_row:
                        continue
                    sid = str(active_row["snapshot_id"] or "")
                    if not sid:
                        continue
                    pending = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM graph_semantic_jobs
                        WHERE project_id = ? AND snapshot_id = ?
                          AND status IN ('ai_pending', 'pending_ai')
                        """,
                        (project_id, sid),
                    ).fetchone()
                    n = int(pending["n"] if pending else 0)
                    if n > 0:
                        log.info(
                            "semantic_worker: startup catchup %s/%s nodes=%d",
                            project_id, sid, n,
                        )
                        _get_executor().submit(_drain_node, project_id, sid)
                    # MF-2026-05-10-017: also drain unenriched edge requests.
                    edge_pending = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM graph_events
                        WHERE project_id = ? AND snapshot_id = ?
                          AND event_type = 'edge_semantic_requested'
                          AND status = 'observed'
                          AND target_id NOT IN (
                            SELECT target_id FROM graph_events
                            WHERE project_id = ? AND snapshot_id = ?
                              AND event_type = 'edge_semantic_enriched'
                              AND status IN ('observed', 'proposed', 'accepted', 'materialized')
                          )
                        """,
                        (project_id, sid, project_id, sid),
                    ).fetchone()
                    en = int(edge_pending["n"] if edge_pending else 0)
                    if en > 0:
                        log.info(
                            "semantic_worker: startup catchup %s/%s edges=%d",
                            project_id, sid, en,
                        )
                        _get_executor().submit(_drain_edge, project_id, sid)
                    if n <= 0 and en <= 0:
                        continue
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001 - per-project failure shouldn't abort
                log.warning(
                    "semantic_worker: startup catchup failed for %s: %s",
                    project_id, exc,
                )
    except Exception as exc:  # noqa: BLE001 - listener must not raise
        log.exception("semantic_worker: on_governance_startup failed: %s", exc)


def register() -> None:
    """Subscribe listeners + run startup catchup. Idempotent."""
    global _registered
    if _registered:
        return
    try:
        from . import event_bus
        bus = event_bus.get_event_bus()
        bus.subscribe("semantic_job.enqueued", on_semantic_job_enqueued)
        bus.subscribe("system.startup", on_governance_startup)
        _registered = True
        log.info("semantic_worker: registered EventBus subscribers")
        # Fire startup catchup immediately (don't wait for system.startup
        # event publication — register() is called during startup itself).
        on_governance_startup({})
    except Exception as exc:  # noqa: BLE001 - registration failure should not block governance
        log.exception("semantic_worker: register failed: %s", exc)
