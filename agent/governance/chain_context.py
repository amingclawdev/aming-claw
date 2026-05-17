from __future__ import annotations

"""Chain Context Store — event-sourced task chain runtime context.

Provides in-memory runtime state for auto-chain task progression.
Events drive state updates and are persisted to chain_events table.
Crash recovery replays events from DB to rebuild in-memory state.

Architecture:
    EventBus → ChainContextStore (in-memory dict)
                    │
                    ├── read: O(1) dict lookup
                    ├── write: event-driven, threading.Lock
                    └── persist: queued single-writer INSERT to chain_events

Consistency boundary: single governance process.
"""

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Fields always preserved in full from task result
RESULT_CORE_FIELDS = [
    "target_files", "changed_files", "verification", "requirements",
    "acceptance_criteria", "test_report", "prd", "proposed_nodes",
    "summary", "related_nodes", "graph_delta", "test_files", "doc_impact",
    "skip_reasons", "criteria_results", "graph_delta_review",
]

# Role-based visibility: which stage types each role can see
ROLE_VISIBLE_STAGES = {
    "pm":          lambda s: s.task_type == "pm",
    "dev":         lambda s: s.task_type in ("pm", "dev"),
    "test":        lambda s: s.task_type in ("dev", "test"),
    "qa":          lambda s: s.task_type in ("test", "qa"),
    "merge":       lambda s: s.task_type in ("qa", "merge"),
    "coordinator": lambda s: True,
}

# Role-based visibility: which result_core fields each role can see
ROLE_RESULT_FIELDS = {
    "pm":          [],
    "dev":         ["target_files", "requirements", "acceptance_criteria",
                    "verification", "prd", "changed_files", "test_files",
                    "doc_impact", "skip_reasons", "proposed_nodes"],
    "test":        ["changed_files", "target_files"],
    "qa":          ["test_report", "changed_files", "target_files",
                    "requirements", "acceptance_criteria", "verification",
                    "test_files", "doc_impact", "proposed_nodes",
                    "graph_delta"],
    "merge":       ["changed_files", "test_report", "criteria_results",
                    "graph_delta_review", "doc_impact"],
    "coordinator": ["target_files", "changed_files", "summary",
                    "test_report", "related_nodes", "doc_impact",
                    "test_files"],
}

