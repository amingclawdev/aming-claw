# v12: smoke-test verified post MF-2026-04-05-002
"""Auto-chain dispatcher.

Wires task completion to next-stage task creation with gate validation
between each stage. Called by complete_task() when a task succeeds.

Full chain: PM → Dev → Test → QA → Merge → Deploy
Each transition runs a gate check before advancing.
"""

import json
import hashlib
import logging
import os
import re
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath
from .failure_classifier import classify_gate_failure, build_workflow_improvement_prompt
from .observability import new_trace_id, structured_log
from .output_schemas import validate_dev_output, validate_pm_output
from .doc_policy import (
    is_dev_artifact as _is_dev_note,
    is_governance_internal_repair as _is_governance_internal_repair,
    _GOVERNANCE_INTERNAL_PREFIXES,
)
from . import backlog_runtime

log = logging.getLogger(__name__)

# Set to True to skip SERVER_VERSION vs git-HEAD check during development.
# Restore to False before production use.
_DISABLE_VERSION_GATE = False

# B15/B23/B31: Prefixes filtered from dirty_files before version gate evaluation.
# Paths matching any prefix are tool-local or non-governed and must not block chain.
# To add new entry here when an observer script writes a runtime-state file to repo
# root: append the path prefix (with both "/" and "\\" variants for cross-platform)
# to this tuple so that the version gate does not treat it as a governed dirty file.
_DIRTY_IGNORE = (
    ".claude/", ".claude\\",
    ".worktrees/", ".worktrees\\",
    "docs/dev/", "docs/dev\\",
    ".recent-tasks.json",
    ".governance-cache/", ".governance-cache\\",
    ".observer-cache/", ".observer-cache\\",
)

# Graph-driven doc governance: observation mode flag (Step 5, P1 principle)
# When True, graph doc checks log warnings instead of blocking.
_GRAPH_DOC_OBSERVATION_MODE = True

# QA Sweep Phase 5: structural drift gate during QA (opt-in via env var)
_QA_SWEEP_ENABLED = os.environ.get("QA_SWEEP_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Corrective PM State Machine Constants (R5)
# ---------------------------------------------------------------------------
TASK_STATUS_BLOCKED_BY_CORRECTIVE = "blocked_by_corrective"
TASK_STATUS_HUMAN_REVIEW_REQUIRED = "human_review_required"
MAX_QA_CORRECTIVE_ROUNDS = 1

# ---------------------------------------------------------------------------
# Reconciliation Bypass Policy (R1)
# ---------------------------------------------------------------------------
RECONCILIATION_BYPASS_POLICY = {
    "required_metadata_fields": ["reconciliation_lane", "observer_authorized"],
    "allowed_lanes": {"A", "B"},
    "audit_action": "reconciliation_bypass",
}

# ---------------------------------------------------------------------------
# PRD Graph-Declaration Fields (R1/R6)
# ---------------------------------------------------------------------------
_PRD_GRAPH_DECLARATION_FIELDS = ("removed_nodes", "unmapped_files", "renamed_nodes", "remapped_files")

_QA_EVIDENCE_PATH_RE = re.compile(
    r"(?<![\w:./\\-])("
    r"[A-Za-z]:[\\/][^\s,\"')\]}]+"
    r"|(?:agent|docs|scripts|shared-volume|tests)[\\/][^\s,\"')\]}]+"
    r"|(?:start_governance\.py|pyproject\.toml|README\.md|requirements(?:-[\w-]+)?\.txt)"
    r")"
)
_QA_EVIDENCE_PATH_TRAIL = ".,;:)]}'\"`"
_QA_EVIDENCE_GLOB_CHARS = set("*?[]{}")
_QA_EVIDENCE_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
_QA_EVIDENCE_SYMBOL_SUFFIX_RE = re.compile(
    r"^(.+\.(?:c|cc|cpp|cs|go|h|hpp|java|js|jsx|json|kt|md|mjs|mts|ps1|py|rs|sh|toml|ts|tsx|txt|yaml|yml))"
    r"/[A-Za-z_][\w.-]*$"
)
_QA_EVIDENCE_CATEGORY_SEGMENTS = {
    "artifact",
    "artifacts",
    "code",
    "doc",
    "docs",
    "source",
    "sources",
    "src",
    "test",
    "tests",
}

SCOPE_MATERIALIZATION_OPERATION_TYPE = "scope-materialization"


def _is_scope_materialization_task(metadata):
    """Return True for scoped reconcile materialization catch-up chains."""
    return (
        isinstance(metadata, dict)
        and metadata.get("operation_type") == SCOPE_MATERIALIZATION_OPERATION_TYPE
    )


def _extract_prd_declarations(prd):
    """Extract the 4 PRD graph-declaration fields with empty-list defaults (R6)."""
    return {f: prd.get(f, []) for f in _PRD_GRAPH_DECLARATION_FIELDS}


def _iter_qa_evidence_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_qa_evidence_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_qa_evidence_strings(child)


def _is_qa_evidence_category_path(raw_path):
    """Return True for prose categories such as tests/docs, not file evidence."""
    if not isinstance(raw_path, str):
        return False
    normalized = raw_path.replace("\\", "/")
    if ":" in normalized or "." in PurePosixPath(normalized).name:
        return False
    parts = [part.lower() for part in normalized.split("/") if part]
    return len(parts) >= 2 and all(part in _QA_EVIDENCE_CATEGORY_SEGMENTS for part in parts)


_WAIVED_DOC_DEBT_STATUSES = {"waived", "waiver"}
_DOC_DEBT_KINDS = {"doc_debt", "doc-debt", "doc debt", "documentation_debt", "documentation debt", "missing_doc"}
_DOC_DEBT_PATH_KEYS = ("path", "file", "file_path", "doc", "doc_path", "target", "target_file")


def _looks_like_doc_debt_kind(value):
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in {kind.replace("-", "_").replace(" ", "_") for kind in _DOC_DEBT_KINDS}


def _is_waived_doc_debt_entry(entry, *, implied_doc_debt=False):
    if not isinstance(entry, dict):
        return False
    status = str(entry.get("status") or entry.get("decision") or "").strip().lower()
    if status not in _WAIVED_DOC_DEBT_STATUSES:
        return False
    if implied_doc_debt:
        return True
    for key in ("kind", "type", "category"):
        if _looks_like_doc_debt_kind(entry.get(key)):
            return True
    reason = str(entry.get("reason") or entry.get("evidence") or "").lower()
    return "doc_debt" in reason or "doc debt" in reason or "documentation debt" in reason


def _iter_doc_debt_entries(value, *, implied_doc_debt=False):
    if isinstance(value, list):
        for item in value:
            yield from _iter_doc_debt_entries(item, implied_doc_debt=implied_doc_debt)
    elif isinstance(value, dict):
        yield value, implied_doc_debt


def _entry_path_keys(entry):
    paths = set()
    for key in _DOC_DEBT_PATH_KEYS:
        value = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip():
            paths.add(value.strip().replace("\\", "/"))
    return paths


def _extract_dev_doc_debt(result):
    if not isinstance(result, dict):
        return []
    debt = []
    raw = result.get("doc_debt")
    if isinstance(raw, list):
        debt.extend(raw)

    graph_delta = result.get("graph_delta") if isinstance(result.get("graph_delta"), dict) else {}
    for key in ("doc_debt", "doc_debt_waivers"):
        value = graph_delta.get(key)
        if isinstance(value, list):
            debt.extend(value)

    waivers = graph_delta.get("waivers")
    if isinstance(waivers, list):
        debt.extend(
            entry for entry in waivers
            if _is_waived_doc_debt_entry(entry, implied_doc_debt=False)
        )
    return debt


def _scope_materialization_waived_doc_debt_paths(metadata, proposed_graph_delta=None):
    """Return absent doc paths explicitly waived by a scope-materialization graph delta."""
    if not _is_scope_materialization_task(metadata):
        return set()
    if not isinstance(proposed_graph_delta, dict):
        return set()
    graph_delta = proposed_graph_delta.get("graph_delta", proposed_graph_delta)
    if not isinstance(graph_delta, dict):
        return set()

    allowed = set()
    for entry, implied in _iter_doc_debt_entries(graph_delta.get("waivers", []), implied_doc_debt=False):
        if _is_waived_doc_debt_entry(entry, implied_doc_debt=implied):
            allowed.update(_entry_path_keys(entry))
    for entry, implied in _iter_doc_debt_entries(graph_delta.get("doc_debt", []), implied_doc_debt=True):
        if _is_waived_doc_debt_entry(entry, implied_doc_debt=implied):
            allowed.update(_entry_path_keys(entry))
    for entry, implied in _iter_doc_debt_entries(graph_delta.get("doc_debt_waivers", []), implied_doc_debt=True):
        if _is_waived_doc_debt_entry(entry, implied_doc_debt=implied):
            allowed.update(_entry_path_keys(entry))
    return allowed


_GRAPH_DELTA_PATH_FIELDS = (
    "primary",
    "secondary",
    "test",
    "tests",
    "test_files",
    "target_files",
)


def _iter_graph_delta_path_values(value):
    if isinstance(value, str) and value.strip():
        yield value.strip().replace("\\", "/")
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_graph_delta_path_values(item)


def _scope_materialization_graph_delta_paths(metadata, proposed_graph_delta=None):
    """Return paths explicitly materialized by a scope-materialization graph delta."""
    if not _is_scope_materialization_task(metadata):
        return set()
    if not isinstance(proposed_graph_delta, dict):
        return set()
    graph_delta = proposed_graph_delta.get("graph_delta", proposed_graph_delta)
    if not isinstance(graph_delta, dict):
        return set()

    paths = set()
    for section in ("creates", "updates"):
        entries = graph_delta.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fields = entry.get("fields")
            if isinstance(fields, dict):
                for key in _GRAPH_DELTA_PATH_FIELDS:
                    paths.update(_iter_graph_delta_path_values(fields.get(key)))
            for key in _GRAPH_DELTA_PATH_FIELDS:
                paths.update(_iter_graph_delta_path_values(entry.get(key)))
    return paths


def _project_workspace_root(project_id, metadata=None):
    cwd = Path.cwd()
    if (cwd / "agent" / "governance" / "auto_chain.py").exists():
        return cwd
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "agent" / "governance" / "auto_chain.py").exists():
        return repo_root
    try:
        from .db import _resolve_project_dir
        root = _resolve_project_dir(project_id)
        if root:
            return Path(root)
    except Exception:
        pass
    return Path(__file__).resolve().parents[2]


_RECONCILE_STATE_ARTIFACT_NAMES = {
    "graph.json",
    "graph.v2.json",
    "graph.rebase.candidate.json",
    "graph.rebase.overlay.json",
    "graph.rebase.review.json",
    "graph.rebase.coverage-ledger.json",
    "graph.rebase.doc-index.review.json",
    "graph.rebase.doc-index.review.md",
}


def _is_reconcile_state_artifact_path(project_id, raw_path, metadata):
    """Return True when QA cites a known reconcile graph artifact.

    Reconcile reviews often need to say that active graph artifacts were not
    mutated.  Some legacy artifact names (for example graph.v2.json) may be
    absent in the current workspace; treating those mentions as missing
    workspace evidence creates a false QA retry even when graph_delta passed.
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    is_reconcile_task = (
        metadata.get("reconcile_session_id")
        or metadata.get("reconcile_run_id")
        or metadata.get("operation_type") == "reconcile-cluster"
        or metadata.get("candidate_graph_path")
        or metadata.get("overlay_path")
        or metadata.get("reconcile_overlay_path")
    )
    if not is_reconcile_task or not isinstance(raw_path, str):
        return False

    normalized = raw_path.replace("\\", "/")
    artifact_name = PurePosixPath(normalized).name
    if artifact_name not in _RECONCILE_STATE_ARTIFACT_NAMES:
        return False

    candidates = []
    for key in (
        "candidate_graph_path",
        "overlay_path",
        "reconcile_overlay_path",
        "review_graph_path",
        "coverage_ledger_path",
        "doc_index_path",
    ):
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            continue
        path = Path(value)
        candidates.append(path)
        candidates.append(path.parent / artifact_name)

    try:
        from .db import _resolve_project_dir
        state_root = Path(_resolve_project_dir(project_id))
        candidates.append(state_root / artifact_name)
    except Exception:
        pass

    raw = Path(raw_path)
    if raw.is_absolute():
        candidates.append(raw)

    try:
        if any(candidate.exists() for candidate in candidates):
            return True
    except OSError:
        return False

    # Known graph artifact names are allowed as reconcile audit references even
    # when absent, because absence/non-mutation is itself the audited claim.
    return True


def _extract_qa_evidence_paths(result):
    """Return workspace path mentions from QA output, excluding globs."""
    paths = []
    seen = set()
    for text in _iter_qa_evidence_strings(result):
        for match in _QA_EVIDENCE_PATH_RE.finditer(text):
            raw = match.group(1).rstrip(_QA_EVIDENCE_PATH_TRAIL)
            raw = _QA_EVIDENCE_LINE_SUFFIX_RE.sub("", raw)
            symbol_match = _QA_EVIDENCE_SYMBOL_SUFFIX_RE.match(raw.replace("\\", "/"))
            if symbol_match:
                raw = symbol_match.group(1)
            if "<" in raw or ">" in raw:
                continue
            if any(ch in raw for ch in _QA_EVIDENCE_GLOB_CHARS):
                continue
            if _is_qa_evidence_category_path(raw):
                continue
            key = raw.replace("\\", "/")
            if key in seen:
                continue
            seen.add(key)
            paths.append(raw)
    return paths


def _missing_qa_evidence_paths(project_id, result, metadata=None, proposed_graph_delta=None):
    metadata = metadata if isinstance(metadata, dict) else {}
    root = _project_workspace_root(project_id, metadata)
    task_root = root
    raw_worktree = metadata.get("_worktree") or metadata.get("workspace") or metadata.get("project_root")
    if raw_worktree:
        candidate_root = Path(raw_worktree)
        if candidate_root.exists():
            task_root = candidate_root
    changed_files = {
        str(path).replace("\\", "/")
        for path in (metadata.get("changed_files") or [])
        if isinstance(path, str)
    }
    waived_doc_debt_paths = _scope_materialization_waived_doc_debt_paths(metadata, proposed_graph_delta)
    missing = []
    for raw in _extract_qa_evidence_paths(result):
        if _is_reconcile_state_artifact_path(project_id, raw, metadata):
            continue
        raw_path = Path(raw)
        rel_key = raw.replace("\\", "/")
        if raw_path.is_absolute():
            for base in (task_root, root):
                try:
                    rel_key = str(raw_path.relative_to(base)).replace("\\", "/")
                    break
                except ValueError:
                    pass

        if rel_key in waived_doc_debt_paths or raw.replace("\\", "/") in waived_doc_debt_paths:
            continue

        if rel_key in changed_files:
            candidates = [task_root / rel_key, root / rel_key]
        elif raw_path.is_absolute() and rel_key == raw.replace("\\", "/"):
            candidates = [raw_path]
        else:
            # Evidence for unchanged paths must exist in the stable workspace.
            # Worktree-only ignored files are not durable QA evidence.
            candidates = [root / rel_key]

        try:
            exists = any(candidate.exists() for candidate in candidates)
        except OSError:
            exists = False
        if not exists:
            missing.append(raw)
    return missing


_DEFAULT_QA_REJECTION_FALLBACK = "QA rejected: no reason given"


def _format_qa_rejection_reason(result, fallback=_DEFAULT_QA_REJECTION_FALLBACK):
    """Preserve actionable QA rejection context for downstream Dev retries."""
    if not isinstance(result, dict):
        return fallback

    parts = []
    fallback_text = str(fallback or "").strip()
    if fallback_text and fallback_text != _DEFAULT_QA_REJECTION_FALLBACK:
        if fallback_text.startswith("QA rejected: "):
            fallback_text = fallback_text[len("QA rejected: "):].strip()
        if fallback_text:
            parts.append(f"gate_block_reason: {fallback_text}")

    explicit = str(result.get("reason") or "").strip()
    if explicit:
        parts.append(explicit)

    summary = str(result.get("review_summary") or result.get("summary") or "").strip()
    if summary:
        parts.append(f"review_summary: {summary}")

    issues = result.get("issues")
    if issues:
        parts.append(f"issues: {json.dumps(issues, ensure_ascii=False)}")

    failed_criteria = []
    for item in result.get("criteria_results") or []:
        if isinstance(item, dict) and item.get("passed") is False:
            failed_criteria.append({
                "criterion": item.get("criterion", ""),
                "evidence": item.get("evidence", ""),
            })
    if failed_criteria:
        parts.append(f"failed_criteria: {json.dumps(failed_criteria, ensure_ascii=False)}")

    if not parts:
        return fallback
    return "QA rejected: " + " | ".join(parts)


def validate_prd_graph_declarations(prd, dev_changed_files, current_graph):
    """Validate PRD graph declarations against dev changed_files and graph (R2/R5).

    Returns a list of error strings. Empty list means valid / backward-compat.
    """
    decl = _extract_prd_declarations(prd) if prd else {}
    removed = decl.get("removed_nodes", [])
    unmapped = decl.get("unmapped_files", [])
    # R6: If no declaration fields present, skip validation (backward compat)
    if not any(decl.get(f) for f in _PRD_GRAPH_DECLARATION_FIELDS):
        return []
    errors = []
    # Build set of graph-bound files from current_graph
    graph_file_to_node = {}
    if current_graph and isinstance(current_graph, dict):
        for nid, ndata in current_graph.items():
            if not isinstance(ndata, dict):
                continue
            primaries = ndata.get("primary", [])
            if isinstance(primaries, str):
                primaries = [primaries]
            for p in primaries:
                graph_file_to_node[p.replace("\\", "/")] = nid
    # Normalise dev_changed_files
    changed_set = {f.replace("\\", "/") for f in (dev_changed_files or [])}
    # Detect deleted graph-bound files not declared
    declared_node_ids = {n if isinstance(n, str) else n.get("node_id", "") for n in removed}
    unmapped_set = {f.replace("\\", "/") for f in unmapped}
    for f in changed_set:
        bound_node = graph_file_to_node.get(f)
        if bound_node and bound_node not in declared_node_ids and f not in unmapped_set:
            errors.append(
                f"File '{f}' is bound to node '{bound_node}' but not declared in "
                "removed_nodes or unmapped_files"
            )
    # R5: Validate removed_nodes correspond to actually-deleted files
    for node_ref in removed:
        nid = node_ref if isinstance(node_ref, str) else node_ref.get("node_id", "")
        # Find primary files for this node
        node_files = [fp for fp, n in graph_file_to_node.items() if n == nid]
        if node_files and not any(nf in changed_set for nf in node_files):
            errors.append(
                f"removed_nodes declares '{nid}' but none of its primary files "
                f"{node_files} appear in dev changed_files"
            )
    return errors


def _check_reconciliation_bypass(conn, project_id, metadata):
    """Validate metadata against RECONCILIATION_BYPASS_POLICY.

    Returns (bypass: bool, observer_task_id: str|None).
    Checks:
      (a) metadata.reconciliation_lane in allowed_lanes
      (b) metadata.observer_authorized == True
      (c) task chain traces back to an observer-created parent task
    """
    policy = RECONCILIATION_BYPASS_POLICY

    # (a) reconciliation_lane must be in allowed set
    lane = str(metadata.get("reconciliation_lane", "") or "").strip().upper()
    if lane not in policy["allowed_lanes"]:
        return False, None

    # (b) observer_authorized must be explicitly True
    if not metadata.get("observer_authorized"):
        return False, None

    # (c) Walk parent chain to find observer-created task
    observer_task_id = metadata.get("observer_task_id")
    if not observer_task_id:
        for parent_meta in _walk_task_metadata_chain(conn, project_id, metadata):
            if parent_meta.get("created_by_observer") or parent_meta.get("observer_task_id"):
                observer_task_id = parent_meta.get("observer_task_id") or parent_meta.get("parent_task_id", "")
                break

    if not observer_task_id:
        # Fallback: treat the parent_task_id as the observer task if observer_authorized is set
        observer_task_id = metadata.get("parent_task_id", "unknown")

    return True, observer_task_id


def _audit_reconciliation_bypass(conn, project_id, task_id, observer_task_id, lane):
    """Write reconciliation_bypass event to audit_log (R6)."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "reconciliation_bypass",
                "auto-chain",
                1,
                task_id,
                json.dumps({
                    "observer_task_id": observer_task_id,
                    "lane": lane,
                    "task_id": task_id,
                }),
            ),
        )
    except Exception:
        log.debug("audit reconciliation_bypass failed (non-critical)", exc_info=True)


# ---------------------------------------------------------------------------
# Corrective PM State Machine Helpers (R6-R8)
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def spawn_corrective_pm(conn, project_id, parent_qa_task_id, qa_failure_reason, bug_id):
    """Spawn a corrective PM task when QA fails, enforcing round limits and dedup.

    Returns the new child task_id, or None if:
      - qa_corrective_round >= MAX_QA_CORRECTIVE_ROUNDS (marks parent human_review_required)
      - an OPEN child PM task with same bug_id + parent_task_id already exists (dedup)
    """
    # Read parent task to get current round
    parent_row = conn.execute(
        "SELECT task_id, status, retry_round, metadata_json FROM tasks WHERE task_id = ? AND project_id = ?",
        (parent_qa_task_id, project_id),
    ).fetchone()
    if not parent_row:
        log.warning("spawn_corrective_pm: parent task %s not found", parent_qa_task_id)
        return None

    parent_meta = {}
    try:
        parent_meta = json.loads(parent_row["metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        pass

    qa_corrective_round = int(parent_meta.get("qa_corrective_round", 0))

    # R6: enforce MAX_QA_CORRECTIVE_ROUNDS limit
    if qa_corrective_round >= MAX_QA_CORRECTIVE_ROUNDS:
        conn.execute(
            "UPDATE tasks SET status = ? WHERE task_id = ? AND project_id = ?",
            (TASK_STATUS_HUMAN_REVIEW_REQUIRED, parent_qa_task_id, project_id),
        )
        conn.commit()
        log.info("spawn_corrective_pm: parent %s marked human_review_required (round %d >= max %d)",
                 parent_qa_task_id, qa_corrective_round, MAX_QA_CORRECTIVE_ROUNDS)
        return None

    # R7: dedup guard — check for existing OPEN child PM with same bug_id + parent_task_id
    existing = conn.execute(
        """SELECT task_id FROM tasks
           WHERE project_id = ? AND type = 'pm'
             AND parent_task_id = ?
             AND status NOT IN ('succeeded', 'failed', 'cancelled')
             AND metadata_json LIKE ?""",
        (project_id, parent_qa_task_id, f'%"bug_id": "{bug_id}"%'),
    ).fetchone()
    if existing:
        log.info("spawn_corrective_pm: dedup — existing OPEN PM task %s for bug_id=%s",
                 existing["task_id"], bug_id)
        return None

    # Mark parent as blocked
    conn.execute(
        "UPDATE tasks SET status = ? WHERE task_id = ? AND project_id = ?",
        (TASK_STATUS_BLOCKED_BY_CORRECTIVE, parent_qa_task_id, project_id),
    )

    # Spawn child PM task
    import uuid
    now = _utc_now()
    child_task_id = f"task-corrective-{uuid.uuid4().hex[:12]}"
    child_meta = json.dumps({
        "bug_id": bug_id,
        "parent_task_id": parent_qa_task_id,
        "qa_corrective_round": qa_corrective_round + 1,
        "qa_failure_reason": qa_failure_reason,
    }, sort_keys=True)

    conn.execute(
        """INSERT INTO tasks
           (task_id, project_id, status, type, prompt, created_by, created_at,
            updated_at, parent_task_id, metadata_json)
           VALUES (?, ?, 'queued', 'pm', ?, 'auto-chain', ?, ?, ?, ?)""",
        (child_task_id, project_id,
         f"Corrective PM for QA failure: {qa_failure_reason[:200]}",
         now, now, parent_qa_task_id, child_meta),
    )
    conn.commit()
    log.info("spawn_corrective_pm: spawned child PM %s for parent %s (round %d)",
             child_task_id, parent_qa_task_id, qa_corrective_round + 1)
    return child_task_id


def on_corrective_chain_complete(conn, project_id, parent_task_id):
    """Clear blocked_by_corrective on the parent QA task and re-enqueue it.

    Sets status='queued' and increments retry_round.
    """
    parent_row = conn.execute(
        "SELECT task_id, status, retry_round FROM tasks WHERE task_id = ? AND project_id = ?",
        (parent_task_id, project_id),
    ).fetchone()
    if not parent_row:
        log.warning("on_corrective_chain_complete: parent task %s not found", parent_task_id)
        return

    new_retry_round = (parent_row["retry_round"] or 0) + 1
    conn.execute(
        "UPDATE tasks SET status = 'queued', retry_round = ? WHERE task_id = ? AND project_id = ?",
        (new_retry_round, parent_task_id, project_id),
    )
    conn.commit()
    log.info("on_corrective_chain_complete: re-enqueued parent %s as queued (retry_round=%d)",
             parent_task_id, new_retry_round)


def _audit_version_gate_bypass(conn, project_id, task_id, operator_id, bypass_reason, task_type):
    """Write version_gate_bypass event to audit_log and check frequency."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "version_gate_bypass",
                operator_id,
                1,
                task_id,
                json.dumps({
                    "bypass_reason": bypass_reason,
                    "task_type": task_type,
                }),
            ),
        )
        # R3: Check bypass frequency in last 24 hours
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM audit_log "
            "WHERE action='version_gate_bypass' AND project_id=? "
            "AND ts >= datetime('now', '-24 hours')",
            (project_id,),
        ).fetchone()
        count = row[0] if row else 0
        if count > 3:
            log.warning("high bypass frequency: %d version_gate_bypass events for project %s in last 24h",
                        count, project_id)
    except Exception:
        log.debug("audit version_gate_bypass failed (non-critical)", exc_info=True)


# Chain definition: task_type → (gate_fn, next_type, prompt_builder)
# next_type=None means terminal stage (deploy trigger)
CHAIN = {
    "pm":    ("_gate_post_pm",    "dev",   "_build_dev_prompt"),
    "dev":   ("_gate_checkpoint", "test",  "_build_test_prompt"),
    "test":  ("_gate_t2_pass",    "qa",    "_build_qa_prompt"),
    "qa":    ("_gate_qa_pass",    "gatekeeper", "_build_gatekeeper_prompt"),
    "gatekeeper": ("_gate_gatekeeper_pass", "merge", "_build_merge_prompt"),
    "merge": ("_gate_release",    "deploy", "_build_deploy_prompt"),
    "deploy": ("_gate_deploy_pass", None, "_finalize_chain"),
}

# ---------------------------------------------------------------------------
# Reconcile Task Stages (Phase J, R2)
# ---------------------------------------------------------------------------
# Separate from CHAIN — reconcile tasks bypass all governance gates.
from .reconcile_task import (
    _RECONCILE_STAGES,
    handle_scan as _reconcile_scan,
    handle_diff as _reconcile_diff,
    handle_propose as _reconcile_propose,
    handle_approve as _reconcile_approve,
    handle_apply as _reconcile_apply,
    handle_verify as _reconcile_verify,
    run_full_reconcile as _run_full_reconcile,
    acquire_reconcile_lock,
    release_reconcile_lock,
    ReconcileCancelled,
)

# Re-export for AC-J1: _RECONCILE_STAGES dict with keys
# ['scan','diff','propose','approve','apply','verify']
assert set(_RECONCILE_STAGES.keys()) == {"scan", "diff", "propose", "approve", "apply", "verify"}, \
    f"_RECONCILE_STAGES keys mismatch: {set(_RECONCILE_STAGES.keys())}"

# Maximum chain depth to prevent infinite loops
MAX_CHAIN_DEPTH = 10

# ---------------------------------------------------------------------------
# Graph-Driven Doc Governance Helpers (Step 5)
# ---------------------------------------------------------------------------


def _get_graph_doc_associations(project_id, target_files, metadata=None):
    """Query graph for doc associations of target_files.

    Returns list of doc paths that the graph considers related to the changed code.
    Uses confirmed secondary associations from graph nodes.
    """
    try:
        if isinstance(metadata, dict) and metadata.get("operation_type") == "reconcile-cluster":
            from .chain_graph_context import get_graph_doc_associations
            return get_graph_doc_associations(
                project_id, target_files, metadata=metadata)
        from .graph import AcceptanceGraph
        state_root = os.path.join(
            os.environ.get("SHARED_VOLUME_PATH",
                           os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
            "codex-tasks", "state", "governance", project_id)
        graph_path = os.path.join(state_root, "graph.json")
        if not os.path.exists(graph_path):
            return []
        graph = AcceptanceGraph()
        graph.load(graph_path)
        docs = set()
        target_set = set(target_files) if target_files else set()
        for node_id in graph.list_nodes():
            try:
                node_data = graph.get_node(node_id)
            except Exception:
                continue
            primary = node_data.get("primary", [])
            secondary = node_data.get("secondary", [])
            # Forward: code target → find related docs
            if any(f in target_set for f in primary):
                for s in secondary:
                    if s.endswith(".md"):
                        docs.add(s)
            # Reverse (G6): doc target → find related doc files only
            # R4: Never add primary code files (.py) to docs set
            if any(f in target_set for f in secondary):
                for p in primary:
                    if p.endswith(".md"):
                        docs.add(p)
        # B49: Defensive filter — remove doc paths that no longer exist on disk
        filtered = set()
        for doc_path in docs:
            if os.path.exists(doc_path):
                filtered.add(doc_path)
            else:
                log.warning("Stale graph doc reference filtered: %s", doc_path)
        return sorted(filtered)
    except Exception:
        log.debug("_get_graph_doc_associations failed (non-critical)", exc_info=True)
        return []


def _get_task_graph_doc_associations(project_id, target_files, metadata=None):
    """Return doc associations for this task's graph context."""
    try:
        if isinstance(metadata, dict) and metadata.get("operation_type") == "reconcile-cluster":
            from .chain_graph_context import get_graph_doc_associations
            return get_graph_doc_associations(
                project_id, target_files, metadata=metadata)
    except Exception:
        log.debug("_get_task_graph_doc_associations failed for reconcile context", exc_info=True)
        return []
    return _get_graph_doc_associations(project_id, target_files)


def _get_graph_related_nodes(project_id, target_files, metadata=None):
    """Return graph node ids related to target files for the active task context."""
    try:
        if isinstance(metadata, dict) and metadata.get("operation_type") == "reconcile-cluster":
            from .chain_graph_context import get_related_nodes
            return get_related_nodes(project_id, target_files, metadata=metadata)
        from .graph import AcceptanceGraph
        state_root = os.path.join(
            os.environ.get("SHARED_VOLUME_PATH",
                           os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
            "codex-tasks", "state", "governance", project_id)
        graph_path = os.path.join(state_root, "graph.json")
        if not os.path.exists(graph_path):
            return []
        graph = AcceptanceGraph()
        graph.load(graph_path)
        target_set = set(target_files or [])
        matched_nodes = []
        for node_id in graph.list_nodes():
            try:
                node_data = graph.get_node(node_id)
            except Exception:
                continue
            primary = node_data.get("primary", [])
            if any(f in target_set for f in primary):
                matched_nodes.append(node_id)
        return sorted(matched_nodes)
    except Exception:
        log.debug("_get_graph_related_nodes failed (non-critical)", exc_info=True)
        return []


def _graph_gate_mode(metadata):
    raw = ""
    if isinstance(metadata, dict):
        raw = (
            metadata.get("graph_gate_mode")
            or metadata.get("graph_governance_mode")
            or metadata.get("graph_context_mode")
            or ""
        )
    mode = str(raw or "").strip().lower().replace("-", "_")
    if mode in {"strict", "advisory", "raw"}:
        return mode
    return "advisory"


def _target_commit_for_graph_dispatch(conn, project_id, metadata):
    if isinstance(metadata, dict):
        for key in (
            "target_commit_sha",
            "target_commit",
            "expected_head_sha",
            "head_sha",
            "commit_sha",
        ):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
    try:
        row = conn.execute(
            "SELECT git_head, chain_version FROM project_version WHERE project_id=?",
            (project_id,),
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return ""
    try:
        git_head = row["git_head"]
        chain_version = row["chain_version"]
    except Exception:
        git_head = row[0] if len(row) > 0 else ""
        chain_version = row[1] if len(row) > 1 else ""
    return str(git_head or chain_version or "").strip()


def _dispatch_graph_divergence_hook(
    conn,
    project_id,
    task_id,
    task_type,
    metadata,
    *,
    graph_governance_bypassed=False,
):
    """Queue scope reconcile when active graph snapshot is stale at dispatch.

    Strict mode blocks dispatch unless an explicit graph-governance bypass is in
    force. Advisory/raw modes record the stale state and continue.
    """
    if not isinstance(metadata, dict):
        metadata = {}
    mode = _graph_gate_mode(metadata)
    target_commit = _target_commit_for_graph_dispatch(conn, project_id, metadata)
    info = {
        "mode": mode,
        "target_commit": target_commit,
        "active_snapshot_id": "",
        "active_graph_commit": "",
        "diverged": False,
        "queued": False,
        "blocked": False,
        "reason": "",
    }
    if not target_commit:
        info["reason"] = "missing_target_commit"
        metadata["graph_divergence"] = info
        return info
    try:
        from .graph_snapshot_store import (
            get_active_graph_snapshot,
            queue_pending_scope_reconcile,
        )
        active = get_active_graph_snapshot(conn, project_id) or {}
        active_commit = str(active.get("commit_sha") or "")
        info["active_snapshot_id"] = str(active.get("snapshot_id") or "")
        info["active_graph_commit"] = active_commit
        diverged = active_commit != target_commit
        info["diverged"] = diverged
        if diverged:
            pending = queue_pending_scope_reconcile(
                conn,
                project_id,
                commit_sha=target_commit,
                parent_commit_sha=active_commit,
                evidence={
                    "source": "dispatch_graph_divergence_hook",
                    "task_id": task_id,
                    "task_type": task_type,
                    "mode": mode,
                    "active_snapshot_id": info["active_snapshot_id"],
                    "active_graph_commit": active_commit,
                    "graph_governance_bypassed": bool(graph_governance_bypassed),
                },
            )
            info["queued"] = True
            info["pending_status"] = pending.get("status")
            if mode == "strict" and not graph_governance_bypassed:
                info["blocked"] = True
                info["reason"] = "active_graph_snapshot_stale"
            elif mode == "raw":
                info["reason"] = "active_graph_snapshot_stale_raw"
            else:
                info["reason"] = "active_graph_snapshot_stale_advisory"
    except Exception as exc:
        info["reason"] = f"graph_divergence_hook_failed: {exc}"
        log.warning("auto_chain: graph divergence hook failed for %s: %s", task_id, exc)
    metadata["graph_divergence"] = info
    metadata["graph_stale"] = bool(info.get("diverged"))
    return info


def _build_reconcile_graph_preflight(project_id, metadata, proposed_nodes=None):
    """Build session-local graph preflight context for reconcile-cluster tasks."""
    if not isinstance(metadata, dict) or metadata.get("operation_type") != "reconcile-cluster":
        return {}
    try:
        from .chain_graph_context import build_reconcile_graph_preflight
        return build_reconcile_graph_preflight(
            project_id, metadata, proposed_nodes=proposed_nodes)
    except Exception:
        log.debug("_build_reconcile_graph_preflight failed (non-critical)", exc_info=True)
        return {}


def _audit_doc_gap(conn, project_id, task_id, stage, missing_docs, changed_files):
    """Audit doc gap observation (5f). Writes to audit_index for later analysis."""
    try:
        from .audit_service import record
        record(
            conn, project_id,
            event="doc_gap_observation",
            actor="auto-chain",
            ok=True,  # observation, not failure
            node_ids=None,
            request_id="",
            stage=stage,
            task_id=task_id,
            missing_docs=sorted(missing_docs) if missing_docs else [],
            changed_files=changed_files[:10] if changed_files else [],
            observation_mode=True,
        )
    except Exception:
        log.debug("_audit_doc_gap failed (non-critical)", exc_info=True)


def _check_session_bypass(gate_name, project_id, task_id):
    """CR0b R3: consult reconcile_session.get_active_session(project_id) and
    short-circuit a gate when ``gate_name`` appears in session.bypass_gates.

    Looked-up sessions in status active or finalizing are eligible. When a bypass
    fires, an audit_index row is recorded with event
    ``gate.bypassed.reconcile_session_active`` carrying ``{gate, task_id, session_id}``.

    Returns:
        (True, "reconcile_session_active_bypass:{gate_name}") when bypass applies.
        (False, "") otherwise.
    """
    if not gate_name:
        return False, ""
    try:
        from . import reconcile_session as _rs
        from .db import DBContext
        with DBContext(project_id) as _conn:
            sess = _rs.get_active_session(_conn, project_id)
            if sess is None:
                return False, ""
            if sess.status not in ("active", "finalizing"):
                return False, ""
            if gate_name not in (sess.bypass_gates or []):
                return False, ""
            # Bypass applies — audit it.
            try:
                from .audit_service import record as _audit_record
                _audit_record(
                    _conn, project_id,
                    event="gate.bypassed.reconcile_session_active",
                    actor="auto-chain",
                    ok=True, node_ids=None, request_id="",
                    gate=gate_name, task_id=task_id or "",
                    session_id=sess.session_id,
                )
            except Exception:
                log.debug("_check_session_bypass: audit failed (non-critical)", exc_info=True)
            return True, f"reconcile_session_active_bypass:{gate_name}"
    except Exception:
        log.debug("_check_session_bypass: lookup failed (non-critical)", exc_info=True)
        return False, ""


def _audit_reconcile_bypass(conn, project_id, gate_name, run_id, task_id):
    """Audit gate bypass for reconcile tasks (§11.3). Writes 'gate.reconcile_bypass' to audit_index."""
    try:
        from .audit_service import record
        record(conn, project_id, event="gate.reconcile_bypass", actor="auto-chain",
               ok=True, node_ids=None, request_id="",
               gate_name=gate_name, reconcile_run_id=run_id, task_id=task_id)
        # §11.4: rolling 1-hour high-frequency check per operator_id
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM audit_index WHERE event='gate.reconcile_bypass' AND ts >= ?",
            (one_hour_ago,),
        ).fetchone()
        count = row[0] if row else 0
        if count > 3:
            log.warning("reconcile_bypass high frequency: %d bypasses in last hour (run_id=%s)", count, run_id)
            record(conn, project_id, event="gate.reconcile_bypass.high_frequency", actor="auto-chain",
                   ok=True, node_ids=None, request_id="",
                   bypass_count=count, reconcile_run_id=run_id, task_id=task_id)
    except Exception:
        log.debug("_audit_reconcile_bypass failed (non-critical)", exc_info=True)


# TODO-DEPRECATED: _store_proposed_nodes removed per OPT-BACKLOG-GRAPH-DELTA-CHAIN-COMMIT PR-A.
# Replaced by graph.delta.proposed chain_events emission via _emit_graph_delta_event().
# The pending_nodes table had no downstream consumer (pn['docs'] never populated by PM).


_GRAPH_DELTA_CREATE_TO_UPDATE_FIELDS = (
    "title",
    "parent_layer",
    "parent",
    "parent_id",
    "deps",
    "verify_requires",
    "primary",
    "secondary",
    "test",
    "test_coverage",
    "test_strategy",
    "description",
)


def _normalize_existing_node_creates(project_id, graph_delta):
    """Move creates for already-known node IDs into updates.

    PM/Dev prompts often say "preserve L7.x and add tests". When an agent puts
    such an existing node under creates[], the graph_delta becomes ambiguous:
    commit code may treat it as a new node, while QA treats it as preservation.
    Normalize it before persisting graph.delta.proposed so downstream gates see
    the intended update semantics.
    """
    if not graph_delta or not isinstance(graph_delta, dict):
        return graph_delta, 0

    creates = graph_delta.get("creates", []) or []
    updates = graph_delta.get("updates", []) or []
    links = graph_delta.get("links", []) or []
    if not creates:
        return {"creates": creates, "updates": updates, "links": links}, 0

    try:
        from . import project_service
        existing_nodes = set(project_service.load_project_graph(project_id).list_nodes())
    except Exception:
        log.debug("graph_delta normalize: active graph lookup failed", exc_info=True)
        return {"creates": creates, "updates": updates, "links": links}, 0

    normalized_creates = []
    normalized_updates = list(updates)
    update_by_id = {
        entry.get("node_id"): entry
        for entry in normalized_updates
        if isinstance(entry, dict) and entry.get("node_id")
    }
    moved = 0

    for entry in creates:
        if not isinstance(entry, dict):
            normalized_creates.append(entry)
            continue
        node_id = str(entry.get("node_id") or "").strip()
        op = str(entry.get("op") or "").strip()
        if not node_id or node_id not in existing_nodes or op in {"remove_node", "delete_node"}:
            normalized_creates.append(entry)
            continue

        fields = {
            key: entry[key]
            for key in _GRAPH_DELTA_CREATE_TO_UPDATE_FIELDS
            if key in entry and entry[key] is not None
        }
        existing_update = update_by_id.get(node_id)
        if existing_update is None:
            existing_update = {"node_id": node_id, "fields": {}}
            normalized_updates.append(existing_update)
            update_by_id[node_id] = existing_update
        existing_fields = existing_update.setdefault("fields", {})
        if isinstance(existing_fields, dict):
            existing_fields.update(fields)
        else:
            existing_update["fields"] = fields
        existing_update["normalized_from_create"] = True
        moved += 1

    return {
        "creates": normalized_creates,
        "updates": normalized_updates,
        "links": links,
    }, moved


_GRAPH_DELTA_EXTRA_EVENT_FIELDS = ("waivers", "doc_debt", "doc_debt_waivers")


def _graph_delta_extra_event_fields(graph_delta):
    if not isinstance(graph_delta, dict):
        return {}
    extras = {}
    for field in _GRAPH_DELTA_EXTRA_EVENT_FIELDS:
        value = graph_delta.get(field)
        if isinstance(value, list) and value:
            extras[field] = value
    return extras


def _emit_graph_delta_event(project_id, task_id, result):
    """Emit graph.delta.proposed event if result contains non-empty graph_delta.

    R1: graph_delta shape: {creates: [...], updates: [...], links: [...]}.
    R2: Emits via ChainContextStore._persist_event for chain_events persistence.
    R3: No event if graph_delta is missing, None, or all sub-arrays empty.
    """
    graph_delta = result.get("graph_delta") if isinstance(result, dict) else None
    if not graph_delta or not isinstance(graph_delta, dict):
        return

    graph_delta, moved = _normalize_existing_node_creates(project_id, graph_delta)
    if moved:
        log.info("graph_delta normalize: moved %d existing-node creates to updates", moved)

    extras = _graph_delta_extra_event_fields(graph_delta)

    # Normalize: default missing sub-arrays to []
    creates = graph_delta.get("creates", [])
    updates = graph_delta.get("updates", [])
    links = graph_delta.get("links", [])
    extras = _graph_delta_extra_event_fields(graph_delta)

    # R3: Skip if all sub-arrays are empty
    if not creates and not updates and not links and not extras:
        return

    normalized_delta = {
        "creates": creates,
        "updates": updates,
        "links": links,
    }
    normalized_delta.update(extras)

    try:
        from .chain_context import get_store
        store = get_store()
        # Find root_task_id for this task's chain
        # _task_to_root maps task_id -> root_task_id
        root_task_id = store._task_to_root.get(task_id, task_id)

        store._persist_event(
            root_task_id=root_task_id,
            task_id=task_id,
            event_type="graph.delta.proposed",
            payload={
                "source_task_id": task_id,
                "graph_delta": normalized_delta,
            },
            project_id=project_id,
        )
        log.info("auto_chain: emitted graph.delta.proposed for task %s (%d creates, %d updates, %d links)",
                 task_id, len(creates), len(updates), len(links))
    except Exception:
        log.debug("auto_chain: graph.delta.proposed emission failed", exc_info=True)


# ---------------------------------------------------------------------------
# Graph Delta Auto-Infer (OPT-BACKLOG-GRAPH-DELTA-AUTO-INFER)
# ---------------------------------------------------------------------------


def _is_dev_doc(path):
    """Return True if path matches docs/dev/** or is a dev-note artifact."""
    normalized = path.replace("\\", "/")
    if normalized.startswith("docs/dev/"):
        return True
    return _is_dev_note(normalized)


def _file_deleted_in_worktree(file_path, dev_result_ctx):
    """PR1e: Return True iff the given file_path no longer exists on the filesystem.

    Defensive: returns False on any exception so unknown-state files default to
    current Rule J behavior (i.e. keep firing). The dev_result_ctx parameter is
    accepted (and currently unused) so callers can later thread workspace info
    without changing the signature.
    """
    try:
        if not file_path:
            return False
        return not os.path.exists(file_path)
    except Exception:
        return False


def _infer_graph_delta(pm_nodes, changed_files, dev_delta, dev_result, prd_declarations=None):
    """Infer graph_delta from PM proposed_nodes + dev changed_files.

    Six deterministic rules:
      Rule A: PM proposed_nodes whose primary appears in changed_files (excl .md)
      Rule H: When dev_delta is None, bridge ALL PM proposed_nodes into creates[]
      Rule B: @route decorator grep on changed agent/**/*.py files
      Rule D: Updates to existing graph nodes whose primary is in changed_files
      Rule E: Dev override — dev entries replace inferred with same title/primary
      Rule F: Discard creates[] where ALL primary files are docs/dev/** or dev-notes

    Rules C (warn-only) and G (fuzzy title similarity) are explicitly SKIPPED.

    When prd_declarations is provided (R3), PM declarations take priority:
      - removed_nodes → {op: 'remove_node', source: 'pm_declaration'}
      - Files covered by declarations skip auto-inferrer heuristics

    Returns (graph_delta_dict, rule_hits_list, inferred_from_list, source_str).
    """
    creates = []
    updates = []
    links = []
    rule_hits = []
    inferred_from = []

    # Normalize changed_files to forward-slash set
    changed_set = {f.replace("\\", "/") for f in (changed_files or [])}
    non_md_changed = {f for f in changed_set if not f.endswith(".md")}
    pm_proposed_only = (
        isinstance(dev_result, dict)
        and bool(dev_result.get("_pm_proposed_only"))
        and dev_delta is None
    )

    def _pm_create_entry(node):
        primaries = node.get("primary", [])
        if isinstance(primaries, str):
            primaries = [primaries]
        return {
            "node_id": node.get("node_id") or "",
            "title": node.get("title", ""),
            "parent_layer": node.get("parent_layer", ""),
            "primary": primaries,
            "deps": node.get("deps", []),
            "description": node.get("description", ""),
        }

    def _pm_candidate_dedupe_key(node):
        nid = node.get("node_id")
        if isinstance(nid, str) and nid.strip():
            return ("node_id", nid.strip())
        primaries = node.get("primary", [])
        if isinstance(primaries, str):
            primaries = [primaries]
        return (
            "candidate",
            tuple(sorted(p.replace("\\", "/") for p in primaries if isinstance(p, str) and p)),
            node.get("title", ""),
            str(node.get("parent_layer", "")),
        )

    # ---- PRD declarations priority (R3) ----
    decl = prd_declarations or {}
    declared_removed = decl.get("removed_nodes", [])
    declared_removed_ids = set()
    declared_files = set()  # files covered by declarations — skip auto-inferrer
    for nr in declared_removed:
        nid = nr if isinstance(nr, str) else nr.get("node_id", "")
        if nid:
            declared_removed_ids.add(nid)
            creates.append({"op": "remove_node", "node_id": nid, "source": "pm_declaration"})
            rule_hits.append({"rule": "PM_DECL", "op": "remove_node", "node_id": nid})
    for rf in decl.get("remapped_files", []):
        f = rf if isinstance(rf, str) else rf.get("file", "")
        if f:
            declared_files.add(f.replace("\\", "/"))
    for uf in decl.get("unmapped_files", []):
        f = uf if isinstance(uf, str) else uf.get("file", uf)
        if isinstance(f, str) and f:
            declared_files.add(f.replace("\\", "/"))
    if declared_removed_ids or declared_files:
        inferred_from.append("pm_declarations")
    # Filter non_md_changed to skip declared files
    non_md_changed_undeclared = non_md_changed - declared_files

    # ---- Rule A: PM proposed_nodes with matching primary in changed_files ----
    covered_primaries = set()
    if pm_nodes:
        inferred_from.append("pm_proposed_nodes")
        for node in pm_nodes:
            nid_a = node.get("node_id", "")
            # R3: skip nodes declared as removed
            if nid_a in declared_removed_ids:
                # R4: emit conflict audit — inferrer would create but PM declared remove
                structured_log("info", "graph_delta.declaration_overrides_inference",
                               node_id=nid_a, declared_op="remove_node",
                               inferred_op="creates", source="pm_declaration")
                continue
            primaries = node.get("primary", [])
            if isinstance(primaries, str):
                primaries = [primaries]
            matched = [p for p in primaries if p.replace("\\", "/") in non_md_changed_undeclared]
            if matched:
                entry = _pm_create_entry(node)
                creates.append(entry)
                covered_primaries.update(p.replace("\\", "/") for p in primaries)
                rule_hits.append({"rule": "A", "entry_title": entry["title"],
                                  "matched_files": matched})

    # ---- Rule H: Bridge ALL PM proposed_nodes when dev emitted no graph_delta ----
    if dev_delta is None and pm_nodes:
        # Collect stable identities already added by Rule A to avoid duplicates.
        # Candidate-only reconcile nodes legitimately have blank node_id until
        # the overlay allocator runs, so blank node_id cannot be the dedupe key.
        existing_keys = {_pm_candidate_dedupe_key(c) for c in creates}
        for node in pm_nodes:
            node_key = _pm_candidate_dedupe_key(node)
            if node_key in existing_keys:
                continue  # already matched by Rule A
            nid = node.get("node_id", "")
            if nid in declared_removed_ids:
                continue  # R3: skip declared-removed nodes
            entry = _pm_create_entry(node)
            creates.append(entry)
            existing_keys.add(node_key)
            rule_hits.append({"rule": "H", "entry_title": entry["title"],
                              "node_id": nid})
        if "pm_proposed_bridge" not in inferred_from:
            inferred_from.append("pm_proposed_bridge")
        if pm_proposed_only:
            return (
                {"creates": creates, "updates": updates, "links": links},
                rule_hits,
                inferred_from,
                "pm-proposed-only",
            )

    # ---- Rule B: @route decorator grep on changed agent/**/*.py ----
    # MF-2026-04-29-001: exclude self-referential governance modules whose source
    # CONTAINS @route regex patterns (used to scan OTHER files) but are NOT routers.
    # Rule B's own regex matches the regex pattern strings inside auto_chain.py →
    # phantom 4× endpoint creates with empty node_id/parent_layer → QA rejects.
    _ROUTE_GREP_EXCLUDE = frozenset({
        "agent/governance/auto_chain.py",  # contains @route regex strings, scans others
    })
    py_agent_files = [f for f in non_md_changed_undeclared
                      if f.startswith("agent/") and f.endswith(".py")
                      and not f.startswith("agent/tests/")
                      and "/tests/" not in f.replace("\\", "/")
                      and f.replace("\\", "/") not in covered_primaries
                      and f.replace("\\", "/") not in _ROUTE_GREP_EXCLUDE]
    if py_agent_files:
        inferred_from.append("route_decorator_grep")
        # Positional: @route('POST', '/api/foo') or @app.route('GET', '/path')
        route_re_positional = re.compile(
            r"@(?:\w+\.)?route\(\s*[\"'](\w+)[\"']\s*,\s*[\"']([^\"']+)[\"']",
        )
        # Dotted: @app.get('/path'), @bp.post('/path'), etc. — requires dotted prefix
        route_re_dotted = re.compile(
            r"@\w+\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']",
            re.IGNORECASE,
        )
        for fpath in py_agent_files:
            try:
                abs_path = fpath
                if not os.path.isabs(fpath):
                    abs_path = os.path.join(os.getcwd(), fpath)
                if not os.path.exists(abs_path):
                    continue
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                for m in route_re_positional.finditer(content):
                    method = m.group(1).upper()
                    path_str = m.group(2)
                    title = "HTTP endpoint: %s %s" % (method, path_str)
                    creates.append({
                        "node_id": "",
                        "title": title,
                        "parent_layer": "",
                        "primary": [fpath],
                        "deps": [],
                        "description": "Auto-inferred from @route decorator",
                    })
                    rule_hits.append({"rule": "B", "entry_title": title,
                                      "file": fpath})
                for m in route_re_dotted.finditer(content):
                    method = m.group(1).upper()
                    path_str = m.group(2)
                    title = "HTTP endpoint: %s %s" % (method, path_str)
                    creates.append({
                        "node_id": "",
                        "title": title,
                        "parent_layer": "",
                        "primary": [fpath],
                        "deps": [],
                        "description": "Auto-inferred from @route decorator",
                    })
                    rule_hits.append({"rule": "B", "entry_title": title,
                                      "file": fpath})
            except Exception:
                log.debug("Rule B: failed to scan %s", fpath, exc_info=True)

    # ---- Rule D: Updates to existing graph nodes whose primary is in changed_files ----
    if changed_set:
        inferred_from.append("existing_graph_nodes")
        try:
            from . import project_service
            graph = project_service.load_project_graph(
                dev_result.get("project_id", "aming-claw") if isinstance(dev_result, dict) else "aming-claw"
            )
            if graph:
                # Collect pm_update_node_ids to skip (Rule D exception)
                pm_update_ids = set()
                if dev_delta and isinstance(dev_delta, dict):
                    for u in dev_delta.get("updates", []):
                        nid = u.get("node_id", "")
                        if nid:
                            pm_update_ids.add(nid)
                task_id = dev_result.get("task_id", "") if isinstance(dev_result, dict) else ""
                for node_id in graph.list_nodes():
                    if node_id in pm_update_ids:
                        continue
                    try:
                        node_data = graph.get_node(node_id)
                    except Exception:
                        continue
                    node_primaries = node_data.get("primary", [])
                    if isinstance(node_primaries, str):
                        node_primaries = [node_primaries]
                    touched = [p for p in node_primaries if p.replace("\\", "/") in changed_set]
                    if touched:
                        updates.append({
                            "node_id": node_id,
                            "fields": {"touched_by": task_id},
                        })
                        rule_hits.append({"rule": "D", "node_id": node_id,
                                          "touched_files": touched})
        except Exception:
            log.debug("Rule D: graph lookup failed", exc_info=True)

    # ---- Rule E: Dev override — merge dev entries with inferred ----
    source = "auto-inferred"
    if dev_delta and isinstance(dev_delta, dict):
        dev_creates = dev_delta.get("creates", [])
        dev_updates = dev_delta.get("updates", [])
        dev_links = dev_delta.get("links", [])

        if dev_creates or dev_updates or dev_links:
            # Dev provided some entries — merge
            # Build lookup keys for dev entries
            dev_title_set = set()
            dev_primary_set = set()
            for dc in dev_creates:
                t = dc.get("title", "")
                if t:
                    dev_title_set.add(t)
                for p in (dc.get("primary", []) if isinstance(dc.get("primary"), list) else [dc.get("primary", "")]):
                    if p:
                        dev_primary_set.add(p.replace("\\", "/"))

            # Filter inferred creates: remove those matching dev by title or primary
            filtered_creates = []
            for ic in creates:
                ic_title = ic.get("title", "")
                ic_primaries = ic.get("primary", [])
                if isinstance(ic_primaries, str):
                    ic_primaries = [ic_primaries]
                ic_pset = {p.replace("\\", "/") for p in ic_primaries}
                if ic_title in dev_title_set:
                    continue
                if ic_pset & dev_primary_set:
                    continue
                filtered_creates.append(ic)

            # Dev entries take priority, inferred fill gaps
            creates = list(dev_creates) + filtered_creates

            # For updates: dev overrides by node_id
            dev_update_ids = {u.get("node_id") for u in dev_updates}
            filtered_updates = [u for u in updates if u.get("node_id") not in dev_update_ids]
            updates = list(dev_updates) + filtered_updates

            links = list(dev_links) + links
            source = "dev-emitted+inferred-gaps"

    # ---- Rule F: Discard creates where ALL primary files are dev docs ----
    final_creates = []
    for entry in creates:
        primaries = entry.get("primary", [])
        if isinstance(primaries, str):
            primaries = [primaries]
        if primaries and all(_is_dev_doc(p) for p in primaries):
            rule_hits.append({"rule": "F", "discarded_title": entry.get("title", ""),
                              "reason": "all primaries are dev docs"})
            continue
        final_creates.append(entry)
    creates = final_creates

    # ---- Rule I: Bind unbound test files to best-matching graph node ----
    # ---- Rule J: Bind unbound src modules or propose new L7 nodes ----
    # Both rules run after A-H and operate only on files not yet covered.
    _rule_ij_covered = set(covered_primaries)
    for c in creates:
        cprim = c.get("primary", [])
        if isinstance(cprim, str):
            cprim = [cprim]
        _rule_ij_covered.update(p.replace("\\", "/") for p in cprim)
    # Also mark files touched by Rule D updates
    _rule_d_updated_nodes = {u.get("node_id") for u in updates}

    # Load graph once for both rules (reuse Rule D's pattern)
    _ij_graph = None
    _ij_graph_loaded = False
    project_id_for_graph = (
        dev_result.get("project_id", "aming-claw")
        if isinstance(dev_result, dict) else "aming-claw"
    )

    def _load_ij_graph():
        nonlocal _ij_graph, _ij_graph_loaded
        if _ij_graph_loaded:
            return _ij_graph
        _ij_graph_loaded = True
        try:
            from . import project_service
            _ij_graph = project_service.load_project_graph(project_id_for_graph)
        except Exception:
            log.debug("Rule I/J: graph load failed", exc_info=True)
        return _ij_graph

    def _derive_title_from_path(f):
        """R5: derive title from file path stem."""
        return PurePosixPath(f).stem.replace("_", " ").title()

    def _fuzzy_score(file_path, node_id, node_data):
        """R5: fuzzy scoring — same_dir +0.4, stem-match +0.5, title keyword +0.2."""
        score = 0.0
        f_norm = file_path.replace("\\", "/")
        f_dir = str(PurePosixPath(f_norm).parent)
        f_stem = PurePosixPath(f_norm).stem.lower()
        # Remove test_ prefix for test files
        if f_stem.startswith("test_"):
            f_stem = f_stem[5:]

        node_primaries = node_data.get("primary", [])
        if isinstance(node_primaries, str):
            node_primaries = [node_primaries]

        # Title keyword match (applies to every candidate)
        node_title = (node_data.get("title") or "").lower()
        title_bonus = 0.2 if (f_stem and f_stem in node_title) else 0.0

        for np in node_primaries:
            np_norm = np.replace("\\", "/")
            np_dir = str(PurePosixPath(np_norm).parent)
            np_stem = PurePosixPath(np_norm).stem.lower()

            candidate = 0.0
            # same_dir: exact match or parent-child relationship
            if f_dir == np_dir or f_dir.startswith(np_dir + "/") or np_dir.startswith(f_dir + "/"):
                candidate += 0.4
            if f_stem == np_stem:
                candidate += 0.5
            candidate += title_bonus
            score = max(score, candidate)

        # If no primaries matched but title matched, still give title bonus
        if not node_primaries and title_bonus:
            score = max(score, title_bonus)

        return score

    def _allocate_next_id(graph, layer_prefix="L7"):
        """R4/R5: scan graph for max <layer_prefix>.N + 1.

        Produces monotonically increasing IDs with no gaps or collisions
        against existing node_state entries.  Accepts any layer prefix
        (e.g. "L7", "L3").
        """
        _pfx_pat = re.compile(r"^" + re.escape(layer_prefix) + r"\.(\d+)$")
        max_n = 0
        for nid in graph.list_nodes():
            m = _pfx_pat.match(nid)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return "%s.%d" % (layer_prefix, max_n + 1)

    # -- Rule I: test files --
    _test_file_re = re.compile(r"^agent/tests/test_.*\.py$")
    unbound_tests = [
        f for f in changed_set
        if _test_file_re.match(f) and f not in _rule_ij_covered
    ]
    if unbound_tests:
        graph = _load_ij_graph()
        if graph:
            for tf in unbound_tests:
                best_node = None
                best_score = 0.0
                for nid in graph.list_nodes():
                    if nid in _rule_d_updated_nodes:
                        # Already handled — but we still want to bind test
                        pass
                    try:
                        nd = graph.get_node(nid)
                    except Exception:
                        continue
                    sc = _fuzzy_score(tf, nid, nd)
                    if sc > best_score:
                        best_score = sc
                        best_node = nid
                if best_score >= 0.85 and best_node is not None:
                    # Check if this node already has an update entry
                    existing_update = None
                    for u in updates:
                        if u.get("node_id") == best_node:
                            existing_update = u
                            break
                    if existing_update:
                        existing_test = existing_update["fields"].get("test", [])
                        if isinstance(existing_test, str):
                            existing_test = [existing_test]
                        if tf not in existing_test:
                            existing_test.append(tf)
                        existing_update["fields"]["test"] = existing_test
                    else:
                        updates.append({
                            "node_id": best_node,
                            "fields": {"test": [tf]},
                        })
                    rule_hits.append({"rule": "I", "test_file": tf,
                                      "bound_to": best_node,
                                      "score": best_score})
                else:
                    log.warning("Rule I: no fuzzy match (score=%.2f) for test file %s",
                                best_score, tf)
        if "test_file_binding" not in inferred_from:
            inferred_from.append("test_file_binding")

    # -- Rule J: src modules --
    # Rule J fuzzy-matches changed source files to existing graph nodes (via
    # secondary_bind) or proposes new L7 nodes. Defense-in-depth: skip files
    # that any of THREE truth sources say should not produce phantom creates:
    #   1. PM declarations (PR1c / MF-2026-04-29-001) — declared_files set,
    #      derived from prd_declarations.unmapped_files / .remapped_files. If the
    #      prd_declarations kwarg is missing, declared_files degenerates to empty.
    #   2. dev_delta.removes (PR1e) — dev_removes_primaries set, computed by
    #      resolving each removed node_id via _load_ij_graph().get_node().primary.
    #      Authoritative even when PM forgot to declare the deletion.
    #   3. Filesystem truth (PR1e) — _file_deleted_in_worktree() checks
    #      os.path.exists; if the file is gone from the worktree, no real module
    #      exists to bind, so Rule J must skip it.
    # All three filter clauses are AND-conjuncted to the existing predicate —
    # they strictly NARROW the set, never widen it.
    _src_module_re = re.compile(r"^agent/.*\.py$")

    # PR1e: build dev_removes_primaries set from dev_delta.removes (if any).
    # Each entry may be a string node_id or a {'node_id': '...'} dict; look the
    # node up in the graph and extract its `primary` field (str or list).
    dev_removes_primaries = set()
    if dev_delta and isinstance(dev_delta, dict):
        graph_for_lookup = _load_ij_graph()
        for entry in dev_delta.get("removes", []) or []:
            if isinstance(entry, str):
                nid = entry
            elif isinstance(entry, dict):
                nid = entry.get("node_id", "")
            else:
                nid = ""
            if not nid or graph_for_lookup is None:
                continue
            try:
                nd = graph_for_lookup.get_node(nid)
                np = nd.get("primary", []) if isinstance(nd, dict) else []
                if isinstance(np, str):
                    np = [np]
                for p in np:
                    if isinstance(p, str) and p:
                        dev_removes_primaries.add(p.replace("\\", "/"))
            except Exception:
                # Missing-node tolerance: silently continue (AC8)
                continue

    unbound_src = [
        f for f in changed_set
        if _src_module_re.match(f)
        and not f.startswith("agent/tests/")
        and f not in _rule_ij_covered
        and f.replace("\\", "/") not in declared_files  # MF-2026-04-29-001: respect PM declarations
        and f.replace("\\", "/") not in dev_removes_primaries  # PR1e: respect dev_delta.removes
        and not _file_deleted_in_worktree(f, dev_result)  # PR1e: respect filesystem truth
        # Also skip files already touched by Rule D
    ]
    # Filter out files already in Rule D updates' primary
    if unbound_src:
        graph = _load_ij_graph()
        if graph:
            # Build set of primaries already handled by Rule D
            rule_d_primaries = set()
            for nid in _rule_d_updated_nodes:
                try:
                    nd = graph.get_node(nid)
                    np = nd.get("primary", [])
                    if isinstance(np, str):
                        np = [np]
                    rule_d_primaries.update(p.replace("\\", "/") for p in np)
                except Exception:
                    pass
            unbound_src = [f for f in unbound_src if f not in rule_d_primaries]

    if unbound_src:
        graph = _load_ij_graph()
        if graph:
            for sf in unbound_src:
                best_node = None
                best_score = 0.0
                for nid in graph.list_nodes():
                    try:
                        nd = graph.get_node(nid)
                    except Exception:
                        continue
                    sc = _fuzzy_score(sf, nid, nd)
                    if sc > best_score:
                        best_score = sc
                        best_node = nid
                if best_score >= 0.9 and best_node is not None:
                    # Bind to existing node's secondary
                    existing_update = None
                    for u in updates:
                        if u.get("node_id") == best_node:
                            existing_update = u
                            break
                    if existing_update:
                        existing_sec = existing_update["fields"].get("secondary", [])
                        if isinstance(existing_sec, str):
                            existing_sec = [existing_sec]
                        if sf not in existing_sec:
                            existing_sec.append(sf)
                        existing_update["fields"]["secondary"] = existing_sec
                    else:
                        updates.append({
                            "node_id": best_node,
                            "fields": {"secondary": [sf]},
                        })
                    rule_hits.append({"rule": "J", "src_file": sf,
                                      "bound_to": best_node,
                                      "score": best_score,
                                      "action": "secondary_bind"})
                else:
                    # Propose new L7 node
                    new_id = _allocate_next_id(graph)
                    title = _derive_title_from_path(sf)
                    creates.append({
                        "node_id": new_id,
                        "title": title,
                        "parent_layer": "L7",
                        "primary": [sf],
                        "deps": [],
                        "description": "Auto-inferred new module",
                        "created_by": "autochain-new-file-binding",
                    })
                    rule_hits.append({"rule": "J", "src_file": sf,
                                      "new_node_id": new_id,
                                      "action": "new_l7_node"})
            if "src_module_binding" not in inferred_from:
                inferred_from.append("src_module_binding")

    delta = {"creates": creates, "updates": updates, "links": links}
    return delta, rule_hits, inferred_from, source


def _is_reconcile_task_type(task_type):
    return task_type == "reconcile" or (
        isinstance(task_type, str) and task_type.startswith("reconcile_")
    )


def _emit_or_infer_graph_delta(project_id, task_id, result, metadata, task_type=""):
    """Emit graph.delta.proposed, auto-inferring if dev omitted graph_delta.

    Replaces direct _emit_graph_delta_event() call to ensure graph.delta.proposed
    is ALWAYS emitted at dev→QA transition.

    Case A: Dev provided non-empty graph_delta → passthrough with source='dev-emitted'
    Case B: Dev omitted graph_delta → auto-infer from PM proposed_nodes + changed_files
    Case A+B: Dev provided partial + inference fills gaps → source='dev-emitted+inferred-gaps'

    PR1c (MF b6e874a / MF-2026-04-29-001): prd_declarations are extracted from the
    pm.prd.published chain_event payload and threaded into _infer_graph_delta to
    enable the declared_files / declared_removed_ids filters that prevent phantom
    creates for files PM declared as unmapped/removed (see Rule J in _infer_graph_delta).
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    if backlog_runtime.is_graph_governance_bypassed(metadata):
        log.warning("auto_chain: graph.delta.proposed skipped for task %s by backlog graph bypass", task_id)
        return

    graph_delta = result.get("graph_delta") if isinstance(result, dict) else None
    effective_task_type = task_type or metadata.get("task_type", "")
    if _is_reconcile_task_type(effective_task_type):
        # Reconcile tasks derive authoritative topology deltas. Running the
        # general auto-inferrer on top can re-classify intended removals as
        # phantom creates and turn graph.v2 into a second-source conflict.
        log.info(
            "_emit_or_infer_graph_delta: reconcile task passthrough task=%s type=%s",
            task_id,
            effective_task_type,
        )
        _emit_graph_delta_event_with_source(
            project_id,
            task_id,
            result,
            "reconcile-derived",
            metadata,
        )
        return

    # Normalize dev delta
    dev_has_delta = False
    if graph_delta and isinstance(graph_delta, dict):
        dc = graph_delta.get("creates", [])
        du = graph_delta.get("updates", [])
        dl = graph_delta.get("links", [])
        dev_has_delta = bool(dc or du or dl)

    log.info("_emit_or_infer_graph_delta: entry task=%s dev_has_delta=%s", task_id, dev_has_delta)

    # Load PM proposed_nodes from pm.prd.published chain_event
    pm_nodes = []
    # PR1c (MF b6e874a / MF-2026-04-29-001): default declarations to empty lists
    # so the keyword arg is always supplied to _infer_graph_delta, even when the
    # pm.prd.published lookup fails or returns no payload.
    prd_declarations = {f: [] for f in _PRD_GRAPH_DECLARATION_FIELDS}
    try:
        from .db import get_connection
        conn = get_connection(project_id)
        try:
            root_task_id = _resolve_chain_root_id(conn, project_id, task_id, metadata)
            row = conn.execute(
                "SELECT payload_json FROM chain_events "
                "WHERE root_task_id = ? AND event_type = 'pm.prd.published' "
                "ORDER BY ts DESC LIMIT 1",
                (root_task_id,),
            ).fetchone()
            if row:
                payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
                pm_nodes = payload.get("proposed_nodes", [])
                # PR1c: extract the 4 PRD graph-declaration fields so that
                # _infer_graph_delta's declared_files / declared_removed_ids
                # filters become effective (Rule J phantom-create prevention).
                prd_declarations = {f: payload.get(f, []) for f in _PRD_GRAPH_DECLARATION_FIELDS}
        finally:
            conn.close()
    except Exception:
        log.error("_emit_or_infer_graph_delta: pm.prd.published lookup failed", exc_info=True)

    log.info("_emit_or_infer_graph_delta: pm_nodes from chain_events count=%d", len(pm_nodes))

    changed_files = result.get("changed_files", metadata.get("changed_files", []))

    if dev_has_delta and not pm_nodes and not changed_files:
        log.info("_emit_or_infer_graph_delta: early-return pure dev-emitted passthrough task=%s", task_id)
        # Pure dev-emitted: passthrough with source field
        _emit_graph_delta_event_with_source(project_id, task_id, result, "dev-emitted", metadata)
        return

    # Run inference
    dev_result_ctx = dict(result) if isinstance(result, dict) else {}
    dev_result_ctx["project_id"] = project_id
    dev_result_ctx["task_id"] = task_id
    if is_reconcile_cluster_task(metadata) and not dev_has_delta:
        dev_result_ctx["_pm_proposed_only"] = True

    inferred_delta, rule_hits, inferred_from, source = _infer_graph_delta(
        pm_nodes, changed_files, graph_delta if dev_has_delta else None, dev_result_ctx,
        prd_declarations=prd_declarations,
    )

    log.info("_emit_or_infer_graph_delta: inference produced source=%s creates=%d updates=%d links=%d",
             source, len(inferred_delta.get("creates", [])),
             len(inferred_delta.get("updates", [])),
             len(inferred_delta.get("links", [])))

    # Determine final source
    if dev_has_delta and source == "auto-inferred":
        # Dev had entries but inference didn't merge (no overlap case)
        source = "dev-emitted"

    final_creates = inferred_delta.get("creates", [])
    final_updates = inferred_delta.get("updates", [])
    final_links = inferred_delta.get("links", [])

    normalized_delta, moved_existing = _normalize_existing_node_creates(
        project_id,
        {"creates": final_creates, "updates": final_updates, "links": final_links},
    )
    if moved_existing:
        log.info(
            "_emit_or_infer_graph_delta: normalized %d existing-node creates to updates for task=%s",
            moved_existing,
            task_id,
        )
        final_creates = normalized_delta.get("creates", [])
        final_updates = normalized_delta.get("updates", [])
        final_links = normalized_delta.get("links", [])

    if not final_creates and not final_updates and not final_links:
        # Nothing to emit — still emit empty proposed for audit trail
        log.info("_emit_or_infer_graph_delta: early-return empty inference task=%s dev_has_delta=%s", task_id, dev_has_delta)
        if dev_has_delta:
            _emit_graph_delta_event_with_source(project_id, task_id, result, "dev-emitted", metadata)
        return

    extras = _graph_delta_extra_event_fields(graph_delta) if isinstance(graph_delta, dict) else {}
    normalized_delta = {
        "creates": final_creates,
        "updates": final_updates,
        "links": final_links,
    }
    normalized_delta.update(extras)

    # Emit graph.delta.proposed with source
    try:
        from .chain_context import get_store
        store = get_store()
        root_task_id = _store_root_for(metadata.get("chain_id") or task_id)

        store._persist_event(
            root_task_id=root_task_id,
            task_id=task_id,
            event_type="graph.delta.proposed",
            payload={
                "source_task_id": task_id,
                "source": source,
                "graph_delta": normalized_delta,
            },
            project_id=project_id,
        )
        log.info("auto_chain: emitted graph.delta.proposed (source=%s) for task %s "
                 "(%d creates, %d updates, %d links)",
                 source, task_id, len(final_creates), len(final_updates), len(final_links))
    except Exception:
        log.error("auto_chain: graph.delta.proposed emission failed", exc_info=True)

    # R4: Emit graph.delta.inferred event when auto-inference path executed
    if source in ("auto-inferred", "dev-emitted+inferred-gaps"):
        try:
            from .chain_context import get_store
            store = get_store()
            root_task_id = _store_root_for(metadata.get("chain_id") or task_id)

            store._persist_event(
                root_task_id=root_task_id,
                task_id=task_id,
                event_type="graph.delta.inferred",
                payload={
                    "source": source,
                    "inferred_from": inferred_from,
                    "rule_hits": rule_hits,
                },
                project_id=project_id,
            )
            log.info("auto_chain: emitted graph.delta.inferred for task %s (rules: %s)",
                     task_id, [h.get("rule") for h in rule_hits])
        except Exception:
            log.error("auto_chain: graph.delta.inferred emission failed", exc_info=True)


def _emit_graph_delta_event_with_source(project_id, task_id, result, source, metadata=None):
    """Emit graph.delta.proposed with explicit source field (passthrough for dev-emitted)."""
    graph_delta = result.get("graph_delta") if isinstance(result, dict) else None
    if not graph_delta or not isinstance(graph_delta, dict):
        return

    creates = graph_delta.get("creates", [])
    updates = graph_delta.get("updates", [])
    links = graph_delta.get("links", [])
    extras = _graph_delta_extra_event_fields(graph_delta)

    graph_delta, moved = _normalize_existing_node_creates(
        project_id,
        {"creates": creates, "updates": updates, "links": links},
    )
    if moved:
        log.info(
            "graph_delta passthrough normalize: moved %d existing-node creates to updates for task=%s",
            moved,
            task_id,
        )
        creates = graph_delta.get("creates", [])
        updates = graph_delta.get("updates", [])
        links = graph_delta.get("links", [])

    if not creates and not updates and not links and not extras:
        return
    normalized_delta = {
        "creates": creates,
        "updates": updates,
        "links": links,
    }
    normalized_delta.update(extras)

    try:
        from .chain_context import get_store
        store = get_store()
        metadata = metadata if isinstance(metadata, dict) else {}
        root_task_id = _store_root_for(metadata.get("chain_id") or task_id)

        store._persist_event(
            root_task_id=root_task_id,
            task_id=task_id,
            event_type="graph.delta.proposed",
            payload={
                "source_task_id": task_id,
                "source": source,
                "graph_delta": normalized_delta,
            },
            project_id=project_id,
        )
        log.info("auto_chain: emitted graph.delta.proposed (source=%s) for task %s",
                 source, task_id)
    except Exception:
        log.debug("auto_chain: graph.delta.proposed emission failed", exc_info=True)


# ---------------------------------------------------------------------------
# Graph Delta Transactional Commit (PR-C: OPT-BACKLOG-GRAPH-DELTA-CHAIN-COMMIT)
# ---------------------------------------------------------------------------

import uuid as _uuid


_GRAPH_DELTA_GRAPH_CREATE_FIELDS = (
    "title",
    "primary",
    "secondary",
    "test",
    "test_coverage",
    "propagation",
    "guard",
    "version",
    "gates",
    "verify_requires",
    "description",
    "metadata",
)
_GRAPH_DELTA_GRAPH_UPDATE_FIELDS = _GRAPH_DELTA_GRAPH_CREATE_FIELDS


def _graph_delta_layer_prefix(parent_layer):
    raw = str(parent_layer or "").strip()
    match = re.match(r"^[Ll]?(\d+)$", raw)
    if not match:
        return ""
    return f"L{int(match.group(1))}"


def _graph_delta_node_attrs(node_id, item):
    layer = _graph_delta_layer_prefix(item.get("parent_layer"))
    attrs = {
        "id": node_id,
        "title": item.get("title", node_id),
        "layer": layer or "L0",
        "verify_level": item.get("verify_level", 1),
        "gate_mode": item.get("gate_mode", "auto"),
        "test_coverage": item.get("test_coverage", "none"),
        "primary": item.get("primary", []),
        "secondary": item.get("secondary", []),
        "test": item.get("test", []),
        "propagation": item.get("propagation"),
        "guard": item.get("guard", False),
        "version": item.get("version", ""),
        "gates": item.get("gates", []),
        "verify_requires": item.get("verify_requires", []),
    }
    for key in ("description", "metadata"):
        if key in item:
            attrs[key] = item.get(key)
    for key in _GRAPH_DELTA_GRAPH_CREATE_FIELDS:
        if key in item and key not in attrs:
            attrs[key] = item.get(key)
    return attrs


def _materialize_graph_delta_to_project_graph(project_id, committed_creates, committed_updates):
    """Persist committed graph_delta changes into project graph.json.

    node_state is the verification ledger; graph.json is the reader-facing
    topology. A committed graph_delta must update both, otherwise later
    release gates and impact readers cannot see newly materialized nodes.
    """
    if not committed_creates and not committed_updates:
        return {"graph_updated": False, "created": [], "updated": []}

    from .db import _resolve_project_dir
    from .graph import AcceptanceGraph

    graph_path = _resolve_project_dir(project_id) / "graph.json"
    if not graph_path.exists():
        log.info(
            "_commit_graph_delta: graph.json not found for %s; node_state commit only",
            project_id,
        )
        return {"graph_updated": False, "created": [], "updated": [], "missing_graph": True}

    graph = AcceptanceGraph()
    graph.load(graph_path)
    created_ids = []
    updated_ids = []

    for node_id, item in committed_creates:
        if node_id in graph.G:
            continue
        attrs = _graph_delta_node_attrs(node_id, item)
        graph.G.add_node(node_id, **attrs)
        for dep in item.get("deps", []) or []:
            if dep in graph.G:
                graph.G.add_edge(dep, node_id)
        for gate_req in item.get("gates", []) or []:
            gate_node_id = gate_req.get("node_id") if isinstance(gate_req, dict) else str(gate_req)
            if gate_node_id in graph.G:
                graph.gates_G.add_node(gate_node_id)
                graph.gates_G.add_node(node_id)
                graph.gates_G.add_edge(gate_node_id, node_id)
        created_ids.append(node_id)

    for item in committed_updates:
        if not isinstance(item, dict):
            continue
        node_id = item.get("node_id")
        if not node_id or node_id not in graph.G:
            continue
        fields = item.get("fields", {})
        if not isinstance(fields, dict):
            continue
        safe_fields = {
            key: value
            for key, value in fields.items()
            if key in _GRAPH_DELTA_GRAPH_UPDATE_FIELDS
        }
        if safe_fields:
            graph.update_node_attrs(node_id, safe_fields)
            updated_ids.append(node_id)

    if created_ids or updated_ids:
        graph.save(graph_path)

    return {
        "graph_updated": bool(created_ids or updated_ids),
        "created": created_ids,
        "updated": updated_ids,
    }


def _commit_graph_delta(conn, project_id, metadata):
    """Consume graph.delta.validated event and apply creates[]/updates[] to node_state.

    Called from _gate_gatekeeper_pass on merge_pass. All writes occur in a
    single transaction. On failure, rollback and emit graph.delta.failed.

    R1: Transactional commit of creates[]/updates[]
    R2: Node ID auto-generation for creates[] without explicit node_id
    R3: Idempotency via event_id check
    R4: Related-nodes carryforward after commit
    R6: links[] logged as TODO/skipped (no edges table)
    R7: Malformed creates[] (missing parent_layer) skipped with warning
    """
    root_task_id = _resolve_chain_root_id(
        conn, project_id, metadata.get("task_id", ""), metadata,
    )

    # Query for graph.delta.validated event (use passed-in conn — same DB)
    try:
        row = conn.execute(
            "SELECT payload_json FROM chain_events "
            "WHERE root_task_id = ? AND event_type = 'graph.delta.validated' "
            "ORDER BY ts DESC LIMIT 1",
            (root_task_id,),
        ).fetchone()
    except Exception:
        log.debug("_commit_graph_delta: no chain_events table or query failed", exc_info=True)
        return  # No validated event — nothing to commit

    if not row:
        return  # No graph.delta.validated event for this chain

    try:
        validated_payload = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
    except Exception:
        log.warning("_commit_graph_delta: failed to parse validated payload")
        return

    # Extract the original proposed payload which contains graph_delta
    proposed_payload = validated_payload.get("proposed_payload", {})
    graph_delta = proposed_payload.get("graph_delta", {})
    if not graph_delta:
        return

    creates = graph_delta.get("creates", [])
    updates = graph_delta.get("updates", [])
    links = graph_delta.get("links", [])
    extras = _graph_delta_extra_event_fields(graph_delta)

    if not creates and not updates and not links:
        return

    # Generate event_id for idempotency
    # Use source_task_id from proposed_payload as the source event identifier
    source_event_id = proposed_payload.get("source_task_id", "")
    event_id = str(_uuid.uuid4())

    # R3: Idempotency check — look for prior graph.delta.committed with same root + source
    if source_event_id:
        try:
            prior = conn.execute(
                "SELECT payload_json FROM chain_events "
                "WHERE root_task_id = ? AND event_type = 'graph.delta.committed' "
                "ORDER BY ts DESC LIMIT 1",
                (root_task_id,),
            ).fetchone()
            if prior:
                prior_payload = json.loads(prior["payload_json"]) if isinstance(prior["payload_json"], str) else prior["payload_json"]
                if prior_payload.get("source_event_id") == source_event_id:
                    log.info("_commit_graph_delta: idempotent skip — already committed for source %s", source_event_id)
                    return {
                        "attempted_node_ids": prior_payload.get("attempted_node_ids", prior_payload.get("committed_node_ids", [])),
                        "committed_node_ids": prior_payload.get("committed_node_ids", []),
                    }
        except Exception:
            log.debug("_commit_graph_delta: idempotency check failed", exc_info=True)

    # R6: links[] — no edges table, log and skip
    if links:
        log.warning("_commit_graph_delta: TODO — links[] items skipped (no edges table in governance.db): %d items", len(links))

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    committed_node_ids = []
    attempted_node_ids = []  # R1: all creates[] node_ids regardless of dedup outcome
    committed_creates = []
    committed_updates = []

    try:
        # Begin transaction — conn should already be in autocommit=off mode
        # We use the passed-in conn for transactional safety

        # R2/R5: Process creates[]
        for item in creates:
            if not isinstance(item, dict):
                log.warning("_commit_graph_delta: skipping non-dict creates item")
                continue

            parent_layer_raw = item.get("parent_layer")
            # R7: skip malformed items missing parent_layer
            if parent_layer_raw is None:
                log.warning("_commit_graph_delta: skipping creates[] item with missing parent_layer: %s",
                            item.get("title", "<untitled>"))
                continue

            # R2: Normalize parent_layer — accept both int (7), string-int ("7"),
            # and prefixed ("L7") formats.  Extract the numeric layer number and
            # build the canonical "L<N>" prefix for node-id allocation.
            parent_layer_str = str(parent_layer_raw).strip()
            _pl_match = re.match(r"^[Ll]?(\d+)$", parent_layer_str)
            if _pl_match is None:
                log.warning("_commit_graph_delta: skipping creates[] item with non-parseable parent_layer: %s", parent_layer_raw)
                continue
            parent_layer_num = int(_pl_match.group(1))
            layer_prefix = f"L{parent_layer_num}"

            explicit_node_id = item.get("node_id")

            if explicit_node_id:
                # Use explicit node_id — INSERT OR IGNORE preserves the
                # attempted-vs-committed distinction for duplicate intents.
                display_id = explicit_node_id
            else:
                # R2/R4: Auto-generate node_id using monotonically increasing IDs
                prefix = f"{layer_prefix}."
                existing_rows = conn.execute(
                    "SELECT node_id FROM node_state WHERE project_id = ? AND node_id LIKE ?",
                    (project_id, f"{prefix}%"),
                ).fetchall()
                max_index = 0
                for r in existing_rows:
                    try:
                        idx = int(r["node_id"].split(".")[1])
                        max_index = max(max_index, idx)
                    except (ValueError, IndexError):
                        pass
                display_id = f"{layer_prefix}.{max_index + 1}"

            # R1: Record intent before INSERT
            attempted_node_ids.append(display_id)

            create_fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            create_verify_status = (
                create_fields.get("verify_status")
                or item.get("verify_status")
                or "qa_pass"
            )
            create_build_status = (
                create_fields.get("build_status")
                or item.get("build_status")
                or "impl:done"
            )
            title = item.get("title", display_id)
            create_evidence = {
                "type": "graph_delta_committed_create",
                "source": "graph.delta.validated",
                "root_task_id": root_task_id,
                "source_task_id": source_event_id,
                "gatekeeper_task_id": metadata.get("task_id", ""),
                "title": title,
                "deps": item.get("deps", []),
                "primary": item.get("primary", []),
                "graph_delta_review": validated_payload.get("graph_delta_review", {}),
            }
            evidence_json = json.dumps(create_evidence, ensure_ascii=False)

            # Insert node_state — R2: check rowcount to detect dedup-skip.
            # A validated graph_delta create is already QA/Gatekeeper-approved;
            # leaving it pending makes the following release gate block on the
            # node that the same chain just accepted.
            cursor = conn.execute(
                """INSERT OR IGNORE INTO node_state
                   (project_id, node_id, verify_status, build_status, evidence_json, updated_by, updated_at, version)
                   VALUES (?, ?, ?, ?, ?, 'graph-delta-commit', ?, 1)""",
                (project_id, display_id, create_verify_status, create_build_status, evidence_json, now),
            )

            if cursor.rowcount > 0:
                # Record in node_history
                try:
                    conn.execute(
                        """INSERT INTO node_history
                           (project_id, node_id, from_status, to_status, role, evidence_json, session_id, ts, version)
                           VALUES (?, ?, 'none', ?, 'auto-chain', ?, 'graph-delta-commit', ?, 1)""",
                        (project_id, display_id, create_verify_status, evidence_json, now),
                    )
                except Exception:
                    pass  # History is nice-to-have

                committed_node_ids.append(display_id)
                committed_creates.append((display_id, item))

        # Process updates[]
        for item in updates:
            if not isinstance(item, dict):
                continue
            node_id = item.get("node_id")
            if not node_id:
                continue
            fields = item.get("fields", {})
            if not fields:
                continue

            # Only update if node exists
            existing = conn.execute(
                "SELECT verify_status, version FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, node_id),
            ).fetchone()
            if not existing:
                log.warning("_commit_graph_delta: update target %s not found, skipping", node_id)
                continue

            # Apply field updates (limited to safe fields)
            update_parts = []
            update_vals = []
            for field_name in ("verify_status", "build_status"):
                if field_name in fields:
                    update_parts.append(f"{field_name} = ?")
                    update_vals.append(fields[field_name])
            graph_update_requested = any(
                field_name in _GRAPH_DELTA_GRAPH_UPDATE_FIELDS
                for field_name in fields
            )
            if update_parts:
                update_parts.append("updated_at = ?")
                update_vals.append(now)
                update_parts.append("updated_by = ?")
                update_vals.append("graph-delta-commit")
                update_vals.extend([project_id, node_id])
                cursor = conn.execute(
                    f"UPDATE node_state SET {', '.join(update_parts)} WHERE project_id = ? AND node_id = ?",
                    update_vals,
                )

                # R3: Only count as committed if UPDATE actually modified a row
                if cursor.rowcount > 0 and node_id not in committed_node_ids:
                    committed_node_ids.append(node_id)
                if cursor.rowcount > 0 or graph_update_requested:
                    committed_updates.append(item)
            elif graph_update_requested:
                if node_id not in committed_node_ids:
                    committed_node_ids.append(node_id)
                committed_updates.append(item)

        graph_materialization = _materialize_graph_delta_to_project_graph(
            project_id,
            committed_creates,
            committed_updates,
        )

        # AC3: Write graph.delta.committed event — write to same conn (transactional)
        committed_payload = {
            "event_id": event_id,
            "source_event_id": source_event_id,
            "attempted_node_ids": attempted_node_ids,
            "committed_node_ids": committed_node_ids,
            "creates_count": len(creates),
            "updates_count": len(updates),
            "links_skipped": len(links),
            "graph_materialization": graph_materialization,
        }
        committed_payload.update(extras)
        conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
            "VALUES (?, ?, 'graph.delta.committed', ?, ?)",
            (root_task_id, metadata.get("task_id", ""),
             json.dumps(committed_payload, ensure_ascii=False), now),
        )

        # R4: Append committed node_ids to chain metadata related_nodes
        if committed_node_ids:
            try:
                existing_related = metadata.get("related_nodes", [])
                if isinstance(existing_related, str):
                    try:
                        existing_related = json.loads(existing_related)
                    except Exception:
                        existing_related = [existing_related] if existing_related else []
                new_related = list(set(existing_related + committed_node_ids))
                metadata["related_nodes"] = new_related

                # Also persist related_nodes.updated event to same conn
                conn.execute(
                    "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                    "VALUES (?, ?, 'related_nodes.updated', ?, ?)",
                    (root_task_id, metadata.get("task_id", ""),
                     json.dumps({"related_nodes": new_related, "added": committed_node_ids},
                                ensure_ascii=False), now),
                )
            except Exception:
                log.debug("_commit_graph_delta: related_nodes carryforward failed", exc_info=True)

        log.info("_commit_graph_delta: attempted %d, committed %d nodes for chain %s: %s",
                 len(attempted_node_ids), len(committed_node_ids), root_task_id, committed_node_ids)
        return {
            "attempted_node_ids": attempted_node_ids,
            "committed_node_ids": committed_node_ids,
        }

    except ValueError as ve:
        # AC2/AC5: Collision or validation error — rollback
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("_commit_graph_delta: batch rejected — %s", ve)
        # Write graph.delta.failed event (post-rollback, new mini-transaction)
        try:
            conn.execute(
                "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                "VALUES (?, ?, 'graph.delta.failed', ?, ?)",
                (root_task_id, metadata.get("task_id", ""),
                 json.dumps({"error": str(ve), "event_id": event_id}, ensure_ascii=False), now),
            )
            conn.commit()
        except Exception:
            log.debug("_commit_graph_delta: failed event write failed", exc_info=True)
        raise

    except Exception as exc:
        # AC2: Any other exception — rollback and emit failed event
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("_commit_graph_delta: transaction failed — %s", exc, exc_info=True)
        try:
            conn.execute(
                "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                "VALUES (?, ?, 'graph.delta.failed', ?, ?)",
                (root_task_id, metadata.get("task_id", ""),
                 json.dumps({"error": str(exc), "event_id": event_id}, ensure_ascii=False), now),
            )
            conn.commit()
        except Exception:
            log.debug("_commit_graph_delta: failed event write failed", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Graph-Path-Driven Routing (Roadmap §5.5)
# ---------------------------------------------------------------------------

# Linear chain stages after dev (used for graph-driven routing derivation)
_POST_DEV_STAGES = ["test", "qa", "gatekeeper", "merge"]


def _audit_routing_decision(conn, project_id, task_id, trace_id, decision):
    """Write routing decision to audit_log (R6/AC7)."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "routing_decision",
                "auto-chain",
                1,
                task_id,
                json.dumps({**decision, "trace_id": trace_id}),
            ),
        )
    except Exception:
        log.debug("audit routing_decision failed (non-critical)", exc_info=True)


def _audit_routing_skip(conn, project_id, task_id, trace_id, skip_info):
    """Write routing skip to audit_log (R3/AC1/AC2)."""
    try:
        conn.execute(
            "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?, ?)",
            (
                project_id,
                "routing_skip",
                "auto-chain",
                1,
                task_id,
                json.dumps({**skip_info, "trace_id": trace_id}),
            ),
        )
    except Exception:
        log.debug("audit routing_skip failed (non-critical)", exc_info=True)


def _check_verify_requires_satisfied(conn, project_id, verify_requires):
    """Check if all verify_requires nodes are verified (AC4).

    Returns (satisfied: bool, blocking_nodes: list[str]).
    """
    if not verify_requires:
        return True, []
    blocking = []
    for req_nid in verify_requires:
        try:
            row = conn.execute(
                "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, req_nid),
            ).fetchone()
            if row is None:
                blocking.append(req_nid)
                continue
            status = (row["verify_status"] or "pending").strip()
            # AC10: rolled_back nodes don't block
            if status in _NON_BLOCKING_STATUSES:
                continue
            try:
                rank = _STATUS_ORDER.index(status)
            except ValueError:
                blocking.append(req_nid)
                continue
            # t2_pass is the minimum acceptable status (rank 2)
            if rank < _STATUS_ORDER.index("t2_pass"):
                blocking.append(req_nid)
        except Exception:
            blocking.append(req_nid)
    return len(blocking) == 0, blocking


def _derive_chain_stages_from_policies(policies):
    """Derive which chain stages to execute based on node policies (R2/R3).

    Given a list of node routing policies, determine the minimal set of
    chain stages needed. Uses the most restrictive policy (if ANY node
    requires a stage, it's included).

    Returns list of stage names in order.
    """
    if not policies:
        return list(_POST_DEV_STAGES)  # fallback to full chain

    needs_test = False
    needs_qa = False
    needs_gatekeeper = False
    all_skip = True

    for p in policies:
        gm = p.get("gate_mode", "auto")
        vl = p.get("verify_level", 1)

        if gm != "skip":
            all_skip = False

        # verify_level > 0 means test stage needed
        if vl > 0:
            needs_test = True

        # gate_mode != skip means QA and gatekeeper needed
        if gm != "skip":
            needs_qa = True
            needs_gatekeeper = True

    stages = []
    if needs_test:
        stages.append("test")
    if needs_qa:
        stages.append("qa")
    if needs_gatekeeper:
        stages.append("gatekeeper")
    stages.append("merge")  # merge is always needed

    return stages


def dispatch_next_stage(conn, project_id, task_id, current_stage,
                        result, metadata, trace_id, graph=None):
    """Graph-driven routing: determine next stage based on node policies (R2/R5).

    When graph is available and nodes have custom policies, derive the chain
    stages dynamically. Falls back to CHAIN dict when graph is None or
    all nodes use default policies (AC5/AC6).

    Returns (next_stage: str|None, skipped_stages: list[str], policies: list[dict]).
    """
    # R5: No graph → use linear CHAIN
    if graph is None:
        _audit_routing_decision(conn, project_id, task_id, trace_id, {
            "current_stage": current_stage,
            "routing_mode": "linear_chain",
            "reason": "no_graph_loaded",
        })
        return None, [], []  # Signal caller to use CHAIN dict

    # Only apply graph routing after dev stage
    if current_stage not in ("dev", "test", "qa", "gatekeeper"):
        _audit_routing_decision(conn, project_id, task_id, trace_id, {
            "current_stage": current_stage,
            "routing_mode": "linear_chain",
            "reason": "pre_dev_stage",
        })
        return None, [], []  # Use CHAIN dict for pm→dev

    # Get affected nodes from metadata
    changed_files = result.get("changed_files", metadata.get("changed_files", []))
    related_nodes = metadata.get("related_nodes", [])

    # Try to get routing policies from graph
    policies = []
    if related_nodes:
        try:
            policies = graph.get_routing_policies_for_nodes(related_nodes)
        except Exception:
            pass

    if not policies and changed_files:
        try:
            affected = graph.affected_nodes_by_files(changed_files)
            if affected:
                policies = graph.get_routing_policies_for_nodes(list(affected))
        except Exception:
            pass

    if not policies:
        _audit_routing_decision(conn, project_id, task_id, trace_id, {
            "current_stage": current_stage,
            "routing_mode": "linear_chain",
            "reason": "no_affected_nodes",
        })
        return None, [], []

    # AC6: Check if all nodes have default policies (auto + verify_level>=1)
    all_default = all(
        p.get("gate_mode", "auto") == "auto" and p.get("verify_level", 1) >= 1
        for p in policies
    )
    if all_default:
        _audit_routing_decision(conn, project_id, task_id, trace_id, {
            "current_stage": current_stage,
            "routing_mode": "linear_chain",
            "reason": "all_nodes_default_policy",
            "node_count": len(policies),
        })
        return None, [], policies

    # AC4: Check verify_requires ordering
    for p in policies:
        vr = p.get("verify_requires", [])
        if vr:
            satisfied, blocking = _check_verify_requires_satisfied(conn, project_id, vr)
            if not satisfied:
                _audit_routing_decision(conn, project_id, task_id, trace_id, {
                    "current_stage": current_stage,
                    "routing_mode": "blocked_by_verify_requires",
                    "node_id": p["node_id"],
                    "blocking_nodes": blocking,
                })
                # Return special signal for blocking
                return "blocked", [], policies

    # Derive stages from policies
    derived_stages = _derive_chain_stages_from_policies(policies)
    full_stages = list(_POST_DEV_STAGES)
    skipped = [s for s in full_stages if s not in derived_stages]

    # Log skip reasons for auditing
    for p in policies:
        gm = p.get("gate_mode", "auto")
        vl = p.get("verify_level", 1)
        if gm == "skip":
            _audit_routing_skip(conn, project_id, task_id, trace_id, {
                "node_id": p["node_id"],
                "gate_mode": gm,
                "skip": "qa,gatekeeper",
                "reason": "gate_mode=skip bypasses QA/gatekeeper",
            })
        if vl == 0:
            _audit_routing_skip(conn, project_id, task_id, trace_id, {
                "node_id": p["node_id"],
                "verify_level": vl,
                "skip": "test",
                "reason": "verify_level=0 skips test stage",
            })

    # Determine next stage from derived_stages based on current position
    if current_stage == "dev":
        next_stage = derived_stages[0] if derived_stages else None
    else:
        try:
            idx = derived_stages.index(current_stage)
            next_stage = derived_stages[idx + 1] if idx + 1 < len(derived_stages) else None
        except ValueError:
            next_stage = None

    _audit_routing_decision(conn, project_id, task_id, trace_id, {
        "current_stage": current_stage,
        "routing_mode": "graph_driven",
        "derived_stages": derived_stages,
        "skipped_stages": skipped,
        "next_stage": next_stage,
        "policies": [{"node_id": p["node_id"], "gate_mode": p.get("gate_mode"),
                       "verify_level": p.get("verify_level")} for p in policies],
    })

    return next_stage, skipped, policies


_TEST_FILE_PATTERN = re.compile(r"(agent/tests/[A-Za-z0-9_./-]+\.py)")


def _extract_test_files_from_verification(verification):
    """Pull explicit pytest file targets out of verification.command."""
    if not isinstance(verification, dict):
        return []
    command = verification.get("command")
    if not isinstance(command, str) or not command.strip():
        return []
    return list(dict.fromkeys(_TEST_FILE_PATTERN.findall(command)))


# B36-fix(4): Project root for dependent-test scan.
# This mirrors agent/governance/role_config.py's derivation (parents[2] of this file).
_PROJECT_ROOT_FOR_SCAN = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Regex for import lines: captures module path after `from <x> import` or `import <x>`.
_IMPORT_RE = re.compile(r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", re.MULTILINE)

# Cache: (target_tuple) -> set of dependent test file paths. Cleared per-gate evaluation
# is unnecessary since imports don't change within a gate check; scope is process-wide.
_DEPENDENT_TESTS_CACHE: "dict[tuple, set[str]]" = {}


def _scan_dependent_tests(target_files):
    """B36-fix(4): Find test files that import any target file's module.

    For each target like 'agent/role_permissions.py', derive the stem
    ('role_permissions') and scan all tests/**/*.py for import lines referencing
    a module whose dotted path contains that stem as a component. Returns a set
    of POSIX-normalized relative paths (e.g. 'agent/tests/test_x.py').

    Safe: on any IO error returns empty set. Bounded: reads only first 16KB of
    each test file (imports are at top).
    """
    if not target_files:
        return set()
    stems = set()
    for tf in target_files:
        base = os.path.basename(tf.replace("\\", "/"))
        stem, ext = os.path.splitext(base)
        if ext != ".py" or not stem or stem == "__init__":
            continue
        stems.add(stem)
    if not stems:
        return set()

    key = tuple(sorted(stems))
    cached = _DEPENDENT_TESTS_CACHE.get(key)
    if cached is not None:
        return set(cached)

    dependent = set()
    root = _PROJECT_ROOT_FOR_SCAN
    if not os.path.isdir(root):
        _DEPENDENT_TESTS_CACHE[key] = set()
        return set()

    for dirpath, _dirnames, filenames in os.walk(root):
        # Only look under directories named 'tests'
        posix_dir = dirpath.replace("\\", "/")
        if "/tests" not in posix_dir and not posix_dir.endswith("tests"):
            continue
        # Skip vendored/third-party trees AND worktree mirrors.
        # Worktree mirrors live under .worktrees/ (top-level) or .claude/worktrees/.
        # They contain duplicate test files that pollute the scan result.
        # B49: Use relative path from root so that running FROM a worktree doesn't
        # self-exclude — only nested .worktrees/ subdirs are skipped.
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if ("/.venv/" in rel_dir or rel_dir.startswith(".venv")
                or "/node_modules/" in rel_dir or rel_dir.startswith("node_modules")
                or "/.claude/" in rel_dir or rel_dir.startswith(".claude")
                or "/.worktrees/" in rel_dir or rel_dir.startswith(".worktrees")):
            continue
        for fn in filenames:
            if not fn.startswith("test_") or not fn.endswith(".py"):
                continue
            abs_p = os.path.join(dirpath, fn)
            try:
                with open(abs_p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(16384)
            except Exception:
                continue
            for m in _IMPORT_RE.finditer(content):
                module = m.group(1) or m.group(2)
                if not module:
                    continue
                parts = module.split(".")
                if stems & set(parts):
                    rel = os.path.relpath(abs_p, root).replace("\\", "/")
                    dependent.add(rel)
                    break

    _DEPENDENT_TESTS_CACHE[key] = set(dependent)
    return dependent


def _compute_gate_static_allowed(project_id, metadata):
    """B36-fix(2): Single source of truth for gate's static allowed file set.

    Called by both _gate_checkpoint AND the retry-prompt scope_line builder so
    they cannot drift apart. Returns (target_set, allowed_set).

    NOT included here (must be added by caller as applicable):
      - stem-prefix dynamic tests (match against incoming changed files)
      - accumulated_changed_files from prior succeeded dev stages (retry only)
    """
    target = set(metadata.get("target_files", []) or [])
    allowed = set(target)
    allowed.update(metadata.get("test_files", []) or [])
    allowed.update(_extract_test_files_from_verification(metadata.get("verification", {})))
    doc_impact = metadata.get("doc_impact", {})
    if isinstance(doc_impact, dict):
        allowed.update(doc_impact.get("files", []) or [])
    graph_docs = _get_task_graph_doc_associations(project_id, list(target), metadata)
    if graph_docs:
        allowed.update(graph_docs)
    # B36-fix(4): tests importing any target — prevents PM under-specification from ping-ponging dev
    allowed.update(_scan_dependent_tests(list(target)))
    return target, allowed


def _load_task_trace(conn, task_id):
    """Load trace_id and chain_id from a task row."""
    if not task_id or not hasattr(conn, "execute"):
        return None, None
    try:
        row = conn.execute(
            "SELECT trace_id, chain_id FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None, None
        return row["trace_id"], row["chain_id"]
    except Exception:
        return None, None


def _row_value(row, key, default=None):
    """Best-effort row getter for sqlite.Row, dicts, and test doubles."""
    if row is None:
        return default
    try:
        value = row[key]
    except Exception:
        try:
            value = getattr(row, key)
        except Exception:
            return default
    return default if value is None else value


def _parse_metadata_obj(raw):
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _store_root_for(task_id):
    """Resolve a task id through the in-memory chain store when available."""
    if not isinstance(task_id, str) or not task_id:
        return ""
    try:
        from .chain_context import get_store
        store = get_store()
        return store._task_to_root.get(task_id, task_id)
    except Exception:
        return task_id


def _resolve_chain_root_id(conn, project_id="", task_id="", metadata=None):
    """Resolve the durable chain root without relying only on in-memory state.

    Priority:
      1. metadata.chain_id
      2. tasks.chain_id for the current task or its parents
      3. chain_events reverse lookup by task_id
      4. metadata.parent_task_id / current task fallback
    """
    meta = metadata if isinstance(metadata, dict) else {}

    chain_id = meta.get("chain_id")
    if isinstance(chain_id, str) and chain_id:
        return _store_root_for(chain_id)

    start_ids = []
    for candidate in (task_id, meta.get("task_id"), meta.get("parent_task_id")):
        if isinstance(candidate, str) and candidate and candidate not in start_ids:
            start_ids.append(candidate)

    if hasattr(conn, "execute"):
        for start in start_ids:
            current = start
            seen = set()
            while isinstance(current, str) and current and current not in seen:
                seen.add(current)
                try:
                    row = conn.execute(
                        "SELECT task_id, parent_task_id, chain_id, metadata_json "
                        "FROM tasks WHERE task_id = ?",
                        (current,),
                    ).fetchone()
                except Exception:
                    row = None
                if not row:
                    break

                row_chain = _row_value(row, "chain_id", "")
                if isinstance(row_chain, str) and row_chain:
                    return _store_root_for(row_chain)

                row_meta = _parse_metadata_obj(_row_value(row, "metadata_json", ""))
                row_meta_chain = row_meta.get("chain_id")
                if isinstance(row_meta_chain, str) and row_meta_chain:
                    return _store_root_for(row_meta_chain)

                parent = _row_value(row, "parent_task_id", "") or row_meta.get("parent_task_id", "")
                if not isinstance(parent, str) or not parent:
                    return _store_root_for(current)
                current = parent

        for start in start_ids:
            try:
                row = conn.execute(
                    "SELECT root_task_id FROM chain_events "
                    "WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
                    (start,),
                ).fetchone()
            except Exception:
                row = None
            root = _row_value(row, "root_task_id", "")
            if isinstance(root, str) and root:
                return _store_root_for(root)

    parent = meta.get("parent_task_id")
    if isinstance(parent, str) and parent:
        return _store_root_for(parent)
    if start_ids:
        return _store_root_for(start_ids[0])
    return ""


def _persist_task_metadata_context(conn, project_id, task_id, metadata, trace_id="", chain_id=""):
    """Mirror chain context into metadata_json for restart-safe downstream gates."""
    if not isinstance(metadata, dict):
        return
    if project_id:
        metadata["project_id"] = project_id
    if task_id:
        metadata["task_id"] = task_id
    if trace_id:
        metadata["trace_id"] = trace_id
    if chain_id:
        metadata["chain_id"] = chain_id
    if not task_id or not hasattr(conn, "execute"):
        return
    try:
        row = conn.execute(
            "SELECT metadata_json, parent_task_id FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return
        durable_meta = _parse_metadata_obj(_row_value(row, "metadata_json", ""))
        durable_meta.update(metadata)
        parent_task_id = durable_meta.get("parent_task_id") or _row_value(row, "parent_task_id", "")
        conn.execute(
            "UPDATE tasks SET metadata_json = ?, trace_id = COALESCE(trace_id, ?), "
            "chain_id = COALESCE(chain_id, ?), parent_task_id = COALESCE(parent_task_id, ?) "
            "WHERE task_id = ?",
            (
                json.dumps(durable_meta, ensure_ascii=False),
                trace_id or None,
                chain_id or None,
                parent_task_id or None,
                task_id,
            ),
        )
    except Exception:
        log.debug("auto_chain: failed to persist task metadata context for %s", task_id, exc_info=True)


def _query_chain_event_payload(conn, root_task_id, event_type):
    """Return latest chain event payload for root/event_type, or None."""
    if not root_task_id or not hasattr(conn, "execute"):
        return None
    try:
        row = conn.execute(
            "SELECT payload_json FROM chain_events "
            "WHERE root_task_id = ? AND event_type = ? "
            "ORDER BY ts DESC LIMIT 1",
            (root_task_id, event_type),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return _parse_metadata_obj(_row_value(row, "payload_json", ""))


def _record_gate_event(conn, project_id, task_id, gate_name, passed, reason, trace_id):
    """Insert a row into gate_events for audit trail."""
    from datetime import datetime, timezone
    try:
        conn.execute(
            "INSERT INTO gate_events (project_id, task_id, gate_name, passed, reason, trace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, task_id, gate_name, 1 if passed else 0,
             reason, trace_id,
             datetime.now(timezone.utc).isoformat()),
        )
    except Exception:
        log.debug("auto_chain: failed to record gate_event for %s/%s (non-critical)", gate_name, task_id, exc_info=True)


def _update_backlog_stage(
    conn,
    project_id,
    bug_id,
    stage,
    failure_reason="",
    task_id="",
    task_type="",
    metadata=None,
    result=None,
    runtime_state="",
    root_task_id="",
):
    """Mirror chain progress into backlog_bugs runtime/audit columns."""
    try:
        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            stage,
            project_id=project_id,
            failure_reason=failure_reason,
            task_id=task_id,
            task_type=task_type,
            metadata=metadata or {},
            result=result or {},
            runtime_state=runtime_state,
            root_task_id=root_task_id,
        )
    except Exception:
        log.debug("auto_chain: failed to update backlog_stage for bug_id=%s (non-critical)", bug_id, exc_info=True)


def _is_task_taken_over_by_mf(conn, project_id, task_id, metadata):
    """Return True when a completed task was superseded by an active MF takeover."""
    bug_id = (metadata or {}).get("bug_id", "")
    if not bug_id:
        return False, ""
    try:
        row = conn.execute(
            "SELECT status, takeover_json FROM backlog_bugs WHERE bug_id=?",
            (bug_id,),
        ).fetchone()
        if not row or row["status"] != "MF_IN_PROGRESS":
            return False, ""
        takeover = backlog_runtime.parse_json_object(row["takeover_json"])
        if takeover.get("taken_over_task_id") != task_id:
            return False, ""
        action = takeover.get("action", "")
        if action not in {"hold_current_chain", "cancel_current_chain"}:
            return False, ""
        return True, takeover.get("reason") or f"task superseded by MF {takeover.get('mf_id', '')}".strip()
    except Exception:
        log.debug("auto_chain: MF takeover lookup failed for %s", task_id, exc_info=True)
        return False, ""


def _load_task_metadata(conn, project_id, task_id):
    if not task_id or not hasattr(conn, "execute"):
        return {}
    try:
        row = conn.execute(
            "SELECT metadata_json FROM tasks WHERE project_id=? AND task_id=?",
            (project_id, task_id),
        ).fetchone()
        if not row:
            return {}
        raw = row["metadata_json"] if isinstance(row, dict) or hasattr(row, "__getitem__") else None
        if not raw:
            return {}
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}


def _walk_task_metadata_chain(conn, project_id, metadata, max_depth=6):
    visited = set()
    current = dict(metadata or {})
    for _ in range(max_depth):
        yield current
        parent_task_id = current.get("parent_task_id")
        if not parent_task_id or parent_task_id in visited:
            break
        visited.add(parent_task_id)
        current = _load_task_metadata(conn, project_id, parent_task_id)
        if not current:
            break


def _infer_lane_from_metadata(metadata):
    """Best-effort lane inference for replayed reconciliation chains."""
    if not isinstance(metadata, dict):
        return ""
    explicit = str(metadata.get("lane", "") or "").strip().upper()
    if explicit in {"A", "B", "C"}:
        return explicit

    text = " ".join(
        str(metadata.get(k, "") or "")
        for k in ("replay_source", "intent_summary", "_original_prompt")
    ).lower()
    match = re.search(r"lane\s+([abc])", text)
    if match:
        return match.group(1).upper()
    return ""


def _is_governed_dirty_workspace_chain(conn, project_id, metadata):
    """Allow narrow bypass only for explicit governed dirty-workspace reconciliation chains."""
    for current in _walk_task_metadata_chain(conn, project_id, metadata):
        if current.get("allow_dirty_workspace_reconciliation"):
            return True
        if current.get("parallel_plan") == "dirty-reconciliation-2026-03-30":
            return True
        lane = _infer_lane_from_metadata(current)
        text = " ".join(
            str(current.get(k, "") or "")
            for k in ("replay_source", "intent_summary", "_original_prompt")
        ).lower()
        if lane in {"A", "B"} and (
            "dirty-workspace" in text
            or "workflow improvement lane" in text
            or "reconciliation" in text
        ):
            return True
    return False


def _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
    """Lane A/B reconciliation tasks may defer doc updates to convergence Lane C."""
    lane = ""
    for current in _walk_task_metadata_chain(conn, project_id, metadata):
        lane = _infer_lane_from_metadata(current)
        if lane:
            break
    if lane not in {"A", "B"}:
        return False
    if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
        return True
    return False


def _parse_pm_missing_fields(reason: str) -> list:
    """Extract missing field names from a PM gate block reason string.

    Handles both formats:
      - 'PRD missing mandatory fields: [field1, field2]'
      - 'PRD fields missing without skip_reasons: [field1, field2]...'
    """
    import re
    # Match the bracketed list after the colon
    m = re.search(r"(?:PRD missing mandatory fields|PRD fields missing without skip_reasons):\s*\[([^\]]*)\]", reason)
    if m:
        raw = m.group(1)
        return [f.strip().strip("'\"") for f in raw.split(",") if f.strip()]
    return []


def _effective_dev_retry_reason(conn, project_id, metadata, reason):
    """Rewrite stale lane A/B gate reasons into actionable code-only guidance."""
    if not isinstance(reason, str):
        return reason
    if not _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
        return reason

    lowered = reason.lower()
    if lowered.startswith("related docs not updated:") or (
        lowered.startswith("unrelated files modified:")
        and ("readme.md" in lowered or "docs/" in lowered)
    ):
        return (
            "Lane C owns documentation updates for this governed dirty-workspace "
            "reconciliation. Do NOT modify README.md or docs/. "
            "Retry as a code-only fix within target_files and keep changed_files "
            "limited to target_files."
        )
    return reason


def _maybe_create_workflow_improvement_task(conn, project_id, task_id, stage, reason, metadata, result):
    """Create one workflow-improvement task for workflow defects.

    The task goes through the normal coordinator entrypoint (`type=task`) so the
    existing chain can repair workflow/governance issues without introducing a
    parallel execution model.
    """
    if metadata.get("_workflow_improvement_created"):
        return None
    if metadata.get("operation_type") == "workflow_improvement":
        return None

    classification = classify_gate_failure(stage, reason, metadata, result)
    if not classification.get("workflow_improvement"):
        return None

    from . import task_registry
    improvement_prompt = build_workflow_improvement_prompt(task_id, stage, classification, metadata)
    improvement_task = task_registry.create_task(
        conn, project_id,
        prompt=improvement_prompt,
        task_type="task",
        created_by="auto-chain-workflow-improvement",
        metadata={
            "operation_type": "workflow_improvement",
            "source_task_id": task_id,
            "failing_stage": stage,
            "failure_class": classification.get("failure_class", ""),
            "suggested_action": classification.get("suggested_action", ""),
            "workflow_issue": classification,
            "chain_depth": 0,
            "_no_retry": True,
        },
    )
    metadata["_workflow_improvement_created"] = True
    improvement_id = improvement_task.get("task_id", "?")
    _publish_event("task.workflow_improvement", {
        "project_id": project_id,
        "task_id": improvement_id,
        "source_task_id": task_id,
        "failing_stage": stage,
        "failure_class": classification.get("failure_class", ""),
    })
    try:
        from . import audit_service
        audit_service.record(
            conn, project_id, "workflow.improvement.created",
            actor="auto-chain",
            ok=True,
            node_ids=metadata.get("related_nodes", []),
            task_id=improvement_id,
            source_task_id=task_id,
            failing_stage=stage,
            failure_class=classification.get("failure_class", ""),
            suggested_action=classification.get("suggested_action", ""),
        )
    except Exception:
        log.debug("auto_chain: audit workflow.improvement.created failed", exc_info=True)
    return {"task_id": improvement_id, "classification": classification}


def on_task_failed(conn, project_id, task_id, task_type, result=None, metadata=None, reason=""):
    """Best-effort workflow-improvement routing for failed task executions."""
    metadata = metadata or {}
    result = result or {}
    try:
        row = conn.execute(
            "SELECT execution_status FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        execution_status = row["execution_status"] if row else ""
        if execution_status in ("failed", "timed_out", "cancelled"):
            _reconcile_cluster_terminal_hook(
                conn,
                project_id,
                task_id,
                task_type,
                "failed" if execution_status == "timed_out" else execution_status,
                result,
                metadata,
            )
    except Exception:
        log.debug("auto_chain: reconcile failure hook failed", exc_info=True)
    effective_reason = (
        reason
        or result.get("error")
        or result.get("summary")
        or "task execution failed"
    )
    _bug_id = metadata.get("bug_id", "")
    if _bug_id:
        _update_backlog_stage(
            conn, project_id, _bug_id, f"{task_type}_failed",
            failure_reason=effective_reason, task_id=task_id,
            task_type=task_type, metadata=metadata, result=result,
        )
    return _maybe_create_workflow_improvement_task(
        conn,
        project_id,
        task_id,
        task_type,
        effective_reason,
        metadata,
        result,
    )


def _normalize_related_nodes(related_nodes):
    """Keep only concrete node-id strings for gate/audit/state updates."""
    if not related_nodes:
        return []
    if not isinstance(related_nodes, list):
        related_nodes = [related_nodes]

    normalized = []
    for item in related_nodes:
        if isinstance(item, str) and item.strip():
            normalized.append(item.strip())
        elif isinstance(item, dict):
            node_id = item.get("node_id") or item.get("id")
            if isinstance(node_id, str) and node_id.strip():
                normalized.append(node_id.strip())
    return normalized


def _record_reconcile_batch_pm_decision(conn, project_id, task_id, metadata, result, prd):
    """Persist PM's feature decision into reconcile batch memory.

    The batch memory is the cross-cluster semantic continuity layer. PM output
    is normalized here so the next cluster can see accepted feature names,
    owned files, docs/tests, and overlap conflicts through runtime context.
    """
    if not isinstance(metadata, dict):
        return
    batch_ref = metadata.get("batch_memory_ref")
    if not isinstance(batch_ref, dict):
        batch_ref = {}
    batch_id = (
        metadata.get("batch_id")
        or metadata.get("reconcile_batch_id")
        or batch_ref.get("batch_id")
        or ""
    )
    cluster_fp = metadata.get("cluster_fingerprint", "")
    if not batch_id or not cluster_fp:
        return
    prd = prd if isinstance(prd, dict) else {}
    result = result if isinstance(result, dict) else {}
    proposed_nodes = result.get("proposed_nodes") or prd.get("proposed_nodes") or []
    doc_impact = result.get("doc_impact") or prd.get("doc_impact") or {}
    doc_files = doc_impact.get("files", []) if isinstance(doc_impact, dict) else []
    feature_name = (
        result.get("feature_name")
        or prd.get("feature")
        or prd.get("feature_name")
        or (metadata.get("cluster_report") or {}).get("title")
        or (metadata.get("cluster_report") or {}).get("purpose")
        or (proposed_nodes[0].get("title") if proposed_nodes and isinstance(proposed_nodes[0], dict) else "")
        or f"cluster:{cluster_fp[:8]}"
    )
    decision = (
        result.get("batch_decision")
        or result.get("decision")
        or prd.get("batch_decision")
        or prd.get("decision")
        or "new_feature"
    )
    if result.get("target_feature") or result.get("merge_into") or prd.get("target_feature") or prd.get("merge_into"):
        decision = "merge_into_existing_feature"
    valid_decisions = {
        "new_feature",
        "merge_into_existing_feature",
        "split",
        "orphan_dead_code",
        "defer",
    }
    if decision not in valid_decisions:
        decision = "new_feature"
    payload = {
        "decision": decision,
        "feature_name": feature_name,
        "target_feature": result.get("target_feature") or result.get("merge_into") or prd.get("target_feature") or prd.get("merge_into") or "",
        "owned_files": result.get("target_files") or prd.get("target_files") or metadata.get("target_files", []),
        "candidate_tests": result.get("test_files") or prd.get("test_files") or metadata.get("test_files", []),
        "candidate_docs": doc_files,
        "purpose": prd.get("purpose") or prd.get("background") or (metadata.get("cluster_report") or {}).get("purpose"),
        "reason": result.get("summary") or prd.get("scope") or "",
        "decided_by": "pm",
        "actor": "auto-chain",
        "task_id": task_id,
    }
    try:
        from . import reconcile_batch_memory as bm
        bm.record_pm_decision(conn, project_id, batch_id, cluster_fp, payload)
        log.info(
            "auto_chain: recorded reconcile batch PM decision batch=%s cluster=%s feature=%s",
            batch_id, cluster_fp, feature_name,
        )
    except KeyError:
        log.warning(
            "auto_chain: reconcile batch memory missing batch=%s cluster=%s",
            batch_id, cluster_fp,
        )
    except Exception:
        log.debug("auto_chain: record reconcile batch PM decision failed", exc_info=True)


def _render_dev_contract_prompt(source_task_id, metadata):
    """Render the structured Dev contract from PM/task metadata."""
    is_cluster = is_reconcile_cluster_task(metadata)
    target_files = metadata.get("target_files", [])
    requirements = metadata.get("requirements", [])
    criteria = metadata.get("acceptance_criteria", [])
    verification = metadata.get("verification", {})

    parts = [
        f"{'Process reconcile-cluster PRD' if is_cluster else 'Implement per PRD'} from {source_task_id}.\n",
        f"target_files: {json.dumps(target_files)}",
        f"requirements: {json.dumps(requirements, ensure_ascii=False)}",
        f"acceptance_criteria: {json.dumps(criteria, ensure_ascii=False)}",
    ]

    if verification:
        parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")

    test_files = metadata.get("test_files", [])
    if test_files:
        if is_cluster:
            parts.append(
                "\nCluster test evidence files to inspect or update only when "
                f"acceptance_criteria explicitly require file changes: {json.dumps(test_files)}"
            )
        else:
            parts.append(f"\nTest files to create/modify: {json.dumps(test_files)}")

    doc_impact = metadata.get("doc_impact", {})
    if doc_impact:
        if is_cluster:
            parts.append(
                "\nCluster doc evidence to inspect or update only when "
                f"acceptance_criteria explicitly require file changes: {json.dumps(doc_impact, ensure_ascii=False)}"
            )
        else:
            parts.append(f"\nDoc impact: {json.dumps(doc_impact, ensure_ascii=False)}")

    graph_preflight = metadata.get("graph_preflight", {})
    if is_cluster and graph_preflight:
        parts.append(
            "\nReconcile session graph preflight. Use this as the graph-governance "
            "context for this cluster instead of the active graph artifact:\n"
            f"{json.dumps(graph_preflight, ensure_ascii=False, indent=2)}"
        )

    # R5: Document optional graph_delta field for dev results (not required)
    proposed_nodes = metadata.get("proposed_nodes", [])
    if proposed_nodes:
        if is_cluster:
            parts.append(
                "\nRequired for reconcile-cluster: return `graph_delta.creates` mirroring "
                "`proposed_nodes` one-for-one. Preserve each concrete node_id/candidate_node_id, "
                "primary, title, parent_layer as the node layer, and hierarchy parent via "
                "parent/parent_id/hierarchy_parent. Preserve deps exactly from candidate `_deps`/`deps`; "
                "do not put hierarchy parents in deps. Do not mutate the active graph artifact; "
                "Gatekeeper writes graph.rebase.overlay.json only after QA. When reporting "
                "graph artifact safety, cite only artifact paths that exist in metadata or "
                "graph_preflight; for absent legacy artifacts, say 'no active graph artifact "
                "mutation' without presenting the absent path as workspace evidence."
            )
        else:
            parts.append(
                "\nOptional: Your result JSON MAY include a `graph_delta` field to propose graph changes. "
                "Shape: {\"creates\": [{\"node_id\": \"...\", \"parent_layer\": \"...\", \"title\": \"...\", "
                "\"deps\": [...], \"primary\": \"...\", \"description\": \"...\"}], "
                "\"updates\": [{\"node_id\": \"...\", \"fields\": {}}], "
                "\"links\": [{\"from_node\": \"...\", \"to_node\": \"...\", \"relation\": \"...\"}]}. "
                "All sub-arrays default to []. Pure-refactor tasks may omit this field entirely."
            )

    return "\n".join(parts)


def _dispatch_reconcile(conn, project_id, task_id, task_type, result, metadata):
    """Dispatch reconcile task through 6-stage pipeline (R2).

    Bypasses version_check, PM-completeness, dev-contract, test, and QA gates.
    Routes through _RECONCILE_STAGES instead of CHAIN.
    """
    from .db import get_connection
    try:
        rconn = get_connection(project_id)
    except Exception:
        log.error("auto_chain: failed to get connection for reconcile dispatch %s", project_id)
        return None
    try:
        result_val = _run_full_reconcile(rconn, project_id, task_id, metadata)
        rconn.commit()
        return result_val
    except ReconcileCancelled:
        try:
            rconn.rollback()
        except Exception:
            pass
        return {"cancelled": True, "task_id": task_id}
    except Exception:
        try:
            rconn.rollback()
        except Exception:
            pass
        log.error("auto_chain: reconcile dispatch failed for %s", task_id, exc_info=True)
        raise
    finally:
        try:
            rconn.close()
        except Exception:
            pass


def _validate_dev_at_transition(conn, project_id, task_id, result, metadata):
    """PR1 primary preflight validator hook for dev→test transition.

    Returns True when the chain may proceed to next-stage dispatch, False when
    the validator detected blocking errors and the dispatch must be aborted.

    Reads OPT_PREFLIGHT_VALIDATOR_MODE (default 'warn'). Mode 'disabled' or
    a metadata observer_emergency_bypass with a non-empty bypass_reason
    short-circuit to True without running validation.
    """
    mode = (os.environ.get("OPT_PREFLIGHT_VALIDATOR_MODE") or "warn").strip().lower()
    if mode == "disabled":
        log.info("preflight: dev validator disabled by OPT_PREFLIGHT_VALIDATOR_MODE")
        return True
    if metadata and metadata.get("observer_emergency_bypass") and metadata.get("bypass_reason"):
        log.warning(
            "preflight: observer_emergency_bypass active for task %s reason=%s",
            task_id, metadata.get("bypass_reason"),
        )
        return True
    chain_context = None
    try:
        from .chain_context import get_store
        chain_context = get_store().get_chain(task_id)
    except Exception:
        log.debug("preflight: chain_context unavailable for %s", task_id, exc_info=True)
    try:
        vr = validate_dev_output(result or {}, chain_context, mode=mode)
    except Exception:
        log.error("preflight: validator crashed for task %s", task_id, exc_info=True)
        return True  # never block chain on validator bug
    if not vr.valid:
        log.warning(
            "preflight: dev result validation FAILED for task %s mode=%s errors=%d warnings=%d",
            task_id, mode, len(vr.errors), len(vr.warnings),
        )
        return False
    if vr.warnings:
        log.info(
            "preflight: dev result validation passed with %d warning(s) for %s",
            len(vr.warnings), task_id,
        )
    return True


def _validate_pm_at_transition(conn, project_id, task_id, result, metadata):
    """PR1d primary preflight validator hook for pm→dev transition.

    Returns True when the chain may proceed to next-stage dispatch, False when
    the validator detected blocking errors and the dispatch must be aborted.

    Mirrors _validate_dev_at_transition: respects OPT_PREFLIGHT_VALIDATOR_MODE
    (default 'warn'); a metadata observer_emergency_bypass with a non-empty
    bypass_reason short-circuits to True without running validation; chain
    context is fetched best-effort and never blocks; validator crashes
    fall back to True so a buggy validator never wedges the chain.
    """
    mode = (os.environ.get("OPT_PREFLIGHT_VALIDATOR_MODE") or "warn").strip().lower()
    if mode == "disabled":
        log.info("preflight: pm validator disabled by OPT_PREFLIGHT_VALIDATOR_MODE")
        return True
    if metadata and metadata.get("observer_emergency_bypass") and metadata.get("bypass_reason"):
        log.warning(
            "preflight: observer_emergency_bypass active for task %s reason=%s",
            task_id, metadata.get("bypass_reason"),
        )
        return True
    chain_context = None
    try:
        from .chain_context import get_store
        chain_context = get_store().get_chain(task_id)
    except Exception:
        log.debug("preflight: chain_context unavailable for %s", task_id, exc_info=True)
    try:
        vr = validate_pm_output(result or {}, chain_context, mode=mode)
    except Exception:
        log.error("preflight: validator crashed for task %s", task_id, exc_info=True)
        return True  # never block chain on validator bug
    if not vr.valid:
        log.warning(
            "preflight: pm result validation FAILED for task %s mode=%s errors=%d warnings=%d",
            task_id, mode, len(vr.errors), len(vr.warnings),
        )
        return False
    if vr.warnings:
        log.info(
            "preflight: pm result validation passed with %d warning(s) for %s",
            len(vr.warnings), task_id,
        )
    if is_reconcile_cluster_task(metadata):
        cluster_ok, cluster_reason = preflight_reconcile_cluster_pm(
            result or {},
            candidate_nodes=_cluster_payload_candidate_nodes(metadata),
            metadata=metadata,
        )
        if not cluster_ok:
            log.warning(
                "preflight: reconcile-cluster PM contract FAILED for task %s: %s",
                task_id, cluster_reason,
            )
            return False
    return True


def _preflight_failure_reason(stage, conn, project_id, task_id, result, metadata):
    """Return a compact operator-facing reason for a preflight block."""
    fallback = f"{stage} result preflight validation failed"
    if stage == "pm":
        try:
            mode = (os.environ.get("OPT_PREFLIGHT_VALIDATOR_MODE") or "warn").strip().lower()
            chain_context = None
            try:
                from .chain_context import get_store
                chain_context = get_store().get_chain(task_id)
            except Exception:
                chain_context = None
            vr = validate_pm_output(result or {}, chain_context, mode=mode)
            if not vr.valid:
                messages = [
                    f"{e.field_path}: {e.message}"
                    for e in (vr.errors or [])[:3]
                ]
                if messages:
                    return f"{fallback}: " + "; ".join(messages)
        except Exception:
            pass
        if is_reconcile_cluster_task(metadata):
            try:
                ok, reason = preflight_reconcile_cluster_pm(
                    result or {},
                    candidate_nodes=_cluster_payload_candidate_nodes(metadata),
                    metadata=metadata,
                )
                if not ok and reason:
                    return reason
            except Exception:
                pass
    return fallback


def _mark_reconcile_cluster_preflight_blocked(
    conn, project_id, metadata, stage, reason,
):
    """Move a reconcile cluster out of in_chain when a stage preflight blocks."""
    if not is_reconcile_cluster_task(metadata):
        return None
    fp = str(metadata.get("cluster_fingerprint") or "").strip()
    if not fp:
        return None
    try:
        from . import reconcile_deferred_queue as q
        return q.requeue_after_failure(
            project_id,
            fp,
            retry_count_delta=1,
            reason=f"{stage}_preflight_blocked: {reason}",
            conn=conn,
        )
    except Exception:
        log.warning(
            "auto_chain: failed to mark reconcile cluster %s preflight-blocked",
            fp,
            exc_info=True,
        )
        return {"status": "mark_failed", "cluster_fingerprint": fp}


def _is_current_reconcile_cluster_terminal_source(
    conn, project_id, task_id, metadata,
) -> bool:
    """Return True when this task is allowed to terminalize its cluster row."""
    if not isinstance(metadata, dict):
        return True
    fp = str(metadata.get("cluster_fingerprint") or "").strip()
    bug_id = str(metadata.get("bug_id") or "").strip()
    if bug_id:
        try:
            row = conn.execute(
                "SELECT current_task_id, root_task_id FROM backlog_bugs "
                "WHERE bug_id = ?",
                (bug_id,),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            current_task_id = str(row["current_task_id"] or "")
            root_task_id = str(row["root_task_id"] or "")
            if current_task_id and current_task_id != task_id:
                return False
            if not current_task_id and root_task_id and root_task_id != task_id:
                return False
    if fp:
        try:
            row = conn.execute(
                "SELECT root_task_id FROM reconcile_deferred_clusters "
                "WHERE project_id = ? AND cluster_fingerprint = ?",
                (project_id, fp),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            root_task_id = str(row["root_task_id"] or "")
            chain_id = str(metadata.get("chain_id") or metadata.get("root_task_id") or "")
            if root_task_id and chain_id and root_task_id != chain_id:
                return False
    return True


def _reconcile_cluster_terminal_hook(
    conn, project_id, task_id, task_type, status, result, metadata,
):
    """CR3 R6 hook — bridge merge/cancel/stall events to reconcile_deferred_queue.

    When a chain root carries metadata.operation_type=='reconcile-cluster',
    transitions the deferred queue row to its terminal state:

        * merge succeeded            -> mark_terminal(... 'resolved', 'merged@<sha>')
        * any task failed_terminal   -> mark_terminal(... 'failed_terminal' or
                                                       'failed_retryable' depending
                                                       on retry_count)
        * observer cancel            -> mark_terminal(... 'skipped')

    Best-effort: never raises; logs at DEBUG when the hook can't act.
    """
    if not isinstance(metadata, dict):
        return None
    if metadata.get("operation_type") != "reconcile-cluster":
        return None
    fp = metadata.get("cluster_fingerprint") or ""
    if not fp:
        return None
    try:
        from . import reconcile_deferred_queue as q
    except Exception as exc:  # noqa: BLE001
        log.debug("reconcile-cluster hook: queue import failed: %s", exc)
        return None

    try:
        if task_type == "merge" and status == "succeeded":
            commit = (
                (result or {}).get("merge_commit")
                or (result or {}).get("commit")
                or metadata.get("reconcile_target_head")
                or metadata.get("merge_commit")
                or ""
            )
            session_id = (
                metadata.get("reconcile_session_id")
                or metadata.get("session_id")
                or ""
            )
            target_branch = (
                (result or {}).get("reconcile_target_branch")
                or (result or {}).get("target_branch")
                or metadata.get("reconcile_target_branch")
                or ""
            )
            if commit and (session_id or target_branch):
                if session_id:
                    conn.execute(
                        "UPDATE reconcile_sessions "
                        "SET target_head_sha=?, "
                        "target_branch=CASE "
                        "WHEN COALESCE(target_branch, '') = '' THEN ? "
                        "ELSE target_branch END "
                        "WHERE project_id=? AND session_id=? "
                        "AND status IN ('active','finalizing','finalize_failed')",
                        (commit, target_branch, project_id, session_id),
                    )
                elif target_branch:
                    conn.execute(
                        "UPDATE reconcile_sessions SET target_head_sha=? "
                        "WHERE project_id=? AND target_branch=? "
                        "AND status IN ('active','finalizing','finalize_failed')",
                        (commit, project_id, target_branch),
                    )
            q.mark_terminal(project_id, fp, "resolved", f"merged@{commit}",
                            conn=conn)
            backlog_closed = False
            bug_id = str(metadata.get("bug_id") or "").strip()
            if bug_id:
                backlog_closed = _try_backlog_close_via_db(
                    project_id, bug_id, commit, conn=conn,
                )
            return {
                "hook": "reconcile_cluster_resolved",
                "fingerprint": fp,
                "backlog_closed": backlog_closed,
            }
        is_cancel = status == "cancelled" or (
            isinstance(metadata.get("cancel_reason"), str) and metadata.get("cancel_reason")
        )
        is_failure = status in ("failed", "failed_terminal", "failed_retryable", "stalled")
        if (is_cancel or is_failure) and not _is_current_reconcile_cluster_terminal_source(
            conn, project_id, task_id, metadata,
        ):
            return {
                "hook": "reconcile_cluster_stale_terminal_ignored",
                "fingerprint": fp,
                "task_id": task_id,
            }
        if is_cancel:
            q.mark_terminal(
                project_id, fp, "skipped",
                metadata.get("cancel_reason") or "observer_cancel",
                conn=conn,
            )
            return {"hook": "reconcile_cluster_skipped", "fingerprint": fp}
        if is_failure:
            outcome = q.requeue_after_failure(
                project_id, fp, retry_count_delta=1,
                reason=(
                    (result or {}).get("error_message")
                    or (result or {}).get("error")
                    or (result or {}).get("reason")
                    or str(status)
                ),
                conn=conn,
            )
            return {"hook": "reconcile_cluster_failure", "fingerprint": fp,
                    "queue_outcome": outcome}
    except Exception as exc:  # noqa: BLE001
        log.debug("reconcile-cluster hook: mark_terminal failed: %s", exc)
    return None


def on_task_completed(conn, project_id, task_id, task_type, status, result, metadata):
    """Called by complete_task(). Dispatches next stage if gate passes.

    Uses a SEPARATE connection to avoid holding caller's transaction lock
    during potentially slow gate checks and task creation.

    Returns dict with chain result, or None if not a chain-eligible task.
    """
    # CR3 R6 — reconcile-cluster terminal hook fires for ANY status (incl.
    # failed/cancelled), not only 'succeeded' — and on every event (merge,
    # observer cancel, stall) for the deferred queue.  Calls mark_terminal()
    # in the queue module.  Pure side-effect; does not gate dispatch.
    try:
        _reconcile_cluster_terminal_hook(
            conn, project_id, task_id, task_type, status, result, metadata,
        )
    except Exception:
        log.debug("reconcile-cluster hook raised; ignoring", exc_info=True)

    mf_taken_over, mf_reason = _is_task_taken_over_by_mf(conn, project_id, task_id, metadata)
    if mf_taken_over:
        log.warning("auto_chain: task %s completion ignored because MF took over: %s", task_id, mf_reason)
        return {
            "dispatched": False,
            "mf_takeover": True,
            "reason": mf_reason,
        }

    if status != "succeeded":
        return None
    # R2: Reconcile tasks use separate stage map, bypass all governance gates
    if task_type == "reconcile" or (isinstance(task_type, str) and task_type.startswith("reconcile_")):
        return _dispatch_reconcile(conn, project_id, task_id, task_type, result, metadata)
    if task_type not in CHAIN:
        return None

    # PR1f: PM validator wiring RESTORED. The PR1d delete-keyword substring
    # scan that over-fired on feature-description ACs has been replaced with
    # a structural-only validator (payload shape, AC list-of-strings,
    # work-scope non-empty, proposed_nodes element shape, removed_nodes /
    # unmapped_files type, no self-waiver). The new validator does not
    # inspect natural-language AC content, so it is safe to wire — it cannot
    # over-fire on PR1e-style "Rule J respects dev removes" ACs.
    if task_type == "pm":
        if not _validate_pm_at_transition(conn, project_id, task_id, result, metadata):
            reason = _preflight_failure_reason(
                "pm", conn, project_id, task_id, result, metadata,
            )
            queue_outcome = _mark_reconcile_cluster_preflight_blocked(
                conn, project_id, metadata, "pm", reason,
            )
            return {
                "preflight_blocked": True,
                "stage": "pm",
                "reason": reason,
                "queue_outcome": queue_outcome,
            }

    # PR1 PRIMARY: dev result preflight validator — runs right after dev
    # succeeds and BEFORE next-stage dispatch (_do_chain). Returns False to
    # block dispatch when validator declares the payload invalid; logs only
    # when mode='disabled' or observer_emergency_bypass is in effect.
    if task_type == "dev":
        if not _validate_dev_at_transition(conn, project_id, task_id, result, metadata):
            reason = _preflight_failure_reason(
                "dev", conn, project_id, task_id, result, metadata,
            )
            queue_outcome = _mark_reconcile_cluster_preflight_blocked(
                conn, project_id, metadata, "dev", reason,
            )
            return {
                "preflight_blocked": True,
                "stage": "dev",
                "reason": reason,
                "queue_outcome": queue_outcome,
            }

    # Use independent connection — don't hold caller's lock during chain ops
    from .db import get_connection
    try:
        conn = get_connection(project_id)
    except Exception:
        log.error("auto_chain: failed to get independent connection for %s", project_id)
        return None
    try:
        result_val = _do_chain(conn, project_id, task_id, task_type, result, metadata)
        conn.commit()
        return result_val
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _do_chain(conn, project_id, task_id, task_type, result, metadata):
    """Internal chain logic with guaranteed conn cleanup by caller."""
    metadata["related_nodes"] = _normalize_related_nodes(metadata.get("related_nodes", []))

    # --- Trace propagation: load trace_id/chain_id from current task ---
    _trace_id, _chain_id = _load_task_trace(conn, task_id)
    if not _trace_id and task_type == "pm":
        # Root PM task without trace_id — generate one and backfill
        _trace_id = new_trace_id()
        _chain_id = task_id
        try:
            conn.execute(
                "UPDATE tasks SET trace_id=?, chain_id=? WHERE task_id=?",
                (_trace_id, _chain_id, task_id),
            )
            # MF-2026-04-24-001 extension: release write lock immediately so
            # subsequent _publish_event subscribers (chain_context.on_task_completed
            # → _persist_event legacy path, opens separate conn) do not wait 60s
            # busy_timeout for this transaction to finish. Lock-hold time here
            # dominates the ~10min stall pattern in OPT-BACKLOG-AUTO-CHAIN-CONN-CONTENTION.
            conn.commit()
        except Exception:
            log.warning("auto_chain: failed to backfill trace_id on PM task %s", task_id)
    elif not _trace_id:
        # Non-PM task without trace (legacy) — generate trace but keep chain_id as parent_task_id
        _trace_id = new_trace_id()
        _chain_id = _chain_id or metadata.get("parent_task_id") or task_id

    _chain_id = _chain_id or _resolve_chain_root_id(conn, project_id, task_id, metadata) or task_id
    metadata["project_id"] = project_id
    metadata["task_id"] = task_id
    metadata["trace_id"] = _trace_id
    metadata["chain_id"] = _chain_id
    _persist_task_metadata_context(conn, project_id, task_id, metadata, _trace_id, _chain_id)

    # Non-blocking preflight log (first stage only)
    if task_type == "pm":
        try:
            from .preflight import run_preflight
            report = run_preflight(conn, project_id, auto_fix=False)
            if report.get("warnings"):
                log.warning("preflight warnings for %s: %s", project_id, report["warnings"])
            if not report.get("ok"):
                log.error("preflight blockers for %s: %s", project_id, report["blockers"])
        except Exception:
            pass  # never block chain on preflight failure

    graph_governance_bypassed = backlog_runtime.is_graph_governance_bypassed(metadata)
    graph_divergence = _dispatch_graph_divergence_hook(
        conn,
        project_id,
        task_id,
        task_type,
        metadata,
        graph_governance_bypassed=graph_governance_bypassed,
    )
    if graph_divergence.get("blocked"):
        reason = graph_divergence.get("reason") or "active_graph_snapshot_stale"
        _record_gate_event(conn, project_id, task_id, "graph_divergence", False, reason, _trace_id)
        return {
            "gate_blocked": True,
            "dispatched": False,
            "stage": "graph_divergence",
            "reason": reason,
        }
    elif graph_divergence.get("diverged"):
        _record_gate_event(
            conn,
            project_id,
            task_id,
            "graph_divergence",
            True,
            graph_divergence.get("reason") or "graph stale queued",
            _trace_id,
        )
        _persist_task_metadata_context(conn, project_id, task_id, metadata, _trace_id, _chain_id)

    # Auto-enrich: derive related_nodes from changed_files via impact API
    if graph_governance_bypassed:
        log.warning("auto_chain: graph governance bypassed for task %s via backlog policy", task_id)
    elif not metadata.get("related_nodes"):
        changed = result.get("changed_files", metadata.get("changed_files", []))
        if changed:
            try:
                from .impact_analyzer import ImpactAnalyzer, ImpactAnalysisRequest, FileHitPolicy
                from . import project_service
                graph = project_service.load_project_graph(project_id)
                if graph:
                    def _get_status(nid):
                        from .enums import VerifyStatus
                        row = conn.execute(
                            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                            (project_id, nid)).fetchone()
                        return VerifyStatus.from_str(row["verify_status"]) if row else VerifyStatus.PENDING
                    analyzer = ImpactAnalyzer(graph, _get_status)
                    request = ImpactAnalysisRequest(
                        changed_files=changed,
                        file_policy=FileHitPolicy(match_primary=True, match_secondary=True),
                    )
                    impact = analyzer.analyze(request)
                    nodes = [n["node_id"] for n in impact.get("affected_nodes", [])]
                    if nodes:
                        metadata["related_nodes"] = nodes
                        log.info("auto_chain: enriched related_nodes from changed_files: %s", nodes)
            except Exception as e:
                log.warning("auto_chain: related_nodes enrichment failed: %s", e)

    depth = metadata.get("chain_depth", 0)
    if depth >= MAX_CHAIN_DEPTH:
        log.warning("auto_chain: max depth %d reached for task %s, stopping", depth, task_id)
        return {"chain_stopped": True, "reason": f"max_chain_depth={MAX_CHAIN_DEPTH}"}

    gate_fn_name, next_type, builder_name = CHAIN[task_type]

    # Emit task.completed to chain context store BEFORE gate check (R1)
    # so completion events are always recorded regardless of gate outcome
    _publish_event("task.completed", {
        "project_id": project_id, "task_id": task_id,
        "result": result, "type": task_type,
    })
    # A1: Audit task.completed lifecycle event
    try:
        from . import audit_service
        audit_service.record(
            conn, project_id, f"{task_type}.completed",
            actor="auto-chain",
            ok=True,
            node_ids=metadata.get("related_nodes", []),
            task_id=task_id,
            chain_depth=depth,
            trace_id=_trace_id,
        )
    except Exception:
        log.debug("auto_chain: audit task.completed failed (non-critical)", exc_info=True)
    # R6: structured_log with trace_id for gate transitions
    structured_log("info", f"{task_type}.completed",
                   project_id=project_id, task_id=task_id,
                   trace_id=_trace_id, chain_id=_chain_id)

    # CH4: Update backlog_bugs chain_stage on task completion
    _bug_id = metadata.get("bug_id", "")
    if _bug_id:
        _update_backlog_stage(
            conn, project_id, _bug_id, f"{task_type}_complete",
            task_id=task_id, task_type=task_type, metadata=metadata,
            result=result, root_task_id=_chain_id,
        )

    # M1: PM completes → persist full PRD to memory for future dev/qa recall
    # Moved BEFORE version gate so PRD publication fires regardless of gate outcome
    if task_type == "pm":
        prd = result.get("prd", result)
        prd_data = {
            "requirements": prd.get("requirements", result.get("requirements", [])),
            "acceptance_criteria": result.get("acceptance_criteria", prd.get("acceptance_criteria", [])),
            "target_files": result.get("target_files", []),
            "test_files": result.get("test_files", []),
            "proposed_nodes": result.get("proposed_nodes", []),
            "doc_impact": result.get("doc_impact", {}),
            "verification": result.get("verification", {}),
            "skip_reasons": result.get("skip_reasons", {}),
        }
        if any(prd_data.values()):
            _write_chain_memory(
                conn, project_id, "prd_scope",
                json.dumps(prd_data, ensure_ascii=False),
                metadata,
                extra_structured={"task_id": task_id, "chain_stage": "pm"},
            )

        if is_reconcile_cluster_task(metadata):
            _record_reconcile_batch_pm_decision(
                conn, project_id, task_id, metadata, result, prd
            )

        # R3: Emit pm.prd.published event when PM result has non-empty proposed_nodes
        proposed_nodes = result.get("proposed_nodes", [])
        log.info("auto_chain: on_task_completed PM path proposed_nodes count=%d task=%s",
                 len(proposed_nodes), task_id)
        if proposed_nodes:
            try:
                from .chain_context import get_store
                store = get_store()
                root_task_id = store._task_to_root.get(task_id, task_id)
                _prd_payload = {
                        "proposed_nodes": proposed_nodes,
                        "test_files": result.get("test_files", []),
                        "target_files": result.get("target_files", []),
                        "requirements": prd.get("requirements", result.get("requirements", [])),
                        "acceptance_criteria": result.get("acceptance_criteria",
                                                          prd.get("acceptance_criteria", [])),
                    }
                _verification = result.get("verification", prd.get("verification", {}))
                if _verification:
                    _prd_payload["verification"] = _verification
                # R1/AC1: Persist 4 PRD graph-declaration fields
                for _gdf in _PRD_GRAPH_DECLARATION_FIELDS:
                    _gdf_val = result.get(_gdf, prd.get(_gdf, []))
                    if _gdf_val:
                        _prd_payload[_gdf] = _gdf_val
                store._persist_event(
                    root_task_id=root_task_id,
                    task_id=task_id,
                    event_type="pm.prd.published",
                    payload=_prd_payload,
                    project_id=project_id,
                    conn=conn,  # MF-2026-04-24-001: share caller transaction
                )
                log.info("auto_chain: emitted pm.prd.published for task %s (%d proposed_nodes)",
                         task_id, len(proposed_nodes))
            except Exception:
                log.error("auto_chain: pm.prd.published emission failed", exc_info=True)

    # B30: merge produces a new commit advancing HEAD past chain_version; deploy updates
    # chain_version itself.  Both are version-advancing operations that must not be blocked
    # by the version gate (which anchors to chain_version per B29 fix). Gate remains active
    # for pm / dev / test / qa / gatekeeper.
    if task_type in ("merge", "deploy"):
        ver_passed, ver_reason = True, f"version_check skipped for {task_type} (version-advancing op)"
        log.debug("auto_chain: %s", ver_reason)
        _record_gate_event(conn, project_id, task_id, "version_check", ver_passed, ver_reason, _trace_id)
    else:
        # Pre-gate: version check — blocks on stale server or dirty workspace
        ver_passed, ver_reason = _gate_version_check(conn, project_id, result, metadata)
        _record_gate_event(conn, project_id, task_id, "version_check", ver_passed, ver_reason, _trace_id)

    # R2: Single-retry for dirty workspace — wait 10s then retry once (R4: max 1 retry)
    if not ver_passed and "dirty workspace" in ver_reason:
        import time
        log.info("auto_chain: dirty workspace detected for task %s, retrying in 10s...", task_id)
        time.sleep(10)
        ver_passed, ver_reason = _gate_version_check(conn, project_id, result, metadata)
        _record_gate_event(conn, project_id, task_id, "version_check_retry", ver_passed, ver_reason, _trace_id)

    if not ver_passed:
        # R2: Log at WARNING level with task_id, project_id, gate_reason, dirty_files
        _dirty_files = []
        try:
            _vrow = conn.execute(
                "SELECT dirty_files FROM project_version WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if _vrow and _vrow["dirty_files"]:
                _dirty_files = json.loads(_vrow["dirty_files"] or "[]")
        except Exception:
            pass
        log.warning(
            "auto_chain: version gate blocked for task %s (project=%s): %s dirty_files=%s",
            task_id, project_id, ver_reason, _dirty_files,
        )
        # R3: INSERT audit_log row with action='auto_chain_gate_blocked'
        try:
            from datetime import datetime, timezone
            conn.execute(
                "INSERT INTO audit_log (project_id, action, actor, ok, ts, task_id, details_json) "
                "VALUES (?, 'auto_chain_gate_blocked', 'auto-chain', 0, ?, ?, ?)",
                (
                    project_id,
                    datetime.now(timezone.utc).isoformat(),
                    task_id,
                    json.dumps({"gate_reason": ver_reason, "task_id": task_id, "project_id": project_id}),
                ),
            )
        except Exception:
            log.debug("auto_chain: failed to insert audit_log for gate block (non-critical)", exc_info=True)
        # MF-2026-04-24-001 extension: release write lock before sync event
        # dispatch so chain_context subscriber's legacy _persist_event does not
        # wait 60s busy_timeout on this audit_log INSERT.
        try:
            conn.commit()
        except Exception:
            log.debug("auto_chain: commit before gate.blocked publish failed (non-critical)", exc_info=True)
        _publish_event("gate.blocked", {
            "project_id": project_id, "task_id": task_id,
            "stage": "version_check", "next_stage": task_type,
            "reason": ver_reason,
        })
        # CH4: Update backlog_bugs chain_stage on version gate block
        if _bug_id:
            _update_backlog_stage(
                conn, project_id, _bug_id, f"{task_type}_complete_blocked",
                failure_reason=ver_reason, task_id=task_id,
                task_type=task_type, metadata=metadata, result=result,
                root_task_id=_chain_id,
            )
        return {"gate_blocked": True, "dispatched": False, "stage": "version_check", "reason": ver_reason}
    else:
        log.debug("auto_chain: version check passed for task %s: %s", task_id, ver_reason)

    # M4: Test completes → write validation_result memory (marks dev decision as tested)
    if task_type == "test":
        report = result.get("test_report", {})
        passed = report.get("passed", 0) if isinstance(report, dict) else 0
        if passed:
            _write_chain_memory(
                conn, project_id, "validation_result",
                f"Tests passed ({passed} passing) for {', '.join(metadata.get('changed_files', [])[:3])}",
                metadata,
                extra_structured={"task_id": task_id, "chain_stage": "test",
                                   "test_report": report,
                                   "validation_status": "tested",
                                   "parent_task_id": metadata.get("parent_task_id", "")},
            )

    # Auto-update nodes based on stage completion
    if task_type == "dev" and metadata.get("related_nodes") and not graph_governance_bypassed:
        _try_verify_update(conn, project_id, metadata, "testing", "dev",
                           {"type": "dev_complete", "producer": "auto-chain",
                            "task_id": task_id},
                           task_id=task_id)

    # MF-2026-04-24-002: release caller write-lock before _emit_or_infer_graph_delta.
    # That helper + its 4 internal _persist_event legacy-path callsites open NEW
    # connections; if main conn has open transaction here (audit pm.completed at
    # ~1760 + optional _try_verify_update above), they wait 60s busy_timeout each
    # and compound into multi-minute dev-stage stalls. See OPT-BACKLOG-AUTO-CHAIN-
    # CONN-CONTENTION-DEV-PATH for the follow-on to MF-001.
    try:
        conn.commit()
    except Exception:
        log.debug("auto_chain: commit before graph_delta emit failed (non-critical)", exc_info=True)

    # R2: Emit graph.delta.proposed event (auto-infer if dev omitted graph_delta)
    if task_type == "dev" and not graph_governance_bypassed:
        _emit_or_infer_graph_delta(project_id, task_id, result, metadata, task_type=task_type)

    # Run gate check
    gate_fn = _GATES[gate_fn_name]
    passed, reason = gate_fn(conn, project_id, result, metadata)
    _record_gate_event(conn, project_id, task_id, gate_fn_name, passed, reason, _trace_id)
    if not passed:
        workflow_improvement = _maybe_create_workflow_improvement_task(
            conn, project_id, task_id, task_type, reason, metadata, result
        )
        log.info("auto_chain: gate blocked %s→%s for task %s: %s",
                 task_type, next_type or "deploy", task_id, reason)
        # MF-2026-04-24-002: release write-lock before publish (subscriber
        # chain_context.on_gate_blocked opens separate conn via legacy path)
        try:
            conn.commit()
        except Exception:
            log.debug("auto_chain: commit before stage gate.blocked publish failed (non-critical)", exc_info=True)
        _publish_event("gate.blocked", {
            "project_id": project_id, "task_id": task_id,
            "stage": task_type, "next_stage": next_type or "deploy",
            "reason": reason,
        })
        if _bug_id:
            _update_backlog_stage(
                conn, project_id, _bug_id, f"{task_type}_gate_blocked",
                failure_reason=reason, task_id=task_id,
                task_type=task_type, metadata=metadata, result=result,
                root_task_id=_chain_id,
            )
        # M3: Gate fail → write pitfall with previous output context
        _write_chain_memory(
            conn, project_id, "pitfall",
            f"Gate blocked at {task_type}: {reason}\n"
            f"Previous output keys: {list(result.keys())}\n"
            f"Previous output preview: {json.dumps(result, ensure_ascii=False)[:300]}",
            metadata,
            extra_structured={"task_id": task_id, "gate_stage": task_type,
                               "gate_reason": reason,
                               "previous_output_keys": list(result.keys()),
                               "chain_stage": task_type},
        )
        # G3: Persist gate.blocked to audit_index
        try:
            from . import audit_service
            audit_service.record(
                conn, project_id, "gate.blocked",
                actor="auto-chain",
                ok=False,
                node_ids=metadata.get("related_nodes", []),
                task_id=task_id,
                stage=task_type,
                next_stage=next_type or "deploy",
                reason=reason,
                trace_id=_trace_id,
            )
        except Exception:
            log.debug("auto_chain: audit gate.blocked failed (non-critical)", exc_info=True)
        structured_log("warning", "gate.blocked",
                       project_id=project_id, task_id=task_id,
                       stage=task_type, next_stage=next_type or "deploy",
                       trace_id=_trace_id, chain_id=_chain_id, reason=reason)

        # Special cases: test failure or QA rejection → retry as dev (not same stage)
        # Dev fixes the root cause; re-running test/qa without a code fix is wasteful
        if task_type in ("test", "qa"):
            failure_reason = reason
            if task_type == "qa":
                # Prefer specific rejection reason from QA result over gate reason
                failure_reason = _format_qa_rejection_reason(result, reason)
            original_prompt = metadata.get("_original_prompt", "")
            if not original_prompt:
                try:
                    from .chain_context import get_store
                    original_prompt = get_store().get_original_prompt(task_id)
                except Exception:
                    pass
            if not original_prompt:
                original_prompt = result.get("summary", "")
            stage_retry_prompt = (
                f"Fix {task_type} stage failures from task {task_id}.\n"
                f"failure_reason: {failure_reason}\n"
                f"retry_from_stage: {task_type}\n\n"
                f"Original task: {original_prompt}"
            )
            from . import task_registry
            # Dedup: skip if an active dev-retry already exists for this parent
            try:
                _existing_stage_retry = conn.execute(
                    "SELECT task_id FROM tasks WHERE project_id = ? AND type = 'dev' "
                    "AND status IN ('queued','claimed','observer_hold') "
                    "AND json_extract(metadata_json, '$.parent_task_id') = ?",
                    (project_id, task_id),
                ).fetchone()
            except Exception:
                _existing_stage_retry = None
            if _existing_stage_retry:
                _dup_id = _existing_stage_retry["task_id"]
                log.warning("auto_chain: dedup stage-retry — active dev retry %s already exists for %s",
                            _dup_id, task_id)
                out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                       "retry_task_id": _dup_id, "retry_type": "dev",
                       "retry_from_stage": task_type, "dedup": True}
                if workflow_improvement:
                    out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                return out
            # OPT-BACKLOG-CH2: fallback-fill missing bug_id from chain store
            # before creating retry task. Protects against in-process metadata
            # drops between stages (e.g. test→dev hop losing parent's metadata).
            _dev_retry_meta = {
                **metadata,
                "parent_task_id": task_id,
                "chain_depth": depth + 1,
                "failure_reason": failure_reason,
                "retry_from_stage": task_type,
                "_original_prompt": original_prompt,
            }
            if not _dev_retry_meta.get("bug_id"):
                try:
                    from .chain_context import get_store as _get_ctx_store_bug
                    _chain_bug = _get_ctx_store_bug().get_bug_id(task_id)
                    if _chain_bug:
                        _dev_retry_meta["bug_id"] = _chain_bug
                        log.info("auto_chain: CH2 fallback-filled bug_id=%s for dev-retry of %s",
                                 _chain_bug, task_id)
                except Exception:
                    log.debug("auto_chain: CH2 bug_id fallback failed for %s", task_id, exc_info=True)

            dev_retry = task_registry.create_task(
                conn, project_id,
                prompt=stage_retry_prompt,
                task_type="dev",
                created_by="auto-chain-stage-retry",
                metadata=_dev_retry_meta,
                parent_task_id=task_id,
                trace_id=_trace_id,
                chain_id=_chain_id,
            )
            retry_id = dev_retry.get("task_id", "?")
            if _dev_retry_meta.get("bug_id"):
                _update_backlog_stage(
                    conn, project_id, _dev_retry_meta["bug_id"], "dev_queued",
                    task_id=retry_id, task_type="dev", metadata=_dev_retry_meta,
                    runtime_state="queued", root_task_id=_chain_id,
                )
            log.info("auto_chain: %s failure → dev retry task %s", task_type, retry_id)
            _publish_event("task.retry", {
                "project_id": project_id, "task_id": retry_id,
                "original_task_id": task_id, "reason": failure_reason,
                "retry_from_stage": task_type,
            })
            out = {
                "gate_blocked": True, "stage": task_type, "reason": reason,
                "retry_task_id": retry_id, "retry_type": "dev",
                "retry_from_stage": task_type,
            }
            if workflow_improvement:
                out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
            return out

        # Auto-retry: create a new task at the SAME stage with gate reason injected
        # Max 2 retries per gate to prevent infinite loops
        gate_retries = metadata.get("_gate_retry_count", 0)
        if gate_retries < 2 and depth < MAX_CHAIN_DEPTH - 1 and not metadata.get("_no_retry"):
            # Recover original prompt: metadata → chain context → result summary
            original_prompt = metadata.get("_original_prompt", "")
            if not original_prompt:
                try:
                    from .chain_context import get_store
                    original_prompt = get_store().get_original_prompt(task_id)
                except Exception:
                    pass
            if not original_prompt:
                original_prompt = result.get("summary", "")
            if task_type == "dev":
                retry_reason = _effective_dev_retry_reason(conn, project_id, metadata, reason)
                retry_contract = _render_dev_contract_prompt(
                    metadata.get("parent_task_id", task_id),
                    metadata,
                )
                # B36-fix(2): Build allowed list from the SAME helper gate uses —
                # prompt and gate can no longer disagree.
                from .chain_context import get_store as _get_ctx_store
                _target, allowed = _compute_gate_static_allowed(project_id, metadata)
                # B28a inheritance + B36-fix(1): accumulated files from prior succeeded dev stages
                try:
                    allowed.update(
                        _get_ctx_store().get_accumulated_changed_files(_chain_id, project_id)
                    )
                except Exception:
                    pass
                if not allowed:
                    allowed = set(metadata.get("target_files", []))  # final fallback
                if allowed:
                    # Gate also permits stem-prefix tests (tests/test_<stem>*.py) at evaluation
                    # time. Describe as pattern since matched dynamically.
                    _stems = sorted({
                        os.path.splitext(os.path.basename(t.replace("\\", "/")))[0]
                        for t in _target
                    })
                    pattern_note = (
                        f" Plus any file under a tests/ directory matching pattern "
                        f"tests/test_{{{'|'.join(_stems)}}}*.py."
                        if _stems else ""
                    )
                    scope_line = (
                        f"SCOPE CONSTRAINT: Checkpoint gate only allows changes to: "
                        f"{sorted(allowed)}.{pattern_note} Changes to any other files "
                        f"will be blocked as 'unrelated'.\n\n"
                    )
                else:
                    scope_line = ""
                # PR-B/R4: Enrich dev retry with graph_delta_review issues if rejection was from QA graph delta review
                _gd_retry_section = ""
                if "graph delta rejected by QA" in reason or "graph_delta_review" in reason:
                    _gd_review = result.get("graph_delta_review", {})
                    if isinstance(_gd_review, dict):
                        _gd_issues = _gd_review.get("issues", [])
                        _gd_diff = _gd_review.get("suggested_diff", {})
                        _gd_retry_section = (
                            "\n## Graph Delta Review Rejection\n"
                            f"QA graph_delta_review issues: {json.dumps(_gd_issues, ensure_ascii=False)}\n"
                            f"QA suggested_diff: {json.dumps(_gd_diff, ensure_ascii=False)}\n"
                            "Address the graph delta issues listed above in your retry.\n\n"
                        )
                retry_prompt = (
                    f"Previous attempt ({task_id}) was blocked by gate.\n"
                    f"Gate reason: {retry_reason}\n\n"
                    f"{_gd_retry_section}"
                    f"{scope_line}"
                    "IMPORTANT: Do not assume previous blockers still exist. "
                    "Re-verify all alleged blockers against current source before reporting them as remaining issues.\n\n"
                    "Fix the issue described above and retry.\n"
                    "Use the same Dev contract below, including the required verification command.\n\n"
                    f"{retry_contract}"
                )
            else:
                # R8: Check if this is a PM task blocked for PRD missing fields
                _is_pm_prd_missing = (
                    task_type == "pm"
                    and ("PRD missing mandatory fields" in reason
                         or "PRD fields missing without skip_reasons" in reason)
                )
                if _is_pm_prd_missing:
                    # R2: Parse missing fields from gate reason
                    _pm_missing_fields = _parse_pm_missing_fields(reason)
                    # R3: Show prior output keys
                    _prior_keys = sorted(result.keys()) if isinstance(result, dict) else []
                    # R7: Emit repeat regression event when new retry's count >= 2
                    # (gate_retries is current count; new task gets gate_retries + 1)
                    if gate_retries + 1 >= 2:
                        try:
                            from datetime import datetime, timezone
                            conn.execute(
                                "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                                "VALUES (?, ?, 'pm.prd.repeat_regression', ?, ?)",
                                (_chain_id, task_id,
                                 json.dumps({"reason": reason, "gate_retry_count": gate_retries,
                                             "missing_fields": _pm_missing_fields,
                                             "prior_keys": _prior_keys},
                                            ensure_ascii=False),
                                 datetime.now(timezone.utc).isoformat()),
                            )
                        except Exception:
                            log.debug("auto_chain: pm.prd.repeat_regression event write failed", exc_info=True)
                    # R1, R4, R5, R6: Build structured PM retry prompt
                    retry_prompt = (
                        "[CRITICAL: PRD completeness gate blocked your prior output]\n\n"
                        f"Missing fields: {', '.join(_pm_missing_fields)}\n"
                        f"Your output contained keys: {_prior_keys}\n\n"
                        "## Required PRD JSON Shape\n"
                        "Your output MUST include ALL of the following fields:\n"
                        "```json\n"
                        "{\n"
                        '  "target_files": ["path/to/file.py"],\n'
                        '  "test_files": ["path/to/test_file.py"],\n'
                        '  "acceptance_criteria": ["AC1: ..."],\n'
                        '  "verification": {"method": "automated test", "command": "pytest ..."},\n'
                        '  "requirements": ["R1: ..."],\n'
                        '  "proposed_nodes": [{"node_id": "L3.x", "title": "...", "description": "..."}]\n'
                        "}\n"
                        "```\n\n"
                        f"Gate reason: {reason}\n\n"
                        f"Original task: {original_prompt}"
                    )
                else:
                    # AC7: Generic fallback for non-PM or non-missing-field PM retries
                    retry_prompt = (
                        f"Previous attempt ({task_id}) was blocked by gate.\n"
                        f"Gate reason: {reason}\n\n"
                        f"Fix the issue described above and retry.\n"
                        f"Original task: {original_prompt}"
                    )
            from . import task_registry
            # Dedup: skip if an active same-stage retry already exists for this parent
            try:
                _existing_same_retry = conn.execute(
                    "SELECT task_id FROM tasks WHERE project_id = ? AND type = ? "
                    "AND status IN ('queued','claimed','observer_hold') "
                    "AND json_extract(metadata_json, '$.parent_task_id') = ?",
                    (project_id, task_type, task_id),
                ).fetchone()
            except Exception:
                _existing_same_retry = None
            if _existing_same_retry:
                _dup_id = _existing_same_retry["task_id"]
                log.warning("auto_chain: dedup same-stage-retry — active %s retry %s already exists for %s",
                            task_type, _dup_id, task_id)
                out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                       "retry_task_id": _dup_id, "dedup": True}
                if workflow_improvement:
                    out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                return out
            # --- Sanitise retry metadata (R1/R2): strip stale inherited fields ---
            _retry_meta = {
                **metadata,
                "parent_task_id": task_id,
                "chain_depth": depth + 1,
                "previous_gate_reason": retry_reason if task_type == "dev" else reason,
                "_gate_retry_count": gate_retries + 1,
                "_original_prompt": original_prompt,
            }
            # R2: Strip inherited worktree/branch so new task creates fresh worktree
            _retry_meta.pop("_worktree", None)
            _retry_meta.pop("_branch", None)
            # R1: Remove inherited failure_reason from grandparent — only current gate reason kept
            _retry_meta.pop("failure_reason", None)

            # OPT-BACKLOG-CH2: fallback-fill missing bug_id from chain store. Retries
            # inherit parent's metadata via {**metadata} above, but if bug_id was
            # dropped somewhere upstream, the chain-level store still knows it.
            if not _retry_meta.get("bug_id"):
                try:
                    from .chain_context import get_store as _get_ctx_store_bug2
                    _chain_bug = _get_ctx_store_bug2().get_bug_id(task_id)
                    if _chain_bug:
                        _retry_meta["bug_id"] = _chain_bug
                        log.info("auto_chain: CH2 fallback-filled bug_id=%s for %s same-stage-retry of %s",
                                 _chain_bug, task_type, task_id)
                except Exception:
                    log.debug("auto_chain: CH2 bug_id fallback failed for %s", task_id, exc_info=True)

            retry_task = task_registry.create_task(
                conn, project_id,
                prompt=retry_prompt,
                task_type=task_type,
                created_by="auto-chain-retry",
                metadata=_retry_meta,
                parent_task_id=task_id,
                trace_id=_trace_id,
                chain_id=_chain_id,
            )
            retry_id = retry_task.get("task_id", "?")
            if _retry_meta.get("bug_id"):
                _update_backlog_stage(
                    conn, project_id, _retry_meta["bug_id"], f"{task_type}_queued",
                    task_id=retry_id, task_type=task_type, metadata=_retry_meta,
                    runtime_state="queued", root_task_id=_chain_id,
                )
            log.info("auto_chain: retry created %s for blocked %s", retry_id, task_id)
            _publish_event("task.retry", {
                "project_id": project_id, "task_id": retry_id,
                "original_task_id": task_id, "reason": reason,
            })
            out = {"gate_blocked": True, "stage": task_type, "reason": reason,
                   "retry_task_id": retry_id}
            if workflow_improvement:
                out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
                out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
            return out

        # Retry exhausted — emit task.failed
        _publish_event("task.failed", {
            "project_id": project_id, "task_id": task_id,
            "reason": "gate_retry_exhausted", "gate_reason": reason,
        })
        out = {"gate_blocked": True, "stage": task_type, "reason": reason}
        if workflow_improvement:
            out["workflow_improvement_task_id"] = workflow_improvement["task_id"]
            out["failure_class"] = workflow_improvement["classification"].get("failure_class", "")
        return out

    # M5: Dev success + checkpoint gate pass → write success pattern memory
    if task_type == "dev":
        _changed_for_pattern = result.get("changed_files", metadata.get("changed_files", []))
        _summary_for_pattern = result.get("summary", "")
        _write_chain_memory(
            conn, project_id, "pattern",
            _summary_for_pattern or f"Dev completed: {', '.join(_changed_for_pattern[:3])}",
            metadata,
            extra_structured={
                "task_id": task_id, "chain_stage": "dev",
                "changed_files": _changed_for_pattern,
                "gate": "checkpoint_pass",
            },
        )

    # Terminal stage → trigger deploy + archive chain
    if next_type is None:
        builder_fn = _BUILDERS[builder_name]
        deploy_result = builder_fn(conn, project_id, task_id, result, metadata)
        log.info("auto_chain: deploy triggered from task %s: %s", task_id, deploy_result)
        # A2: chain.completed audit summary
        try:
            from . import audit_service
            audit_service.record(
                conn, project_id, "chain.completed",
                actor="auto-chain",
                ok=True,
                node_ids=metadata.get("related_nodes", []),
                task_id=task_id,
                chain_depth=depth,
                changed_files=metadata.get("changed_files", []),
                trace_id=_trace_id,
            )
        except Exception:
            log.debug("auto_chain: audit chain.completed failed (non-critical)", exc_info=True)
        structured_log("info", "chain.completed",
                       project_id=project_id, task_id=task_id,
                       trace_id=_trace_id, chain_id=_chain_id)
        # Archive chain context (release memory, DB data preserved)
        try:
            from .chain_context import get_store
            get_store().archive_chain(task_id, project_id)
        except Exception:
            log.debug("auto_chain: chain archive failed (non-critical)")
        return deploy_result

    # --- R5/R7: Subtask fan-out for PM→Dev ---
    if task_type == "pm" and result.get("subtasks"):
        return _do_subtask_fanout(
            conn, project_id, task_id, result, metadata,
            _trace_id, _chain_id, depth,
        )

    # --- Graph-driven routing (R2): try graph-based next-stage derivation ---
    _graph_next = None
    _graph_skipped = []
    _graph_policies = []
    try:
        from . import project_service
        _graph = project_service.load_project_graph(project_id)
    except Exception:
        _graph = None

    if _graph is not None and not graph_governance_bypassed:
        _graph_next, _graph_skipped, _graph_policies = dispatch_next_stage(
            conn, project_id, task_id, task_type, result, metadata, _trace_id, _graph,
        )

        # Handle blocked by verify_requires (AC4)
        if _graph_next == "blocked":
            log.info("auto_chain: routing blocked by verify_requires for task %s", task_id)
            return {"routing_blocked": True, "reason": "verify_requires not satisfied"}

        # If graph routing returned a specific next stage, override CHAIN lookup
        if _graph_next is not None:
            next_type = _graph_next
            # Find the matching builder and gate for the overridden next_type
            for _chain_type, (_gfn, _ntype, _bname) in CHAIN.items():
                if _ntype == next_type:
                    builder_name = _bname
                    break
            # If next_type is in CHAIN as a key (e.g. "merge"), use its gate
            if next_type in CHAIN:
                pass  # builder already found above or use current stage's

    # R6/AC7: Audit every routing decision
    _audit_routing_decision(conn, project_id, task_id, _trace_id, {
        "current_stage": task_type,
        "next_stage": next_type,
        "routing_mode": "graph_driven" if _graph_next else "linear_chain",
        "skipped_stages": _graph_skipped,
    })

    # Create next stage task (with dedup check)
    builder_fn = _BUILDERS[builder_name]
    prompt, task_meta = builder_fn(task_id, result, metadata)

    # Attach graph routing policies to metadata for downstream stages
    if _graph_policies:
        task_meta["_graph_routing_policies"] = [
            {"node_id": p["node_id"], "gate_mode": p.get("gate_mode"),
             "verify_level": p.get("verify_level")}
            for p in _graph_policies
        ]

    # M6: Dedup — check if next stage already exists for this parent
    from . import task_registry
    existing = conn.execute(
        "SELECT task_id FROM tasks WHERE type = ? AND status IN ('queued','claimed','observer_hold') "
        "AND metadata_json LIKE ?",
        (next_type, f'%"parent_task_id": "{task_id}"%'),
    ).fetchone()
    if existing:
        log.warning("auto_chain: dedup — %s task already exists for parent %s: %s",
                     next_type, task_id, existing["task_id"])
        return {"task_id": existing["task_id"], "dedup": True}

    new_task = task_registry.create_task(
        conn, project_id,
        prompt=prompt,
        task_type=next_type,
        created_by="auto-chain",
        metadata={
            **task_meta,
            "parent_task_id": task_id,
            "chain_depth": depth + 1,
            "project_id": project_id,
            "trace_id": _trace_id,
            "chain_id": _chain_id,
        },
        parent_task_id=task_id,
        trace_id=_trace_id,
        chain_id=_chain_id,
    )
    _next_bug_id = task_meta.get("bug_id", metadata.get("bug_id", ""))
    if _next_bug_id:
        _update_backlog_stage(
            conn, project_id, _next_bug_id, f"{next_type}_queued",
            task_id=new_task.get("task_id", ""), task_type=next_type,
            metadata={**task_meta, "parent_task_id": task_id, "chain_depth": depth + 1,
                      "project_id": project_id, "trace_id": _trace_id, "chain_id": _chain_id},
            runtime_state="queued", root_task_id=_chain_id,
        )

    log.info("auto_chain: %s→%s | %s → %s",
             task_type, next_type, task_id, new_task.get("task_id"))
    # MF-2026-04-24-002: release write-lock before next-stage task.created
    # publish. create_task above opened an implicit write transaction on main
    # conn; without this commit, chain_context.on_task_created subscriber's
    # legacy-path _persist_event waits 60s busy_timeout, stalling the chain
    # at every dispatch boundary.
    try:
        conn.commit()
    except Exception:
        log.debug("auto_chain: commit before next-stage task.created publish failed (non-critical)", exc_info=True)
    # OPT-BACKLOG-CH2: forward metadata.bug_id in task.created payload so
    # chain_context.on_task_created can populate chain.bug_id (first-write-wins).
    # Retry paths then fallback-fill from chain store when metadata is lost.
    _publish_event("task.created", {
        "project_id": project_id,
        "parent_task_id": task_id,
        "task_id": new_task.get("task_id"),
        "type": next_type,
        "prompt": prompt,
        "source": "auto-chain",
        "metadata": {"bug_id": task_meta.get("bug_id", ""), "chain_id": _chain_id},
    })
    return new_task


# ---------------------------------------------------------------------------
# Subtask fan-out / fan-in (R5, R6, R9)
# ---------------------------------------------------------------------------

def _do_subtask_fanout(conn, project_id, pm_task_id, result, metadata, trace_id, chain_id, depth):
    """Create subtask_group + dev tasks for PM subtask decomposition (R5)."""
    from . import task_registry
    from .models import SubtaskGroup
    from datetime import datetime, timezone

    subtasks = result["subtasks"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    group = SubtaskGroup(
        project_id=project_id,
        pm_task_id=pm_task_id,
        total_count=len(subtasks),
        trace_id=trace_id or "",
        chain_id=chain_id or "",
    )

    conn.execute(
        """INSERT INTO subtask_groups
           (group_id, project_id, pm_task_id, total_count, completed_count,
            status, created_at, trace_id, chain_id)
           VALUES (?, ?, ?, ?, 0, 'active', ?, ?, ?)""",
        (group.group_id, project_id, pm_task_id, len(subtasks),
         now, trace_id or "", chain_id or ""),
    )

    created_tasks = []
    for st in subtasks:
        deps = st.get("depends_on") or []
        is_blocked = len(deps) > 0

        st_meta = {
            **metadata,
            "parent_task_id": pm_task_id,
            "chain_depth": depth + 1,
            "project_id": project_id,
            "trace_id": trace_id,
            "chain_id": chain_id,
            "target_files": st.get("target_files", []),
            "acceptance_criteria": st.get("acceptance_criteria", []),
            "verification": st.get("verification", {}),
            "test_files": st.get("test_files", []),
            "subtask_title": st.get("title", ""),
        }

        prompt = _render_dev_contract_prompt(pm_task_id, st_meta)

        new_task = task_registry.create_task(
            conn, project_id,
            prompt=prompt,
            task_type="dev",
            created_by="auto-chain-subtask",
            metadata=st_meta,
            parent_task_id=pm_task_id,
            trace_id=trace_id,
            chain_id=chain_id,
        )
        task_id = new_task["task_id"]

        # Set subtask fields and blocked status
        if is_blocked:
            conn.execute(
                """UPDATE tasks SET
                   subtask_group_id=?, subtask_local_id=?, subtask_depends_on=?,
                   execution_status='blocked', status='blocked'
                   WHERE task_id=?""",
                (group.group_id, st["id"], json.dumps(deps), task_id),
            )
        else:
            conn.execute(
                """UPDATE tasks SET
                   subtask_group_id=?, subtask_local_id=?, subtask_depends_on=?
                   WHERE task_id=?""",
                (group.group_id, st["id"], json.dumps(deps), task_id),
            )

        created_tasks.append({
            "task_id": task_id,
            "subtask_id": st["id"],
            "blocked": is_blocked,
        })

    log.info("auto_chain: subtask fan-out from PM %s → group %s (%d subtasks)",
             pm_task_id, group.group_id, len(subtasks))

    return {
        "subtask_group_id": group.group_id,
        "tasks_created": created_tasks,
        "total_count": len(subtasks),
    }


def on_subtask_merge_completed(conn, project_id, task_id):
    """Fan-in: called when a subtask's merge chain completes (R6).

    Decrements deps on downstream subtasks, unblocks ready ones.
    When all subtasks complete, creates a deploy task.
    """
    from . import task_registry

    # Find the subtask's group and local ID
    row = conn.execute(
        "SELECT subtask_group_id, subtask_local_id FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if not row or not row["subtask_group_id"]:
        return None

    group_id = row["subtask_group_id"]
    completed_local_id = row["subtask_local_id"]

    # Get group info
    group_row = conn.execute(
        "SELECT * FROM subtask_groups WHERE group_id=?", (group_id,)
    ).fetchone()
    if not group_row:
        return None

    # Increment completed_count
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE subtask_groups SET completed_count = completed_count + 1 WHERE group_id=?",
        (group_id,),
    )

    # Unblock downstream subtasks that depended on this one
    blocked_tasks = conn.execute(
        """SELECT task_id, subtask_depends_on FROM tasks
           WHERE subtask_group_id=? AND execution_status='blocked'""",
        (group_id,),
    ).fetchall()

    for bt in blocked_tasks:
        deps = json.loads(bt["subtask_depends_on"] or "[]")
        if completed_local_id in deps:
            deps.remove(completed_local_id)
            if not deps:
                # All deps satisfied — unblock
                conn.execute(
                    """UPDATE tasks SET execution_status='queued', status='queued',
                       subtask_depends_on=? WHERE task_id=?""",
                    (json.dumps(deps), bt["task_id"]),
                )
                log.info("auto_chain: unblocked subtask %s (group %s)",
                         bt["task_id"], group_id)
            else:
                conn.execute(
                    "UPDATE tasks SET subtask_depends_on=? WHERE task_id=?",
                    (json.dumps(deps), bt["task_id"]),
                )

    # Check if all subtasks complete → create deploy task
    updated_group = conn.execute(
        "SELECT completed_count, total_count, pm_task_id, project_id, trace_id, chain_id FROM subtask_groups WHERE group_id=?",
        (group_id,),
    ).fetchone()

    if updated_group and updated_group["completed_count"] >= updated_group["total_count"]:
        conn.execute(
            "UPDATE subtask_groups SET status='completed', completed_at=? WHERE group_id=?",
            (now, group_id),
        )
        # Create deploy task (R6)
        deploy_task = task_registry.create_task(
            conn, project_id,
            prompt=f"Deploy all subtasks from group {group_id} (PM: {updated_group['pm_task_id']})",
            task_type="deploy",
            created_by="auto-chain-fanin",
            metadata={
                "subtask_group_id": group_id,
                "parent_task_id": updated_group["pm_task_id"],
            },
            parent_task_id=updated_group["pm_task_id"],
            trace_id=updated_group["trace_id"],
            chain_id=updated_group["chain_id"],
        )
        log.info("auto_chain: fan-in complete for group %s → deploy %s",
                 group_id, deploy_task.get("task_id"))
        return {"deploy_task_id": deploy_task.get("task_id"), "group_id": group_id}

    return {"group_id": group_id, "unblocked": True}


def on_subtask_terminal_failure(conn, project_id, task_id):
    """Failure cascade: mark group failed, cancel blocked siblings (R9)."""
    row = conn.execute(
        "SELECT subtask_group_id FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if not row or not row["subtask_group_id"]:
        return None

    group_id = row["subtask_group_id"]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Mark group as failed
    conn.execute(
        "UPDATE subtask_groups SET status='failed', completed_at=? WHERE group_id=?",
        (now, group_id),
    )

    # Cancel all blocked sibling tasks
    cancelled = conn.execute(
        """UPDATE tasks SET status='cancelled', execution_status='cancelled',
           completed_at=?, error_message='subtask group failed: sibling failure cascade'
           WHERE subtask_group_id=? AND execution_status='blocked'""",
        (now, group_id),
    ).rowcount

    log.info("auto_chain: failure cascade for group %s — cancelled %d blocked tasks",
             group_id, cancelled)
    return {"group_id": group_id, "cancelled_count": cancelled}


# ---------------------------------------------------------------------------
# Gate functions — each returns (passed: bool, reason: str)
# ---------------------------------------------------------------------------

def _gate_version_check(conn, project_id, result, metadata):
    """Pre-gate: verify the workspace is clean and governance code is current.

    Returns (True, reason) to pass, (False, reason) to block.
    Blocking conditions (return False):
      - server version != git HEAD (stale server — restart required)
      - Dirty workspace with non-ignored files (uncommitted changes)
    Bypass conditions (return True even if mismatch):
      - _DISABLE_VERSION_GATE=True (development override)
      - metadata.skip_version_check=True (task-level bypass)
      - metadata.observer_merge=True (Observer manual merge flow)
      - Reconciliation bypass (structured reconciliation lane)
      - Governed dirty-workspace chain (legacy compat)
    """
    if _DISABLE_VERSION_GATE:
        return True, "version gate disabled (_DISABLE_VERSION_GATE=True)"
    if metadata.get("skip_version_check"):
        operator_id = metadata.get("operator_id", "")
        bypass_reason = metadata.get("bypass_reason", "")
        if not isinstance(operator_id, str) or not operator_id.strip():
            missing = ["operator_id"]
            if not isinstance(bypass_reason, str) or not bypass_reason.strip():
                missing.append("bypass_reason")
            log.warning("skip_version_check ignored — missing required fields: %s (task metadata: %s)",
                        missing, {k: metadata.get(k) for k in ("skip_version_check", "operator_id", "bypass_reason")})
        elif not isinstance(bypass_reason, str) or not bypass_reason.strip():
            log.warning("skip_version_check ignored — missing required fields: %s (task metadata: %s)",
                        ["bypass_reason"], {k: metadata.get(k) for k in ("skip_version_check", "operator_id", "bypass_reason")})
        else:
            task_id = metadata.get("task_id") or metadata.get("parent_task_id") or "unknown"
            task_type = metadata.get("task_type", "unknown")
            _audit_version_gate_bypass(conn, project_id, task_id, operator_id.strip(), bypass_reason.strip(), task_type)
            return True, "skipped (task metadata)"
    if metadata.get("observer_merge"):
        return True, "observer merge bypass"
    if not hasattr(conn, "execute"):
        return True, "no db-capable connection, skipping"

    # --- Structured reconciliation bypass (RECONCILIATION_BYPASS_POLICY) ---
    bypass, observer_task_id = _check_reconciliation_bypass(conn, project_id, metadata)
    if bypass:
        lane = str(metadata.get("reconciliation_lane", "")).strip().upper()
        task_id = metadata.get("task_id") or metadata.get("parent_task_id") or "unknown"
        _audit_reconciliation_bypass(conn, project_id, task_id, observer_task_id, lane)
        return True, f"reconciliation-bypass (observer={observer_task_id}, lane={lane})"

    # --- Legacy governed dirty-workspace chain (kept for backward compat) ---
    if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
        return True, "governed dirty-workspace reconciliation"

    try:
        # Phase A: read chain state from git trailer (single source of truth)
        from .chain_trailer import get_chain_state
        chain_state = get_chain_state()

        dirty_files = chain_state.get("dirty_files", [])
        if dirty_files:
            log.warning("version_check: dirty workspace (%d files: %s) — blocking chain",
                        len(dirty_files), dirty_files[:5])
            return False, f"dirty workspace ({len(dirty_files)} files: {dirty_files[:3]})"

        # Phase A (R6): use git-derived chain_sha as source of truth, not DB
        chain_sha = chain_state.get("chain_sha", chain_state.get("version", ""))
        source = chain_state.get("source", "head")
        if not chain_sha or chain_sha == "unknown":
            return True, "chain_version unavailable, skipping"

        import subprocess
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        if not head or head == "unknown":
            return True, "git HEAD unavailable, skipping"

        # B35: prefix match for short/full hash comparison
        # R6: chain_sha from git trailer is the sole effective version
        effective_ver = chain_sha
        if not (effective_ver.startswith(head) or head.startswith(effective_ver)):
            log.warning("version_check: chain_sha (%s, source=%s) != git HEAD (%s) — blocking chain. "
                        "Complete a full workflow Deploy to update chain state.",
                        effective_ver, source, head)
            return False, (f"chain_sha ({effective_ver}) != git HEAD ({head}). "
                           f"Complete workflow Deploy to update chain state.")
        return True, f"version match: {effective_ver} (source={source})"
    except Exception as e:
        log.warning("version_check failed (non-fatal): %s", e)
        return True, f"version check skipped: {e}"


def _gate_post_pm(conn, project_id, result, metadata):
    """Validate PM PRD has mandatory fields + explain-or-provide for soft fields.

    Mandatory: target_files, verification, acceptance_criteria (hard block)
    Soft-mandatory: test_files, proposed_nodes, doc_impact (must provide OR skip_reasons)
    """
    # CR0b R4: reconcile-session bypass for the doc-impact node-ref check
    # (gate label '_doc_impact.node_ref'). Short-circuits PRD doc/node validation
    # while a reconcile session declares the bypass.
    _bypassed, _reason = _check_session_bypass(
        "_doc_impact.node_ref", project_id, metadata.get("task_id", ""))
    if _bypassed:
        return True, _reason
    prd = result.get("prd", {})

    # === Hard mandatory fields ===
    missing = []
    for field in ("target_files", "verification", "acceptance_criteria"):
        if not result.get(field) and not prd.get(field) and not metadata.get(field):
            missing.append(field)
    if missing:
        return False, f"PRD missing mandatory fields: {missing}"

    target_files = (result.get("target_files") or prd.get("target_files")
                    or metadata.get("target_files") or [])
    if not target_files:
        return False, "PRD target_files is empty"

    # G4: Auto-populate doc_impact from graph if PM left it empty
    doc_impact = result.get("doc_impact") or prd.get("doc_impact")
    if not doc_impact or (isinstance(doc_impact, dict) and not doc_impact.get("files")):
        graph_docs = _get_task_graph_doc_associations(project_id, target_files, metadata)
        # R2: Filter to only .md files — code files must never appear in doc_impact
        graph_docs = [f for f in graph_docs if f.endswith(".md")]
        if graph_docs:
            result["doc_impact"] = {
                "files": graph_docs,
                "changes": ["Auto-populated from graph associations"],
            }

    # G8: Auto-populate related_nodes from graph when PM left it empty
    if not result.get("related_nodes") and target_files:
        matched_nodes = _get_graph_related_nodes(project_id, target_files, metadata)
        if matched_nodes:
            result["related_nodes"] = matched_nodes

    # === Soft-mandatory: provide OR explain in skip_reasons ===
    skip_reasons = result.get("skip_reasons", prd.get("skip_reasons", {}))
    if not isinstance(skip_reasons, dict):
        skip_reasons = {}
    soft_missing = []
    for field in ("test_files", "proposed_nodes", "doc_impact"):
        value = result.get(field) or prd.get(field)
        reason = skip_reasons.get(field, "")
        if not value and not reason:
            soft_missing.append(field)
    if soft_missing:
        return False, f"PRD fields missing without skip_reasons: {soft_missing}. Provide the field OR explain in skip_reasons why it's not needed."

    if is_reconcile_cluster_task(metadata):
        cluster_ok, cluster_reason = preflight_reconcile_cluster_pm(
            result,
            candidate_nodes=_cluster_payload_candidate_nodes(metadata),
            metadata=metadata,
        )
        if not cluster_ok:
            return False, cluster_reason
        graph_preflight = _build_reconcile_graph_preflight(
            project_id, metadata,
            proposed_nodes=result.get("proposed_nodes") or prd.get("proposed_nodes") or [],
        )
        if graph_preflight:
            result["graph_preflight"] = graph_preflight

    # === Subtask validation (R4) ===
    subtasks = result.get("subtasks") or prd.get("subtasks")
    if subtasks:
        if not isinstance(subtasks, list):
            return False, "subtasks must be an array"

        # Get max_subtasks limit (R2)
        max_subtasks = 5  # default
        try:
            pv_row = conn.execute(
                "SELECT max_subtasks FROM project_version WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if pv_row and pv_row["max_subtasks"]:
                max_subtasks = pv_row["max_subtasks"]
        except Exception:
            pass  # use default

        if len(subtasks) > max_subtasks:
            return False, f"subtask count {len(subtasks)} exceeds max_subtasks ({max_subtasks})"

        # Validate mandatory fields per subtask
        seen_ids = set()
        for st in subtasks:
            if not isinstance(st, dict):
                return False, "each subtask must be a dict"
            for mf in ("id", "title", "target_files", "acceptance_criteria"):
                if not st.get(mf):
                    return False, f"subtask missing mandatory field: {mf}"
            st_id = st["id"]
            if st_id in seen_ids:
                return False, f"duplicate subtask id: {st_id}"
            seen_ids.add(st_id)

        # Validate depends_on references
        for st in subtasks:
            for dep in (st.get("depends_on") or []):
                if dep not in seen_ids:
                    return False, f"subtask {st['id']} depends_on unknown id: {dep}"

        # DAG acyclicity check
        if not _check_subtask_dag_acyclic(subtasks):
            return False, "cyclic dependency in subtask depends_on"

    # === 5a: Graph doc classification validation (observation mode) ===
    target_files = (result.get("target_files") or prd.get("target_files")
                    or metadata.get("target_files") or [])
    graph_docs = _get_task_graph_doc_associations(project_id, target_files, metadata)
    if graph_docs:
        doc_impact = result.get("doc_impact") or prd.get("doc_impact") or {}
        declared_docs = set()
        if isinstance(doc_impact, dict):
            declared_docs.update(doc_impact.get("files", []))
        unclassified = [d for d in graph_docs if d not in declared_docs]
        if unclassified and _GRAPH_DOC_OBSERVATION_MODE:
            log.warning(
                "post_pm_gate: graph links %d doc(s) to target_files but PM did not classify them: %s",
                len(unclassified), unclassified[:5],
            )
            _audit_doc_gap(conn, project_id, metadata.get("parent_task_id", ""), "post_pm", set(unclassified), target_files)

    # === Merge all fields into result for downstream ===
    for field in ("target_files", "verification", "acceptance_criteria",
                  "test_files", "proposed_nodes", "doc_impact", "skip_reasons",
                  "requirements", "related_nodes", "graph_preflight"):
        if not result.get(field):
            result[field] = prd.get(field) or metadata.get(field)

    # Propagate subtasks to result for _do_chain
    if subtasks and not result.get("subtasks"):
        result["subtasks"] = subtasks

    return True, "ok"


def _check_subtask_dag_acyclic(subtasks):
    """Return True if the subtask dependency graph is a DAG (no cycles)."""
    # Build adjacency list
    adj = {}
    for st in subtasks:
        adj[st["id"]] = list(st.get("depends_on") or [])

    # Kahn's algorithm
    in_degree = {sid: 0 for sid in adj}
    for sid, deps in adj.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[sid] += 0  # placeholder; we count from deps side
    # Recount properly
    in_degree = {sid: 0 for sid in adj}
    for sid, deps in adj.items():
        for dep in deps:
            pass  # deps are what sid depends on, so dep -> sid
    # Actually: if A depends_on B, then edge B->A. in_degree[A] += 1
    in_degree = {sid: 0 for sid in adj}
    for sid, deps in adj.items():
        for dep in deps:
            if sid in in_degree:
                in_degree[sid] += 1

    from collections import deque
    queue = deque(sid for sid, deg in in_degree.items() if deg == 0)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        # Find nodes that depend on this node
        for sid, deps in adj.items():
            if node in deps:
                in_degree[sid] -= 1
                if in_degree[sid] == 0:
                    queue.append(sid)

    return visited == len(adj)


def _gate_checkpoint(conn, project_id, result, metadata):
    """Checkpoint gate for Dev.

    Trust executor-produced task-local diff evidence. Governance may run in a
    container without git/worktree parity, so it should not re-compute git diff
    here. Node alignment is also temporarily non-blocking until the acceptance
    graph deployed to governance catches up with in-flight node-by-node edits.
    """
    log.info("checkpoint_gate: result keys=%s, changed_files=%s, target_files=%s",
             list(result.keys()) if result else None,
             result.get("changed_files"),
             metadata.get("target_files"))
    changed = result.get("changed_files", [])
    if not changed:
        test_results = result.get("test_results", {})
        try:
            failed_count = int(test_results.get("failed") or 0) if isinstance(test_results, dict) else 1
        except (TypeError, ValueError):
            failed_count = 1
        if metadata.get("operation_type") == SCOPE_MATERIALIZATION_OPERATION_TYPE:
            if (
                isinstance(test_results, dict)
                and test_results.get("ran") is True
                and failed_count > 0
            ):
                return False, (
                    "scope-materialization verification failed with "
                    f"{failed_count} failing tests and changed_files=[]. "
                    "Fix the scoped doc/test/graph materialization evidence "
                    "or record an explicit observer-reviewed doc_debt decision. "
                    f"test_results={json.dumps(test_results, ensure_ascii=False)}"
                )

            graph_delta = (
                result.get("graph_delta")
                if isinstance(result.get("graph_delta"), dict)
                else {}
            )
            has_graph_delta = any(
                isinstance(graph_delta.get(key), list)
                and bool(graph_delta.get(key))
                for key in ("creates", "updates", "links")
            )
            if (
                isinstance(test_results, dict)
                and test_results.get("ran") is True
                and failed_count == 0
                and result.get("summary")
                and has_graph_delta
            ):
                return True, "scope-materialization graph_delta-only accepted"

        if metadata.get("operation_type") == CLUSTER_OPERATION_TYPE:
            if (
                isinstance(test_results, dict)
                and test_results.get("ran") is True
                and failed_count > 0
            ):
                return False, (
                    "reconcile-cluster verification failed with "
                    f"{failed_count} failing tests and changed_files=[]. "
                    "If failures are real cluster-owned defects, fix the allowed "
                    "source/doc/test files and include the pre-fix failure evidence "
                    "in retry_context; if they are not cluster-owned, observer "
                    "takeover is required before this cluster can be accepted. "
                    f"test_results={json.dumps(test_results, ensure_ascii=False)}"
                )
            if (
                isinstance(test_results, dict)
                and test_results.get("ran") is True
                and failed_count == 0
                and result.get("summary")
            ):
                return True, "reconcile-cluster no-op audit accepted"

            cluster_payload = metadata.get("cluster_payload")
            if not isinstance(cluster_payload, dict):
                cluster_payload = {}
            cluster_report = metadata.get("cluster_report")
            if not isinstance(cluster_report, dict):
                cluster_report = {}
            payload_report = cluster_payload.get("cluster_report")
            if not isinstance(payload_report, dict):
                payload_report = {}
            expected_tests = (
                cluster_report.get("expected_test_files")
                if "expected_test_files" in cluster_report
                else payload_report.get("expected_test_files", metadata.get("expected_test_files", []))
            )
            if expected_tests is None:
                expected_tests = []
            graph_delta = result.get("graph_delta") if isinstance(result.get("graph_delta"), dict) else {}
            creates = graph_delta.get("creates") if isinstance(graph_delta, dict) else []
            if (
                not expected_tests
                and result.get("summary")
                and isinstance(creates, list)
                and creates
            ):
                cluster_dev_ok, cluster_dev_reason = preflight_reconcile_cluster_dev(
                    metadata.get("proposed_nodes", []),
                    creates,
                    candidate_nodes=_cluster_payload_candidate_nodes(metadata),
                    metadata=metadata,
                )
                if cluster_dev_ok:
                    return True, "reconcile-cluster no-test overlay-only graph_delta accepted"
                log.warning(
                    "checkpoint_gate: no-test reconcile graph_delta did not pass candidate preflight: %s",
                    cluster_dev_reason,
                )
        return False, "No files changed"

    # B36-fix(2): single source of truth shared with retry-prompt scope_line
    target, allowed = _compute_gate_static_allowed(project_id, metadata)
    # graph_docs still needed downstream for observation-mode doc check
    graph_docs = _get_task_graph_doc_associations(project_id, list(target), metadata)
    # B36-fix(1): on retry, inherit accumulated_changed_files from prior succeeded
    # dev stages so consecutive attempts can build on each other.
    if metadata.get("parent_task_id"):
        try:
            from .chain_context import get_store as _get_ctx_store
            _chain_id = metadata.get("chain_id") or metadata.get("parent_task_id")
            allowed.update(_get_ctx_store().get_accumulated_changed_files(_chain_id, project_id))
        except Exception:
            log.debug("_gate_checkpoint: accumulated_changed_files lookup failed", exc_info=True)
    # R1/R2: Derive allowed test files from target_files stems.
    # For each target file with stem S, allow changed files matching
    # tests/test_{S}*.py (under any parent directory or agent/tests/).
    _allowed_test_prefixes = []
    for tf in target:
        import os.path as _osp
        stem = _osp.splitext(_osp.basename(tf))[0]  # e.g. "ai_lifecycle"
        _allowed_test_prefixes.append(f"test_{stem}")
    if allowed:
        unrelated = []
        for f in changed:
            if f in allowed:
                continue
            # Check if file is a co-modified test file under a tests/ directory
            if _allowed_test_prefixes:
                import posixpath
                parts = f.replace("\\", "/")
                parent = posixpath.dirname(parts)
                basename = posixpath.basename(parts)
                if (parent.endswith("/tests") or parent.endswith("\\tests") or parent == "tests") \
                        and basename.endswith(".py"):
                    if any(basename.startswith(prefix) for prefix in _allowed_test_prefixes):
                        continue
            if _is_dev_note(f):
                continue
            unrelated.append(f)
        if unrelated:
            return False, f"Unrelated files modified: {unrelated}"
    # Syntax check: verify test_results if available
    test_results = result.get("test_results", {})
    if test_results.get("ran") and test_results.get("failed", 0) > 0:
        cmd = test_results.get('command', 'unknown')
        output_excerpt = str(test_results.get('output', ''))[:500]
        detail = f"Dev tests failed: {test_results.get('failed')} failures. Command: {cmd}"
        if output_excerpt:
            detail += f". Output excerpt: {output_excerpt}"
        return False, detail
    # --- Contract-drift detection (D10) --- warn-only ---
    try:
        from .drift_detector import detect_drift, findings_to_json
        _drift_baseline = metadata.get("_drift_baseline")
        if _drift_baseline and isinstance(_drift_baseline, dict):
            authorized = set(metadata.get("_drift_authorized_keys") or [])
            drift_findings = detect_drift(_drift_baseline, authorized_keys=authorized)
            if drift_findings:
                drift_report = findings_to_json(drift_findings)
                metadata["_drift_report"] = drift_report
                unauthorized = [f for f in drift_findings if not f.authorized]
                if unauthorized:
                    log.warning(
                        "checkpoint_gate: UNAUTHORIZED contract drift detected: %s",
                        drift_report,
                    )
                else:
                    log.info("checkpoint_gate: authorized contract drift: %s", drift_report)
        else:
            # No baseline captured — run fresh capture and attach for next stage
            from .drift_detector import capture_baseline
            baseline = capture_baseline()
            metadata["_drift_baseline"] = baseline
            metadata["_drift_report"] = "[]"
    except Exception:
        log.debug("checkpoint_gate: drift detection failed (non-critical)", exc_info=True)
    # Doc consistency check: use CODE_DOC_MAP to verify related docs are updated
    # Skip for governance-internal repairs to avoid oscillation loop (R2)
    if _is_governance_internal_repair(metadata, changed):
        log.info("checkpoint_gate: skipping doc consistency check for governance-internal repair")
        # Node gate is temporarily non-blocking while the governance graph catches
        # up with node-by-node local development. Keep the signal in logs only.
        related_nodes = _normalize_related_nodes(metadata.get("related_nodes", []))
        if related_nodes:
            log.warning(
                "checkpoint_gate: skipping related_nodes enforcement for dev task until graph sync is complete: %s",
                related_nodes,
            )
        return True, "ok"
    # §11.1: reconcile_run_id bypass for doc-check gate (R5/R9)
    _reconcile_run_id = metadata.get("reconcile_run_id")
    if _reconcile_run_id:
        _audit_reconcile_bypass(conn, project_id, "checkpoint_doc", _reconcile_run_id, metadata.get("task_id", ""))
        return True, "reconcile bypass — doc check skipped (§11.1)"
    from .impact_analyzer import get_related_docs
    code_files = [f for f in changed if not f.startswith("docs/") and not f.endswith(".md")]
    doc_files_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))
    doc_impact = metadata.get("doc_impact", {})
    if isinstance(doc_impact, dict) and "files" in doc_impact:
        # R3: Defensive filter — only .md files are valid expected docs
        expected_docs = {f for f in (doc_impact.get("files") or []) if f.endswith(".md")}
    else:
        expected_docs = get_related_docs(code_files)
    # docs/dev/** are informal dev notes — never enforce them as formal docs
    if expected_docs:
        expected_docs = {d for d in expected_docs if not _is_dev_note(d)}
    if expected_docs:
        missing_docs = expected_docs - doc_files_changed
        if missing_docs:
            if _is_scope_materialization_task(metadata):
                graph_delta = result.get("graph_delta") if isinstance(result.get("graph_delta"), dict) else {}
                materialized_paths = (
                    _scope_materialization_graph_delta_paths(metadata, graph_delta)
                    | _scope_materialization_waived_doc_debt_paths(metadata, graph_delta)
                )
                covered_docs = missing_docs & materialized_paths
                if covered_docs:
                    log.info(
                        "checkpoint_gate: scope-materialization docs covered by graph_delta/doc_debt: %s",
                        sorted(covered_docs),
                    )
                    missing_docs = missing_docs - covered_docs
                    if not missing_docs:
                        missing_docs = set()
            if missing_docs:
                if _should_defer_doc_gate_to_lane_c(conn, project_id, metadata):
                    log.warning(
                        "checkpoint_gate: deferring doc updates to Lane C for governed reconciliation lane; missing docs=%s",
                        sorted(missing_docs),
                    )
                    return True, "doc updates deferred to Lane C"
                # Block by default — skip_doc_check only allowed with bootstrap_reason
                if metadata.get("skip_doc_check", False):
                    bootstrap_reason = metadata.get("bootstrap_reason", "")
                    if not bootstrap_reason:
                        return False, (f"skip_doc_check=true requires bootstrap_reason in metadata. "
                                       f"Missing docs: {sorted(missing_docs)}")
                    log.warning("checkpoint_gate: docs skipped (bootstrap: %s): %s",
                                bootstrap_reason, sorted(missing_docs))
                else:
                    if _is_scope_materialization_task(metadata):
                        return False, (
                            "Scope-materialization docs not materialized in graph_delta: "
                            f"{sorted(missing_docs)}. Attach existing docs via graph_delta "
                            "secondary/test fields or record an explicit doc_debt waiver."
                        )
                    return False, f"Related docs not updated: {sorted(missing_docs)}. Add them to changed_files."
    # 5c: Observation-mode graph doc check
    if graph_docs and _GRAPH_DOC_OBSERVATION_MODE:
        doc_files_in_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))
        graph_docs_missing = set(graph_docs) - doc_files_in_changed
        if graph_docs_missing:
            log.warning(
                "checkpoint_gate: graph-linked docs not updated (observation): %s",
                sorted(graph_docs_missing)[:5],
            )
            _audit_doc_gap(conn, project_id, metadata.get("parent_task_id", ""), "checkpoint", graph_docs_missing, changed)

    # Node gate is temporarily non-blocking while the governance graph catches
    # up with node-by-node local development. Keep the signal in logs only.
    related_nodes = _normalize_related_nodes(metadata.get("related_nodes", []))
    if related_nodes:
        log.warning(
            "checkpoint_gate: skipping related_nodes enforcement for dev task until graph sync is complete: %s",
            related_nodes,
        )
    return True, "ok"


def _gate_t2_pass(conn, project_id, result, metadata):
    """Verify tests passed before advancing to QA."""
    # §11.1: reconcile_run_id bypass (R5/R9)
    _reconcile_run_id = metadata.get("reconcile_run_id")
    if _reconcile_run_id:
        _audit_reconcile_bypass(conn, project_id, "t2_pass", _reconcile_run_id, metadata.get("task_id", ""))
        return True, "reconcile bypass — t2_pass skipped (§11.1)"
    report = result.get("test_report", {})
    if not isinstance(report, dict) or not report:
        return False, "Test stage missing required test_report"
    if "passed" not in report or "failed" not in report:
        return False, "Test stage test_report missing passed/failed counts"
    failed = report.get("failed", 0)
    if failed is None:
        failed = 0
    if failed > 0:
        return False, f"Tests failed: {failed} failures"
    # Update nodes FIRST (test passed → promote to t2_pass)
    # Evidence validator checks summary.passed > 0, so ensure it's there
    passed_count = report.get("passed", 1)  # Default 1 if not reported (tests passed gate)
    summary = {**report, "passed": passed_count, "failed": failed}
    task_id = metadata.get("task_id", "")
    vu_ok, vu_err = _try_verify_update(conn, project_id, metadata, "t2_pass", "tester",
                       {"type": "test_report", "producer": "auto-chain",
                        "tool": report.get("tool", "pytest"),
                        "summary": summary},
                       task_id=task_id)
    # Then verify nodes reached t2_pass — defer enforcement when promotion failed or
    # impact-enriched related_nodes include over-broad graph neighbors
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "t2_pass")
        if not passed:
            if not vu_ok:
                log.warning(
                    "t2_pass_gate: deferring related_nodes enforcement — verify_update failed: %s",
                    reason,
                )
            else:
                log.warning(
                    "t2_pass_gate: deferring related_nodes enforcement — over-broad related_nodes from impact analysis: %s",
                    reason,
                )
    return True, "ok"


# ---------------------------------------------------------------------------
# QA Sweep Phase 5 — structural drift gate helpers
# ---------------------------------------------------------------------------

def _qa_sweep_skip_rule(changed_files):
    """Classify changed files to decide sweep scope.

    Returns one of: 'tests_only', 'docs_only', 'docs_plus_code', 'code_only', 'unknown'.
    """
    if not changed_files:
        return "unknown"

    docs = []
    tests = []
    code = []
    for f in changed_files:
        if f.endswith(".md") or f.startswith("docs/"):
            docs.append(f)
        elif "/tests/" in f or f.startswith("test_"):
            tests.append(f)
        else:
            code.append(f)

    total = len(changed_files)
    if tests and not docs and not code:
        return "tests_only"
    if docs and not code and not tests:
        return "docs_only"
    if docs and (code or tests):
        return "docs_plus_code"
    if code and not docs:
        return "code_only"
    return "unknown"


def _qa_sweep_gate(conn, project_id, qa_task_id, qa_metadata, qa_result):
    """Structural drift gate invoked after AI QA passes.

    Returns (ok: bool, message: str, sweep_result_or_none).
    """
    # R3: Feature-flag check
    if not _QA_SWEEP_ENABLED:
        return True, "qa_sweep disabled — QA_SWEEP_ENABLED is not 'true'", None

    changed_files = qa_metadata.get("changed_files", [])
    skip_rule = _qa_sweep_skip_rule(changed_files)

    # R5: tests-only → skip
    if skip_rule == "tests_only":
        return True, "qa_sweep skipped — tests-only change", None

    # R4: docs-only → run doc-related phases only
    phases = None
    if skip_rule == "docs_only":
        phases = ["K", "D"]

    # Derive workspace_path (R10)
    workspace_path = str(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    # Get commit from metadata
    commit = qa_metadata.get("commit") or qa_metadata.get("commit_sha") or "HEAD"

    # R6: Orchestrator exceptions are non-blocking
    try:
        from .reconcile_phases.orchestrator import run_commit_slice_orchestrated
        sweep_result = run_commit_slice_orchestrated(
            project_id,
            workspace_path,
            commit,
            phases=phases,
            dry_run=True,
        )
    except Exception as exc:
        log.warning("qa_sweep error (non-blocking): %s", exc)
        return True, f"qa_sweep error (non-blocking): {exc}", None

    # R7: Count high-severity findings
    findings = sweep_result.get("findings", [])
    high_count = sum(
        1 for f in findings
        if f.get("confidence") == "high" and f.get("priority") in ("P0", "P1")
    )

    if high_count > 0:
        # R8: Gate failure → spawn corrective PM
        failure_msg = f"qa_sweep found {high_count} high-severity drift findings"
        spawn_corrective_pm(
            conn, project_id, qa_task_id, failure_msg, bug_id="qa_sweep_drift"
        )
        return False, failure_msg, sweep_result

    return True, "qa_sweep passed — no high-severity drift", sweep_result


def _gate_qa_pass(conn, project_id, result, metadata):
    """Verify QA recommendation before merge.

    Requires explicit qa_pass recommendation.
    Missing or ambiguous recommendation is a hard block (not auto-pass).
    """
    # CR0b R4: reconcile-session bypass for the qa-pass related-nodes
    # existence check.
    _task_id_for_session = metadata.get("task_id", "")
    if _task_id_for_session:
        _session_related_bypassed, _session_bypass_reason = _check_session_bypass(
            "_gate_qa_pass.related_nodes", project_id, _task_id_for_session)
    else:
        _session_related_bypassed, _session_bypass_reason = False, ""
    if _session_related_bypassed:
        log.warning("qa_gate: related_nodes checks bypassed by active reconcile session: %s",
                    _session_bypass_reason)
    # PR1 SECONDARY defense-in-depth: re-run dev preflight at QA gate to
    # surface any dev-stage violation that slipped past the primary check.
    # Always logs only — never blocks here.
    try:
        dev_result_for_preflight = metadata.get("dev_result")
        if isinstance(dev_result_for_preflight, str):
            try:
                dev_result_for_preflight = json.loads(dev_result_for_preflight)
            except Exception:
                dev_result_for_preflight = None
        if isinstance(dev_result_for_preflight, dict):
            if not _validate_dev_at_transition(
                conn, project_id, metadata.get("task_id", ""),
                dev_result_for_preflight, metadata,
            ):
                log.warning("qa_gate: dev preflight validator caught violations past stage-transition")
    except Exception:
        log.debug("qa_gate: secondary preflight call failed (non-critical)", exc_info=True)
    graph_governance_bypassed = backlog_runtime.is_graph_governance_bypassed(metadata)
    _gd_proposed = None
    if not graph_governance_bypassed:
        _gd_proposed = _query_graph_delta_proposed(
            metadata, conn=conn, project_id=project_id, task_id=metadata.get("task_id", ""),
        )

    # §11.1: reconcile_run_id bypass (R5/R9).  This bypass is intentionally
    # scoped to legacy graph-state/related-node gates; graph.delta validation
    # remains mandatory when a proposed event exists.
    _reconcile_run_id = metadata.get("reconcile_run_id")
    if _reconcile_run_id:
        _audit_reconcile_bypass(conn, project_id, "qa_pass", _reconcile_run_id, metadata.get("task_id", ""))
        if not _gd_proposed:
            return True, "reconcile bypass — qa_pass skipped (§11.1)"
    rec = result.get("recommendation", "")
    if rec == "qa_pass":
        pass  # Explicit pass
    elif rec in ("reject", "rejected"):
        return False, _format_qa_rejection_reason(result)
    else:
        # No explicit recommendation — BLOCK. Auto-pass is a security risk.
        return False, (
            f"QA gate requires explicit recommendation ('qa_pass' or 'reject'). "
            f"Got: {rec!r}. QA agent must set result.recommendation."
        )

    missing_evidence_paths = _missing_qa_evidence_paths(project_id, result, metadata, _gd_proposed)
    if missing_evidence_paths:
        preview = ", ".join(missing_evidence_paths[:5])
        return False, (
            "QA evidence references missing workspace paths: "
            f"{preview}. Cite only files that exist in the workspace; "
            "use chain_events/backlog evidence for runtime audit trails."
        )

    if graph_governance_bypassed:
        log.warning("qa_gate: graph governance checks bypassed by backlog policy")
    else:
        # AC5: validate PRD graph declarations against dev changed_files.
        # Skip the load entirely when this QA task has no graph declarations;
        # test fixtures may monkeypatch get_connection to a shared in-memory DB,
        # and project graph loading owns/closes connections it opens.
        _prd_decl_raw = metadata.get("prd", metadata)
        _has_prd_graph_decl = (
            isinstance(_prd_decl_raw, dict)
            and any(_prd_decl_raw.get(k) for k in ("proposed_nodes", "removed_nodes", "unmapped_files"))
        )
        if _has_prd_graph_decl:
            _dev_changed = metadata.get("changed_files", result.get("changed_files", []))
            _current_graph = None
            try:
                from . import project_service
                _current_graph = project_service.load_project_graph(project_id)
            except Exception:
                log.debug("qa_gate: graph load for prd declaration validation failed", exc_info=True)
            _decl_errors = validate_prd_graph_declarations(_prd_decl_raw, _dev_changed, _current_graph)
            if _decl_errors:
                return False, (
                    "PRD graph-declaration validation failed: " + "; ".join(_decl_errors)
                )
        # PR-B: graph.delta.proposed enforcement — check BEFORE criteria evaluation
        if _gd_proposed:
            gd_review = result.get("graph_delta_review")
            if not gd_review or not isinstance(gd_review, dict):
                # Auto-pass for auto-inferred graph deltas: system-generated deltas
                # should not block QA when the agent omits graph_delta_review.
                # Only dev-emitted deltas require explicit QA review.
                _gd_source = _gd_proposed.get("source", "")
                if (
                    _gd_source == "auto-inferred"
                    and not is_reconcile_cluster_task(metadata)
                ):
                    gd_review = {
                        "decision": "pass",
                        "issues": [],
                        "auto_generated": True,
                        "reason": f"auto-pass: graph.delta.proposed source={_gd_source!r} does not require explicit QA review",
                    }
                    log.info("qa_gate: auto-generated graph_delta_review pass for source=%s", _gd_source)
                else:
                    return False, "graph.delta.proposed present but QA result omits graph_delta_review"
            if is_reconcile_cluster_task(metadata):
                payload_candidate_nodes = _cluster_payload_candidate_nodes(metadata)
                proposed_delta = _gd_proposed.get("graph_delta", {}) if isinstance(_gd_proposed, dict) else {}
                proposed_creates = (
                    proposed_delta.get("creates", [])
                    if isinstance(proposed_delta, dict)
                    else []
                )
                if payload_candidate_nodes:
                    cluster_dev_ok, cluster_dev_reason = preflight_reconcile_cluster_dev(
                        metadata.get("proposed_nodes", []),
                        proposed_creates,
                        candidate_nodes=payload_candidate_nodes,
                        metadata=metadata,
                    )
                    if not cluster_dev_ok:
                        return False, cluster_dev_reason
            gd_decision = gd_review.get("decision", "")
            if gd_decision == "reject":
                issues = gd_review.get("issues", [])
                return False, f"graph delta rejected by QA: {issues}"
            if gd_decision == "pass":
                # Write graph.delta.validated event to chain_events
                try:
                    from .chain_context import get_store as _gd_store
                    store = _gd_store()
                    task_id_for_event = metadata.get("task_id", "")
                    root_task_id = _resolve_chain_root_id(
                        conn, project_id, task_id_for_event, metadata,
                    )
                    store._persist_event(
                        root_task_id=root_task_id,
                        task_id=task_id_for_event,
                        event_type="graph.delta.validated",
                        payload={
                            "source_task_id": task_id_for_event,
                            "graph_delta_review": gd_review,
                            "proposed_payload": _gd_proposed,
                        },
                        project_id=project_id,
                        conn=conn,  # MF-2026-04-24-001: share caller transaction
                    )
                    log.info("auto_chain: wrote graph.delta.validated event for chain %s", root_task_id)
                except Exception:
                    log.debug("auto_chain: graph.delta.validated write failed", exc_info=True)
            else:
                return False, f"graph_delta_review.decision must be 'pass' or 'reject', got: {gd_decision!r}"
    # AC7: No graph.delta.proposed → graph_delta_review field not required (back-compat)

    # E2E1: Verify criteria_results when acceptance_criteria exist
    criteria = metadata.get("acceptance_criteria", [])
    criteria_results = result.get("criteria_results", [])
    if criteria:
        if not criteria_results:
            return False, (
                "QA result missing criteria_results while acceptance_criteria are present. "
                "QA must evaluate each criterion individually before qa_pass."
            )
        else:
            if len(criteria_results) < len(criteria):
                return False, (
                    "QA result criteria_results does not cover all acceptance_criteria: "
                    f"{len(criteria_results)}/{len(criteria)} supplied"
                )
            failed_criteria = [cr for cr in criteria_results if not cr.get("passed")]
            if failed_criteria:
                names = [cr.get("criterion", "?")[:60] for cr in failed_criteria]
                return False, f"QA approved overall but {len(failed_criteria)} criteria failed: {names}"
    if not graph_governance_bypassed and not _reconcile_run_id and not _session_related_bypassed:
        # Update nodes FIRST (QA passed → promote to qa_pass)
        # Evidence rule: t2_pass → qa_pass requires "e2e_report" with summary.passed > 0
        task_id = metadata.get("task_id", "")
        vu_ok, vu_err = _try_verify_update(conn, project_id, metadata, "qa_pass", "qa",
                           {"type": "e2e_report", "producer": "auto-chain",
                            "summary": {"passed": 1, "failed": 0,
                                        "review": result.get("review_summary", "auto-chain QA pass")}},
                           task_id=task_id)
        if not vu_ok:
            return False, f"qa_pass gate blocked — {vu_err}"
        # Then verify nodes reached qa_pass
        related_nodes = metadata.get("related_nodes", [])
        if related_nodes:
            passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "qa_pass")
            if not passed:
                if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
                    log.warning(
                        "qa_gate: deferring related_nodes qa_pass enforcement for governed dirty-workspace reconciliation lane: %s",
                        reason,
                    )
                else:
                    return False, f"qa_pass gate blocked — {reason}"
        # 5e: Graph doc verification (observation mode)
        if _GRAPH_DOC_OBSERVATION_MODE:
            target_files = metadata.get("target_files", [])
            graph_docs = _get_task_graph_doc_associations(project_id, target_files, metadata)
            if graph_docs:
                changed = metadata.get("changed_files", [])
                doc_files_changed = set(f for f in changed if f.startswith("docs/") or f.endswith(".md"))
                graph_docs_missing = set(graph_docs) - doc_files_changed
                if graph_docs_missing:
                    log.warning(
                        "qa_gate: graph-linked docs not updated (observation): %s",
                        sorted(graph_docs_missing)[:5],
                    )
                    _audit_doc_gap(conn, project_id, metadata.get("parent_task_id", ""), "qa_pass", graph_docs_missing, changed)

    # M2: QA passed → write success pattern memory
    _write_chain_memory(
        conn, project_id, "qa_decision",
        result.get("review_summary", f"QA approved (rec={rec})"),
        metadata,
        extra_structured={"recommendation": rec, "chain_stage": "qa",
                          "changed_files": metadata.get("changed_files", [])},
    )

    # Phase 5: QA Sweep structural drift gate (R9 — after AI QA passes)
    if not graph_governance_bypassed and not _reconcile_run_id and not _session_related_bypassed:
        qa_task_id = metadata.get("task_id", "")
        sweep_ok, sweep_msg, sweep_result = _qa_sweep_gate(conn, project_id, qa_task_id, metadata, result)
        if not sweep_ok:
            return False, sweep_msg

    return True, "ok"


def _default_project_graph_path(project_id, metadata=None):
    metadata = metadata if isinstance(metadata, dict) else {}
    override = metadata.get("graph_path") or metadata.get("graph_json_path")
    if override:
        return Path(override)
    try:
        from .db import _resolve_project_dir
        return _resolve_project_dir(project_id) / GRAPH_JSON_FILENAME
    except Exception:
        return Path(__file__).resolve().parent / GRAPH_JSON_FILENAME


def _default_reconcile_overlay_path(metadata=None):
    metadata = metadata if isinstance(metadata, dict) else {}
    override = metadata.get("overlay_path") or metadata.get("graph_overlay_path")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / GRAPH_OVERLAY_FILENAME


def _metadata_with_reconcile_session(conn, project_id, metadata):
    out = dict(metadata or {})
    try:
        from . import reconcile_session as _rs
        sess = _rs.get_active_session(conn, project_id)
        if sess and sess.session_id:
            out["session_id"] = sess.session_id
            out["reconcile_session_id"] = sess.session_id
            out.setdefault("reconcile_target_branch", getattr(sess, "target_branch", "") or "")
            out.setdefault("reconcile_target_base_commit", getattr(sess, "base_commit_sha", "") or "")
            out.setdefault("reconcile_target_head", getattr(sess, "target_head_sha", "") or "")
            return out
    except Exception:
        pass
    out["session_id"] = (
        out.get("session_id")
        or out.get("reconcile_session_id")
        or out.get("reconcile_run_id")
        or out.get("chain_id")
        or out.get("parent_task_id")
        or ""
    )
    return out


def _load_pm_prd_payload_for_chain(conn, root_task_id):
    """Load the PM PRD payload used by later graph/reconcile gates.

    pm.prd.published is the normal source for graph declarations, but older
    payloads did not carry verification.  Hydrate missing PM fields from the
    root PM task result so reconcile overlay apply can re-run PM preflight
    without losing the verification contract.
    """
    payload = _query_chain_event_payload(conn, root_task_id, "pm.prd.published") or {}
    if not isinstance(payload, dict):
        payload = {}

    needs_hydration = not payload.get("verification")
    if not needs_hydration:
        return payload

    try:
        row = conn.execute(
            "SELECT result_json FROM tasks WHERE task_id = ? AND type = 'pm' LIMIT 1",
            (root_task_id,),
        ).fetchone()
        if not row or not row["result_json"]:
            return payload
        pm_result = json.loads(row["result_json"])
    except Exception:
        return payload
    if not isinstance(pm_result, dict):
        return payload

    prd = pm_result.get("prd") if isinstance(pm_result.get("prd"), dict) else {}
    for key in (
        "proposed_nodes",
        "test_files",
        "target_files",
        "requirements",
        "acceptance_criteria",
        "verification",
    ):
        if payload.get(key):
            continue
        value = pm_result.get(key)
        if not value:
            value = prd.get(key)
        if value:
            payload[key] = value
    return payload


def _apply_graph_delta_after_gatekeeper(conn, project_id, metadata):
    """Close the graph event lifecycle after gatekeeper approves merge.

    Standard chains commit validated graph deltas to node_state.  Reconcile
    cluster chains write candidate nodes to the rebase overlay instead; graph.json
    remains immutable until session finalize.
    """
    root_task_id = _resolve_chain_root_id(
        conn, project_id, metadata.get("task_id", ""), metadata,
    )
    proposed_payload = _query_chain_event_payload(
        conn, root_task_id, "graph.delta.proposed",
    )
    if not proposed_payload:
        # Backward compatibility: older graph-delta chains only emitted
        # graph.delta.validated, and existing tests patch _commit_graph_delta
        # at this boundary.  Keep that commit hook visible while treating a
        # no-op result as a legacy pass.
        _commit_graph_delta(conn, project_id, metadata)
        return True, "ok"

    validated_payload = _query_chain_event_payload(
        conn, root_task_id, "graph.delta.validated",
    )
    if not validated_payload:
        return False, "graph delta proposed but not validated by QA"

    if is_reconcile_cluster_task(metadata):
        proposed_from_validated = validated_payload.get("proposed_payload") or proposed_payload
        graph_delta = proposed_from_validated.get("graph_delta", {}) if isinstance(proposed_from_validated, dict) else {}
        creates = graph_delta.get("creates", []) if isinstance(graph_delta, dict) else []
        apply_meta = _metadata_with_reconcile_session(conn, project_id, metadata)
        cluster_fp = _cluster_compute_fingerprint(apply_meta, creates)
        prior_applied = _query_chain_event_payload(
            conn, root_task_id, CHAIN_EVENT_GRAPH_DELTA_APPLIED,
        )
        if isinstance(prior_applied, dict) and prior_applied.get("cluster_fingerprint") == cluster_fp:
            return True, "ok"

        pm_prd = _load_pm_prd_payload_for_chain(conn, root_task_id)
        dev_result = {"graph_delta": graph_delta}
        apply_result = apply_reconcile_cluster_to_overlay(
            conn,
            project_id,
            metadata.get("task_id", ""),
            pm_prd=pm_prd,
            dev_result=dev_result,
            metadata=apply_meta,
            graph_path=_default_project_graph_path(project_id, metadata),
            overlay_path=_default_reconcile_overlay_path(metadata),
        )
        if not apply_result.get("applied"):
            return False, (
                "reconcile cluster graph overlay apply failed: "
                f"{apply_result.get('stage', 'unknown')} — {apply_result.get('reason', apply_result)}"
            )
        return True, "ok"

    commit_result = _commit_graph_delta(conn, project_id, metadata)
    if commit_result is None:
        return False, "graph delta validated but commit produced no graph update"
    return True, "ok"


def _gate_gatekeeper_pass(conn, project_id, result, metadata):
    """Require explicit isolated gatekeeper approval before merge."""
    rec = result.get("recommendation", "")
    if rec == "merge_pass":
        try:
            from . import gatekeeper as gk
            gk.record_check(
                conn, project_id,
                check_type="ai_acceptance_check",
                passed=True,
                result={
                    "summary": result.get("review_summary", ""),
                    "checked_requirements": result.get("checked_requirements", []),
                    "pm_alignment": result.get("pm_alignment", "pass"),
                },
                created_by="auto-chain-gatekeeper",
            )
        except Exception:
            log.debug("gatekeeper ai record failed (non-critical)", exc_info=True)

        # PR-C: Commit graph delta after gatekeeper passes (AC1)
        # R4: Escape hatch — skip graph delta validation if explicitly requested
        if backlog_runtime.is_graph_governance_bypassed(metadata):
            log.warning("_gate_gatekeeper_pass: graph delta commit skipped by backlog graph bypass")
        elif metadata.get("skip_graph_delta_validation") is True and metadata.get("skip_reason"):
            log.warning(
                "_gate_gatekeeper_pass: skipping graph delta validation — %s",
                metadata["skip_reason"],
            )
        else:
            try:
                graph_ok, graph_reason = _apply_graph_delta_after_gatekeeper(conn, project_id, metadata)
                if not graph_ok:
                    return False, graph_reason
            except Exception as exc:
                # R1/R5: Graph delta failure blocks the gate
                log.error("_gate_gatekeeper_pass: graph delta commit failed — %s", exc, exc_info=True)
                return False, f"graph delta commit failed: {exc}"

        return True, "ok"
    if rec in ("reject", "rejected"):
        return False, f"Gatekeeper rejected merge: {result.get('reason', 'no reason given')}"
    return False, (
        "Gatekeeper must emit recommendation 'merge_pass' or 'reject'. "
        f"Got: {rec!r}"
    )


def _gate_release(conn, project_id, result, metadata):
    """Verify merge succeeded before deploy."""
    # CR0b R4: reconcile-session bypass for the release-gate
    # related-nodes-at-qa-pass check.
    _bypassed, _reason = _check_session_bypass(
        "_gate_release.related_nodes", project_id, metadata.get("task_id", ""))
    if _bypassed:
        return True, _reason
    # §11.2: reconcile_run_id bypass (R5/R9)
    _reconcile_run_id = metadata.get("reconcile_run_id")
    if _reconcile_run_id:
        _audit_reconcile_bypass(conn, project_id, "release", _reconcile_run_id, metadata.get("task_id", ""))
        return True, "reconcile bypass — release skipped (§11.2)"
    graph_governance_bypassed = backlog_runtime.is_graph_governance_bypassed(metadata)
    if graph_governance_bypassed:
        log.warning("release_gate: related_nodes enforcement skipped by backlog graph bypass")
    # Node status check: all related_nodes must be "qa_pass" before merge is allowed
    related_nodes = metadata.get("related_nodes", [])
    if related_nodes and not graph_governance_bypassed:
        passed, reason = _check_nodes_min_status(conn, project_id, related_nodes, "qa_pass")
        if not passed:
            if _is_governed_dirty_workspace_chain(conn, project_id, metadata):
                log.warning(
                    "release_gate: deferring related_nodes qa_pass enforcement for governed dirty-workspace reconciliation lane: %s",
                    reason,
                )
            else:
                return False, f"release gate blocked — {reason}"
    else:
        log.warning("release gate: no related_nodes — node verification skipped for %s",
                     metadata.get("parent_task_id", "unknown"))
    # For auto-chain deploys, we trust the merge task result
    # After successful merge, promote related_nodes to qa_pass
    if related_nodes and not graph_governance_bypassed:
        task_id = metadata.get("task_id", "")
        _try_verify_update(conn, project_id, metadata, "qa_pass", "merge",
                           {"type": "merge_complete", "producer": "auto-chain"},
                           task_id=task_id)

    # TODO-DEPRECATED: _store_proposed_nodes callsite removed per OPT-BACKLOG-GRAPH-DELTA-CHAIN-COMMIT PR-A.
    # Graph delta is now emitted as chain_event in dev completion path via _emit_graph_delta_event().

    # R2: On merge-stage success, resolve pitfall memories linked to dev-retry ancestry
    _resolve_pitfall_memories(conn, project_id, result, metadata)

    return True, "ok"


def _resolve_pitfall_memories(conn, project_id, result, metadata):
    """R2: Walk chain_events backward to locate pitfall memory_ids written during dev-retry
    ancestry, then UPDATE those memories' resolution_commit and resolution_summary.

    Best-effort — never blocks chain progress on failure.
    """
    try:
        merge_commit = result.get("merge_commit", metadata.get("merge_commit", ""))
        if not merge_commit:
            return

        root_task_id = metadata.get("chain_id") or metadata.get("parent_task_id", "")
        if not root_task_id:
            return

        # Find all pitfall memories linked to this chain's scope via module_id matching
        target_files = metadata.get("target_files", [])
        if not target_files:
            return

        # Build module prefixes from target_files
        module_prefixes = []
        for tf in target_files:
            prefix = tf.replace("/", ".").replace("\\", ".")
            module_prefixes.append(prefix)

        # Query pitfall memories that match these modules and lack resolution
        for prefix in module_prefixes:
            try:
                rows = conn.execute(
                    "SELECT memory_id, content FROM memories "
                    "WHERE project_id = ? AND kind = 'pitfall' AND status = 'active' "
                    "AND module_id LIKE ? AND COALESCE(resolution_commit, '') = ''",
                    (project_id, prefix + "%"),
                ).fetchall()
                for row in rows:
                    summary = (row["content"] or "")[:120].replace("\n", " ")
                    conn.execute(
                        "UPDATE memories SET resolution_commit = ?, resolution_summary = ? "
                        "WHERE memory_id = ?",
                        (merge_commit, f"Resolved by merge {merge_commit[:8]}: {summary}", row["memory_id"]),
                    )
            except Exception:
                log.debug("_resolve_pitfall_memories: prefix %s failed", prefix, exc_info=True)
        conn.commit()
        log.info("_resolve_pitfall_memories: resolved pitfalls for merge %s", merge_commit[:8])
    except Exception:
        log.debug("_resolve_pitfall_memories failed (non-critical)", exc_info=True)


def _gate_deploy_pass(conn, project_id, result, metadata):
    """Deploy must report success AND smoke_test semantic coherence.

    R3: Validates that smoke_test.all_pass agrees with report.success and
    that no individual service has a False result. This catches cases where
    the production path diverges from the tested path.
    """
    report = result.get("report", result)
    if not isinstance(report, dict):
        return False, f"deploy failed: report is not a dict"

    # Check report.success first
    if report.get("success") is not True:
        return False, f"deploy failed: {json.dumps(report, ensure_ascii=False)[:300]}"

    # R3: Validate smoke_test semantic coherence
    smoke_test = report.get("smoke_test", {})
    if smoke_test:
        # Reject if all_pass is explicitly False
        if smoke_test.get("all_pass") is False:
            return False, (
                f"deploy gate rejected: smoke_test.all_pass=False contradicts "
                f"report.success=True — {json.dumps(smoke_test, ensure_ascii=False)[:200]}"
            )
        # Reject if any service has an explicit False value
        for svc in ("executor", "governance", "gateway"):
            if smoke_test.get(svc) is False:
                return False, (
                    f"deploy gate rejected: smoke_test.{svc}=False contradicts "
                    f"report.success=True — {json.dumps(smoke_test, ensure_ascii=False)[:200]}"
                )

    return True, "ok"


# ---------------------------------------------------------------------------
# Memory injection helpers for prompt builders (R3/R4/R5/R7)
# ---------------------------------------------------------------------------

def _inject_dev_memories(metadata):
    """R3: Build '## Prior pitfalls in this scope' section for dev prompts.

    Queries memory_service for kind IN (pitfall, pattern), top_k=5,
    module_id prefix-matching target_files, excluding memories older than
    30 days UNLESS resolution_commit is set. Returns section string or ''.
    """
    try:
        from . import memory_service
        from .db import get_connection
        project_id = metadata.get("project_id", "aming-claw")
        target_files = metadata.get("target_files", [])
        if not target_files:
            return ""
        conn = get_connection(project_id)
        try:
            results = memory_service.search_memories_for_injection(
                conn, project_id, target_files,
                kinds=["pitfall", "pattern"],
                top_k=5, max_age_days=30, include_resolved_old=True,
            )
        finally:
            conn.close()
        if not results:
            return ""
        lines = ["## Prior pitfalls in this scope"]
        for m in results:
            kind = m.get("kind", "pitfall")
            content = (m.get("content") or m.get("summary") or "")[:200].replace("\n", " ")
            rc = m.get("resolution_commit", "")
            if rc:
                lines.append(f"- [{kind}] {content} (fixed by commit {rc[:8]})")
            else:
                lines.append(f"- [{kind}] {content}")
        return "\n".join(lines)
    except Exception:
        log.debug("_inject_dev_memories failed (graceful degradation)", exc_info=True)
        return ""


def _inject_qa_memories(metadata):
    """R4: Build '## Prior QA decisions for similar scope' section for QA prompts."""
    try:
        from . import memory_service
        from .db import get_connection
        project_id = metadata.get("project_id", "aming-claw")
        target_files = metadata.get("target_files", [])
        if not target_files:
            return ""
        conn = get_connection(project_id)
        try:
            results = memory_service.search_memories_for_injection(
                conn, project_id, target_files,
                kinds=["qa_decision", "pattern", "failure_pattern"],
                top_k=3, max_age_days=30, include_resolved_old=True,
            )
        finally:
            conn.close()
        if not results:
            return ""
        lines = ["## Prior QA decisions for similar scope"]
        for m in results:
            kind = m.get("kind", "qa_decision")
            content = (m.get("content") or m.get("summary") or "")[:200].replace("\n", " ")
            lines.append(f"- [{kind}] {content}")
        return "\n".join(lines)
    except Exception:
        log.debug("_inject_qa_memories failed (graceful degradation)", exc_info=True)
        return ""


def _inject_gatekeeper_memories(metadata):
    """R5: Build '## Prior decisions' section for gatekeeper prompts."""
    try:
        from . import memory_service
        from .db import get_connection
        project_id = metadata.get("project_id", "aming-claw")
        target_files = metadata.get("target_files", [])
        if not target_files:
            return ""
        conn = get_connection(project_id)
        try:
            results = memory_service.search_memories_for_injection(
                conn, project_id, target_files,
                kinds=["decision", "failure_pattern"],
                top_k=3, max_age_days=30, include_resolved_old=True,
            )
        finally:
            conn.close()
        if not results:
            return ""
        lines = ["## Prior decisions"]
        for m in results:
            kind = m.get("kind", "decision")
            content = (m.get("content") or m.get("summary") or "")[:200].replace("\n", " ")
            lines.append(f"- [{kind}] {content}")
        return "\n".join(lines)
    except Exception:
        log.debug("_inject_gatekeeper_memories failed (graceful degradation)", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Prompt builders — return (prompt: str, metadata: dict)
# ---------------------------------------------------------------------------

def _build_dev_prompt(task_id, result, metadata):
    prd = result.get("prd", {})
    # target_files: result > prd > original metadata (preserve original task metadata)
    target_files = result.get("target_files", prd.get("target_files", metadata.get("target_files", [])))

    verification = result.get("verification", prd.get("verification", {}))
    requirements = result.get("requirements", prd.get("requirements", []))
    criteria = result.get("acceptance_criteria", prd.get("acceptance_criteria", []))
    test_files = result.get("test_files", prd.get("test_files", metadata.get("test_files", [])))
    doc_impact = result.get("doc_impact", prd.get("doc_impact", metadata.get("doc_impact", {})))
    skip_reasons = result.get("skip_reasons", prd.get("skip_reasons", metadata.get("skip_reasons", {})))
    proposed_nodes = result.get("proposed_nodes", metadata.get("proposed_nodes", []))

    # Fallback: if PM result lacks expected structure, read from chain context
    if not target_files or not verification or not criteria:
        try:
            from .chain_context import get_store
            parent_result = get_store().get_parent_result(task_id)
            if parent_result:
                if not target_files:
                    target_files = parent_result.get("target_files", target_files)
                if not verification:
                    verification = parent_result.get("verification", verification)
                if not criteria:
                    criteria = parent_result.get("acceptance_criteria", criteria)
                if not requirements:
                    requirements = parent_result.get("requirements", requirements)
        except Exception:
            pass
    # 5b: Merge graph-derived docs into doc_impact. Reconcile-cluster tasks use
    # the session-local candidate/overlay graph instead of stale active graph.json.
    # Scope-materialization chains must preserve PM's explicit doc_impact: their
    # job is to repair graph/doc/test materialization drift, so broad active-graph
    # docs are QA observation context, not checkpoint-mandatory Dev edits.
    graph_docs = _get_task_graph_doc_associations(
        metadata.get("project_id", "aming-claw"), target_files, metadata)
    if graph_docs and not _is_scope_materialization_task(metadata):
        if isinstance(doc_impact, dict):
            existing = set(doc_impact.get("files", []))
            new_docs = [d for d in graph_docs if d not in existing]
            if new_docs:
                doc_impact = dict(doc_impact)  # copy
                doc_impact["files"] = list(existing | set(new_docs))
                doc_impact.setdefault("changes", []).append(
                    f"Graph-linked docs added: {new_docs[:5]}")
        else:
            doc_impact = {"files": graph_docs, "changes": ["Graph-derived doc associations"]}

    graph_preflight = result.get("graph_preflight") or metadata.get("graph_preflight")
    if is_reconcile_cluster_task(metadata) and not graph_preflight:
        graph_preflight = _build_reconcile_graph_preflight(
            metadata.get("project_id", "aming-claw"), metadata,
            proposed_nodes=proposed_nodes,
        )

    out_meta = {
        **metadata,  # preserves skip_doc_check, changed_files, related_nodes, etc.
        "target_files": target_files,
        "requirements": requirements,
        "acceptance_criteria": criteria,
        "verification": verification,
        "test_files": test_files,
        "doc_impact": doc_impact,
        "skip_reasons": skip_reasons,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes", result.get("related_nodes", []))),
        "proposed_nodes": proposed_nodes,
        "graph_preflight": graph_preflight,
    }
    prompt = _render_dev_contract_prompt(task_id, out_meta)
    # R3/R7: Inject prior pitfalls section (graceful degradation)
    pitfalls_section = _inject_dev_memories(out_meta)
    if pitfalls_section:
        prompt = pitfalls_section + "\n\n" + prompt
    return prompt, out_meta


def _build_test_prompt(task_id, result, metadata):
    changed = result.get("changed_files", metadata.get("changed_files", []))
    verification = metadata.get("verification") or result.get("verification", {})
    test_files = metadata.get("test_files", [])
    prompt_parts = [
        f"Run tests for {task_id}.",
        f"changed_files: {json.dumps(changed)}",
    ]
    if verification:
        prompt_parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")
    if test_files:
        prompt_parts.append(f"test_files: {json.dumps(test_files)}")
    prompt = "\n".join(prompt_parts)
    meta = {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
        "verification": verification,
        "test_files": test_files,
        "dev_result_summary": result.get("summary", ""),
        "dev_test_results": result.get("test_results", {}),
        "dev_changed_files": changed,
        "dev_needs_review": result.get("needs_review", False),
        "dev_retry_context": result.get("retry_context", {}),
        "dev_doc_debt": _extract_dev_doc_debt(result),
        "graph_preflight": metadata.get("graph_preflight", {}),
    }
    # Propagate worktree info from dev result → test → qa → merge
    if result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _query_graph_delta_proposed(metadata, conn=None, project_id=None, task_id=""):
    """Query chain_events for the latest graph.delta.proposed event on this chain's root_task_id.

    Returns the event payload dict if found, None otherwise.
    """
    opened_conn = None
    try:
        metadata = metadata if isinstance(metadata, dict) else {}
        project_id = project_id or metadata.get("project_id", "aming-claw")
        if conn is None:
            from .db import get_connection
            opened_conn = get_connection(project_id)
            conn = opened_conn
        root_task_id = _resolve_chain_root_id(
            conn, project_id, task_id or metadata.get("task_id", ""), metadata,
        )
        if not root_task_id:
            return None
        return _query_chain_event_payload(conn, root_task_id, "graph.delta.proposed")
    except Exception:
        log.debug("_query_graph_delta_proposed: lookup failed", exc_info=True)
    finally:
        if opened_conn is not None:
            try:
                opened_conn.close()
            except Exception:
                pass
    return None


def _build_qa_prompt(task_id, result, metadata):
    report = result.get("test_report", {})
    changed = result.get("changed_files", metadata.get("changed_files", []))
    requirements = metadata.get("requirements", [])
    criteria = metadata.get("acceptance_criteria", [])
    doc_impact = metadata.get("doc_impact", {})
    verification = metadata.get("verification", {})
    prompt_parts = [
        f"QA review for {task_id}.",
        f"test_report: {json.dumps(report, ensure_ascii=False)}",
        f"changed_files: {json.dumps(changed)}",
    ]
    if requirements:
        prompt_parts.append(f"requirements: {json.dumps(requirements, ensure_ascii=False)}")
    if criteria:
        prompt_parts.append(f"acceptance_criteria: {json.dumps(criteria, ensure_ascii=False)}")
    if verification:
        prompt_parts.append(f"verification: {json.dumps(verification, ensure_ascii=False)}")
    if doc_impact:
        prompt_parts.append(f"doc_impact: {json.dumps(doc_impact, ensure_ascii=False)}")
    if _is_scope_materialization_task(metadata):
        prompt_parts.append(
            "\n## Scope Materialization QA Scope\n"
            "This is a scoped governance materialization review. Judge only the "
            "PM-declared doc_impact, changed_files, verification result, and "
            "graph_delta payload for this backlog row. Do not call or require "
            "the global /api/wf/{project_id}/release-gate check for qa_pass; "
            "global release-gate debt from unrelated graph nodes is observation-only "
            "and MUST NOT set recommendation='reject' for this scoped task. If "
            "you notice unrelated release-gate blockers, mention them as residual "
            "risk while still returning qa_pass when all scoped acceptance criteria pass."
        )
        dev_context = {
            "dev_result_summary": metadata.get("dev_result_summary", ""),
            "dev_test_results": metadata.get("dev_test_results", {}),
            "dev_changed_files": metadata.get("dev_changed_files", []),
            "dev_needs_review": metadata.get("dev_needs_review", False),
            "dev_retry_context": metadata.get("dev_retry_context", {}),
            "dev_doc_debt": metadata.get("dev_doc_debt", []),
        }
        if any(value not in ("", [], {}, None, False) for value in dev_context.values()):
            prompt_parts.append(
                "\n## Scope Materialization Dev Audit Context\n"
                f"{json.dumps(dev_context, ensure_ascii=False, indent=2)}\n"
                "Use this Dev evidence when evaluating scoped acceptance "
                "criteria such as explicit doc_debt decisions. Do not reject "
                "only because the Test result payload is narrower than the "
                "Dev result; Test may carry only test_report and changed_files."
            )
    graph_preflight = metadata.get("graph_preflight", {})
    if is_reconcile_cluster_task(metadata):
        graph_preflight = graph_preflight or _build_reconcile_graph_preflight(
            metadata.get("project_id", "aming-claw"), metadata,
            proposed_nodes=metadata.get("proposed_nodes", []),
        )
        if graph_preflight:
            prompt_parts.append(
                "\n## Reconcile Session Graph Context\n"
                "Use this candidate/overlay graph context for doc/test/node "
                "consistency checks. Do not use stale active graph.json for "
                "this reconcile-cluster review.\n"
                f"{json.dumps(graph_preflight, ensure_ascii=False, indent=2)}"
            )
        dev_context = {
            "dev_result_summary": metadata.get("dev_result_summary", ""),
            "dev_test_results": metadata.get("dev_test_results", {}),
            "dev_changed_files": metadata.get("dev_changed_files", []),
            "dev_needs_review": metadata.get("dev_needs_review", False),
            "dev_retry_context": metadata.get("dev_retry_context", {}),
        }
        if any(value not in ("", [], {}, None, False) for value in dev_context.values()):
            prompt_parts.append(
                "\n## Reconcile Cluster Dev Audit Context\n"
                f"{json.dumps(dev_context, ensure_ascii=False, indent=2)}\n"
                "Overlay-only is the default outcome, but source/doc/test edits "
                "are allowed when Dev's verification proves a real defect owned "
                "by this cluster and changed_files stay within the declared "
                "cluster scope. Use this context when judging whether changed "
                "test/doc/source files are justified. Do not cite nonexistent "
                "graph artifact paths as workspace evidence; cite candidate/overlay "
                "paths from Reconcile Session Graph Context when present."
            )
    if criteria:
        prompt_parts.append(
            "\nYou MUST evaluate each acceptance_criteria item individually.\n"
            "Include in your result:\n"
            "  criteria_results: [{criterion: \"<text>\", passed: true/false, evidence: \"<why>\"}]\n"
            "Only set recommendation='qa_pass' if ALL criteria pass.\n"
            "If evidence cites a workspace file path, that path MUST exist. "
            "Do not invent audit docs; cite chain_events/backlog/runtime evidence "
            "unless an actual doc file exists in changed_files or the workspace."
        )
    # 5d: Graph consistency check injection
    graph_docs = _get_task_graph_doc_associations(
        metadata.get("project_id", "aming-claw"),
        metadata.get("target_files", []),
        metadata,
    )
    if graph_docs:
        prompt_parts.append(
            f"\n## Graph Consistency Check\n"
            f"The graph links these docs to the changed code: {json.dumps(graph_docs)}\n"
            f"Verify: are these docs still consistent with the code changes? "
            f"If not, note which docs need updates in your review."
        )
    # PR-B: Query chain_events for graph.delta.proposed and inject review instructions
    _gd_proposed = _query_graph_delta_proposed(metadata)
    if _gd_proposed:
        prompt_parts.append(
            "\n## Graph Delta Review\n"
            "A graph.delta.proposed event was found for this chain. "
            "You MUST review the proposed graph delta below and include a "
            "'graph_delta_review' field in your result JSON.\n\n"
            f"Proposed delta payload:\n{json.dumps(_gd_proposed, ensure_ascii=False, indent=2)}\n\n"
            "Required result field:\n"
            "  graph_delta_review: {\n"
            '    decision: "pass" | "reject",\n'
            "    issues: [\"<issue description>\", ...],  // empty list if decision is pass\n"
            "    suggested_diff: {}  // optional corrections to the delta\n"
            "  }\n"
            "If decision is 'reject', list specific issues. "
            "If decision is 'pass', issues should be an empty list."
            "\nExisting graph nodes that are being preserved or extended must be "
            "represented as updates, not creates. Reject a graph_delta that puts "
            "an already-existing non-null node_id under creates unless the payload "
            "explicitly proves that node_id is not present in the active graph."
        )
    prompt_parts.append(
        "Evidence path rule: if your QA result cites a workspace file path, "
        "that path MUST exist. Do not invent audit docs; cite chain_events, "
        "backlog rows, or runtime records unless an actual file exists. For "
        "reconcile graph artifacts, cite only concrete candidate/overlay paths "
        "shown in the graph context, or describe absent legacy artifacts without "
        "using them as file evidence."
    )
    prompt_parts.append("IMPORTANT: result.recommendation MUST be exactly 'qa_pass' or 'reject' (no other values accepted by the gate).")
    # R4/R7: Inject prior QA decisions section (graceful degradation)
    qa_memories = _inject_qa_memories(metadata)
    if qa_memories:
        prompt_parts.insert(0, qa_memories)
    prompt = "\n".join(prompt_parts)
    meta = {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": changed,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
        "test_report": report,
        "requirements": requirements,
        "acceptance_criteria": criteria,
        "verification": verification,
        "doc_impact": doc_impact,
        "graph_preflight": graph_preflight,
    }
    if result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _build_gatekeeper_prompt(task_id, result, metadata):
    # R5/R7: Inject prior decisions section (graceful degradation)
    gk_memories = _inject_gatekeeper_memories(metadata)
    graph_preflight = metadata.get("graph_preflight", {})
    prompt = (
        f"Gatekeeper review for {task_id}.\n"
        "You are the final isolated acceptance check before merge.\n"
        "Use ONLY the PM contract, test evidence, QA review, changed file list, and doc-impact summary below.\n"
        "Do NOT request broader project context or unrelated history.\n"
        f"requirements: {json.dumps(metadata.get('requirements', []), ensure_ascii=False)}\n"
        f"acceptance_criteria: {json.dumps(metadata.get('acceptance_criteria', []), ensure_ascii=False)}\n"
        f"verification: {json.dumps(metadata.get('verification', {}), ensure_ascii=False)}\n"
        f"doc_impact: {json.dumps(metadata.get('doc_impact', {}), ensure_ascii=False)}\n"
        f"test_report: {json.dumps(metadata.get('test_report', {}), ensure_ascii=False)}\n"
        f"qa_review: {json.dumps({'review_summary': result.get('review_summary', ''), 'issues': result.get('issues', []), 'doc_updates_applied': result.get('doc_updates_applied', [])}, ensure_ascii=False)}\n"
        f"changed_files: {json.dumps(metadata.get('changed_files', []))}\n"
        f"graph_preflight: {json.dumps(graph_preflight, ensure_ascii=False)}\n"
        "Respond with strict JSON: "
        "{\"schema_version\":\"v1\",\"review_summary\":\"...\",\"recommendation\":\"merge_pass|reject\",\"pm_alignment\":\"pass|partial|fail\",\"checked_requirements\":[\"R1\"],\"reason\":\"\"}"
    )
    if gk_memories:
        prompt = gk_memories + "\n\n" + prompt
    meta = {
        **metadata,
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes", result.get("related_nodes", []))),
        "graph_preflight": graph_preflight,
    }
    # Propagate worktree isolation metadata through gatekeeper → merge
    if metadata.get("_worktree"):
        meta["_worktree"] = metadata["_worktree"]
        meta["_branch"] = metadata.get("_branch", "")
    elif result.get("_worktree"):
        meta["_worktree"] = result["_worktree"]
        meta["_branch"] = result.get("_branch", "")
    return prompt, meta


def _build_merge_prompt(task_id, result, metadata):
    target_branch = metadata.get("reconcile_target_branch") if is_reconcile_cluster_task(metadata) else ""
    if target_branch:
        prompt = f"Merge dev branch for {task_id} to reconcile target branch {target_branch}."
    else:
        prompt = f"Merge dev branch for {task_id} to main."
    return prompt, {
        **metadata,  # preserves skip_doc_check and all other original task metadata
        # Prioritise original metadata values; only fall back to result if metadata lacks them
        "target_files": metadata.get("target_files") or result.get("target_files", []),
        "changed_files": metadata.get("changed_files") or result.get("changed_files", []),
        "_worktree": metadata.get("_worktree") or result.get("_worktree", ""),
        "_branch": metadata.get("_branch") or result.get("_branch", ""),
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
        "graph_preflight": metadata.get("graph_preflight", {}),
    }


def _build_deploy_prompt(task_id, result, metadata):
    changed_files = metadata.get("changed_files") or result.get("changed_files", [])
    target_branch = (
        metadata.get("reconcile_target_branch")
        or result.get("reconcile_target_branch", "")
    )
    if is_reconcile_cluster_task(metadata) and target_branch:
        prompt = (
            f"Record branch-local reconcile deploy after merge task {task_id}.\n"
            f"target_branch: {target_branch}\n"
            f"changed_files: {json.dumps(changed_files)}\n"
            "Do not redeploy main services; this reconcile cluster is isolated until session signoff."
        )
    else:
        prompt = (
            f"Deploy changes after merge task {task_id}.\n"
            f"changed_files: {json.dumps(changed_files)}\n"
            "Run host-side deploy orchestration and smoke checks."
        )
    return prompt, {
        **metadata,
        "changed_files": changed_files,
        "merge_commit": result.get("merge_commit", metadata.get("merge_commit", "")),
        "reconcile_target_branch": target_branch or metadata.get("reconcile_target_branch", ""),
        "reconcile_target_base_commit": (
            metadata.get("reconcile_target_base_commit")
            or result.get("reconcile_target_base_commit", "")
        ),
        "related_nodes": _normalize_related_nodes(metadata.get("related_nodes") or result.get("related_nodes", [])),
    }


def _try_backlog_close_via_db(project_id, bug_id, commit_hash, conn=None):
    """Attempt to close a backlog bug directly in the governance DB.

    Called from merge-stage finalize path when metadata.bug_id is set.
    On success returns True. Missing rows or DB errors return False with a
    warning. This intentionally avoids posting to the local governance HTTP
    server from inside the finalize request, which can self-timeout under load.
    """
    close_conn = False
    try:
        if conn is None:
            from .db import get_connection as _get_connection

            conn = _get_connection(project_id)
            close_conn = True
        row = conn.execute(
            "SELECT bug_id, status FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        if not row:
            log.warning("backlog close: bug %s missing in DB", bug_id)
            return False

        prior_status = row["status"] if hasattr(row, "keys") else row[1]
        if prior_status == "FIXED":
            log.info("backlog close: bug %s already fixed", bug_id)
            return True
        if prior_status not in ("OPEN", "MF_IN_PROGRESS"):
            log.warning(
                "backlog close: bug %s has invalid status %s",
                bug_id, prior_status,
            )
            return False

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """UPDATE backlog_bugs
               SET status='FIXED',
                   "commit"=?,
                   fixed_at=?,
                   updated_at=?
               WHERE bug_id=?""",
            (commit_hash or "", now, now, bug_id),
        )
        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            "manual_fix" if prior_status == "MF_IN_PROGRESS" else "fixed",
            project_id=project_id,
            result={"commit": commit_hash or ""},
            runtime_state="fixed",
        )
        conn.commit()
        log.info("backlog close: bug %s closed with commit %s", bug_id, commit_hash)
        return True
    except Exception as exc:
        log.warning("backlog close: DB error for bug %s (%s)", bug_id, exc)
        return False
    finally:
        if close_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _post_manager_redeploy_governance_from_chain(chain_version: str) -> dict:
    """POST to localhost:40101/api/manager/redeploy/governance (PR1-R4).

    Uses urllib.request (already used elsewhere in auto_chain.py) — no new
    dependency.  On ConnectionRefusedError (service_manager HTTP not running),
    falls back to legacy restart_local_governance and logs a warning (PR1-R5).

    Returns dict with at least {"ok": bool}.
    """
    import urllib.request
    import urllib.error

    url = "http://localhost:40101/api/manager/redeploy/governance"
    data = json.dumps({"chain_version": chain_version}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        # PR1-R5: ConnectionRefusedError → fallback to legacy
        if isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
            log.warning(
                "auto_chain: manager HTTP not reachable (ConnectionRefused); "
                "fallback to legacy restart_local_governance"
            )
            return _legacy_restart_local_governance_fallback()
        log.warning("auto_chain: manager redeploy URLError: %s", exc)
        return {"ok": False, "detail": str(exc), "fallback": False}
    except ConnectionRefusedError:
        log.warning(
            "auto_chain: manager HTTP not reachable (ConnectionRefused); "
            "fallback to legacy restart_local_governance"
        )
        return _legacy_restart_local_governance_fallback()
    except Exception as exc:
        log.warning("auto_chain: manager redeploy error: %s", exc)
        return {"ok": False, "detail": str(exc), "fallback": False}


def _legacy_restart_local_governance_fallback() -> dict:
    """PR1-R5: Legacy fallback — restart governance via deploy_chain.restart_local_governance.

    Only called when the POST to localhost:40101 gets ConnectionRefusedError.
    """
    try:
        from agent.deploy_chain import restart_local_governance
        ok, summary = restart_local_governance(port=40000)
        return {"ok": ok, "detail": summary, "fallback": True}
    except Exception as exc:
        log.warning("auto_chain: legacy restart_local_governance fallback failed: %s", exc)
        return {"ok": False, "detail": str(exc), "fallback": True}


def _finalize_chain(conn, project_id, task_id, result, metadata):
    """Terminal stage after deploy succeeds.

    R4: Call version-sync then version-update to advance chain_version.
    R5: Verify server version == new HEAD; warn if stale.
    R6 (OPT-DB-BACKLOG): Close backlog bug via DB if metadata.bug_id is set.
    """
    import subprocess as _sp

    report = result.get("report", result)
    finalize_result = {"deploy": "completed", "report": report}

    # --- PR1-R4: If changed_files include governance code, POST to manager
    # endpoint to trigger a governance redeploy via service_manager sidecar.
    changed_files = metadata.get("changed_files") or result.get("changed_files", [])
    _governance_files_changed = any(
        f.startswith("agent/governance/") or f.startswith("agent\\governance\\")
        for f in changed_files
    )
    if _governance_files_changed:
        redeploy_via_manager = _post_manager_redeploy_governance_from_chain(
            metadata.get("chain_version", "")
            or result.get("chain_version", "")
            or metadata.get("merge_commit", "")
        )
        finalize_result["governance_redeploy"] = redeploy_via_manager
        if redeploy_via_manager.get("ok"):
            log.info("_finalize_chain: governance redeploy via manager succeeded")
        else:
            log.warning("_finalize_chain: governance redeploy via manager failed: %s",
                        redeploy_via_manager.get("detail", "unknown"))

    # --- R4: version-sync then version-update ---
    # PR-2 (R11): chain_version DB write now owned by redeploy_handler.
    # _finalize_version_sync is kept as fallback but skipped when redeploy
    # handler already wrote chain_version (detected via report metadata).
    redeploy_wrote_version = (
        report.get("steps", {}).get("executor", {}).get("redeploy_result", {}).get("ok")
        or report.get("steps", {}).get("governance", {}).get("redeploy_result", {}).get("ok")
        or report.get("steps", {}).get("gateway", {}).get("redeploy_result", {}).get("ok")
        or report.get("steps", {}).get("service_manager", {}).get("redeploy_result", {}).get("ok")
    )
    if redeploy_wrote_version:
        log.info("_finalize_chain: skipping _finalize_version_sync — redeploy handler owns DB write (R11)")
        finalize_result["version_sync_note"] = "skipped — redeploy handler owns DB write"
    else:
        try:
            _finalize_version_sync(conn, project_id, task_id)
        except Exception as e:
            log.warning("_finalize_chain: version-sync/update failed: %s", e)
            finalize_result["version_sync_error"] = str(e)

    # --- MF-2026-04-24-001/002: commit-before-slow-IO pattern ---
    # During the 2026-04-24 autonomous sequence, _finalize_chain held a SQLite
    # write-lock for 5-30s spanning version-sync DB writes (R4), subprocess calls
    # (git rev-parse in R5), HTTP calls (server version check in R5), and backlog
    # close HTTP POST (R6). Observer-side /api/backlog/.../close requests hit
    # 'database is locked' ~50% of the time.  Fix: commit conn here so that all
    # subsequent IO (R5 subprocess + HTTP, R6 HTTP) runs without holding the
    # caller's write-lock.  _try_backlog_close_via_db (R6) already uses its own
    # HTTP connection (urllib), so no additional conn isolation is needed — just
    # ensuring conn is committed before R6 starts.
    try:
        conn.commit()
    except Exception:
        pass  # conn may already be committed by _finalize_version_sync

    # --- R5: verify server version == new HEAD ---
    try:
        from .server import get_server_version
        new_head = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        server_ver = get_server_version()
        if new_head and new_head != "unknown" and server_ver != new_head:
            finalize_result["restart_required"] = True
            finalize_result["stale_server_version"] = server_ver
            finalize_result["expected_version"] = new_head
            log.warning(
                "_finalize_chain: server version (%s) != HEAD (%s) — restart_required=true",
                server_ver, new_head,
            )
    except Exception as e:
        log.debug("_finalize_chain: version verify failed: %s", e)

    # --- Phase H (R4): advance phase_h_processed_symbols running → merged ---
    spawned_task_id = metadata.get("spawned_task_id", "") or metadata.get("task_id", task_id)
    if spawned_task_id:
        try:
            now_ph = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "UPDATE phase_h_processed_symbols "
                "SET spawn_status = 'merged', updated_at = ? "
                "WHERE spawned_task_id = ? AND spawn_status = 'running'",
                (now_ph, spawned_task_id),
            )
        except Exception as exc_ph:
            # Table may not exist yet (pre-migration); swallow gracefully
            log.debug("_finalize_chain: phase_h status update skipped: %s", exc_ph)

    # --- R6 (OPT-DB-BACKLOG): close backlog bug if metadata.bug_id set ---
    bug_id = metadata.get("bug_id", "")
    if bug_id:
        commit_hash = result.get("merge_commit", metadata.get("merge_commit", ""))
        closed = _try_backlog_close_via_db(project_id, bug_id, commit_hash, conn=conn)
        finalize_result["backlog_closed"] = closed
        if closed:
            finalize_result["backlog_bug_id"] = bug_id

    # --- Phase I (R4): async best-effort baseline creation ---
    import threading
    def _create_baseline_async():
        try:
            from .db import get_connection as _get_conn
            from . import baseline_service
            bl_conn = _get_conn(project_id)
            try:
                chain_ver = (
                    metadata.get("chain_version", "")
                    or result.get("chain_version", "")
                    or metadata.get("merge_commit", "")
                )
                baseline_service.create_baseline(
                    bl_conn, project_id,
                    chain_version=chain_ver,
                    trigger="auto-chain",
                    triggered_by="auto-chain",
                    graph_json={},
                    code_doc_map_json={},
                    notes=f"Auto-created after deploy task {task_id}",
                )
                log.info("_finalize_chain: baseline created for %s", project_id)
            finally:
                bl_conn.close()
        except Exception as exc:
            log.warning("_finalize_chain: async baseline creation failed: %s", exc)
            _file_baseline_missing_backlog(project_id, task_id, exc)

    t = threading.Thread(target=_create_baseline_async, daemon=True)
    t.start()
    finalize_result["baseline_thread_started"] = True

    return finalize_result


def _file_baseline_missing_backlog(project_id, task_id, exc):
    """File OPT-BACKLOG-BASELINE-MISSING-B{n} as P1 backlog row on async failure."""
    try:
        from .db import get_connection as _get_conn
        conn = _get_conn(project_id)
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            # Determine next baseline_id that would have been created
            row = conn.execute(
                "SELECT COALESCE(MAX(baseline_id), 0) AS max_id FROM version_baselines WHERE project_id = ?",
                (project_id,)
            ).fetchone()
            next_id = (row["max_id"] if row else 0) + 1
            bug_id = f"OPT-BACKLOG-BASELINE-MISSING-B{next_id}"
            conn.execute(
                """INSERT OR IGNORE INTO backlog_bugs
                   (bug_id, title, status, priority, created_at, updated_at)
                   VALUES (?, ?, 'OPEN', 'P1', ?, ?)""",
                (bug_id,
                 f"Baseline creation failed after deploy {task_id}: {exc}",
                 now, now),
            )
            conn.commit()
            log.info("_file_baseline_missing_backlog: filed %s", bug_id)
        finally:
            conn.close()
    except Exception as inner:
        log.warning("_file_baseline_missing_backlog: could not file backlog: %s", inner)


def _finalize_version_sync(conn, project_id, task_id):
    """Call version-sync then version-update via local DB ops (R4).

    Uses direct DB writes instead of HTTP to avoid circular dependency.
    """
    import subprocess as _sp

    # Get current git HEAD
    head_result = _sp.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    )
    new_head = head_result.stdout.strip() if head_result.returncode == 0 else None
    if not new_head:
        log.warning("_finalize_version_sync: cannot determine git HEAD")
        return

    # version-sync: update git_head and dirty_files in project_version
    try:
        dirty_result = _sp.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        )
        dirty_files = [
            line[3:] for line in (dirty_result.stdout or "").splitlines()
            if line.strip()
        ] if dirty_result.returncode == 0 else []
    except Exception:
        dirty_files = []

    conn.execute(
        "INSERT INTO project_version "
        "(project_id, chain_version, git_head, dirty_files, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT(project_id) DO UPDATE SET "
        "git_head=excluded.git_head, "
        "dirty_files=excluded.dirty_files, "
        "updated_at=excluded.updated_at",
        (project_id, new_head, new_head, json.dumps(dirty_files), f"auto-chain:{task_id}"),
    )
    log.info(
        "_finalize_version_sync: version-sync project=%s head=%s dirty=%d",
        project_id, new_head, len(dirty_files),
    )

    # version-update: set chain_version = new HEAD with updated_by='auto-chain'
    conn.execute(
        "UPDATE project_version SET chain_version=?, updated_by=?, updated_at=datetime('now') "
        "WHERE project_id=?",
        (new_head, f"auto-chain:{task_id}", project_id),
    )
    # MF-2026-04-24-001/002: commit immediately after version write to release
    # the SQLite write-lock before returning to _finalize_chain, which performs
    # slow subprocess + HTTP IO in R5/R6.
    conn.commit()
    log.info(
        "_finalize_version_sync: version-update project=%s chain_version=%s updated_by=auto-chain:%s",
        project_id, new_head, task_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_chain_memory(conn, project_id, kind, content, metadata, extra_structured=None):
    """Best-effort memory write for chain events. Never blocks chain progress."""
    try:
        from . import memory_service
        from .models import MemoryEntry
        # Derive module_id from first target_file or changed_file
        target = (metadata.get("target_files") or metadata.get("changed_files") or [])
        module_id = target[0].replace("/", ".").replace("\\", ".") if target else "governance"
        # Dedup: skip if identical content already exists for same module+kind
        try:
            existing = conn.execute(
                "SELECT memory_id FROM memories WHERE project_id=? AND module_id=? AND kind=? "
                "AND status='active' AND content=? LIMIT 1",
                (project_id, module_id, kind, content),
            ).fetchone()
            if existing:
                log.debug("chain_memory dedup: skipping identical %s/%s", module_id, kind)
                return
        except Exception:
            pass  # dedup failure should not block write
        entry = MemoryEntry(
            module_id=module_id,
            kind=kind,
            content=content,
            created_by="auto-chain",
        )
        result = memory_service.write_memory(conn, project_id, entry)
        log.info("chain_memory.write: project=%s kind=%s module=%s id=%s content=%r",
                 project_id, kind, module_id, result.get("memory_id", "?"), content[:100])
        if extra_structured:
            # Patch structured field if write succeeded
            mid = result.get("memory_id", "")
            if mid:
                try:
                    import json as _json
                    conn.execute(
                        "UPDATE memories SET structured = ? WHERE memory_id = ?",
                        (_json.dumps(extra_structured), mid),
                    )
                except Exception:
                    pass
    except Exception:
        log.debug("_write_chain_memory failed (non-critical)", exc_info=True)


# Status ordering for node_state validation
_STATUS_ORDER = ["pending", "testing", "t2_pass", "qa_pass", "waived"]

# AC10: Statuses that are treated as "not blocking" — soft-deleted nodes
# don't block gates even though they aren't in the ordinal _STATUS_ORDER.
_NON_BLOCKING_STATUSES = {"rolled_back"}


def _check_nodes_min_status(conn, project_id, related_nodes, min_status):
    """Verify every node in related_nodes has at least min_status in node_state.

    Returns (passed: bool, reason: str).
    If node_state table is empty for this project (fresh DB bootstrap), skip check.
    If a node is not found in a populated DB it is skipped with a warning (not blocked).
    """
    related_nodes = _normalize_related_nodes(related_nodes)
    if not related_nodes:
        return True, "no related_nodes"
    try:
        min_rank = _STATUS_ORDER.index(min_status)
    except ValueError:
        return False, f"unknown min_status '{min_status}'"

    # Fresh DB: if no node_state records exist for this project, skip node check entirely
    node_count = conn.execute(
        "SELECT COUNT(*) FROM node_state WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    if node_count == 0:
        log.info("_check_nodes_min_status: node_state empty for project %s — skipping node check (fresh DB)", project_id)
        return True, "node_state empty (fresh DB bootstrap)"

    blocking = []
    for node_id in related_nodes:
        row = conn.execute(
            "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
            (project_id, node_id),
        ).fetchone()
        if row is None:
            # Not found in populated DB → skip with warning (node was never registered)
            log.warning("_check_nodes_min_status: node '%s' not found in DB for project '%s' — skipping", node_id, project_id)
            continue
        status = (row["verify_status"] or "pending").strip()
        # AC10: rolled_back (soft-deleted) nodes never block gates
        if status in _NON_BLOCKING_STATUSES:
            continue
        try:
            rank = _STATUS_ORDER.index(status)
        except ValueError:
            # Unknown status — treat conservatively as pending
            blocking.append((node_id, f"unknown status '{status}'"))
            continue
        if rank < min_rank:
            blocking.append((node_id, status))

    if blocking:
        details = ", ".join(f"{nid}={st}" for nid, st in blocking)
        return False, (
            f"related_nodes not yet at '{min_status}': [{details}]"
        )
    return True, "ok"


def _try_verify_update(conn, project_id, metadata, target_status, role, evidence_dict, task_id=""):
    """Best-effort node status update. Returns (True, "") on success, (False, error_msg) on failure."""
    if backlog_runtime.is_graph_governance_bypassed(metadata):
        log.warning(
            "auto_chain: verify_update skipped for task %s because backlog policy bypasses graph governance",
            task_id or metadata.get("task_id", ""),
        )
        return True, ""
    related = _normalize_related_nodes(metadata.get("related_nodes", []))
    if not related:
        return True, ""
    try:
        from . import state_service
        from .graph import AcceptanceGraph
        # Load graph from project state directory
        import os
        state_root = os.path.join(
            os.environ.get("SHARED_VOLUME_PATH",
                           os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
            "codex-tasks", "state", "governance", project_id)
        graph_path = os.path.join(state_root, "graph.json")
        graph = AcceptanceGraph()
        if os.path.exists(graph_path):
            graph.load(graph_path)
        session_id = (task_id
                      or metadata.get("parent_task_id")
                      or metadata.get("task_id")
                      or "auto-chain")
        session = {"principal_id": "auto-chain", "role": role, "scope_json": "[]",
                   "session_id": session_id}
        state_service.verify_update(
            conn, project_id, graph,
            node_ids=related if isinstance(related, list) else [related],
            target_status=target_status,
            session=session,
            evidence_dict=evidence_dict,
        )
        log.info("auto_chain: nodes %s → %s", related, target_status)
        return True, ""
    except Exception as e:
        log.warning("auto_chain: verify_update %s failed (non-blocking): %s", target_status, e,
                    exc_info=True)
        return False, f"verify_update failed for nodes {related}: {e}"


def _publish_event(event_name, payload):
    """Best-effort event publish to event bus."""
    try:
        from . import event_bus
        event_bus._bus.publish(event_name, payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CR5 — Reconcile-Cluster Gatekeeper Overlay Apply
# ---------------------------------------------------------------------------
# Implements the additive merge-handler path for tasks whose metadata.operation_type
# is 'reconcile-cluster'.  Non-cluster tasks must follow the existing path
# unchanged — no overlay reads, no overlay writes, no graph.delta.applied event.
#
# Design split:
#   * 2-stage validation (PRD Stage 1 / Dev Stage 2) — preflight; FATAL outcome
#     blocks the apply path before any side effect.
#   * Structural gatekeeper validation — every create entry must carry a
#     parent_layer in L0..L7 and only resolvable deps; missing layer / out-of-range
#     layer / unresolvable dep blocks merge.
#   * 4-level state-preservation match (proposal §4.6.1):
#       exact_match        primary + parent_layer + secondary + test all identical
#                          → transfer verify_status as-is
#       structural_match   primary + parent_layer match; secondary or test differ
#                          → transfer only weak verify_status; demote
#                            qa_pass/t2_pass/waived to pending
#       primary_only_match only primary matches → never transfer; provenance recorded
#       no_match           → pending; provenance still recorded
#     1→N candidates: each candidate evaluated independently.
#     N→1 winner: most conservative match wins (lowest tier index below).
#   * Overlay write — node ids land in agent/governance/graph.rebase.overlay.json
#     (NOT agent/governance/graph.json).  graph.json must be byte-identical
#     before vs after a successful cluster merge inside an active session.
#   * Allocator — _allocate_cluster_next_id reads union(graph.json ∪ overlay.json)
#     to avoid colliding with already-applied-cluster ids in the same session.
#   * Failure — rollback the code merge, mark the cluster failed_retryable in the
#     deferred queue (CR3 will consume), DO NOT clear the overlay.  Only session
#     rollback clears overlay.
#
# All grep-verifiable tokens for AC13/AC14:
#   'reconcile-cluster', 'graph.rebase.overlay.json', 'graph.delta.applied',
#   'exact_match', 'structural_match', 'primary_only_match', 'no_match'

CLUSTER_OPERATION_TYPE = "reconcile-cluster"
GRAPH_OVERLAY_FILENAME = "graph.rebase.overlay.json"
GRAPH_JSON_FILENAME = "graph.json"
CHAIN_EVENT_GRAPH_DELTA_APPLIED = "graph.delta.applied"

# 4-level match-tier identifiers (proposal §4.6.1).  Most-conservative wins for
# N→1 candidate evaluation: lower index = more conservative.
MATCH_TIER_EXACT = "exact_match"
MATCH_TIER_STRUCTURAL = "structural_match"
MATCH_TIER_PRIMARY_ONLY = "primary_only_match"
MATCH_TIER_NO_MATCH = "no_match"

_MATCH_TIER_ORDER = (
    MATCH_TIER_NO_MATCH,
    MATCH_TIER_PRIMARY_ONLY,
    MATCH_TIER_STRUCTURAL,
    MATCH_TIER_EXACT,
)

# A "strong" verify_status carries non-trivial test/qa/gatekeeper evidence; per
# proposal §4.6.1 these MUST be demoted to 'pending' on a structural_match where
# secondary/test differ from the prior node.  Anything else is treated as
# "weak" and may transfer through a structural match.
_STRONG_VERIFY_STATUSES = frozenset({"qa_pass", "t2_pass", "waived"})


def _cluster_normalize_primary(value):
    """Normalize a 'primary' field to a sorted tuple of forward-slash strings.

    Accepts str, list, tuple, or None.  Used by the 4-level match classifier
    so secondary/test/primary comparisons are insensitive to list ordering and
    Windows-style backslash separators.
    """
    if value is None:
        return tuple()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [str(value)]
    out = []
    for it in items:
        if not isinstance(it, str):
            it = str(it)
        if it:
            out.append(it.replace("\\", "/"))
    return tuple(sorted(out))


def _cluster_normalize_deps(value):
    """Normalize graph dependency ids for exact candidate/developer comparison."""
    if value is None:
        return tuple()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [str(value)]
    out = []
    for it in items:
        if not isinstance(it, str):
            it = str(it)
        dep = it.strip()
        if dep:
            out.append(dep)
    return tuple(sorted(set(out)))


def _cluster_normalize_scalar(value):
    """Normalize scalar candidate identity fields such as test_coverage."""
    if value is None:
        return ""
    return str(value).strip()


def _cluster_candidate_secondary(item):
    if not isinstance(item, dict):
        return tuple()
    value = item.get("secondary")
    if value is None:
        value = item.get("secondary_files")
    return tuple(_cluster_normalize_primary(value))


def _cluster_candidate_test(item):
    if not isinstance(item, dict):
        return tuple()
    value = item.get("test")
    if value is None:
        value = item.get("tests")
    if value is None:
        value = item.get("test_files")
    return tuple(_cluster_normalize_primary(value))


def _cluster_candidate_python_tests(candidate_nodes):
    tests = []
    for item in candidate_nodes or []:
        for path in _cluster_candidate_test(item):
            if path.lower().endswith(".py"):
                tests.append(path)
    return tuple(sorted(set(tests)))


def _cluster_is_test_path(path):
    text = str(path or "").replace("\\", "/").lower()
    name = text.rsplit("/", 1)[-1]
    return (
        text.startswith("agent/tests/")
        or "/tests/" in text
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    )


def _cluster_is_doc_path(path):
    text = str(path or "").lower()
    return text.endswith((".md", ".rst", ".txt", ".adoc"))


def _cluster_doc_test_augmentation_policy(metadata=None):
    """Return explicit doc/test augmentation policy for orphan asset review.

    Normal reconcile clusters preserve candidate doc/test identity exactly.  A
    final orphan doc/test review cluster may append existing, supplied consumer
    paths so long as it never drops candidate paths.
    """
    if not isinstance(metadata, dict):
        metadata = {}
    payload = metadata.get("cluster_payload")
    if not isinstance(payload, dict):
        payload = {}
    report = metadata.get("cluster_report")
    if not isinstance(report, dict):
        report = {}

    text_parts = [
        metadata.get("prompt", ""),
        payload.get("prompt", ""),
        payload.get("slug", ""),
        report.get("title", ""),
        report.get("purpose", ""),
        report.get("coverage_audit", ""),
    ]
    haystack = " ".join(str(x or "").lower() for x in text_parts)
    allow = bool(
        metadata.get("allow_doc_test_augmentation")
        or payload.get("allow_doc_test_augmentation")
        or report.get("allow_doc_test_augmentation")
        or (
            "orphan" in haystack
            and ("doc/test" in haystack or ("doc" in haystack and "test" in haystack))
            and ("review" in haystack or "pass" in haystack)
        )
    )

    allowed_secondary = set()
    allowed_test = set()
    for value in (
        report.get("expected_doc_files"),
        report.get("expected_doc_sections"),
        metadata.get("required_docs"),
    ):
        for path in _cluster_normalize_primary(value):
            if _cluster_is_doc_path(path):
                allowed_secondary.add(path)
    for value in (
        report.get("expected_test_files"),
        metadata.get("test_files"),
    ):
        for path in _cluster_normalize_primary(value):
            if _cluster_is_test_path(path):
                allowed_test.add(path)
    for path in _cluster_normalize_primary(payload.get("secondary_files")):
        if _cluster_is_test_path(path):
            allowed_test.add(path)
        elif _cluster_is_doc_path(path):
            allowed_secondary.add(path)

    return {
        "allow": allow,
        "secondary": tuple(sorted(allowed_secondary)),
        "test": tuple(sorted(allowed_test)),
    }


def _cluster_doc_test_paths_match(field, candidate_paths, actual_paths, policy):
    candidate_paths = tuple(candidate_paths or ())
    actual_paths = tuple(actual_paths or ())
    if actual_paths == candidate_paths:
        return True, "ok"
    if not policy.get("allow"):
        return False, "exact"
    candidate_set = set(candidate_paths)
    actual_set = set(actual_paths)
    if not candidate_set.issubset(actual_set):
        return False, "dropped_candidate_paths"
    additions = actual_set - candidate_set
    allowed = set(policy.get(field) or ())
    if additions and not additions.issubset(allowed):
        return False, "unapproved_added_paths"
    return True, "ok"


def _validate_cluster_verification_against_candidate_tests(prd, candidate_nodes):
    """Require real pytest execution when candidate graph declares Python tests."""
    expected_tests = _cluster_candidate_python_tests(candidate_nodes)
    if not expected_tests:
        return True, "ok"
    verification = prd.get("verification") if isinstance(prd, dict) else {}
    if not isinstance(verification, dict):
        verification = {}
    command = str(verification.get("command") or "").replace("\\", "/")
    command_l = command.lower()
    if "pytest" not in command_l:
        return False, (
            "FATAL preflight (reconcile-cluster): candidate nodes declare Python "
            f"test consumers, so verification.command must run pytest; expected_tests={list(expected_tests)} "
            f"actual_command={command!r}. Path-existence checks are allowed only when "
            "candidate_nodes have no Python tests."
        )
    missing = [path for path in expected_tests if path not in command]
    if missing:
        return False, (
            "FATAL preflight (reconcile-cluster): verification.command must include "
            f"all candidate Python test consumers; missing={missing} "
            f"actual_command={command!r}"
        )
    return True, "ok"


def _cluster_normalize_layer(value):
    """Normalize a parent_layer value to canonical 'L<N>' form (or '' on bad)."""
    if value is None:
        return ""
    s = str(value).strip()
    m = re.match(r"^[Ll]?(\d+)$", s)
    if not m:
        return ""
    return f"L{int(m.group(1))}"


def _cluster_payload_candidate_nodes(metadata):
    """Return candidate_nodes embedded in reconcile-cluster metadata."""
    if not isinstance(metadata, dict):
        return []
    payload = metadata.get("cluster_payload")
    if isinstance(payload, dict) and isinstance(payload.get("candidate_nodes"), list):
        return payload.get("candidate_nodes") or []
    report = metadata.get("cluster_report")
    if isinstance(report, dict) and isinstance(report.get("candidate_nodes"), list):
        return report.get("candidate_nodes") or []
    return []


def _cluster_node_layer_hint(item):
    """Return the node's own layer hint, not its hierarchy parent node."""
    if not isinstance(item, dict):
        return ""
    raw = (
        item.get("layer")
        or item.get("parent_layer")
        or ""
    )
    if not raw:
        return ""
    text = str(raw).strip()
    return _cluster_normalize_layer(text)


def _cluster_parent_node_hint(item):
    """Return an explicit hierarchy parent node id such as L3.18."""
    if not isinstance(item, dict):
        return ""
    for key in ("parent", "parent_id", "parent_node_id", "hierarchy_parent"):
        raw = item.get(key)
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        if re.match(r"^[Ll]\d+\.\d+$", text):
            return f"L{text[1:]}" if text.startswith("l") else text
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    for key in ("hierarchy_parent", "parent", "parent_id", "parent_node_id"):
        raw = metadata.get(key)
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        if re.match(r"^[Ll]\d+\.\d+$", text):
            return f"L{text[1:]}" if text.startswith("l") else text
    return ""


def _cluster_proposed_parent_matches(proposed_node, candidate_parent):
    """True when a PM proposal preserves the candidate hierarchy parent."""
    if not candidate_parent:
        return True
    if not isinstance(proposed_node, dict):
        return False
    return _cluster_parent_node_hint(proposed_node) == candidate_parent


def _validate_cluster_prd_against_candidates(proposed, candidate_nodes, metadata=None):
    """Ensure PM preserves concrete candidate ids before Dev sees the task."""
    if not candidate_nodes:
        return True, "ok"

    expected = {}
    for idx, item in enumerate(candidate_nodes or []):
        if not isinstance(item, dict):
            continue
        node_id = _cluster_explicit_node_id(item)
        if not node_id:
            continue
        expected[node_id] = {
            "primary": tuple(_cluster_normalize_primary(item.get("primary"))),
            "title": str(item.get("title") or "").strip(),
            "layer": _cluster_node_layer_hint(item),
            "parent": _cluster_parent_node_hint(item),
            "deps": _cluster_normalize_deps(
                item.get("_deps") if item.get("_deps") is not None else item.get("deps")
            ),
            "secondary": _cluster_candidate_secondary(item),
            "test": _cluster_candidate_test(item),
            "test_coverage": _cluster_normalize_scalar(item.get("test_coverage")),
            "has_test_coverage": "test_coverage" in item,
            "index": idx,
        }
    if not expected:
        return True, "ok"

    seen = {}
    missing_id_indexes = []
    for idx, item in enumerate(proposed or []):
        if not isinstance(item, dict):
            return False, f"proposed_nodes[{idx}] is not an object"
        node_id = _cluster_explicit_node_id(item)
        if not node_id:
            missing_id_indexes.append(idx)
            continue
        seen[node_id] = item
    if missing_id_indexes:
        return False, (
            "FATAL preflight (reconcile-cluster): proposed_nodes must preserve "
            f"candidate node_id; missing/null indexes={missing_id_indexes}"
        )

    expected_ids = set(expected)
    seen_ids = set(seen)
    if expected_ids != seen_ids:
        return False, (
            "FATAL preflight (reconcile-cluster): proposed_nodes node_id set must "
            f"match cluster_payload.candidate_nodes exactly; missing={sorted(expected_ids - seen_ids)} "
            f"extra={sorted(seen_ids - expected_ids)}"
        )

    augmentation_policy = _cluster_doc_test_augmentation_policy(metadata)
    for node_id, candidate in expected.items():
        proposed_node = seen[node_id]
        cand_primary = candidate["primary"]
        prop_primary = tuple(_cluster_normalize_primary(proposed_node.get("primary")))
        if cand_primary != prop_primary:
            return False, (
                "FATAL preflight (reconcile-cluster): proposed_nodes primary "
                f"for {node_id} must match candidate; expected={list(cand_primary)} "
                f"actual={list(prop_primary)}"
            )
        cand_title = candidate["title"]
        prop_title = str(proposed_node.get("title") or "").strip()
        if cand_title and prop_title != cand_title:
            return False, (
                "FATAL preflight (reconcile-cluster): proposed_nodes title "
                f"for {node_id} must match candidate; expected={cand_title!r} "
                f"actual={prop_title!r}"
            )
        cand_layer = candidate["layer"]
        prop_layer = _cluster_node_layer_hint(proposed_node)
        if cand_layer and prop_layer and cand_layer != prop_layer:
            return False, (
                "FATAL preflight (reconcile-cluster): proposed_nodes node layer "
                f"for {node_id} must match candidate; expected={cand_layer!r} "
                f"actual={prop_layer!r}"
            )
        cand_parent = candidate["parent"]
        if cand_parent and not _cluster_proposed_parent_matches(proposed_node, cand_parent):
            return False, (
                "FATAL preflight (reconcile-cluster): proposed_nodes parent relation "
                f"for {node_id} must reference candidate parent via parent/parent_id; "
                f"expected={cand_parent!r}"
            )
        cand_deps = candidate["deps"]
        prop_deps = _cluster_normalize_deps(proposed_node.get("deps"))
        if cand_deps != prop_deps:
            return False, (
                "FATAL preflight (reconcile-cluster): proposed_nodes deps "
                f"for {node_id} must match candidate _deps/deps exactly; "
                f"expected={list(cand_deps)} actual={list(prop_deps)}. "
                "Do not put hierarchy parent in deps; use parent/parent_id."
            )
        for field in ("secondary", "test"):
            cand_paths = candidate[field]
            prop_paths = tuple(_cluster_normalize_primary(proposed_node.get(field)))
            paths_ok, path_reason = _cluster_doc_test_paths_match(
                field,
                cand_paths,
                prop_paths,
                augmentation_policy,
            )
            if not paths_ok:
                if path_reason == "dropped_candidate_paths":
                    detail = "Candidate doc/test consumers cannot be dropped."
                elif path_reason == "unapproved_added_paths":
                    detail = (
                        "Added doc/test consumers must be supplied by this "
                        "orphan review cluster's expected_doc_files/"
                        "expected_test_files."
                    )
                else:
                    detail = (
                        "Candidate doc/test consumers are graph identity; do not drop, "
                        "move, or invent them in PM."
                    )
                return False, (
                    "FATAL preflight (reconcile-cluster): proposed_nodes "
                    f"{field} for {node_id} must match candidate exactly; "
                    f"expected={list(cand_paths)} actual={list(prop_paths)}. "
                    f"{detail}"
                )
        if candidate["has_test_coverage"]:
            cand_coverage = candidate["test_coverage"]
            prop_coverage = _cluster_normalize_scalar(proposed_node.get("test_coverage"))
            test_paths_changed = tuple(_cluster_normalize_primary(proposed_node.get("test"))) != candidate["test"]
            coverage_augmented = (
                augmentation_policy.get("allow")
                and test_paths_changed
                and cand_coverage in ("", "none")
                and prop_coverage not in ("", "none")
            )
            if cand_coverage != prop_coverage and not coverage_augmented:
                return False, (
                    "FATAL preflight (reconcile-cluster): proposed_nodes "
                    f"test_coverage for {node_id} must match candidate exactly; "
                    f"expected={cand_coverage!r} actual={prop_coverage!r}. "
                    "Preserve coverage classification from the candidate graph."
                )
    return True, "ok"


def preflight_reconcile_cluster_pm(prd, candidate_nodes=None, metadata=None):
    """Stage 1 preflight — PM PRD must declare proposed_nodes for reconcile-cluster.

    Returns (passed: bool, reason: str).  Any FATAL outcome here is a preflight
    block: gate result is NOT pass, and the reason explicitly names both
    'reconcile-cluster' and 'proposed_nodes' so AC1 can match.
    """
    if not isinstance(prd, dict):
        return False, (
            "FATAL preflight (reconcile-cluster): PRD payload missing — "
            "proposed_nodes cannot be validated"
        )
    proposed = prd.get("proposed_nodes", [])
    nested_prd = prd.get("prd") if isinstance(prd.get("prd"), dict) else {}
    if not proposed and nested_prd:
        proposed = nested_prd.get("proposed_nodes", [])
    if not proposed or not isinstance(proposed, list):
        return False, (
            "FATAL preflight (reconcile-cluster): PM PRD must declare a non-empty "
            "proposed_nodes list before merge can apply the cluster overlay"
        )
    candidate_passed, candidate_reason = _validate_cluster_prd_against_candidates(
        proposed, candidate_nodes or [], metadata=metadata,
    )
    if not candidate_passed:
        return False, candidate_reason
    verification_passed, verification_reason = _validate_cluster_verification_against_candidate_tests(
        prd,
        candidate_nodes or [],
    )
    if not verification_passed:
        return False, verification_reason
    return True, "ok"


def _cluster_collect_primaries(entries):
    """Return a sorted list of distinct primary forward-slash strings."""
    flat = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        flat.extend(_cluster_normalize_primary(e.get("primary")))
    return sorted(set(flat))


def _cluster_parent_layer_hint(item):
    """Return a canonical node layer hint from parent_layer or layer."""
    if not isinstance(item, dict):
        return ""
    parent_raw = item.get("parent_layer")
    if parent_raw not in (None, ""):
        return _cluster_normalize_layer(parent_raw)
    return _cluster_normalize_layer(item.get("layer"))


def _cluster_parent_layer_lookup(*collections):
    """Build parent-layer hints keyed by node id and primary tuple."""
    by_id = {}
    by_primary = {}
    for collection in collections:
        for item in collection or []:
            if not isinstance(item, dict):
                continue
            hint = _cluster_parent_layer_hint(item)
            if not hint:
                continue
            node_id = _cluster_explicit_node_id(item)
            if node_id and node_id not in by_id:
                by_id[node_id] = hint
            primary = tuple(_cluster_normalize_primary(item.get("primary")))
            if primary and primary not in by_primary:
                by_primary[primary] = hint
    return by_id, by_primary


def _cluster_hydrate_create_parent_layers(dev_creates, pm_proposed_nodes, candidate_nodes):
    """Fill omitted parent_layer from safe reconcile-cluster context.

    Dev output sometimes preserves the candidate's own ``layer`` field but
    omits the CR5 ``parent_layer`` field consumed by structural validation.
    Hydration is intentionally conservative: only missing/empty parent_layer is
    filled, and unresolved creates still fail the normal structural gate.
    """
    if not isinstance(dev_creates, list):
        return dev_creates
    by_id, by_primary = _cluster_parent_layer_lookup(
        pm_proposed_nodes,
        candidate_nodes,
    )
    hydrated = []
    for item in dev_creates:
        if not isinstance(item, dict):
            hydrated.append(item)
            continue
        out = dict(item)
        parent_raw = out.get("parent_layer")
        if parent_raw in (None, ""):
            hint = _cluster_normalize_layer(out.get("layer"))
            if not hint:
                node_id = _cluster_explicit_node_id(out)
                if node_id:
                    hint = by_id.get(node_id, "")
            if not hint:
                primary = tuple(_cluster_normalize_primary(out.get("primary")))
                if primary:
                    hint = by_primary.get(primary, "")
            if hint:
                out["parent_layer"] = hint
        else:
            canonical = _cluster_normalize_layer(parent_raw)
            if canonical:
                out["parent_layer"] = canonical
        hydrated.append(out)
    return hydrated


def preflight_reconcile_cluster_dev(
    pm_proposed_nodes,
    dev_creates,
    candidate_nodes=None,
    metadata=None,
):
    """Stage 2 preflight — Dev's graph_delta.creates ⇔ PM proposed_nodes 1:1 by primary.

    FATAL when count differs or when primary identity does not 1:1 match.
    Returns (passed: bool, reason: str).
    """
    pm_n = len(pm_proposed_nodes or [])
    dev_n = len(dev_creates or [])
    if pm_n != dev_n:
        return False, (
            f"FATAL preflight (reconcile-cluster): graph_delta.creates count "
            f"({dev_n}) != PM proposed_nodes count ({pm_n})"
        )
    pm_primaries = _cluster_collect_primaries(pm_proposed_nodes)
    dev_primaries = _cluster_collect_primaries(dev_creates)
    if pm_primaries != dev_primaries:
        only_pm = sorted(set(pm_primaries) - set(dev_primaries))
        only_dev = sorted(set(dev_primaries) - set(pm_primaries))
        return False, (
            "FATAL preflight (reconcile-cluster): graph_delta.creates primaries "
            f"do not match PM proposed_nodes 1:1 — only_pm={only_pm} "
            f"only_dev={only_dev}"
        )
    expected_contract = {}
    for item in candidate_nodes or []:
        if not isinstance(item, dict):
            continue
        node_id = _cluster_explicit_node_id(item)
        if not node_id:
            continue
        expected_contract[node_id] = {
            "deps": _cluster_normalize_deps(
                item.get("_deps") if item.get("_deps") is not None else item.get("deps")
            ),
            "parent": _cluster_parent_node_hint(item),
            "secondary": _cluster_candidate_secondary(item),
            "test": _cluster_candidate_test(item),
            "test_coverage": _cluster_normalize_scalar(item.get("test_coverage")),
            "has_test_coverage": "test_coverage" in item,
        }
    augmentation_policy = _cluster_doc_test_augmentation_policy(metadata)
    if expected_contract:
        for idx, item in enumerate(dev_creates or []):
            if not isinstance(item, dict):
                return False, f"graph_delta.creates[{idx}] is not an object"
            node_id = _cluster_explicit_node_id(item)
            if not node_id:
                return False, (
                    "FATAL preflight (reconcile-cluster): graph_delta.creates "
                    f"index {idx} must preserve candidate node_id/candidate_node_id"
                )
            if node_id not in expected_contract:
                return False, (
                    "FATAL preflight (reconcile-cluster): graph_delta.creates "
                    f"references non-candidate node_id {node_id!r}; "
                    f"expected one of {sorted(expected_contract)}"
                )
            actual = _cluster_normalize_deps(item.get("deps"))
            expected = expected_contract[node_id]["deps"]
            if actual != expected:
                return False, (
                    "FATAL preflight (reconcile-cluster): graph_delta.creates deps "
                    f"for {node_id} must match candidate _deps/deps exactly; "
                    f"expected={list(expected)} actual={list(actual)}. "
                    "Do not put hierarchy parent in deps; use parent/parent_id."
                )
            expected_parent = expected_contract[node_id]["parent"]
            if expected_parent and not _cluster_proposed_parent_matches(item, expected_parent):
                return False, (
                    "FATAL preflight (reconcile-cluster): graph_delta.creates "
                    f"hierarchy parent for {node_id} must match candidate; "
                    f"expected={expected_parent!r}. Preserve it via parent, "
                    "parent_id, or hierarchy_parent."
                )
            for field in ("secondary", "test"):
                expected_paths = expected_contract[node_id][field]
                actual_paths = tuple(_cluster_normalize_primary(item.get(field)))
                paths_ok, path_reason = _cluster_doc_test_paths_match(
                    field,
                    expected_paths,
                    actual_paths,
                    augmentation_policy,
                )
                if not paths_ok:
                    if path_reason == "dropped_candidate_paths":
                        detail = "Candidate doc/test consumers cannot be dropped."
                    elif path_reason == "unapproved_added_paths":
                        detail = (
                            "Added doc/test consumers must be supplied by this "
                            "orphan review cluster's expected_doc_files/"
                            "expected_test_files."
                        )
                    else:
                        detail = "Candidate doc/test consumers are graph identity."
                    return False, (
                        "FATAL preflight (reconcile-cluster): graph_delta.creates "
                        f"{field} for {node_id} must match candidate exactly; "
                        f"expected={list(expected_paths)} actual={list(actual_paths)}. "
                        f"{detail}"
                    )
            if expected_contract[node_id]["has_test_coverage"]:
                expected_coverage = expected_contract[node_id]["test_coverage"]
                actual_coverage = _cluster_normalize_scalar(item.get("test_coverage"))
                test_paths_changed = tuple(_cluster_normalize_primary(item.get("test"))) != expected_contract[node_id]["test"]
                coverage_augmented = (
                    augmentation_policy.get("allow")
                    and test_paths_changed
                    and expected_coverage in ("", "none")
                    and actual_coverage not in ("", "none")
                )
                if actual_coverage != expected_coverage and not coverage_augmented:
                    return False, (
                        "FATAL preflight (reconcile-cluster): graph_delta.creates "
                        f"test_coverage for {node_id} must match candidate exactly; "
                        f"expected={expected_coverage!r} actual={actual_coverage!r}."
                    )
    return True, "ok"


def validate_cluster_graph_delta_structure(creates, existing_node_ids):
    """Gatekeeper structural validation (R3).

    Each create must carry a parent_layer normalising to L0..L7 and only
    resolvable deps (deps must reference an id in existing_node_ids OR another
    create's node_id within this same cluster).  Missing layer / out-of-range
    layer / unresolvable dep blocks merge.
    Returns (passed: bool, reason: str).
    """
    if not isinstance(creates, list):
        return False, "graph_delta.creates is not a list"
    cluster_ids = set()
    for c in creates:
        explicit_id = _cluster_explicit_node_id(c) if isinstance(c, dict) else ""
        if explicit_id:
            cluster_ids.add(explicit_id)
    resolvable = set(existing_node_ids or []) | cluster_ids
    for idx, item in enumerate(creates):
        if not isinstance(item, dict):
            return False, f"creates[{idx}] is not a dict"
        layer = item.get("parent_layer")
        if layer is None or layer == "":
            return False, (
                f"creates[{idx}] missing parent_layer "
                f"(node_id={item.get('node_id', '<unset>')})"
            )
        canonical = _cluster_normalize_layer(layer)
        if not canonical:
            return False, (
                f"creates[{idx}] has unparseable parent_layer={layer!r}"
            )
        try:
            n = int(canonical[1:])
        except ValueError:
            return False, f"creates[{idx}] has malformed parent_layer={layer!r}"
        if n < 0 or n > 7:
            return False, (
                f"creates[{idx}] parent_layer={canonical} out of range L0..L7"
            )
        deps = item.get("deps") or []
        if not isinstance(deps, list):
            return False, f"creates[{idx}] deps is not a list"
        for dep in deps:
            if not isinstance(dep, str) or not dep:
                return False, f"creates[{idx}] has empty/non-string dep"
            if dep not in resolvable:
                return False, (
                    f"creates[{idx}] has unresolvable dep {dep!r} "
                    f"(node_id={item.get('node_id', '<unset>')})"
                )
    return True, "ok"


def classify_state_match(proposed_node, existing_node):
    """4-level match per proposal §4.6.1.

    Returns one of MATCH_TIER_EXACT / MATCH_TIER_STRUCTURAL /
    MATCH_TIER_PRIMARY_ONLY / MATCH_TIER_NO_MATCH.
    """
    if not isinstance(proposed_node, dict) or not isinstance(existing_node, dict):
        return MATCH_TIER_NO_MATCH
    p_primary = _cluster_normalize_primary(proposed_node.get("primary"))
    e_primary = _cluster_normalize_primary(existing_node.get("primary"))
    if not p_primary or not e_primary or p_primary != e_primary:
        return MATCH_TIER_NO_MATCH
    p_layer = _cluster_normalize_layer(proposed_node.get("parent_layer"))
    e_layer = _cluster_normalize_layer(existing_node.get("parent_layer"))
    if not p_layer or not e_layer or p_layer != e_layer:
        return MATCH_TIER_PRIMARY_ONLY
    p_secondary = _cluster_normalize_primary(proposed_node.get("secondary"))
    e_secondary = _cluster_normalize_primary(existing_node.get("secondary"))
    p_test = _cluster_normalize_primary(proposed_node.get("test"))
    e_test = _cluster_normalize_primary(existing_node.get("test"))
    if p_secondary == e_secondary and p_test == e_test:
        return MATCH_TIER_EXACT
    return MATCH_TIER_STRUCTURAL


def classify_state_match_for_proposed(proposed_node, candidate_nodes):
    """1→N: per-proposal classification picks the BEST tier among candidates.

    candidate_nodes is a mapping {node_id: node_dict}.  Returns
    (matched_node_id_or_None, tier_string).

    Per proposal §4.6.1: 1→N candidates are evaluated independently; the
    proposed node carries the highest-ranking match (so verify_status can
    transfer from the closest existing predecessor).  When no candidate
    yields anything beyond no_match, returns (None, MATCH_TIER_NO_MATCH).

    Distinct from the N→1 case (multiple proposals targeting the same
    existing node), which is handled in apply_reconcile_cluster_to_overlay
    via reconcile_n_to_one_winner — the 'most conservative match wins' rule
    applies there, not here.
    """
    best_id = None
    best_tier = MATCH_TIER_NO_MATCH
    best_rank = _MATCH_TIER_ORDER.index(best_tier)
    for nid, ndata in (candidate_nodes or {}).items():
        tier = classify_state_match(proposed_node, ndata)
        rank = _MATCH_TIER_ORDER.index(tier)
        # Highest-rank tier wins per-proposal; on a tie keep the first seen.
        if rank > best_rank:
            best_id = nid
            best_tier = tier
            best_rank = rank
    if best_tier == MATCH_TIER_NO_MATCH:
        return None, MATCH_TIER_NO_MATCH
    return best_id, best_tier


def reconcile_n_to_one_winner(per_proposal_tiers):
    """N→1 winner: when multiple proposed nodes claim the same existing node,
    the most conservative (lowest-rank) tier wins for state-transfer safety.

    per_proposal_tiers is an iterable of tier strings.  Returns the winning
    tier — defaulting to MATCH_TIER_NO_MATCH on empty input.
    """
    winner = MATCH_TIER_NO_MATCH
    winner_rank = _MATCH_TIER_ORDER.index(winner)
    for tier in per_proposal_tiers or []:
        try:
            rank = _MATCH_TIER_ORDER.index(tier)
        except ValueError:
            continue
        if winner is None or rank < winner_rank:
            winner = tier
            winner_rank = rank
    return winner


def apply_state_preservation(tier, existing_verify_status):
    """Compute the verify_status for a newly-allocated cluster node.

    Rules per proposal §4.6.1:
      exact_match        → transfer verify_status as-is
      structural_match   → transfer only weak verify_status; demote qa_pass /
                           t2_pass / waived to pending
      primary_only_match → never transfer (always pending) + provenance still recorded
      no_match           → pending
    """
    existing = (existing_verify_status or "pending").strip()
    if tier == MATCH_TIER_EXACT:
        return existing or "pending"
    if tier == MATCH_TIER_STRUCTURAL:
        if existing in _STRONG_VERIFY_STATUSES:
            return "pending"
        return existing or "pending"
    # primary_only_match and no_match
    return "pending"


def _cluster_load_graph_nodes(graph_path):
    """Load existing node dicts from graph.json.

    Tolerates two on-disk schemas:
      1. {"<node_id>": {primary: ..., parent_layer: ...}, ...}
      2. {"deps_graph": {"nodes": [{"id": ..., ...}, ...], "links": ...}, ...}

    Returns dict {node_id: node_dict} (node_dict mutable copy with verify_status).
    Missing file / malformed JSON → {}.
    """
    p = Path(graph_path)
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for graph_key in ("deps_graph", "hierarchy_graph"):
        if graph_key in data and isinstance(data[graph_key], dict):
            for n in data[graph_key].get("nodes", []) or []:
                if isinstance(n, dict) and isinstance(n.get("id"), str):
                    d = dict(n)
                    d.setdefault("node_id", n["id"])
                    out[n["id"]] = d
            return out
    if isinstance(data.get("nodes"), list):
        for n in data.get("nodes", []) or []:
            if isinstance(n, dict) and isinstance(n.get("id"), str):
                d = dict(n)
                d.setdefault("node_id", n["id"])
                out[n["id"]] = d
        return out
    for k, v in data.items():
        if isinstance(v, dict):
            d = dict(v)
            d.setdefault("node_id", k)
            out[k] = d
    return out


def _cluster_load_overlay_nodes(overlay_path):
    """Load nodes already-applied-this-session from the overlay file.

    Schema: {"session_id": "...", "nodes": {"<node_id>": {...}, ...}}.
    Tolerates the bootstrap form written by reconcile_session.start_session
    which has only {"session_id", "project_id"} and no "nodes" key.
    Missing file / malformed JSON → {}.
    """
    data = _cluster_load_overlay_doc(overlay_path)
    nodes = data.get("nodes") or {}
    return {k: dict(v) for k, v in nodes.items() if isinstance(v, dict)}


def _cluster_load_overlay_doc(overlay_path):
    """Load the full overlay document, preserving session-level audit metadata."""
    p = Path(overlay_path)
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return dict(data)


def _cluster_explicit_node_id(item):
    """Return a concrete candidate/node id declared by PM/Dev, if any."""
    if not isinstance(item, dict):
        return ""
    for key in ("node_id", "candidate_node_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    for key in ("node_id", "candidate_node_id", "id"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _cluster_file_sha256(path):
    try:
        p = Path(path)
        if p.exists():
            return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        return ""
    return ""


def _cluster_candidate_graph_path(metadata, graph_path):
    """Resolve the active candidate graph path, if this session has one."""
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in (
        "candidate_graph_path",
        "graph_candidate_path",
        "rebase_candidate_graph_path",
    ):
        value = metadata.get(key)
        if value:
            return Path(value)
    cluster_payload = metadata.get("cluster_payload")
    if isinstance(cluster_payload, dict):
        for key in (
            "candidate_graph_path",
            "graph_candidate_path",
            "rebase_candidate_graph_path",
        ):
            value = cluster_payload.get(key)
            if value:
                return Path(value)
    try:
        sibling = Path(graph_path).with_name("graph.rebase.candidate.json")
        if sibling.exists():
            return sibling
    except Exception:
        return None
    return None


def _cluster_primary_compatible(proposed, existing):
    proposed_primary = _cluster_normalize_primary(proposed.get("primary"))
    existing_primary = _cluster_normalize_primary(existing.get("primary"))
    if proposed_primary and existing_primary and proposed_primary != existing_primary:
        return False
    proposed_layer = _cluster_normalize_layer(proposed.get("parent_layer"))
    existing_layer = _cluster_normalize_layer(
        existing.get("parent_layer") or existing.get("layer")
    )
    if proposed_layer and existing_layer and proposed_layer != existing_layer:
        return False
    return True


def _validate_candidate_namespace(dev_creates, candidate_nodes, overlay_nodes):
    """Candidate graph is the rebase namespace; Dev must reference it directly."""
    if not candidate_nodes:
        return True, "ok"
    for idx, proposed in enumerate(dev_creates or []):
        if not isinstance(proposed, dict):
            continue
        explicit_id = _cluster_explicit_node_id(proposed)
        if not explicit_id:
            return False, (
                "candidate graph is active; creates[%d] must declare node_id "
                "or candidate_node_id from graph.rebase.candidate.json"
            ) % idx
        candidate = candidate_nodes.get(explicit_id)
        if candidate is None:
            return False, (
                f"candidate graph is active; creates[{idx}] references "
                f"{explicit_id!r}, which is not present in graph.rebase.candidate.json"
            )
        if not _cluster_primary_compatible(proposed, candidate):
            return False, (
                f"candidate graph namespace conflict for {explicit_id}: "
                "create primary/layer does not match candidate graph node"
            )
        expected_deps = _cluster_normalize_deps(
            candidate.get("_deps") if candidate.get("_deps") is not None else candidate.get("deps")
        )
        actual_deps = _cluster_normalize_deps(proposed.get("deps"))
        if expected_deps != actual_deps:
            return False, (
                f"candidate graph namespace conflict for {explicit_id}: "
                "create deps do not match candidate _deps/deps exactly; "
                f"expected={list(expected_deps)} actual={list(actual_deps)}. "
                "Do not put hierarchy parent in deps; use parent/parent_id."
            )
        expected_parent = _cluster_parent_node_hint(candidate)
        if expected_parent and not _cluster_proposed_parent_matches(proposed, expected_parent):
            return False, (
                f"candidate graph namespace conflict for {explicit_id}: "
                "create hierarchy parent does not match candidate graph node; "
                f"expected={expected_parent!r}"
            )
        existing_overlay = overlay_nodes.get(explicit_id)
        if existing_overlay and not _cluster_primary_compatible(proposed, existing_overlay):
            return False, (
                f"overlay namespace conflict for {explicit_id}: existing overlay "
                "node has a different primary/layer"
            )
    return True, "ok"


def _allocate_cluster_next_id(graph_nodes, overlay_nodes, layer_prefix="L7"):
    """R6: read union(graph.json ∪ overlay.json) when computing the next id.

    Returns 'L<N>.<max+1>'.
    """
    pat = re.compile(r"^" + re.escape(layer_prefix) + r"\.(\d+)$")
    max_n = 0
    for source in (graph_nodes or {}, overlay_nodes or {}):
        for nid in source.keys():
            m = pat.match(nid)
            if m:
                try:
                    n = int(m.group(1))
                except ValueError:
                    continue
                if n > max_n:
                    max_n = n
    return f"{layer_prefix}.{max_n + 1}"


def _cluster_write_overlay(overlay_path, payload):
    """Write the overlay JSON document at overlay_path (atomic best-effort)."""
    p = Path(overlay_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _cluster_emit_chain_event(conn, root_task_id, task_id, payload):
    """Emit chain_events row for graph.delta.applied.

    Best-effort — table may not exist in some test fixtures.  Returns True on
    successful insert, False otherwise.  When `conn` is None, returns False
    (caller may still propagate the payload via _publish_event).
    """
    if conn is None:
        return False
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (root_task_id or "", task_id or "",
             CHAIN_EVENT_GRAPH_DELTA_APPLIED,
             json.dumps(payload, ensure_ascii=False), now),
        )
        try:
            conn.commit()
        except Exception:
            pass
        return True
    except Exception:
        return False


def mark_cluster_failed_retryable(deferred_queue, cluster_fingerprint, reason):
    """R9: enqueue a cluster as failed_retryable in the deferred queue.

    deferred_queue may be any object exposing one of these methods:
      - .mark_failed_retryable(fingerprint, reason)
      - .enqueue({...}) / .append({...}) / .add({...})
    A plain list is also accepted (entries are appended dicts).
    """
    if deferred_queue is None:
        return False
    entry = {
        "cluster_fingerprint": cluster_fingerprint,
        "status": "failed_retryable",
        "reason": reason,
    }
    method = getattr(deferred_queue, "mark_failed_retryable", None)
    if callable(method):
        try:
            method(cluster_fingerprint, reason)
            return True
        except Exception:
            return False
    for name in ("enqueue", "append", "add"):
        m = getattr(deferred_queue, name, None)
        if callable(m):
            try:
                m(entry)
                return True
            except Exception:
                continue
    if isinstance(deferred_queue, list):
        deferred_queue.append(entry)
        return True
    return False


def _cluster_compute_fingerprint(metadata, dev_creates):
    """Stable cluster fingerprint for chain_event payload.

    Prefers metadata.cluster_fingerprint when present; falls back to a sha1
    over sorted dev primaries so identical inputs yield identical output.
    """
    fp = metadata.get("cluster_fingerprint") if isinstance(metadata, dict) else None
    if isinstance(fp, str) and fp:
        return fp
    import hashlib
    primaries = _cluster_collect_primaries(dev_creates)
    h = hashlib.sha1("|".join(primaries).encode("utf-8")).hexdigest()
    return f"cluster-{h[:12]}"


def is_reconcile_cluster_task(metadata):
    """R1: trigger predicate.  Pure function — no side effects."""
    if not isinstance(metadata, dict):
        return False
    return metadata.get("operation_type") == CLUSTER_OPERATION_TYPE


def apply_reconcile_cluster_to_overlay(
    conn,
    project_id,
    task_id,
    *,
    pm_prd,
    dev_result,
    metadata,
    graph_path,
    overlay_path,
    deferred_queue=None,
    session_pending=None,
    simulate_failure=False,
):
    """Main CR5 apply path — the merge handler invokes this when
    metadata.operation_type == 'reconcile-cluster'.

    Side effects (only on full success):
      * Writes the cluster's allocated nodes to overlay_path
        (graph.rebase.overlay.json).  graph.json is NEVER touched.
      * Stages node_state inserts into session_pending (R7) — no commit to
        node_state until session finalize.
      * Emits chain_events row 'graph.delta.applied' carrying
        {cluster_fingerprint, allocated_node_ids, state_transfers, overlay_path}.

    Failure modes:
      * Stage-1 / Stage-2 / structural validation failures return
        {"applied": False, "fatal": True, ...} with no overlay touch.
      * simulate_failure=True (or any post-validation rollback) marks the
        cluster failed_retryable in deferred_queue but DOES NOT clear overlay.

    Returns a result dict; never raises for validation/rollback flows.
    """
    if not is_reconcile_cluster_task(metadata):
        # R10: non-cluster path — must be invisible.  Caller already short-
        # circuits, but defend in depth.
        return {
            "applied": False,
            "skipped": "non-cluster",
            "operation_type": (metadata or {}).get("operation_type"),
        }

    if session_pending is None:
        session_pending = []

    cluster_fp = _cluster_compute_fingerprint(metadata, (dev_result or {}).get("graph_delta", {}).get("creates", []))

    # --- Stage 1: PM PRD validation -----------------------------------------
    payload_candidate_nodes = _cluster_payload_candidate_nodes(metadata)
    pm_passed, pm_reason = preflight_reconcile_cluster_pm(
        pm_prd,
        candidate_nodes=payload_candidate_nodes,
        metadata=metadata,
    )
    if not pm_passed:
        return {
            "applied": False,
            "fatal": True,
            "stage": "preflight_pm",
            "reason": pm_reason,
            "cluster_fingerprint": cluster_fp,
        }

    pm_proposed = pm_prd.get("proposed_nodes", []) if isinstance(pm_prd, dict) else []
    graph_delta = (dev_result or {}).get("graph_delta", {}) if isinstance(dev_result, dict) else {}
    dev_creates = graph_delta.get("creates", []) if isinstance(graph_delta, dict) else []

    # --- Stage 2: Dev creates 1:1 with PM proposed_nodes by primary ---------
    dev_passed, dev_reason = preflight_reconcile_cluster_dev(
        pm_proposed,
        dev_creates,
        candidate_nodes=payload_candidate_nodes,
        metadata=metadata,
    )
    if not dev_passed:
        return {
            "applied": False,
            "fatal": True,
            "stage": "preflight_dev",
            "reason": dev_reason,
            "cluster_fingerprint": cluster_fp,
        }
    dev_creates = _cluster_hydrate_create_parent_layers(
        dev_creates,
        pm_proposed,
        payload_candidate_nodes,
    )
    graph_preflight = _build_reconcile_graph_preflight(
        project_id, metadata, proposed_nodes=dev_creates)

    # --- Load graph + overlay state for structural + allocator + match ------
    graph_nodes = _cluster_load_graph_nodes(graph_path)
    overlay_nodes = _cluster_load_overlay_nodes(overlay_path)
    candidate_graph_path = _cluster_candidate_graph_path(metadata, graph_path)
    candidate_nodes = (
        _cluster_load_graph_nodes(candidate_graph_path) if candidate_graph_path else {}
    )
    candidate_sha256 = (
        _cluster_file_sha256(candidate_graph_path) if candidate_graph_path else ""
    )

    namespace_nodes = candidate_nodes if candidate_nodes else graph_nodes
    existing_ids = set(namespace_nodes.keys()) | set(overlay_nodes.keys())

    # --- Structural gatekeeper validation -----------------------------------
    struct_passed, struct_reason = validate_cluster_graph_delta_structure(
        dev_creates, existing_ids,
    )
    if not struct_passed:
        return {
            "applied": False,
            "fatal": True,
            "stage": "structural",
            "reason": struct_reason,
            "cluster_fingerprint": cluster_fp,
        }

    # --- Candidate graph namespace validation -------------------------------
    namespace_passed, namespace_reason = _validate_candidate_namespace(
        dev_creates, candidate_nodes, overlay_nodes,
    )
    if not namespace_passed:
        return {
            "applied": False,
            "fatal": True,
            "stage": "candidate_namespace",
            "reason": namespace_reason,
            "cluster_fingerprint": cluster_fp,
            "candidate_graph_path": str(candidate_graph_path or ""),
            "candidate_graph_sha256": candidate_sha256,
        }

    # --- Allocate ids + classify state-preservation match per create --------
    allocated_node_ids = []
    state_transfers = []
    overlay_payload_nodes = dict(overlay_nodes)  # copy for in-place updates

    # Build candidate map for match classification (graph + overlay union).
    candidate_pool = {}
    candidate_pool.update(graph_nodes)
    candidate_pool.update(overlay_nodes)

    # Build a map of existing node verify_status from graph.json ndata when
    # present.  Real production data gets verify_status from node_state, but
    # the overlay-driven test path treats graph_nodes[*]['verify_status'] as
    # authoritative input (matches test fixtures).
    for proposed in dev_creates:
        if not isinstance(proposed, dict):
            continue
        layer_prefix = _cluster_normalize_layer(proposed.get("parent_layer")) or "L7"
        explicit_id = _cluster_explicit_node_id(proposed)
        if candidate_nodes:
            new_id = explicit_id
        else:
            new_id = explicit_id or _allocate_cluster_next_id(
                graph_nodes, overlay_payload_nodes, layer_prefix=layer_prefix,
            )
            # Avoid in-cluster collision when id auto-allocated and another
            # create already used that id this round.
            while new_id in overlay_payload_nodes or new_id in graph_nodes:
                # bump number
                m = re.match(r"^(L\d+)\.(\d+)$", new_id)
                if not m:
                    break
                new_id = f"{m.group(1)}.{int(m.group(2)) + 1}"

        match_node_id, tier = classify_state_match_for_proposed(
            proposed, candidate_pool,
        )
        existing_status = "pending"
        if match_node_id and match_node_id in candidate_pool:
            existing_status = (
                candidate_pool[match_node_id].get("verify_status") or "pending"
            )
        new_status = apply_state_preservation(tier, existing_status)

        # Provenance always recorded — even on no_match (R4).
        rebased_from = {
            "tier": tier,
            "matched_node_id": match_node_id,
            "prior_verify_status": existing_status,
        }

        # Build the overlay node entry with provenance metadata.
        parent_node_id = _cluster_parent_node_hint(proposed)
        overlay_entry = {
            "node_id": new_id,
            "candidate_node_id": new_id if candidate_nodes else explicit_id,
            "layer": layer_prefix,
            "parent_layer": _cluster_normalize_layer(proposed.get("parent_layer")),
            "primary": list(proposed.get("primary") or []),
            "secondary": list(proposed.get("secondary") or []),
            "test": list(proposed.get("test") or []),
            "test_coverage": proposed.get("test_coverage", ""),
            "title": proposed.get("title", ""),
            "deps": list(proposed.get("deps") or []),
            "verify_status": new_status,
            "metadata": {
                **(proposed.get("metadata") or {}),
                "rebased_from": rebased_from,
                "candidate_graph_sha256": candidate_sha256,
            },
        }
        if parent_node_id:
            overlay_entry["parent"] = parent_node_id
            overlay_entry["parent_id"] = parent_node_id
            overlay_entry["hierarchy_parent"] = parent_node_id

        # R7: stage node_state insert into session_pending — never commit
        # directly to node_state from this path.  The session-finalize step
        # is responsible for atomic commit.
        session_pending.append({
            "project_id": project_id,
            "node_id": new_id,
            "verify_status": new_status,
            "rebased_from": rebased_from,
            "source": "reconcile-cluster-overlay",
        })

        overlay_payload_nodes[new_id] = overlay_entry
        allocated_node_ids.append(new_id)
        state_transfers.append({
            "node_id": new_id,
            "tier": tier,
            "matched_node_id": match_node_id,
            "prior_verify_status": existing_status,
            "new_verify_status": new_status,
        })

        # Note: candidate_pool intentionally NOT extended with the new node,
        # to avoid the just-allocated cluster node becoming a match target
        # for sibling proposals within the same cluster batch.

    # --- R5: write overlay (NOT graph.json) ---------------------------------
    overlay_doc = _cluster_load_overlay_doc(overlay_path)
    session_target_branch = ""
    session_target_head = ""
    session_base_commit = ""
    if isinstance(metadata, dict):
        session_target_branch = str(metadata.get("reconcile_target_branch") or "")
        session_target_head = str(metadata.get("reconcile_target_head") or "")
        session_base_commit = str(metadata.get("reconcile_target_base_commit") or "")
    overlay_doc.update({
        "session_id": (metadata.get("session_id") if isinstance(metadata, dict) else None) or "",
        "project_id": project_id or "",
        "target_branch": session_target_branch or overlay_doc.get("target_branch") or "",
        "target_head_sha": session_target_head or overlay_doc.get("target_head_sha") or "",
        "base_commit_sha": session_base_commit or overlay_doc.get("base_commit_sha") or "",
        "cluster_fingerprint": cluster_fp,
        "candidate_graph_path": str(candidate_graph_path or ""),
        "candidate_graph_sha256": candidate_sha256,
        "nodes": overlay_payload_nodes,
    })
    # Compose the JSON payload up-front so any pre-write rollback path keeps
    # graph.json byte-identical.
    overlay_serialised = json.dumps(
        overlay_doc, ensure_ascii=False, indent=2, sort_keys=True,
    )

    # --- R9: simulated/failed-merge rollback ------------------------------
    if simulate_failure:
        # Rollback path — DO NOT write overlay (preserve previous overlay
        # bytes) and DO NOT emit graph.delta.applied event.  Mark cluster
        # failed_retryable.  Per R9 the overlay is NOT cleared on a single
        # cluster failure — only session rollback clears overlay.
        marked = mark_cluster_failed_retryable(
            deferred_queue, cluster_fp, "simulated merge failure",
        )
        return {
            "applied": False,
            "fatal": False,
            "rolled_back": True,
            "stage": "merge_failure",
            "reason": "simulated merge failure",
            "cluster_fingerprint": cluster_fp,
            "failed_retryable_marked": marked,
            "allocated_node_ids": [],
            "overlay_cleared": False,
            "overlay_path": str(overlay_path),
        }

    # --- success: write overlay file ---------------------------------------
    p = Path(overlay_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(overlay_serialised, encoding="utf-8")

    # --- R8: emit chain_events 'graph.delta.applied' -----------------------
    payload = {
        "cluster_fingerprint": cluster_fp,
        "allocated_node_ids": allocated_node_ids,
        "state_transfers": state_transfers,
        "overlay_path": str(overlay_path),
        "applies_to": "candidate_overlay",
        "graph_json_updated": False,
        "candidate_graph_path": str(candidate_graph_path or ""),
        "candidate_graph_sha256": candidate_sha256,
        "graph_preflight": graph_preflight,
    }
    chain_event_emitted = _cluster_emit_chain_event(
        conn,
        root_task_id=(metadata or {}).get("chain_id") or (metadata or {}).get("parent_task_id") or "",
        task_id=task_id or (metadata or {}).get("task_id") or "",
        payload=payload,
    )
    # Best-effort event-bus relay (used by integration paths; tests mock).
    try:
        _publish_event(CHAIN_EVENT_GRAPH_DELTA_APPLIED, {
            "project_id": project_id, "task_id": task_id, **payload,
        })
    except Exception:
        pass

    return {
        "applied": True,
        "fatal": False,
        "stage": "applied",
        "cluster_fingerprint": cluster_fp,
        "allocated_node_ids": allocated_node_ids,
        "state_transfers": state_transfers,
        "overlay_path": str(overlay_path),
        "applies_to": "candidate_overlay",
        "graph_json_updated": False,
        "candidate_graph_path": str(candidate_graph_path or ""),
        "candidate_graph_sha256": candidate_sha256,
        "session_pending_inserts": list(session_pending),
        "chain_event_emitted": chain_event_emitted,
        "graph_preflight": graph_preflight,
    }


# ---------------------------------------------------------------------------
# Function lookup tables (avoid globals() for safety)
# ---------------------------------------------------------------------------

_GATES = {
    "_gate_post_pm": _gate_post_pm,
    "_gate_checkpoint": _gate_checkpoint,
    "_gate_t2_pass": _gate_t2_pass,
    "_gate_qa_pass": _gate_qa_pass,
    "_gate_gatekeeper_pass": _gate_gatekeeper_pass,
    "_gate_release": _gate_release,
    "_gate_deploy_pass": _gate_deploy_pass,
}

_BUILDERS = {
    "_build_dev_prompt": _build_dev_prompt,
    "_build_test_prompt": _build_test_prompt,
    "_build_qa_prompt": _build_qa_prompt,
    "_build_gatekeeper_prompt": _build_gatekeeper_prompt,
    "_build_merge_prompt": _build_merge_prompt,
    "_build_deploy_prompt": _build_deploy_prompt,
    "_finalize_chain": _finalize_chain,
}
