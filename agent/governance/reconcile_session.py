"""Reconcile session state machine (CR0a).

Pure module: NO DB writes, NO filesystem I/O, NO network at import time.
State machine: idle -> active -> finalizing -> finalized | rolled_back.
"""
from __future__ import annotations
import io, json, shutil, sqlite3, subprocess, tarfile, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

_GOVERNANCE_DIR = Path(__file__).resolve().parent
_SNAPSHOT_DIRNAME = "reconcile_snapshots"
_OVERLAY_FILENAME = "graph.rebase.overlay.json"
_GRAPH_FILENAME = "graph.json"


class SessionAlreadyActiveError(Exception):
    """Raised when an active/finalizing session already exists (CR0b -> HTTP 409)."""


class SessionClusterGateError(ValueError):
    """Raised when queued reconcile clusters are not safe to finalize."""

    def __init__(self, message: str, summary: Optional[dict] = None):
        super().__init__(message)
        self.summary = summary or {}


@dataclass
class ReconcileSession:
    project_id: str
    session_id: str
    run_id: Optional[str] = None
    status: str = "active"
    started_at: str = ""
    finalized_at: Optional[str] = None
    cluster_count_total: int = 0
    cluster_count_resolved: int = 0
    cluster_count_failed: int = 0
    bypass_gates: List[str] = field(default_factory=list)
    started_by: Optional[str] = None
    snapshot_path: Optional[str] = None
    snapshot_head_sha: Optional[str] = None


@dataclass
class SessionFinalizationResult:
    project_id: str
    session_id: str
    status: str
    finalized_at: str
    overlay_archived_to: Optional[str] = None


@dataclass
class SessionRollbackResult:
    project_id: str
    session_id: str
    status: str
    rolled_back_at: str
    snapshot_path: Optional[str] = None


@dataclass
class RestoreResult:
    project_id: str
    session_id: str
    graph_bytes: int
    node_state_rows: int


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _gov(base: Optional[Path] = None) -> Path:
    return Path(base) if base is not None else _GOVERNANCE_DIR

def _snapshot_dir(base: Optional[Path] = None) -> Path:
    return _gov(base) / _SNAPSHOT_DIRNAME

def _overlay_path(base: Optional[Path] = None) -> Path:
    return _gov(base) / _OVERLAY_FILENAME

def _graph_path(base: Optional[Path] = None) -> Path:
    return _gov(base) / _GRAPH_FILENAME


def _row_to_session(row: sqlite3.Row) -> ReconcileSession:
    raw = row["bypass_gates_json"] if row["bypass_gates_json"] is not None else "[]"
    try:
        bypass = list(json.loads(raw) or [])
    except (TypeError, ValueError):
        bypass = []
    return ReconcileSession(
        project_id=row["project_id"], session_id=row["session_id"],
        run_id=row["run_id"], status=row["status"],
        started_at=row["started_at"], finalized_at=row["finalized_at"],
        cluster_count_total=int(row["cluster_count_total"] or 0),
        cluster_count_resolved=int(row["cluster_count_resolved"] or 0),
        cluster_count_failed=int(row["cluster_count_failed"] or 0),
        bypass_gates=bypass, started_by=row["started_by"],
        snapshot_path=row["snapshot_path"], snapshot_head_sha=row["snapshot_head_sha"],
    )


def _git_head_sha(cwd: Optional[Path] = None) -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=str(cwd) if cwd else None, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def get_active_session(conn: sqlite3.Connection, project_id: str) -> Optional[ReconcileSession]:
    """Return the active or finalizing session for project_id, else None."""
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM reconcile_sessions WHERE project_id = ? "
        "AND status IN ('active','finalizing') LIMIT 1", (project_id,)).fetchone()
    return _row_to_session(row) if row else None


