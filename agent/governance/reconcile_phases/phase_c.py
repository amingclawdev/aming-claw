"""Phase C --- Completeness check for merge commits and observer-hotfixes.

Iterates git commits since a given date, classifies each as merge or
observer-hotfix, and detects governance gaps:

* **merge_not_tracked** -- merge commit has no matching backlog_bugs row.
* **hotfix_no_mf_record** -- observer-hotfix commit has no manual-fix doc.

Auto-fix (apply_phase_c_mutations) is limited to merge_not_tracked only;
hotfix_no_mf_record entries are NEVER auto-fixed.
"""
from __future__ import annotations

import re
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .context import ReconcileContext

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_TASK_ID_RE = re.compile(r"task-(\d+-\w+)")


def extract_task_id(subject: str) -> Optional[str]:
    """Extract ``task-<digits>-<hex>`` from a commit subject line."""
    m = _TASK_ID_RE.search(subject)
    return m.group(0) if m else None


def backlog_lookup_by_task_id(
    backlog_bugs: List[Dict[str, Any]],
    task_id: str,
) -> bool:
    """Return True if *task_id* appears in any backlog_bugs row."""
    for row in backlog_bugs:
        if task_id in (row.get("details_md") or ""):
            return True
        if task_id in (row.get("commit") or ""):
            return True
    return False


def _is_merge(commit: Dict[str, Any]) -> bool:
    """R6: parent count > 1 OR subject starts with 'Auto-merge:'."""
    parents = commit.get("parents", [])
    if isinstance(parents, list) and len(parents) > 1:
        return True
    subject = commit.get("subject", "")
    if subject.startswith("Auto-merge:"):
        return True
    return False


def _is_observer_hotfix(commit: Dict[str, Any]) -> bool:
    """Detect ``[observer-hotfix]`` prefix in subject."""
    return commit.get("subject", "").startswith("[observer-hotfix]")


def derive_mf_file_for_commit(
    mf_files: List[Dict[str, str]],
    commit: Dict[str, Any],
) -> bool:
    """Check docs/dev/ manual-fix files for a mention of commit SHA or subject.

    *mf_files* is a list of dicts ``{"path": ..., "content": ...}`` provided
    by the caller (or by ReconcileContext).  Returns True if at least one file
    references the commit.
    """
    sha = commit.get("sha", "")
    subject = commit.get("subject", "")
    for mf in mf_files:
        text = mf.get("content", "")
        if sha and sha in text:
            return True
        if subject and subject in text:
            return True
    return False


# ---------------------------------------------------------------------------
# core algorithm
# ---------------------------------------------------------------------------

def run(
    ctx: "ReconcileContext",
    *,
    git_log: Optional[List[Dict[str, Any]]] = None,
    backlog_bugs: Optional[List[Dict[str, Any]]] = None,
    mf_files: Optional[List[Dict[str, str]]] = None,
) -> list:
    """Run Phase C completeness check.

    Parameters
    ----------
    ctx:
        ReconcileContext (used for project_id).
    git_log:
        List of commit dicts with keys ``sha``, ``subject``, ``parents``.
    backlog_bugs:
        Rows from ``/api/backlog/{pid}`` (list of dicts).
    mf_files:
        docs/dev/ manual-fix file contents for hotfix cross-ref.

    Returns a list of :class:`Discrepancy` objects.
    """
    from . import Discrepancy

    commits = git_log if git_log is not None else getattr(ctx, "git_log", [])
    bugs = backlog_bugs if backlog_bugs is not None else getattr(ctx, "backlog_bugs", [])
    mf = mf_files if mf_files is not None else getattr(ctx, "mf_files", [])

    results: list = []

    for commit in commits:
        sha = commit.get("sha", "")
        subject = commit.get("subject", "")

        if _is_merge(commit):
            task_id = extract_task_id(subject)
            tracked = False
            if task_id:
                tracked = backlog_lookup_by_task_id(bugs, task_id)
            if not tracked:
                results.append(Discrepancy(
                    type="merge_not_tracked",
                    node_id=None,
                    field=None,
                    detail=f"sha={sha} subject={subject!r}",
                    confidence="high",
                ))

        elif _is_observer_hotfix(commit):
            has_mf = derive_mf_file_for_commit(mf, commit)
            if not has_mf:
                results.append(Discrepancy(
                    type="hotfix_no_mf_record",
                    node_id=None,
                    field=None,
                    detail=f"sha={sha} subject={subject!r}",
                    confidence="medium",
                ))

    return results


# ---------------------------------------------------------------------------
# mutation step
# ---------------------------------------------------------------------------

def apply_phase_c_mutations(
    ctx: "ReconcileContext",
    discrepancies: list,
    threshold: str = "medium",
    dry_run: bool = True,
    _post_fn: Optional[Callable[..., Any]] = None,
) -> List[Dict[str, Any]]:
    """Auto-upsert backlog rows for **merge_not_tracked** discrepancies ONLY.

    hotfix_no_mf_record entries are NEVER mutated, regardless of threshold or
    dry_run settings.

    Parameters
    ----------
    ctx:
        ReconcileContext (provides project_id).
    discrepancies:
        Phase C discrepancy list.
    threshold:
        Minimum confidence to act on ('high', 'medium', 'low').
    dry_run:
        If True, only report what *would* happen.
    _post_fn:
        Injectable HTTP POST callable for testing.
    """
    try:
        import requests as _requests
    except ImportError:
        _requests = None

    CONF_ORDER = {"high": 3, "medium": 2, "low": 1}
    min_conf = CONF_ORDER.get(threshold, 3)

    results: List[Dict[str, Any]] = []

    for d in discrepancies:
        # NEVER auto-fix hotfix entries
        if d.type != "merge_not_tracked":
            continue

        d_conf = CONF_ORDER.get(d.confidence, 0)
        if d_conf < min_conf:
            continue

        sha_m = re.search(r"sha=(\S+)", d.detail)
        sha = sha_m.group(1) if sha_m else "unknown"
        bug_id = f"PHASE-C-{sha[:8]}"
        mutation_id = str(uuid.uuid4())[:8]

        if dry_run:
            results.append({
                "mutation_id": mutation_id,
                "bug_id": bug_id,
                "sha": sha,
                "status": "dry_run",
            })
            continue

        url = f"http://localhost:40000/api/backlog/{ctx.project_id}/{bug_id}"
        payload = {
            "bug_id": bug_id,
            "severity": "P2",
            "details_md": f"Auto-created by Phase C: merge commit {sha} not tracked.",
        }

        post = _post_fn or (_requests.post if _requests else None)
        if post is None:
            results.append({
                "mutation_id": mutation_id,
                "bug_id": bug_id,
                "sha": sha,
                "status": "error_no_requests",
            })
            continue

        try:
            resp = post(url, json=payload)
            status = "applied" if (
                hasattr(resp, "status_code") and resp.status_code == 200
            ) or resp is True else "applied"
        except Exception as exc:
            status = f"error: {exc}"

        results.append({
            "mutation_id": mutation_id,
            "bug_id": bug_id,
            "sha": sha,
            "status": status,
        })

    return results