# Valid chain states
CHAIN_STATES = {
    "running", "blocked", "retrying", "completed",
    "failed", "cancelled", "archived",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int_metadata(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _chain_parallel_branch_meta(metadata: dict[str, Any]) -> dict[str, Any]:
    nested = metadata.get("parallel_branch") or metadata.get("parallel_branch_runtime")
    if isinstance(nested, dict):
        return {**metadata, **nested}
    return metadata


def build_parallel_branch_context_from_chain_payload(payload: dict[str, Any]):
    """Map a Chain task payload into BranchTaskRuntimeContext without running Chain."""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    branch_meta = _chain_parallel_branch_meta(metadata)
    branch_ref = str(branch_meta.get("branch_ref") or branch_meta.get("worktree_branch") or "").strip()
    if not branch_ref:
        return None

    from .parallel_branch_runtime import branch_context_from_chain_stage

    task_id = str(payload.get("task_id") or metadata.get("task_id") or "").strip()
    stage_task_id = str(branch_meta.get("stage_task_id") or task_id).strip()
    chain_id = str(
        branch_meta.get("chain_id")
        or metadata.get("chain_id")
        or branch_meta.get("root_task_id")
        or metadata.get("root_task_id")
        or payload.get("parent_task_id")
        or task_id
    ).strip()
    root_task_id = str(
        branch_meta.get("root_task_id")
        or metadata.get("root_task_id")
        or chain_id
        or task_id
    ).strip()
    retry_round = _int_metadata(branch_meta.get("retry_round", metadata.get("retry_round", 0)), 0)
    attempt_value = branch_meta.get("attempt")
    attempt = _int_metadata(attempt_value, retry_round + 1) if attempt_value is not None else None
    depends_on = branch_meta.get("depends_on") or branch_meta.get("hard_depends_on") or ()
    if isinstance(depends_on, str):
        depends_on = (depends_on,)
    elif isinstance(depends_on, list):
        depends_on = tuple(str(item) for item in depends_on if str(item or "").strip())
    elif not isinstance(depends_on, tuple):
        depends_on = ()

    return branch_context_from_chain_stage(
        project_id=str(payload.get("project_id") or branch_meta.get("project_id") or "").strip(),
        chain_id=chain_id,
        root_task_id=root_task_id,
        stage_task_id=stage_task_id,
        stage_type=str(branch_meta.get("stage_type") or payload.get("type") or "").strip(),
        retry_round=retry_round,
        task_id=task_id,
        batch_id=str(branch_meta.get("batch_id") or "").strip(),
        backlog_id=str(branch_meta.get("backlog_id") or metadata.get("bug_id") or "").strip(),
        agent_id=str(branch_meta.get("agent_id") or "").strip(),
        worker_id=str(branch_meta.get("worker_id") or "").strip(),
        status=str(branch_meta.get("status") or "running").strip(),
        ref_name=str(branch_meta.get("ref_name") or "main").strip(),
        branch_ref=branch_ref,
        worktree_id=str(branch_meta.get("worktree_id") or "").strip(),
        worktree_path=str(branch_meta.get("worktree_path") or "").strip(),
        base_commit=str(branch_meta.get("base_commit") or "").strip(),
        head_commit=str(branch_meta.get("head_commit") or "").strip(),
        target_head_commit=str(branch_meta.get("target_head_commit") or "").strip(),
        snapshot_id=str(branch_meta.get("snapshot_id") or "").strip(),
        projection_id=str(branch_meta.get("projection_id") or "").strip(),
        merge_queue_id=str(branch_meta.get("merge_queue_id") or "").strip(),
        merge_preview_id=str(branch_meta.get("merge_preview_id") or "").strip(),
        rollback_epoch=str(branch_meta.get("rollback_epoch") or "").strip(),
        replay_epoch=str(branch_meta.get("replay_epoch") or "").strip(),
        depends_on=depends_on,
        checkpoint_id=str(branch_meta.get("checkpoint_id") or "").strip(),
        replay_source=str(branch_meta.get("replay_source") or "").strip(),
        lease_id=str(branch_meta.get("lease_id") or "").strip(),
        lease_expires_at=str(branch_meta.get("lease_expires_at") or "").strip(),
        fence_token=str(branch_meta.get("fence_token") or "").strip(),
        attempt=attempt,
    )


def parallel_branch_event_payload_from_context(
    context,
    *,
    event_type: str,
    actor: str = "chain_adapter",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a compact event envelope shared by Chain and branch runtime."""
    return {
        "event_type": event_type,
        "project_id": context.project_id,
        "batch_id": context.batch_id,
        "backlog_id": context.backlog_id,
        "task_id": context.task_id,
        "chain_id": context.chain_id,
        "root_task_id": context.root_task_id,
        "stage_task_id": context.stage_task_id,
        "stage_type": context.stage_type,
        "retry_round": context.retry_round,
        "attempt": context.attempt,
        "branch_ref": context.branch_ref,
        "ref_name": context.ref_name,
        "worktree_id": context.worktree_id,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "head_commit": context.head_commit,
        "target_head_commit": context.target_head_commit,
        "snapshot_id": context.snapshot_id,
        "projection_id": context.projection_id,
        "merge_queue_id": context.merge_queue_id,
        "merge_preview_id": context.merge_preview_id,
        "rollback_epoch": context.rollback_epoch,
        "replay_epoch": context.replay_epoch,
        "depends_on": list(context.depends_on),
        "checkpoint_id": context.checkpoint_id,
        "replay_source": context.replay_source,
        "lease_id": context.lease_id,
        "lease_expires_at": context.lease_expires_at,
        "fence_token": context.fence_token,
        "actor": actor,
        "payload": payload or {},
        "created_at": _utc_iso(),
    }


def _extract_core(result: dict) -> dict:
    """Extract key fields from result for durable storage."""
    if not result:
        return {}
    core = {}
    for field in RESULT_CORE_FIELDS:
        val = result.get(field)
        if val is None and isinstance(result.get("prd"), dict):
            val = result["prd"].get(field)
        if val is not None:
            core[field] = val
    return core


def _persist_connection(project_id: str) -> sqlite3.Connection:
    """Dedicated connection for _persist_event with extended busy_timeout.

    Uses the same DB path resolution as db.get_connection (governance.db),
    but with timeout=60s and PRAGMA busy_timeout=60000ms to survive heavy
    WAL contention during auto-chain pipeline surges.
    """
    from .db import _project_db_path
    db_path = _project_db_path(project_id)
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _chain_event_spool_path(project_id: str) -> Path:
    from .db import _project_db_path
    return _project_db_path(project_id).parent / "chain_events_spool.jsonl"


class _ChainEventWriteQueue:
    """Single-writer queue for chain_events legacy persistence.

    The event-bus path must not synchronously compete with the active auto-chain
    transaction. Queueing keeps caller latency flat while a daemon writer drains
    events in FIFO order through one SQLite writer.
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._stats = {"enqueued": 0, "written": 0, "spooled": 0}

    def enqueue(self, record: dict) -> None:
        with self._lock:
            self._stats["enqueued"] += 1
        self._queue.put(record)

    def start(self) -> None:
        self._ensure_started()

    def stop_for_tests(self, timeout: float = 5.0) -> bool:
        thread = self._thread
        if not thread:
            return True
        self._queue.put(None)
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def stats(self) -> dict:
        with self._lock:
            out = dict(self._stats)
        out["pending"] = self._queue.unfinished_tasks
        return out

    def drain_for_tests(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _ensure_started(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="chain-events-writer",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            record = self._queue.get()
            if record is None:
                self._queue.task_done()
                return
            try:
                self._write(record)
                with self._lock:
                    self._stats["written"] += 1
            except Exception as exc:
                self._spool(record, exc)
                with self._lock:
                    self._stats["spooled"] += 1
            finally:
                self._queue.task_done()

    def _write(self, record: dict) -> None:
        own_conn = _persist_connection(record["project_id"])
        try:
            from .db import sqlite_write_lock

            with sqlite_write_lock():
                own_conn.execute(
                    "INSERT INTO chain_events "
                    "(root_task_id, task_id, event_type, payload_json, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        record["root_task_id"],
                        record["task_id"],
                        record["event_type"],
                        record["payload_json"],
                        record["ts"],
                    ),
                )
                own_conn.commit()
        finally:
            own_conn.close()

    def _spool(self, record: dict, exc: Exception) -> None:
        spool_path = _chain_event_spool_path(record["project_id"])
        spool_path.parent.mkdir(parents=True, exist_ok=True)
        spool_record = {
            **record,
            "spooled_at": _utc_iso(),
            "error": str(exc),
        }
        with spool_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(spool_record, ensure_ascii=False, default=str) + "\n")
        log.error(
            "chain_context: queued event spooled (%s/%s): %s",
            record.get("task_id", ""),
            record.get("event_type", ""),
            exc,
        )


_chain_event_write_queue = _ChainEventWriteQueue()


def _drain_chain_event_write_queue_for_tests(timeout: float = 5.0) -> bool:
    return _chain_event_write_queue.drain_for_tests(timeout=timeout)


def _chain_event_write_queue_stats() -> dict:
    return _chain_event_write_queue.stats()


def _start_chain_event_write_queue_for_tests() -> None:
    _chain_event_write_queue.start()


def _stop_chain_event_write_queue_for_tests(timeout: float = 5.0) -> bool:
    return _chain_event_write_queue.stop_for_tests(timeout=timeout)


class StageSnapshot:
    """One stage's context snapshot."""
    __slots__ = (
        "task_id", "task_type", "prompt", "result_core", "result_raw",
        "gate_reason", "attempt", "parent_task_id", "ts",
    )

    def __init__(self, task_id, task_type, prompt, parent_task_id=None):
        self.task_id = task_id
        self.task_type = task_type
        self.prompt = prompt
        self.result_core = None
        self.result_raw = None
        self.gate_reason = None
        self.attempt = 1
        self.parent_task_id = parent_task_id
        self.ts = _utc_iso()


class ChainContext:
    """Single chain's runtime context."""
    __slots__ = (
        "root_task_id", "project_id", "stages",
        "current_stage", "state", "created_at", "updated_at",
        "bug_id",  # OPT-BACKLOG-CH2: chain-level bug_id for retry inheritance
    )

    def __init__(self, root_task_id, project_id):
        self.root_task_id = root_task_id
        self.project_id = project_id
        self.stages = {}
        self.current_stage = None
        self.state = "running"
        self.created_at = _utc_iso()
        self.updated_at = self.created_at
        # OPT-BACKLOG-CH2: first-write-wins bug_id, populated by on_task_created
        # from payload.metadata.bug_id. Once set, never overwritten for the life
        # of the chain. Used by retry paths to fallback-fill missing metadata.bug_id.
        self.bug_id = None


class ChainContextStore:
    """Process-wide chain context store. Thread-safe."""

    def __init__(self):
        self._chains: dict[str, ChainContext] = {}
        self._task_to_root: dict[str, str] = {}
        self._lock = threading.Lock()
        self._recovering = False  # suppress DB writes during replay

    # ── Event handlers (called by EventBus subscribers) ──

    def on_task_created(self, payload: dict):
        """Handle task.created event."""
        task_id = payload.get("task_id", "")
        parent_id = payload.get("parent_task_id", "")
        task_type = payload.get("type", "task")
        prompt = payload.get("prompt", "")
        project_id = payload.get("project_id", "")
        # OPT-BACKLOG-CH2: extract bug_id from payload.metadata for first-write-wins
        _meta = payload.get("metadata") or {}
        incoming_bug_id = _meta.get("bug_id") if isinstance(_meta, dict) else None
        if incoming_bug_id is not None and not isinstance(incoming_bug_id, str):
            incoming_bug_id = None  # Only accept non-empty strings
        if not incoming_bug_id:
            incoming_bug_id = None

        with self._lock:
            # Skip if already registered (idempotent)
            if task_id in self._task_to_root:
                return

            if parent_id and parent_id in self._task_to_root:
                root_id = self._task_to_root[parent_id]
                chain = self._chains.get(root_id)
                if not chain:
                    return
            else:
                root_id = task_id
                chain = ChainContext(root_id, project_id)
                self._chains[root_id] = chain

            stage = StageSnapshot(task_id, task_type, prompt, parent_id)
            chain.stages[task_id] = stage
            chain.current_stage = task_id
            chain.updated_at = _utc_iso()
            self._task_to_root[task_id] = root_id

            # OPT-BACKLOG-CH2: first-write-wins bug_id at chain level
            if incoming_bug_id and chain.bug_id is None:
                chain.bug_id = incoming_bug_id

        self._persist_event(root_id, task_id, "task.created", payload, project_id)

    def on_task_completed(self, payload: dict):
        """Handle task.completed event."""
        task_id = payload.get("task_id", "")
        result = payload.get("result", {})
        project_id = payload.get("project_id", "")
        task_type = payload.get("type", "pm")
        archive_after_complete = False

        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                # Bootstrap: task was created externally (e.g. initial PM task via user API,
                # not by auto-chain), so task.created was never published for it.
                # Treat it as chain root so events are persisted going forward.
                root_id = task_id
                chain = ChainContext(root_id, project_id)
                stage = StageSnapshot(task_id, task_type, "", None)
                chain.stages[task_id] = stage
                self._chains[root_id] = chain
                self._task_to_root[task_id] = root_id
                log.debug("chain_context: bootstrapped root chain for %s (%s)", task_id, task_type)
            chain = self._chains.get(root_id)
            if not chain or task_id not in chain.stages:
                return
            stage = chain.stages[task_id]
            stage.result_core = _extract_core(result)
            stage.result_raw = result
            chain.updated_at = _utc_iso()
            # If this was a merge, mark chain completed
            if stage.task_type == "merge":
                chain.state = "completed"
                archive_after_complete = True

        # Persist with core only (not raw) to limit DB size
        persist_payload = {**payload, "result": _extract_core(result)}
        self._persist_event(root_id, task_id, "task.completed",
                            persist_payload, project_id)

        # Merge is the durable success boundary for the standard chain. Deploy is
        # best-effort service hygiene; archiving here prevents deploy failures from
        # keeping completed chains alive forever while chain_events retain audit data.
        if archive_after_complete:
            self.archive_chain(task_id, project_id)

    def on_gate_blocked(self, payload: dict):
        """Handle gate.blocked event. Append-only (audit)."""
        task_id = payload.get("task_id", "")
        reason = payload.get("reason", "")
        project_id = payload.get("project_id", "")

        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return
            chain = self._chains.get(root_id)
            if not chain:
                return
            chain.state = "blocked"
            if task_id in chain.stages:
                chain.stages[task_id].gate_reason = reason
            chain.updated_at = _utc_iso()

        self._persist_event(root_id, task_id, "gate.blocked",
                            payload, project_id)

    def on_task_retry(self, payload: dict):
        """Handle task.retry event."""
        retry_id = payload.get("task_id", "")
        original_id = payload.get("original_task_id", "")
        project_id = payload.get("project_id", "")

        with self._lock:
            root_id = self._task_to_root.get(original_id)
            if not root_id:
                return
            chain = self._chains.get(root_id)
            if not chain:
                return

            chain.state = "retrying"
            original = chain.stages.get(original_id)
            if original:
                stage = StageSnapshot(
                    retry_id, original.task_type,
                    original.prompt, original_id,
                )
                stage.attempt = original.attempt + 1
                chain.stages[retry_id] = stage
                chain.current_stage = retry_id
            self._task_to_root[retry_id] = root_id
            chain.updated_at = _utc_iso()

        self._persist_event(root_id, retry_id, "task.retry",
                            payload, project_id)

    def on_task_failed(self, payload: dict):
        """Handle task.failed event (retry exhausted). Auto-archives to release memory."""
        task_id = payload.get("task_id", "")
        project_id = payload.get("project_id", "")

        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return
            chain = self._chains.get(root_id)
            if not chain:
                return
            chain.state = "failed"
            chain.updated_at = _utc_iso()

        self._persist_event(root_id, task_id, "task.failed",
                            payload, project_id)

        # Auto-archive failed chains to prevent memory leak
        if not self._recovering:
            self.archive_chain(task_id, project_id)

    # ── Read API (from memory, O(1)) ──

    def get_chain(self, task_id: str, role: str = None) -> dict | None:
        """Get chain context for a task, optionally filtered by role."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return None
            chain = self._chains.get(root_id)
            if not chain:
                return None
            return self._serialize(chain, role)

    def get_original_prompt(self, task_id: str) -> str:
        """Get root task prompt (no role filter). For retry prompt building."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return ""
            chain = self._chains.get(root_id)
            if not chain:
                return ""
            root_stage = chain.stages.get(root_id)
            return root_stage.prompt if root_stage else ""

    def get_parent_result(self, task_id: str) -> dict | None:
        """Get parent stage result_core (no role filter). For prompt fallback."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return None
            chain = self._chains.get(root_id)
            if not chain:
                return None
            stage = chain.stages.get(task_id)
            if stage and stage.parent_task_id:
                parent = chain.stages.get(stage.parent_task_id)
                return parent.result_core if parent else None
            return None

    def get_state(self, task_id: str) -> str | None:
        """Get current chain state."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return None
            chain = self._chains.get(root_id)
            return chain.state if chain else None

    def get_bug_id(self, task_id: str) -> str | None:
        """Get chain-level bug_id for any task in chain (OPT-BACKLOG-CH2).

        Returns the chain's first-write-wins bug_id, or None if unknown/unset.
        Used by auto_chain retry paths as a fallback when retry metadata lost
        its inherited bug_id (e.g. in-process metadata dict dropped).
        """
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return None
            chain = self._chains.get(root_id)
            return chain.bug_id if chain else None

    def get_latest_test_report(self, task_id: str, project_id: str = "") -> dict | None:
        """Get the most recent test_report for the chain containing task_id.

        Memory-first: walks chain stages for the latest test stage with a
        non-empty test_report in result_core.

        DB fallback: queries chain_events if the chain is not in memory
        (TODO:B25-remove — remove DB path once chain_events emission is complete).

        Returns None if no test stage with test_report is found.
        """
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if root_id:
                chain = self._chains.get(root_id)
                if chain:
                    test_report = None
                    for stage in chain.stages.values():
                        if stage.task_type == "test" and stage.result_core:
                            tr = stage.result_core.get("test_report")
                            if tr:
                                test_report = tr
                    return test_report

        # TODO:B25-remove: DB fallback — only needed until chain_events is complete
        pid = project_id
        if not pid:
            return None
        try:
            from .db import get_connection
            conn = get_connection(pid)
            rows = conn.execute(
                "SELECT payload_json FROM chain_events "
                "WHERE root_task_id = ("
                "  SELECT root_task_id FROM chain_events WHERE task_id = ? LIMIT 1"
                ") AND event_type = 'task.completed' "
                "ORDER BY ts DESC",
                (task_id,),
            ).fetchall()
            conn.close()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    tr = payload.get("result", {}).get("test_report")
                    if tr:
                        return tr
                except Exception:
                    continue
        except Exception:
            log.debug(
                "chain_context: get_latest_test_report DB fallback failed for %s",
                task_id, exc_info=True,
            )
        return None

    def get_accumulated_changed_files(self, chain_id: str, project_id: str) -> list[str]:
        """Union of changed_files from all predecessor dev stages in the chain.

        Memory-first: reads result_core of dev stages already in memory.
        DB fallback: queries tasks table if the chain is not in memory or has no
        dev stages with result_core (TODO:B25-remove — remove after chain_events
        emission is complete).
        """
        result: set[str] = set()
        with self._lock:
            chain = self._chains.get(chain_id)
            if chain:
                for stage in chain.stages.values():
                    if stage.task_type == "dev" and stage.result_core:
                        result.update(stage.result_core.get("changed_files", []))
                if result:
                    return sorted(result)

        # TODO:B25-remove: DB fallback when chain not in memory or result_core missing
        if not project_id:
            return []
        try:
            from .db import get_connection
            conn = get_connection(project_id)
            rows = conn.execute(
                "SELECT result_json FROM tasks "
                "WHERE chain_id=? AND type='dev' AND status='succeeded'",
                (chain_id,),
            ).fetchall()
            conn.close()
            for row in rows:
                try:
                    result.update(
                        (json.loads(row["result_json"] or "{}")).get("changed_files", [])
                    )
                except Exception:
                    pass
        except Exception:
            log.debug(
                "chain_context: get_accumulated_changed_files DB fallback failed for %s",
                chain_id, exc_info=True,
            )
        return sorted(result)

    def get_retry_scope(
        self, chain_id: str, project_id: str, base_metadata: dict
    ) -> set[str]:
        """Complete allowed-file set for a retry dev task.

        = PM declared files (target_files + test_files + doc_impact.files)
          UNION all previous dev changed_files in this chain.

        Additive-only: never removes files that PM declared.
        """
        allowed: set[str] = set(base_metadata.get("target_files", []))
        allowed.update(base_metadata.get("test_files", []) or [])
        allowed.update((base_metadata.get("doc_impact") or {}).get("files", []) or [])
        allowed.update(self.get_accumulated_changed_files(chain_id, project_id))
        return allowed

    # ── Archive ──

    def archive_chain(self, task_id: str, project_id: str = ""):
        """Mark chain as archived, release memory."""
        with self._lock:
            root_id = self._task_to_root.get(task_id)
            if not root_id:
                return
            chain = self._chains.get(root_id)
            if not chain:
                return
            pid = project_id or chain.project_id
            chain.state = "archived"

        self._persist_event(root_id, task_id, "chain.archived",
                            {"root_task_id": root_id, "archived_at": _utc_iso()},
                            pid)

        # Release memory
        with self._lock:
            chain = self._chains.pop(root_id, None)
            if chain:
                for tid in list(chain.stages.keys()):
                    self._task_to_root.pop(tid, None)

        log.info("chain_context: archived chain %s (%d stages)",
                 root_id, len(chain.stages) if chain else 0)

    # ── Crash Recovery ──

    def recover_from_db(self, project_id: str):
        """Replay chain_events to rebuild in-memory state for active chains."""
        try:
            from .db import get_connection
            conn = get_connection(project_id)
        except Exception:
            log.warning("chain_context: cannot open DB for recovery (%s)", project_id)
            return

        try:
            rows = conn.execute(
                "SELECT root_task_id, task_id, event_type, payload_json, ts "
                "FROM chain_events "
                "WHERE root_task_id NOT IN ("
                "  SELECT root_task_id FROM chain_events "
                "  WHERE event_type = 'chain.archived'"
                ") ORDER BY ts"
            ).fetchall()
        except Exception:
            log.debug("chain_context: chain_events table not found, skip recovery")
            return
        finally:
            conn.close()

        if not rows:
            return

        self._recovering = True
        handlers = {
            "task.created": self.on_task_created,
            "task.completed": self.on_task_completed,
            "gate.blocked": self.on_gate_blocked,
            "task.retry": self.on_task_retry,
            "task.failed": self.on_task_failed,
        }
        # R6: Events that are persisted but have no in-memory handler.
        # They are preserved in chain_events and replayed without error.
        # PR-C adds graph.delta.validated, graph.delta.committed, graph.delta.failed,
        # and related_nodes.updated for gatekeeper graph delta commit flow.
        passthrough_events = {
            "graph.delta.proposed", "graph.delta.validated",
            "graph.delta.committed", "graph.delta.failed",
            "related_nodes.updated",
        }

        count = 0
        for row in rows:
            evt_type = row["event_type"]
            handler = handlers.get(evt_type)
            if handler:
                try:
                    payload = json.loads(row["payload_json"])
                    handler(payload)
                    count += 1
                except Exception:
                    log.debug("chain_context: skip bad event %s/%s",
                              row["task_id"], evt_type)
            elif evt_type in passthrough_events:
                # Preserved in DB, no in-memory state change needed
                count += 1

        self._recovering = False
        log.info("chain_context: recovered %d events, %d active chains for %s",
                 count, len(self._chains), project_id)

    # ── Serialization ──

    def _serialize(self, chain: ChainContext, role: str = None) -> dict:
        """Serialize chain to dict, optionally filtered by role."""
        stage_filter = ROLE_VISIBLE_STAGES.get(role, lambda s: True) if role else lambda s: True
        result_fields = ROLE_RESULT_FIELDS.get(role) if role else None

        stages = []
        for s in chain.stages.values():
            if not stage_filter(s):
                continue

            result_data = s.result_core or {}
            if result_fields is not None:
                result_data = {k: v for k, v in result_data.items()
                               if k in result_fields}

            stage_dict = {
                "task_id": s.task_id,
                "type": s.task_type,
                "attempt": s.attempt,
            }
            # Prompt visibility: dev/pm get full, coordinator gets truncated, others none
            if role in (None, "pm", "dev"):
                stage_dict["prompt"] = s.prompt
            elif role == "coordinator":
                stage_dict["prompt"] = s.prompt[:200] if s.prompt else ""

            if result_data:
                stage_dict["result_core"] = result_data

            # Gate reason: only visible to own stage (dev retry) and coordinator
            if s.gate_reason and (role in (None, "coordinator") or
                                   s.task_type == role):
                stage_dict["gate_reason"] = s.gate_reason

            stages.append(stage_dict)

        out = {
            "root_task_id": chain.root_task_id,
            "project_id": chain.project_id,
            "state": chain.state,
            "current_stage": chain.current_stage,
            "stage_count": len(chain.stages),
            "stages": stages,
            "created_at": chain.created_at,
            "updated_at": chain.updated_at,
        }
        # OPT-BACKLOG-CH2: include bug_id only when set (backward compatible)
        if chain.bug_id:
            out["bug_id"] = chain.bug_id
        return out

    # ── DB Persistence ──

    def _persist_event(self, root_task_id: str, task_id: str,
                       event_type: str, payload: dict, project_id: str,
                       conn: sqlite3.Connection | None = None):
        """Append event to chain_events table. Non-blocking, best-effort.

        MF-2026-04-24-001: conn param added to break the 3-conn contention in
        auto_chain.on_task_completed. When caller passes their open conn, this
        writes in the caller's transaction (caller controls commit). When conn
        is None (event-bus subscribers), falls back to the Z1 dedicated
        _persist_connection with 60s busy_timeout.

        See OPT-BACKLOG-AUTO-CHAIN-CONN-CONTENTION for root cause.
        """
        if self._recovering:
            return  # Don't write back to DB during replay

        log.info("_persist_event: entry event_type=%s task_id=%s root_task_id=%s payload_keys=%s caller_conn=%s",
                 event_type, task_id, root_task_id,
                 list(payload.keys()) if payload else [],
                 conn is not None)

        payload_json = json.dumps(payload, ensure_ascii=False, default=str)[:20000]
        ts = _utc_iso()

        # --- Fast path: caller-provided conn (MF-2026-04-24-001) ---
        if conn is not None:
            try:
                conn.execute(
                    "INSERT INTO chain_events "
                    "(root_task_id, task_id, event_type, payload_json, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (root_task_id, task_id, event_type, payload_json, ts),
                )
                # Do NOT commit here — caller owns the transaction.
            except Exception:
                log.error("chain_context: persist event (caller-conn) failed (%s/%s)",
                          task_id, event_type, exc_info=True)
            return

        # --- Event-bus path: enqueue for the single writer ---
        _chain_event_write_queue.enqueue({
            "project_id": project_id,
            "root_task_id": root_task_id,
            "task_id": task_id,
            "event_type": event_type,
            "payload_json": payload_json,
            "ts": ts,
        })

    def _project_id_for(self, root_task_id: str) -> str:
        chain = self._chains.get(root_task_id)
        return chain.project_id if chain else ""


# ── Singleton + EventBus registration ──

_store = ChainContextStore()


def get_store() -> ChainContextStore:
    return _store


def register_events():
    """Subscribe store handlers to EventBus. Call once on startup."""
    from . import event_bus
    _chain_event_write_queue.start()
    bus = event_bus.get_event_bus()
    bus.subscribe("task.created", _store.on_task_created)
    bus.subscribe("task.completed", _store.on_task_completed)
    bus.subscribe("gate.blocked", _store.on_gate_blocked)
    bus.subscribe("task.retry", _store.on_task_retry)
    bus.subscribe("task.failed", _store.on_task_failed)
    log.info("chain_context: registered EventBus subscribers")