def start_session(conn: sqlite3.Connection, project_id: str, *,
        session_id: Optional[str] = None, run_id: Optional[str] = None,
        started_by: Optional[str] = None,
        bypass_gates: Optional[Sequence[str]] = None,
        full_rebase: bool = False,
        dropped_cluster_fingerprints: Optional[Sequence[str]] = None,
        governance_dir: Optional[Path] = None) -> ReconcileSession:
    """Insert a new active session; raise SessionAlreadyActiveError on conflict."""
    if full_rebase and not dropped_cluster_fingerprints:
        raise ValueError("full_rebase=True requires explicit dropped_cluster_fingerprints")
    sid = session_id or uuid.uuid4().hex
    now = _utcnow_iso()
    bypass_json = json.dumps(list(bypass_gates or []))
    try:
        conn.execute(
            "INSERT INTO reconcile_sessions (project_id, session_id, run_id, status, "
            "started_at, bypass_gates_json, started_by) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (project_id, sid, run_id, now, bypass_json, started_by))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        msg = str(exc).lower()
        if "idx_reconcile_sessions_one_active" in msg or "unique" in msg:
            raise SessionAlreadyActiveError(
                f"a reconcile session is already active for project {project_id!r}") from exc
        raise
    overlay = _overlay_path(governance_dir)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(json.dumps({"session_id": sid, "project_id": project_id}))
    return ReconcileSession(project_id=project_id, session_id=sid, run_id=run_id,
        status="active", started_at=now,
        bypass_gates=list(bypass_gates or []), started_by=started_by)


def transition_to_finalizing(conn: sqlite3.Connection, project_id: str,
        session_id: str) -> ReconcileSession:
    cur = conn.execute(
        "UPDATE reconcile_sessions SET status='finalizing' "
        "WHERE project_id=? AND session_id=? AND status='active'",
        (project_id, session_id))
    if cur.rowcount == 0:
        raise ValueError(f"no active session {session_id!r} for {project_id!r}")
    conn.commit()
    sess = get_active_session(conn, project_id)
    if sess is None:
        raise ValueError("session vanished after transition")
    return sess


def _cluster_gate_summary(
        conn: sqlite3.Connection, project_id: str, session_id: str) -> dict:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT run_id FROM reconcile_sessions WHERE project_id=? AND session_id=?",
        (project_id, session_id)).fetchone()
    run_id = row["run_id"] if row is not None else None
    try:
        from . import reconcile_deferred_queue as q

        summary = q.sync_session_counts(
            project_id, run_id=run_id, session_id=session_id, conn=conn)
    except Exception:
        return {"total": 0, "ready_for_finalize": True}
    return summary


def finalize_session(conn: sqlite3.Connection, project_id: str, session_id: str, *,
        governance_dir: Optional[Path] = None,
        enforce_cluster_completion: bool = True) -> SessionFinalizationResult:
    if enforce_cluster_completion:
        summary = _cluster_gate_summary(conn, project_id, session_id)
        if int(summary.get("total") or 0) > 0 and not summary.get("ready_for_finalize"):
            raise SessionClusterGateError(
                "reconcile clusters are not complete; finish cluster chain pass before finalize",
                summary=summary,
            )
    now = _utcnow_iso()
    cur = conn.execute(
        "UPDATE reconcile_sessions SET status='finalized', finalized_at=? "
        "WHERE project_id=? AND session_id=? AND status IN ('active','finalizing')",
        (now, project_id, session_id))
    if cur.rowcount == 0:
        raise ValueError(f"no in-flight session {session_id!r} for {project_id!r}")
    conn.commit()
    overlay = _overlay_path(governance_dir)
    archived: Optional[str] = None
    if overlay.exists():
        bak = overlay.with_suffix(overlay.suffix + ".bak")
        shutil.copy2(str(overlay), str(bak))
        overlay.unlink()
        archived = str(bak)
    return SessionFinalizationResult(project_id=project_id, session_id=session_id,
        status="finalized", finalized_at=now, overlay_archived_to=archived)


def rollback_session(conn: sqlite3.Connection, project_id: str, session_id: str, *,
        snapshot_path: Optional[Path] = None,
        governance_dir: Optional[Path] = None) -> SessionRollbackResult:
    now = _utcnow_iso()
    cur = conn.execute(
        "UPDATE reconcile_sessions SET status='rolled_back', finalized_at=? "
        "WHERE project_id=? AND session_id=? AND status IN ('active','finalizing')",
        (now, project_id, session_id))
    if cur.rowcount == 0:
        raise ValueError(f"no in-flight session {session_id!r} for {project_id!r}")
    conn.commit()
    if snapshot_path is None:
        snapshot_path = _snapshot_dir(governance_dir) / f"{session_id}.tar.gz"
    snapshot_path = Path(snapshot_path)
    if snapshot_path.exists():
        restore_snapshot(conn, project_id, session_id,
            snapshot_path=snapshot_path, governance_dir=governance_dir)
    overlay = _overlay_path(governance_dir)
    if overlay.exists():
        overlay.unlink()
    return SessionRollbackResult(project_id=project_id, session_id=session_id,
        status="rolled_back", rolled_back_at=now,
        snapshot_path=str(snapshot_path) if snapshot_path else None)


def is_gate_bypassed(session: Optional[ReconcileSession], gate_name: str) -> bool:
    if session is None or not gate_name:
        return False
    return gate_name in (session.bypass_gates or [])


