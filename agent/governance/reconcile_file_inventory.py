"""Full-project file inventory for reconcile coverage accounting."""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional

from agent.governance.project_profile import ProjectProfile, discover_project_profile


CONFIG_FILENAMES = {
    ".env", ".env.example", "Dockerfile", "Makefile", "Pipfile",
    "pyproject.toml", "requirements.txt", "package.json", "tsconfig.json",
    "Cargo.toml", "go.mod", "CMakeLists.txt", "compile_commands.json",
    ".gitignore", ".mcp.json", "VERSION",
}
CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
SCRIPT_EXTENSIONS = {".sh", ".bash", ".ps1", ".bat", ".cmd"}
DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}
GENERATED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", ".coverage", "governance.db",
}
GENERATED_EXTENSIONS = {".log", ".db", ".sqlite", ".sqlite3", ".pyc"}
GENERATED_DIR_MARKERS = {"generated", "__generated__", "gen"}


@dataclass(frozen=True)
class FileInventoryRow:
    """A single file's reconcile processing state."""

    run_id: str
    path: str
    file_kind: str
    language: str
    sha256: str
    scan_status: str
    cluster_id: str = ""
    candidate_node_id: str = ""
    attached_to: str = ""
    reason: str = ""
    decision: str = "pending"
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_relpath(project_root: str, path: Any) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, project_root)
    except ValueError:
        pass
    rel = raw.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.strip("/")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _language_for(path: str, kind: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".py", ".pyi"}:
        return "python"
    if suffix in {".js", ".jsx"}:
        return "javascript"
    if suffix in {".ts", ".tsx"}:
        return "typescript"
    if suffix == ".go":
        return "go"
    if suffix == ".rs":
        return "rust"
    if suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
        return "cpp"
    if suffix in {".sh", ".bash"}:
        return "shell"
    if suffix == ".ps1":
        return "powershell"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    if suffix == ".json":
        return "json"
    if suffix in {".toml"}:
        return "toml"
    if kind == "doc":
        return "markdown" if suffix == ".md" else "text"
    return ""


def classify_file_kind(profile: ProjectProfile, rel_path: str) -> str:
    """Classify a project file into a governance inventory bucket."""
    rel = profile.normalize_relpath(rel_path)
    name = Path(rel).name
    suffix = Path(rel).suffix.lower()
    parts = {p.lower() for p in rel.split("/") if p}

    if name in GENERATED_FILENAMES or suffix in GENERATED_EXTENSIONS or parts & GENERATED_DIR_MARKERS:
        return "generated"
    if profile.is_test_path(rel):
        return "test"
    if profile.is_doc_path(rel) or suffix in DOC_EXTENSIONS:
        return "doc"
    if profile.is_production_source_path(rel):
        return "source"
    if name in CONFIG_FILENAMES or name.startswith("Dockerfile") or suffix in CONFIG_EXTENSIONS:
        return "config"
    if suffix in SCRIPT_EXTENSIONS or rel.startswith("scripts/"):
        return "script"
    return "unknown"


def _walk_project_files(project_root: str, profile: ProjectProfile) -> Iterable[str]:
    root = Path(project_root)
    for dirpath, dirnames, filenames in os.walk(root):
        kept_dirs = []
        for dirname in dirnames:
            rel_dir = normalize_relpath(project_root, str(Path(dirpath) / dirname))
            if not profile.is_excluded_path(rel_dir):
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for fname in filenames:
            rel = normalize_relpath(project_root, str(Path(dirpath) / fname))
            if rel and not profile.is_excluded_path(rel):
                yield rel


