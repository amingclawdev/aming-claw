"""Full-project file inventory for reconcile coverage accounting."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
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
INDEX_DOC_FILENAMES = {
    "README.md",
    "WORKFLOW.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
}
GENERATED_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", ".coverage", "governance.db",
}
GENERATED_EXTENSIONS = {".log", ".db", ".sqlite", ".sqlite3", ".pyc"}
GENERATED_DIR_MARKERS = {"generated", "__generated__", "gen"}
GENERATED_DIR_SUFFIXES = (".egg-info",)
GENERATED_PATH_PREFIXES = ("docs/dev/scratch/", "docs/dev/observer/logs/")
TEST_SUPPORT_FILENAMES = {"__init__.py", "conftest.py"}
TEST_SUPPORT_DIRS = {"fixtures", "fixture", "testdata", "test_data", "snapshots", "__snapshots__"}
DOC_ARCHIVE_PREFIXES = ("Ying_work/doc/",)
DOC_ARCHIVE_NAME_PREFIXES = (
    "audit-",
    "case-study-",
    "handoff-",
    "manual-fix-current-",
    "mf-",
    "next-session-prompt-",
    "observer-hotfix-record-",
    "postmortem-",
)
DOC_ARCHIVE_FILENAMES = {"MEMORY.md"}


@dataclass(frozen=True)
class FileInventoryRow:
    """A single file's reconcile processing state."""

    run_id: str
    path: str
    file_kind: str
    language: str
    sha256: str
    scan_status: str
    file_hash: str = ""
    size_bytes: int = 0
    last_scanned_commit: str = ""
    graph_status: str = ""
    mapped_node_ids: list[str] = field(default_factory=list)
    attached_node_ids: list[str] = field(default_factory=list)
    attachment_role: str = ""
    attachment_source: str = ""
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


def _file_hash(sha256: str) -> str:
    return f"sha256:{sha256}" if sha256 else ""


def _file_facts(path: Path) -> tuple[str, str, int]:
    digest = _sha256(path)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0
    return digest, _file_hash(digest), size_bytes


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
    if kind in {"doc", "index_doc"}:
        return "markdown" if suffix == ".md" else "text"
    return ""


def classify_file_kind(profile: ProjectProfile, rel_path: str) -> str:
    """Classify a project file into a governance inventory bucket."""
    rel = profile.normalize_relpath(rel_path)
    name = Path(rel).name
    suffix = Path(rel).suffix.lower()
    parts = {p.lower() for p in rel.split("/") if p}

    if (
        name in GENERATED_FILENAMES
        or suffix in GENERATED_EXTENSIONS
        or parts & GENERATED_DIR_MARKERS
        or any(part.endswith(GENERATED_DIR_SUFFIXES) for part in parts)
        or any(rel.startswith(prefix) for prefix in GENERATED_PATH_PREFIXES)
    ):
        return "generated"
    if name in CONFIG_FILENAMES or name.startswith("Dockerfile") or suffix in CONFIG_EXTENSIONS:
        return "config"
    if profile.is_test_path(rel):
        return "test"
    if is_index_doc_path(rel):
        return "index_doc"
    if profile.is_doc_path(rel) or suffix in DOC_EXTENSIONS:
        return "doc"
    if profile.is_production_source_path(rel):
        return "source"
    if suffix in SCRIPT_EXTENSIONS or rel.startswith("scripts/"):
        return "script"
    return "unknown"


def is_test_support_path(rel_path: str) -> bool:
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    name = Path(rel).name
    parts = {p.lower() for p in rel.split("/") if p}
    return name in TEST_SUPPORT_FILENAMES or bool(parts & TEST_SUPPORT_DIRS)


