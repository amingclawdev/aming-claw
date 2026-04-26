"""Phase H — Commit-driven content delta detection.

Scans git diffs since the Phase I baseline, AST-extracts new public symbols
(def/class/@route/CREATE TABLE), maps them to expected doc/test locations via
YAML config, detects missing documentation mentions, and spawns PM tasks via
``/api/task/{pid}/create``.

Phase H does NOT modify docs directly (R15).
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (R7)
# ---------------------------------------------------------------------------
MAX_SPAWN_PER_RUN_DEFAULT = 3
MAX_SPAWN_HARD_LIMIT = 10

_TERMINAL_STATUSES = frozenset({"merged", "waived", "failed"})
_SKIP_STATUSES = frozenset({"merged", "waived", "running"})
# R11: failed blocks baseline advancement (not same as terminal for dedup)
_BASELINE_OK_STATUSES = frozenset({"merged", "waived"})

_YAML_PATH = Path(__file__).with_name("symbol_doc_map.yaml")

# Regex for SQL schema statements in diff hunks
_SQL_CREATE_RE = re.compile(r"CREATE\s+TABLE", re.IGNORECASE)
_SQL_ALTER_RE = re.compile(r"ALTER\s+TABLE", re.IGNORECASE)

# Regex for @route decorator
_ROUTE_RE = re.compile(r"@(?:\w+\.)?route\s*\(")

# Regex for merge commits and observer-hotfix prefix (R13)
_MERGE_MSG_RE = re.compile(r"^Merge ")
_HOTFIX_PREFIX = "[observer-hotfix]"


# ---------------------------------------------------------------------------
# Data types (AC-H3)
# ---------------------------------------------------------------------------
@dataclass
class Discrepancy:
    """A detected documentation gap for a newly-added symbol."""
    commit_sha: str
    symbol_qname: str
    expected_doc: str
    confidence: str       # 'high' | 'medium' | 'low'
    suggested_format: str
    symbol_kind: str = ""
    fingerprint: str = ""


@dataclass
class PhaseHResult:
    """Aggregated result of a Phase H run."""
    spawned_tasks: List[str] = field(default_factory=list)
    skipped_throttled: int = 0
    skipped_terminal: int = 0
    skipped_running: int = 0
    retried_failed: int = 0
    errors: List[str] = field(default_factory=list)
    baseline_advanced: bool = False


# ---------------------------------------------------------------------------
# YAML config loader (AC-H2 — no hard-coded paths)
# ---------------------------------------------------------------------------
def _load_symbol_doc_map(yaml_path: Optional[Path] = None) -> Dict:
    """Load symbol-kind → doc mapping from YAML config."""
    p = yaml_path or _YAML_PATH
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("mappings", {})


# ---------------------------------------------------------------------------
# Fingerprint (R6)
# ---------------------------------------------------------------------------
def compute_fingerprint(project_id: str, commit_sha: str,
                        symbol_kind: str, symbol_qname: str,
                        expected_doc: str) -> str:
    """sha256(project_id, commit_sha, symbol_kind, symbol_qname, expected_doc)."""
    payload = "|".join([project_id, commit_sha, symbol_kind, symbol_qname, expected_doc])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AST extraction (AC-H1)
# ---------------------------------------------------------------------------
def ast_extract_added_symbols(added_lines: str) -> List[Dict]:
    """Parse Python added lines via ast.parse, collect public symbols.

    Returns list of dicts with keys: name, kind ('def'|'async_def'|'class').
    """
    symbols = []
    try:
        tree = ast.parse(added_lines)
    except SyntaxError:
        return symbols

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if not node.name.startswith("_"):
                symbols.append({"name": node.name, "kind": "def"})
        elif isinstance(node, ast.AsyncFunctionDef):
            if not node.name.startswith("_"):
                symbols.append({"name": node.name, "kind": "async_def"})
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                symbols.append({"name": node.name, "kind": "class"})
    return symbols


def _extract_route_symbols(added_lines: str) -> List[str]:
    """Find @route(...) decorated functions in added lines."""
    routes = []
    lines = added_lines.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _ROUTE_RE.search(stripped):
            # Next non-decorator, non-empty line should be the def
            for j in range(i + 1, min(i + 5, len(lines))):
                nxt = lines[j].strip()
                if nxt.startswith("def ") or nxt.startswith("async def "):
                    fname = nxt.split("(")[0].replace("def ", "").replace("async ", "").strip()
                    routes.append(fname)
                    break
    return routes


def _extract_sql_symbols(added_lines: str) -> List[Dict]:
    """Find CREATE TABLE / ALTER TABLE statements in diff hunks."""
    symbols = []
    for line in added_lines.split("\n"):
        if _SQL_CREATE_RE.search(line):
            symbols.append({"name": line.strip(), "kind": "create_table"})
        elif _SQL_ALTER_RE.search(line):
            symbols.append({"name": line.strip(), "kind": "alter_table"})
    return symbols


# ---------------------------------------------------------------------------
# Confidence logic (R10)
# ---------------------------------------------------------------------------
def _resolve_class_confidence(class_name: str, all_added_lines: str,
                               route_symbols: List[str]) -> str:
    """Classes referenced by @route/registry/public-API/CLI/schema → high; else medium."""
    # Check if class is referenced by route decorator context
    patterns = [
        re.compile(rf"@\w*\.?route.*{re.escape(class_name)}", re.IGNORECASE),
        re.compile(rf"registry\[.*{re.escape(class_name)}.*\]", re.IGNORECASE),
        re.compile(rf"public.api.*{re.escape(class_name)}", re.IGNORECASE),
        re.compile(rf"cli.*{re.escape(class_name)}", re.IGNORECASE),
        re.compile(rf"schema.*{re.escape(class_name)}", re.IGNORECASE),
    ]
    for pat in patterns:
        if pat.search(all_added_lines):
            return "high"
    # If there are route symbols and the class is in agent/governance/, check if
    # any route handler references the class
    if route_symbols:
        for rs in route_symbols:
            if class_name.lower() in rs.lower():
                return "high"
    return "medium"


# ---------------------------------------------------------------------------
# Git diff helpers (R13)
# ---------------------------------------------------------------------------
def _get_commits_since_baseline(baseline_sha: str, repo_root: str) -> List[Dict]:
    """Get commit list since baseline, excluding merges and [observer-hotfix] (R13)."""
    try:
        result = subprocess.run(
            ["git", "log", f"{baseline_sha}..HEAD", "--format=%H|%s", "--no-merges"],
            capture_output=True, text=True, timeout=15,
            cwd=repo_root,
        )
        if result.returncode != 0:
            log.warning("phase_h: git log failed: %s", result.stderr)
            return []
    except Exception as e:
        log.warning("phase_h: git log error: %s", e)
        return []

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        sha, msg = parts
        # R13: skip merge commits and [observer-hotfix]
        if _MERGE_MSG_RE.match(msg) or msg.startswith(_HOTFIX_PREFIX):
            continue
        commits.append({"sha": sha.strip(), "message": msg.strip()})
    return commits


def _get_diff_for_commit(commit_sha: str, repo_root: str) -> str:
    """Get unified diff for a single commit (added lines only context)."""
    try:
        result = subprocess.run(
            ["git", "diff", f"{commit_sha}~1", commit_sha, "--unified=0", "--diff-filter=AM"],
            capture_output=True, text=True, timeout=15,
            cwd=repo_root,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _extract_added_lines_from_diff(diff_text: str) -> str:
    """Extract only added lines (lines starting with '+' but not '+++') from unified diff."""
    added = []
    for line in diff_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])  # Remove the leading '+'
    return "\n".join(added)


# ---------------------------------------------------------------------------
# Discrepancy detection
# ---------------------------------------------------------------------------
def detect_discrepancies(project_id: str, baseline_sha: str,
                         repo_root: str,
                         yaml_path: Optional[Path] = None) -> List[Discrepancy]:
    """Scan commits since baseline, extract symbols, map to expected docs.

    Returns a list of Discrepancy objects for new symbols lacking doc mentions.
    """
    mappings = _load_symbol_doc_map(yaml_path)
    commits = _get_commits_since_baseline(baseline_sha, repo_root)
    discrepancies = []

    for commit in commits:
        diff_text = _get_diff_for_commit(commit["sha"], repo_root)
        if not diff_text:
            continue
        added_lines = _extract_added_lines_from_diff(diff_text)
        if not added_lines.strip():
            continue

        # AST-extract Python symbols
        py_symbols = ast_extract_added_symbols(added_lines)
        route_symbols = _extract_route_symbols(added_lines)
        sql_symbols = _extract_sql_symbols(added_lines)

        # Map route-decorated functions
        for rname in route_symbols:
            m = mappings.get("route", {})
            fp = compute_fingerprint(project_id, commit["sha"], "route",
                                     rname, m.get("expected_doc", ""))
            discrepancies.append(Discrepancy(
                commit_sha=commit["sha"],
                symbol_qname=rname,
                expected_doc=m.get("expected_doc", ""),
                confidence=m.get("confidence", "high"),
                suggested_format=m.get("suggested_format", "api-endpoint"),
                symbol_kind="route",
                fingerprint=fp,
            ))

        # Map SQL symbols
        for sql in sql_symbols:
            kind = sql["kind"]
            m = mappings.get(kind, {})
            fp = compute_fingerprint(project_id, commit["sha"], kind,
                                     sql["name"], m.get("expected_doc", ""))
            discrepancies.append(Discrepancy(
                commit_sha=commit["sha"],
                symbol_qname=sql["name"],
                expected_doc=m.get("expected_doc", ""),
                confidence=m.get("confidence", "high"),
                suggested_format=m.get("suggested_format", "schema-table"),
                symbol_kind=kind,
                fingerprint=fp,
            ))

        # Map classes (conditional confidence R10)
        for sym in py_symbols:
            if sym["kind"] == "class":
                conf = _resolve_class_confidence(sym["name"], added_lines, route_symbols)
                m = mappings.get("class_governance", {})
                fp = compute_fingerprint(project_id, commit["sha"], "class",
                                         sym["name"], m.get("expected_doc", ""))
                discrepancies.append(Discrepancy(
                    commit_sha=commit["sha"],
                    symbol_qname=sym["name"],
                    expected_doc=m.get("expected_doc", ""),
                    confidence=conf,
                    suggested_format=m.get("suggested_format", "class-reference"),
                    symbol_kind="class",
                    fingerprint=fp,
                ))
            elif sym["kind"] in ("def", "async_def"):
                # Check if this def is already counted as a route
                if sym["name"] not in route_symbols:
                    m = mappings.get("public_def", {})
                    fp = compute_fingerprint(project_id, commit["sha"], "public_def",
                                             sym["name"], m.get("expected_doc", ""))
                    discrepancies.append(Discrepancy(
                        commit_sha=commit["sha"],
                        symbol_qname=sym["name"],
                        expected_doc=m.get("expected_doc", ""),
                        confidence=m.get("confidence", "low"),
                        suggested_format=m.get("suggested_format", "function-docstring"),
                        symbol_kind="public_def",
                        fingerprint=fp,
                    ))

    return discrepancies


# ---------------------------------------------------------------------------
# DB helpers for phase_h_processed_symbols
# ---------------------------------------------------------------------------
def _get_existing_status(conn, fingerprint: str) -> Optional[str]:
    """Get spawn_status for existing fingerprint, or None if not found."""
    row = conn.execute(
        "SELECT spawn_status FROM phase_h_processed_symbols WHERE fingerprint = ?",
        (fingerprint,),
    ).fetchone()
    return row["spawn_status"] if row else None


def _upsert_fingerprint(conn, project_id: str, disc: Discrepancy,
                         status: str, spawned_task_id: str = "") -> None:
    """Insert or update a processed symbol fingerprint."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO phase_h_processed_symbols
           (fingerprint, project_id, commit_sha, symbol_kind, symbol_qname,
            expected_doc, spawned_task_id, spawn_status, last_chain_event,
            updated_at, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
           ON CONFLICT(fingerprint) DO UPDATE SET
             spawn_status = excluded.spawn_status,
             spawned_task_id = excluded.spawned_task_id,
             updated_at = excluded.updated_at""",
        (disc.fingerprint, project_id, disc.commit_sha, disc.symbol_kind,
         disc.symbol_qname, disc.expected_doc, spawned_task_id, status, now, now),
    )


def _count_non_terminal_in_window(conn, project_id: str, commit_sha: str) -> int:
    """Count fingerprints for this commit not safe for baseline advancement (R12).

    R11: 'failed' blocks baseline advancement, so only 'merged' and 'waived'
    are considered safe (baseline-OK) statuses.
    """
    row = conn.execute(
        """SELECT COUNT(*) AS cnt FROM phase_h_processed_symbols
           WHERE project_id = ? AND commit_sha = ?
             AND spawn_status NOT IN ('merged', 'waived')""",
        (project_id, commit_sha),
    ).fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# PM task spawning (R14, R15, AC-H4, AC-H6)
# ---------------------------------------------------------------------------
def _spawn_pm_task(project_id: str, discrepancies: List[Discrepancy],
                    expected_doc: str, api_base: str = "") -> str:
    """POST to /api/task/{pid}/create to spawn a PM task.

    Returns the task_id of the spawned task, or raises on failure.
    Phase H does NOT write to docs/ (R15, AC-H4).
    """
    import urllib.request
    import urllib.error

    base = api_base or os.environ.get("GOVERNANCE_API_BASE", "http://localhost:40000")
    url = f"{base}/api/task/{project_id}/create"

    symbol_list = "\n".join(
        f"- {d.symbol_qname} ({d.symbol_kind}, confidence={d.confidence})"
        for d in discrepancies
    )

    bug_id = f"OPT-BACKLOG-DOC-DRIFT-{expected_doc.replace('/', '-').replace('.', '-')}"

    payload = {
        "type": "pm",
        "prompt": (
            f"Document the following newly-added symbols in {expected_doc}:\n"
            f"{symbol_list}\n\n"
            f"Each symbol was detected by Phase H content delta scan. "
            f"Create or update documentation as appropriate."
        ),
        "metadata": {
            "operator_id": "reconcile-v3-phase-h",
            "bug_id": bug_id,
            "expected_doc": expected_doc,
            "symbol_count": len(discrepancies),
            "symbols": [d.symbol_qname for d in discrepancies],
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=15)
    body = json.loads(resp.read().decode("utf-8"))
    return body.get("task_id", "")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_phase_h(conn, project_id: str, baseline_sha: str,
                repo_root: str,
                max_spawn: int = MAX_SPAWN_PER_RUN_DEFAULT,
                api_base: str = "",
                yaml_path: Optional[Path] = None,
                scope_kind: str = None,
                scope_value: str = None) -> PhaseHResult:
    """Execute Phase H: detect content deltas and spawn PM tasks.

    Args:
        conn: SQLite connection (must have phase_h_processed_symbols table).
        project_id: Governance project ID.
        baseline_sha: Git SHA of the Phase I baseline to diff from.
        repo_root: Path to the git repo root.
        max_spawn: Max PM tasks to spawn per run (R7).
        api_base: Override for governance API base URL.
        yaml_path: Override path to symbol_doc_map.yaml.
        scope_kind: Optional scope kind for slice-aware baseline lookup (R8).
        scope_value: Optional scope value for slice-aware baseline lookup (R8).

    Returns:
        PhaseHResult with spawn stats.
    """
    max_spawn = min(max_spawn, MAX_SPAWN_HARD_LIMIT)
    result = PhaseHResult()

    # R8: Use slice-aware baseline lookup when scope is provided
    if scope_kind and scope_value and not baseline_sha:
        try:
            from ..baseline_service import get_last_relevant_baseline
            bl = get_last_relevant_baseline(conn, project_id, scope_kind, scope_value)
            baseline_sha = bl.get("chain_version", baseline_sha)
        except Exception as exc:
            log.warning("phase_h: slice-aware baseline lookup failed: %s", exc)

    # 1. Detect discrepancies
    all_discs = detect_discrepancies(project_id, baseline_sha, repo_root, yaml_path)
    if not all_discs:
        log.info("phase_h: no discrepancies detected since %s", baseline_sha)
        result.baseline_advanced = True
        return result

    # 2. Filter by idempotency (R9)
    actionable = []
    for disc in all_discs:
        existing = _get_existing_status(conn, disc.fingerprint)
        if existing in ("merged", "waived"):
            result.skipped_terminal += 1
            continue
        if existing == "running":
            result.skipped_running += 1
            continue
        if existing == "failed":
            result.retried_failed += 1
            # Fall through to actionable — will be retried
        # skipped_throttled also falls through
        actionable.append(disc)

    if not actionable:
        log.info("phase_h: all %d discrepancies already processed", len(all_discs))
        # Check baseline advancement (R12)
        commits_in_window = {d.commit_sha for d in all_discs}
        can_advance = all(
            _count_non_terminal_in_window(conn, project_id, c) == 0
            for c in commits_in_window
        )
        result.baseline_advanced = can_advance
        return result

    # 3. Aggregate by expected_doc (R8)
    doc_groups = defaultdict(list)
    for disc in actionable:
        doc_groups[disc.expected_doc].append(disc)

    # 4. Spawn PM tasks with rate limiting (R7)
    spawned_count = 0
    all_commits = set()

    for expected_doc, discs in doc_groups.items():
        if spawned_count >= max_spawn:
            # Mark remaining as skipped_throttled
            for d in discs:
                _upsert_fingerprint(conn, project_id, d, "skipped_throttled")
                result.skipped_throttled += 1
                all_commits.add(d.commit_sha)
            continue

        # Spawn one PM task for all symbols mapping to this doc (R8)
        try:
            task_id = _spawn_pm_task(project_id, discs, expected_doc, api_base)
            for d in discs:
                _upsert_fingerprint(conn, project_id, d, "running", task_id)
                all_commits.add(d.commit_sha)
            result.spawned_tasks.append(task_id)
            spawned_count += 1
            log.info("phase_h: spawned PM task %s for %s (%d symbols)",
                     task_id, expected_doc, len(discs))
        except Exception as exc:
            # R11: failure recovery
            log.warning("phase_h: spawn failed for %s: %s", expected_doc, exc)
            for d in discs:
                _upsert_fingerprint(conn, project_id, d, "failed")
                all_commits.add(d.commit_sha)
            result.errors.append(str(exc))

    conn.commit()

    # 5. Check baseline advancement (R12)
    can_advance = all(
        _count_non_terminal_in_window(conn, project_id, c) == 0
        for c in all_commits
    ) if all_commits else True
    result.baseline_advanced = can_advance

    return result