def _cluster_indexes(
    project_root: str,
    nodes: List[dict[str, Any]],
    feature_clusters: List[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    primary_to_cluster: dict[str, str] = {}
    secondary_to_cluster: dict[str, str] = {}
    primary_to_node: dict[str, str] = {}

    for node in nodes or []:
        rel = normalize_relpath(project_root, node.get("primary_file"))
        node_id = str(node.get("node_id") or node.get("id") or "")
        if rel and node_id:
            primary_to_node[rel] = node_id

    for cluster in feature_clusters or []:
        cluster_id = str(cluster.get("cluster_fingerprint") or cluster.get("cluster_id") or "")
        for path in cluster.get("primary_files") or []:
            rel = normalize_relpath(project_root, path)
            if rel:
                primary_to_cluster.setdefault(rel, cluster_id)
        for path in cluster.get("secondary_files") or []:
            rel = normalize_relpath(project_root, path)
            if rel:
                secondary_to_cluster.setdefault(rel, cluster_id)
    return primary_to_cluster, secondary_to_cluster, primary_to_node


def build_file_inventory(
    *,
    project_root: str,
    run_id: str,
    nodes: Optional[List[dict[str, Any]]] = None,
    feature_clusters: Optional[List[dict[str, Any]]] = None,
    profile: Optional[ProjectProfile] = None,
) -> List[dict[str, Any]]:
    """Build an auditable inventory for every non-excluded project file."""
    if profile is None:
        profile = discover_project_profile(project_root)
    nodes = nodes or []
    feature_clusters = feature_clusters or []
    primary_to_cluster, secondary_to_cluster, primary_to_node = _cluster_indexes(
        project_root,
        nodes,
        feature_clusters,
    )
    now = utc_now()
    rows: list[FileInventoryRow] = []

    for rel in sorted(_walk_project_files(project_root, profile)):
        kind = classify_file_kind(profile, rel)
        cluster_id = ""
        candidate_node_id = ""
        attached_to = ""
        reason = ""
        decision = "pending"

        if rel in primary_to_cluster:
            scan_status = "clustered"
            cluster_id = primary_to_cluster[rel]
            candidate_node_id = primary_to_node.get(rel, "")
            attached_to = candidate_node_id or cluster_id
            decision = "govern"
            reason = "primary source covered by symbol cluster"
        elif rel in secondary_to_cluster:
            scan_status = "secondary_attached"
            cluster_id = secondary_to_cluster[rel]
            attached_to = cluster_id
            decision = "attach_to_node"
            reason = "attached as test/doc consumer evidence"
        elif kind in {"source", "test", "doc"}:
            scan_status = "orphan"
            reason = f"{kind} file not attached to any feature cluster"
        elif kind == "generated":
            scan_status = "ignored"
            decision = "ignore"
            reason = "generated or lock artifact"
        else:
            scan_status = "pending_decision"
            reason = f"{kind} file needs PM classification"

        abs_path = Path(project_root) / rel
        try:
            digest = _sha256(abs_path)
        except OSError:
            digest = ""
            scan_status = "error"
            reason = "file could not be read"

        rows.append(FileInventoryRow(
            run_id=run_id,
            path=rel,
            file_kind=kind,
            language=_language_for(rel, kind),
            sha256=digest,
            scan_status=scan_status,
            cluster_id=cluster_id,
            candidate_node_id=candidate_node_id,
            attached_to=attached_to,
            reason=reason,
            decision=decision,
            updated_at=now,
        ))

    return [row.to_dict() for row in rows]


def summarize_file_inventory(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return compact counts for operator dashboards and finalize gates."""
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    pending_paths: list[str] = []
    for row in rows:
        kind = str(row.get("file_kind") or "")
        status = str(row.get("scan_status") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        if status in {"orphan", "pending_decision", "error"}:
            pending_paths.append(str(row.get("path") or ""))
    return {
        "total": sum(by_kind.values()),
        "by_kind": dict(sorted(by_kind.items())),
        "by_status": dict(sorted(by_status.items())),
        "pending_decision_count": len([p for p in pending_paths if p]),
        "pending_decision_sample": [p for p in pending_paths if p][:25],
    }


def upsert_file_inventory(conn, project_id: str, rows: Iterable[dict[str, Any]]) -> int:
    """Persist inventory rows into ``reconcile_file_inventory``."""
    count = 0
    for row in rows:
        conn.execute(
            """
            INSERT INTO reconcile_file_inventory
              (project_id, run_id, path, file_kind, language, sha256,
               scan_status, cluster_id, candidate_node_id, attached_to,
               reason, decision, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, run_id, path) DO UPDATE SET
              file_kind = excluded.file_kind,
              language = excluded.language,
              sha256 = excluded.sha256,
              scan_status = excluded.scan_status,
              cluster_id = excluded.cluster_id,
              candidate_node_id = excluded.candidate_node_id,
              attached_to = excluded.attached_to,
              reason = excluded.reason,
              decision = excluded.decision,
              updated_at = excluded.updated_at
            """,
            (
                project_id,
                row.get("run_id", ""),
                row.get("path", ""),
                row.get("file_kind", ""),
                row.get("language", ""),
                row.get("sha256", ""),
                row.get("scan_status", ""),
                row.get("cluster_id", ""),
                row.get("candidate_node_id", ""),
                row.get("attached_to", ""),
                row.get("reason", ""),
                row.get("decision", "pending"),
                row.get("updated_at", ""),
            ),
        )
        count += 1
    return count


def query_file_inventory(
    conn,
    project_id: str,
    *,
    run_id: str = "",
    scan_status: str = "",
    file_kind: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """Query persisted file inventory rows with a compact summary."""
    if not run_id:
        latest = conn.execute(
            """
            SELECT run_id
            FROM reconcile_file_inventory
            WHERE project_id = ?
            ORDER BY updated_at DESC, run_id DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        run_id = latest["run_id"] if latest else ""
    if not run_id:
        return {"run_id": "", "rows": [], "summary": summarize_file_inventory([])}

    where = ["project_id = ?", "run_id = ?"]
    params: list[Any] = [project_id, run_id]
    if scan_status:
        where.append("scan_status = ?")
        params.append(scan_status)
    if file_kind:
        where.append("file_kind = ?")
        params.append(file_kind)

    safe_limit = max(1, min(int(limit or 200), 1000))
    rows = conn.execute(
        f"""
        SELECT run_id, path, file_kind, language, sha256, scan_status,
               cluster_id, candidate_node_id, attached_to, reason, decision,
               updated_at
        FROM reconcile_file_inventory
        WHERE {' AND '.join(where)}
        ORDER BY scan_status, file_kind, path
        LIMIT ?
        """,
        (*params, safe_limit),
    ).fetchall()
    all_rows = conn.execute(
        """
        SELECT run_id, path, file_kind, language, sha256, scan_status,
               cluster_id, candidate_node_id, attached_to, reason, decision,
               updated_at
        FROM reconcile_file_inventory
        WHERE project_id = ? AND run_id = ?
        """,
        (project_id, run_id),
    ).fetchall()
    return {
        "run_id": run_id,
        "rows": [dict(row) for row in rows],
        "summary": summarize_file_inventory(dict(row) for row in all_rows),
        "limit": safe_limit,
    }


__all__ = [
    "FileInventoryRow",
    "build_file_inventory",
    "classify_file_kind",
    "summarize_file_inventory",
    "query_file_inventory",
    "upsert_file_inventory",
]