def is_archive_doc_path(rel_path: str) -> bool:
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    name = Path(rel).name
    if name in DOC_ARCHIVE_FILENAMES:
        return True
    if any(rel.startswith(prefix) for prefix in DOC_ARCHIVE_PREFIXES):
        return True
    if rel.startswith("docs/dev/") and name.lower().endswith((".md", ".rst", ".txt", ".adoc")):
        return name.lower().startswith(DOC_ARCHIVE_NAME_PREFIXES)
    return False


def is_index_doc_path(rel_path: str) -> bool:
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    name = Path(rel).name
    lower_name = name.lower()
    if name in INDEX_DOC_FILENAMES or lower_name in {"readme.md", "index.md"}:
        return True
    return False


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
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, list[str]],
    dict[str, dict[str, list[str]]],
]:
    primary_to_cluster: dict[str, str] = {}
    secondary_to_cluster: dict[str, str] = {}
    path_to_nodes: dict[str, set[str]] = {}
    path_to_roles: dict[str, dict[str, set[str]]] = {}

    def add_node_path(node_id: str, raw_path: Any, role: str) -> None:
        rel = normalize_relpath(project_root, raw_path)
        if not rel:
            return
        path_to_nodes.setdefault(rel, set()).add(node_id)
        path_to_roles.setdefault(rel, {}).setdefault(node_id, set()).add(role)

    def iter_paths(value: Any) -> list[Any]:
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return []

    for node in nodes or []:
        node_id = str(node.get("node_id") or node.get("id") or "")
        if not node_id:
            continue
        if node.get("primary_file"):
            add_node_path(node_id, node.get("primary_file"), "primary")
        role_keys = {
            "primary": "primary",
            "primary_files": "primary",
            "secondary": "doc",
            "secondary_files": "doc",
            "docs": "doc",
            "doc_files": "doc",
            "test": "test",
            "tests": "test",
            "test_files": "test",
            "config": "config",
            "config_files": "config",
        }
        for key, role in role_keys.items():
            for raw_path in iter_paths(node.get(key)):
                add_node_path(node_id, raw_path, role)
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        for raw_path in iter_paths(metadata.get("config_files")):
            add_node_path(node_id, raw_path, "config")

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
    return (
        primary_to_cluster,
        secondary_to_cluster,
        {path: sorted(node_ids) for path, node_ids in path_to_nodes.items()},
        {
            path: {node_id: sorted(roles) for node_id, roles in sorted(node_roles.items())}
            for path, node_roles in path_to_roles.items()
        },
    )


def _attachment_role(kind: str, node_roles: dict[str, list[str]] | None) -> str:
    roles: set[str] = set()
    for values in (node_roles or {}).values():
        roles.update(str(value or "") for value in values)
    if "primary" in roles:
        return "primary"
    if kind == "test" or "test" in roles:
        return "test"
    if kind in {"doc", "index_doc"} or "doc" in roles:
        return "doc"
    if kind == "config" or "config" in roles:
        return "config"
    if roles:
        return sorted(roles)[0]
    return ""