def _dump_node_state(conn: sqlite3.Connection, project_id: str) -> str:
    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT project_id, node_id, verify_status, build_status, evidence_json, "
        "updated_by, updated_at, version FROM node_state "
        "WHERE project_id = ? ORDER BY node_id", (project_id,)).fetchall()
    parts = ["DELETE FROM node_state WHERE project_id = '"
             + project_id.replace("'", "''") + "';"]
    for r in rows:
        vals = [r["project_id"], r["node_id"], r["verify_status"], r["build_status"],
                r["evidence_json"], r["updated_by"], r["updated_at"], r["version"]]
        rendered = []
        for v in vals:
            if v is None:
                rendered.append("NULL")
            elif isinstance(v, int):
                rendered.append(str(v))
            else:
                rendered.append("'" + str(v).replace("'", "''") + "'")
        parts.append(
            "INSERT INTO node_state (project_id, node_id, verify_status, build_status, "
            "evidence_json, updated_by, updated_at, version) VALUES ("
            + ", ".join(rendered) + ");")
    return "\n".join(parts) + "\n"


def _verify_status_distribution(conn: sqlite3.Connection, project_id: str) -> dict:
    rows = conn.execute(
        "SELECT verify_status, COUNT(*) AS n FROM node_state "
        "WHERE project_id = ? GROUP BY verify_status", (project_id,)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["verify_status"]] = int(r["n"])
        except Exception:
            out[r[0]] = int(r[1])
    return out


def capture_snapshot(conn: sqlite3.Connection, project_id: str, session_id: str, *,
        governance_dir: Optional[Path] = None) -> Path:
    """Write reconcile_snapshots/{session_id}.tar.gz with graph/node_state/manifest."""
    snap_dir = _snapshot_dir(governance_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    out_path = snap_dir / f"{session_id}.tar.gz"
    graph_p = _graph_path(governance_dir)
    graph_bytes = graph_p.read_bytes() if graph_p.exists() else b"{}"
    sql_dump = _dump_node_state(conn, project_id).encode("utf-8")
    node_count = conn.execute(
        "SELECT COUNT(*) FROM node_state WHERE project_id = ?",
        (project_id,)).fetchone()[0]
    manifest = {
        "project_id": project_id, "session_id": session_id,
        "head_commit_sha": _git_head_sha(_gov(governance_dir)),
        "taken_at": _utcnow_iso(), "node_count": int(node_count or 0),
        "verify_status_distribution": _verify_status_distribution(conn, project_id),
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")

    def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(str(out_path), mode="w:gz") as tar:
        _add(tar, "graph.json", graph_bytes)
        _add(tar, "node_state.sql", sql_dump)
        _add(tar, "manifest.json", manifest_bytes)
    try:
        conn.execute(
            "UPDATE reconcile_sessions SET snapshot_path=?, snapshot_head_sha=? "
            "WHERE project_id=? AND session_id=?",
            (str(out_path), manifest["head_commit_sha"], project_id, session_id))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return out_path


def restore_snapshot(conn: sqlite3.Connection, project_id: str, session_id: str, *,
        snapshot_path: Optional[Path] = None,
        governance_dir: Optional[Path] = None) -> RestoreResult:
    if snapshot_path is None:
        snapshot_path = _snapshot_dir(governance_dir) / f"{session_id}.tar.gz"
    snapshot_path = Path(snapshot_path)
    graph_bytes = b""
    sql_text = ""
    with tarfile.open(str(snapshot_path), mode="r:gz") as tar:
        gm = tar.extractfile("graph.json")
        if gm is not None:
            graph_bytes = gm.read()
        sm = tar.extractfile("node_state.sql")
        if sm is not None:
            sql_text = sm.read().decode("utf-8")
    graph_p = _graph_path(governance_dir)
    graph_p.parent.mkdir(parents=True, exist_ok=True)
    graph_p.write_bytes(graph_bytes)
    if sql_text:
        conn.executescript(sql_text)
        conn.commit()
    rows = conn.execute(
        "SELECT COUNT(*) FROM node_state WHERE project_id = ?",
        (project_id,)).fetchone()[0]
    return RestoreResult(project_id=project_id, session_id=session_id,
        graph_bytes=len(graph_bytes), node_state_rows=int(rows or 0))


__all__ = [
    "ReconcileSession", "SessionFinalizationResult", "SessionRollbackResult",
    "RestoreResult", "SessionAlreadyActiveError", "SessionClusterGateError",
    "get_active_session", "start_session", "transition_to_finalizing",
    "finalize_session", "rollback_session", "is_gate_bypassed",
    "capture_snapshot", "restore_snapshot",
]