def build_file_inventory(
    *,
    project_root: str,
    run_id: str,
    nodes: Optional[List[dict[str, Any]]] = None,
    feature_clusters: Optional[List[dict[str, Any]]] = None,
    profile: Optional[ProjectProfile] = None,
    last_scanned_commit: str = "",
) -> List[dict[str, Any]]:
    """Build an auditable inventory for every non-excluded project file."""
    if profile is None:
        profile = discover_project_profile(project_root)
    nodes = nodes or []
    feature_clusters = feature_clusters or []
    primary_to_cluster, secondary_to_cluster, path_to_nodes, path_to_roles = _cluster_indexes(
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
        graph_status = "pending_decision"
        mapped_node_ids = path_to_nodes.get(rel, [])
        attached_node_ids = list(mapped_node_ids)
        attachment_role = _attachment_role(kind, path_to_roles.get(rel))
        attachment_source = "graph_node" if attached_node_ids else ""

        if rel in primary_to_cluster:
            scan_status = "clustered"
            cluster_id = primary_to_cluster[rel]
            candidate_node_id = mapped_node_ids[0] if mapped_node_ids else ""
            attached_to = candidate_node_id or cluster_id
            attachment_role = attachment_role or "primary"
            attachment_source = attachment_source or "feature_cluster"
            decision = "govern"
            graph_status = "mapped"
            reason = "primary source covered by symbol cluster"
        elif rel in secondary_to_cluster:
            scan_status = "secondary_attached"
            cluster_id = secondary_to_cluster[rel]
            candidate_node_id = mapped_node_ids[0] if mapped_node_ids else ""
            attached_to = candidate_node_id or cluster_id
            attachment_role = attachment_role or _attachment_role(kind, None) or "secondary"
            attachment_source = attachment_source or "feature_cluster"
            decision = "attach_to_node"
            graph_status = "attached"
            reason = "attached as test/doc consumer evidence"
        elif kind == "source" and mapped_node_ids:
            scan_status = "clustered"
            candidate_node_id = mapped_node_ids[0]
            attached_to = candidate_node_id
            decision = "govern"
            graph_status = "mapped"
            reason = "source covered by candidate graph fallback node"
        elif kind in {"test", "doc", "index_doc"} and mapped_node_ids:
            scan_status = "secondary_attached"
            candidate_node_id = mapped_node_ids[0]
            attached_to = candidate_node_id
            decision = "attach_to_node"
            graph_status = "attached"
            reason = f"{kind} file attached by graph node evidence"
        elif kind == "test" and is_test_support_path(rel):
            scan_status = "support"
            decision = "keep"
            graph_status = "support"
            reason = "test support file; audited as non-feature-specific support"
        elif kind in {"doc", "index_doc"} and is_archive_doc_path(rel):
            scan_status = "archive"
            decision = "keep"
            graph_status = "archive"
            reason = "historical or operator archive doc; audited as nonblocking"
        elif kind == "index_doc":
            scan_status = "index_asset"
            decision = "attach_to_index_wrapper"
            graph_status = "index_asset"
            reason = "index/navigation documentation; attach to project feature index"
        elif kind in {"source", "test", "doc"}:
            scan_status = "orphan"
            graph_status = "unmapped"
            reason = f"{kind} file not attached to any feature cluster"
        elif kind == "generated":
            scan_status = "ignored"
            decision = "ignore"
            graph_status = "ignored"
            reason = "generated or lock artifact"
        else:
            scan_status = "pending_decision"
            reason = f"{kind} file needs PM classification"

        abs_path = Path(project_root) / rel
        try:
            digest, file_hash, size_bytes = _file_facts(abs_path)
        except OSError:
            digest = ""
            file_hash = ""
            size_bytes = 0
            scan_status = "error"
            graph_status = "error"
            reason = "file could not be read"

        rows.append(FileInventoryRow(
            run_id=run_id,
            path=rel,
            file_kind=kind,
            language=_language_for(rel, kind),
            sha256=digest,
            file_hash=file_hash,
            size_bytes=size_bytes,
            last_scanned_commit=last_scanned_commit,
            graph_status=graph_status,
            mapped_node_ids=mapped_node_ids,
            attached_node_ids=attached_node_ids,
            attachment_role=attachment_role,
            attachment_source=attachment_source,
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


def upsert_file_inventory(
    conn,
    project_id: str,
    rows: Iterable[dict[str, Any]],
    *,
    replace_run: bool = True,
) -> int:
    """Persist inventory rows into ``reconcile_file_inventory``.

    File inventory is produced as a full-project snapshot for a run_id.  When
    ``replace_run`` is true, paths that existed for the same run_id but are no
    longer present in the latest scan are pruned so exclusions/profile changes
    cannot leave stale orphan blockers behind.
    """
    materialized_rows = list(rows)
    if replace_run:
        paths_by_run: dict[str, set[str]] = {}
        for row in materialized_rows:
            run_id = str(row.get("run_id") or "")
            path = str(row.get("path") or "")
            if run_id and path:
                paths_by_run.setdefault(run_id, set()).add(path)
        for run_id, current_paths in paths_by_run.items():
            existing = conn.execute(
                """
                SELECT path FROM reconcile_file_inventory
                WHERE project_id = ? AND run_id = ?
                """,
                (project_id, run_id),
            ).fetchall()
            stale_paths = {
                row["path"] if hasattr(row, "keys") else row[0]
                for row in existing
            } - current_paths
            for stale_path in sorted(stale_paths):
                conn.execute(
                    """
                    DELETE FROM reconcile_file_inventory
                    WHERE project_id = ? AND run_id = ? AND path = ?
                    """,
                    (project_id, run_id, stale_path),
                )
    count = 0
    for row in materialized_rows:
        mapped_node_ids = row.get("mapped_node_ids", [])
        if isinstance(mapped_node_ids, str):
            mapped_node_ids_raw = mapped_node_ids
        else:
            mapped_node_ids_raw = json.dumps(mapped_node_ids or [], ensure_ascii=False)
        attached_node_ids = row.get("attached_node_ids", [])
        if isinstance(attached_node_ids, str):
            attached_node_ids_raw = attached_node_ids
        else:
            attached_node_ids_raw = json.dumps(attached_node_ids or [], ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO reconcile_file_inventory
              (project_id, run_id, path, file_kind, language, sha256,
               file_hash, size_bytes, last_scanned_commit, graph_status, mapped_node_ids,
               attached_node_ids, attachment_role, attachment_source,
               scan_status, cluster_id, candidate_node_id, attached_to,
               reason, decision, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, run_id, path) DO UPDATE SET
              file_kind = excluded.file_kind,
              language = excluded.language,
              sha256 = excluded.sha256,
              file_hash = excluded.file_hash,
              size_bytes = excluded.size_bytes,
              last_scanned_commit = excluded.last_scanned_commit,
              graph_status = excluded.graph_status,
              mapped_node_ids = excluded.mapped_node_ids,
              attached_node_ids = excluded.attached_node_ids,
              attachment_role = excluded.attachment_role,
              attachment_source = excluded.attachment_source,
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
                row.get("file_hash") or _file_hash(str(row.get("sha256") or "")),
                int(row.get("size_bytes") or 0),
                row.get("last_scanned_commit", ""),
                row.get("graph_status", ""),
                mapped_node_ids_raw,
                attached_node_ids_raw,
                row.get("attachment_role", ""),
                row.get("attachment_source", ""),
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
               file_hash, size_bytes, last_scanned_commit, graph_status,
               mapped_node_ids, attached_node_ids, attachment_role,
               attachment_source, cluster_id, candidate_node_id, attached_to,
               reason, decision, updated_at
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
               file_hash, size_bytes, last_scanned_commit, graph_status,
               mapped_node_ids, attached_node_ids, attachment_role,
               attachment_source, cluster_id, candidate_node_id, attached_to,
               reason, decision, updated_at
        FROM reconcile_file_inventory
        WHERE project_id = ? AND run_id = ?
        """,
        (project_id, run_id),
    ).fetchall()
    return {
        "run_id": run_id,
        "rows": [_decode_inventory_row(dict(row)) for row in rows],
        "summary": summarize_file_inventory(_decode_inventory_row(dict(row)) for row in all_rows),
        "limit": safe_limit,
    }


def _decode_inventory_row(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("mapped_node_ids", "attached_node_ids"):
        raw = row.get(key)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                row[key] = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                row[key] = []
        elif not isinstance(raw, list):
            row[key] = []
    return row


__all__ = [
    "FileInventoryRow",
    "build_file_inventory",
    "classify_file_kind",
    "summarize_file_inventory",
    "query_file_inventory",
    "upsert_file_inventory",
]
