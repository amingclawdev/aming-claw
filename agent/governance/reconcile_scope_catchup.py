"""Branch/worktree wrapper for scoped reconcile commit-sweep catch-up.

This module keeps the operational rule explicit: scoped reconcile can chase
main-line MF drift in a dedicated worktree without redeploying runtime services.
Only runtime MF commits on main require gov/sm redeploy.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .reconcile_phases.orchestrator import run_commit_sweep_orchestrated


# Phase G is a global chain-closure audit, not a commit/file scoped check. Keep
# it opt-in so MF catch-up does not drown hot-file drift in historical queue noise.
DEFAULT_PHASES = ["K", "A", "E", "D", "F"]
ACTIONABLE_MATERIALIZATION_TYPES = frozenset({
    "unmapped_file",
    "unmapped_doc",
    "unmapped_medium_conf_suggest",
    "stale_doc_ref",
    "doc_stale",
    "doc_value_drift",
    "contract_no_test",
})


class ScopeCatchupError(RuntimeError):
    """Raised when the catch-up worktree cannot be prepared safely."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(args: Iterable[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *list(args)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise ScopeCatchupError(
            "git {args} failed in {cwd}: {stderr}".format(
                args=" ".join(args),
                cwd=cwd,
                stderr=(proc.stderr or proc.stdout or "").strip(),
            )
        )
    return proc


def _rev_parse(repo_root: Path, ref: str, short: bool = False) -> str:
    args = ["rev-parse"]
    if short:
        args.append("--short")
    args.append(ref)
    return _git(args, repo_root).stdout.strip()


def _branch_exists(repo_root: Path, branch: str) -> bool:
    proc = _git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], repo_root, check=False)
    return proc.returncode == 0


def _worktree_is_clean(worktree_path: Path) -> bool:
    proc = _git(["status", "--porcelain"], worktree_path)
    return not proc.stdout.strip()


def ensure_catchup_worktree(
    repo_root: str | Path,
    *,
    worktree_path: str | Path,
    branch: str,
    base_ref: str = "HEAD",
) -> Dict[str, str]:
    """Create or fast-forward a catch-up worktree.

    Existing worktrees must be clean. Updates use ``git merge --ff-only`` so the
    function never rewrites or discards local work.
    """
    root = Path(repo_root).resolve()
    wt = Path(worktree_path)
    if not wt.is_absolute():
        wt = (root / wt).resolve()
    base_commit = _rev_parse(root, base_ref)
    base_short = _rev_parse(root, base_ref, short=True)

    if wt.exists():
        if not (wt / ".git").exists():
            raise ScopeCatchupError("worktree path exists but is not a git worktree: " + str(wt))
        if not _worktree_is_clean(wt):
            raise ScopeCatchupError("worktree is dirty; refusing to fast-forward: " + str(wt))
        _git(["merge", "--ff-only", base_commit], wt)
        action = "fast_forwarded"
    elif _branch_exists(root, branch):
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", str(wt), branch], root)
        _git(["merge", "--ff-only", base_commit], wt)
        action = "attached_existing_branch"
    else:
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "-b", branch, str(wt), base_ref], root)
        action = "created"

    head = _rev_parse(wt, "HEAD")
    return {
        "action": action,
        "repo_root": str(root),
        "worktree_path": str(wt),
        "branch": branch,
        "base_ref": base_ref,
        "base_commit": base_commit,
        "base_short": base_short,
        "worktree_head": head,
    }


def _default_branch(base_short: str) -> str:
    return "codex/scope-catchup-" + base_short


def _default_worktree(root: Path, base_short: str) -> Path:
    return root / ".worktrees" / ("scope-catchup-" + base_short)


def _extract_discrepancy_path(discrepancy: Dict[str, Any]) -> str:
    detail = str(discrepancy.get("detail") or "")
    for pattern in (r"\bfile=([^\s]+)", r"\bdoc=([^\s]+)", r"\btarget=([^\s]+)"):
        match = re.search(pattern, detail)
        if match:
            return match.group(1).strip().strip(",;")
    if detail and "/" in detail and " " not in detail:
        return detail.strip().strip(",;")
    return str(discrepancy.get("path") or discrepancy.get("file") or "")


def _is_actionable_discrepancy(discrepancy: Dict[str, Any]) -> bool:
    dtype = str(discrepancy.get("type") or "")
    return dtype in ACTIONABLE_MATERIALIZATION_TYPES


def _materialization_bug_id(
    project_id: str,
    worktree: Dict[str, str],
    sweep: Dict[str, Any],
    since_baseline: Optional[str] = None,
) -> str:
    import hashlib

    base = (since_baseline or "")[:7] or worktree.get("base_short") or "base"
    head = worktree.get("worktree_head", "")[:7] or "head"
    hot = "\n".join(sorted(str(p) for p in sweep.get("hot_files", []) or []))
    digest = hashlib.sha1((project_id + "\n" + base + "\n" + head + "\n" + hot).encode("utf-8")).hexdigest()[:8]
    return "RECONCILE-SCOPE-MATERIALIZE-{base}-{head}-{digest}".format(
        base=base,
        head=head,
        digest=digest,
    )


def _summarize_materialization_backlog(
    *,
    project_id: str,
    worktree: Dict[str, str],
    sweep: Dict[str, Any],
    bug_id: Optional[str] = None,
    since_baseline: Optional[str] = None,
) -> Dict[str, Any]:
    actionable = [
        dict(d) for d in sweep.get("dedup_discrepancies", []) or []
        if isinstance(d, dict) and _is_actionable_discrepancy(d)
    ]
    target_files = []
    for path in list(sweep.get("hot_files", []) or []):
        if path and path not in target_files:
            target_files.append(path)
    for d in actionable:
        path = _extract_discrepancy_path(d)
        if path and path not in target_files:
            target_files.append(path)

    test_files = [
        p for p in target_files
        if p.endswith(".py") and ("/test" in p or "\\test" in p or Path(p).name.startswith("test_"))
    ]

    return {
        "enabled": False,
        "filed": False,
        "bug_id": bug_id or _materialization_bug_id(project_id, worktree, sweep, since_baseline),
        "actionable_count": len(actionable),
        "actionable_types": sorted({str(d.get("type") or "") for d in actionable}),
        "target_files": target_files[:50],
        "test_files": test_files[:50],
        "discrepancies": actionable[:20],
    }


def _file_materialization_backlog(
    *,
    project_id: str,
    summary: Dict[str, Any],
    api_base: str,
    provenance_path: str,
) -> Dict[str, Any]:
    if not summary.get("actionable_count"):
        result = dict(summary)
        result["enabled"] = True
        result["filed"] = False
        result["reason"] = "no actionable materialization discrepancies"
        return result

    base = api_base.rstrip("/") or "http://localhost:40000"
    bug_id = str(summary["bug_id"])
    url = "{base}/api/backlog/{project_id}/{bug_id}".format(
        base=base,
        project_id=project_id,
        bug_id=bug_id,
    )
    payload = {
        "title": "Scope reconcile materialization drift for " + bug_id,
        "status": "OPEN",
        "priority": "P1",
        "target_files": summary.get("target_files", []),
        "test_files": summary.get("test_files", []),
        "acceptance_criteria": [
            "Every target file is mapped to an appropriate graph node or explicitly waived.",
            "Runtime behavior changes have node-scoped documentation or explicit doc_debt.",
            "New or changed test files are attached to the owning graph node.",
            "QA verifies graph/doc/test coverage before closing this materialization backlog.",
        ],
        "details_md": (
            "Auto-filed by scope reconcile catch-up. The commit-sweep found actionable "
            "graph/doc/test materialization drift that should be handled by the standard "
            "PM -> Dev -> Test -> QA -> Merge chain, not by silently advancing a baseline.\n\n"
            "Actionable types: {types}\n\n"
            "Discrepancy sample:\n```json\n{sample}\n```"
        ).format(
            types=", ".join(summary.get("actionable_types", [])),
            sample=json.dumps(summary.get("discrepancies", [])[:10], indent=2, ensure_ascii=False),
        ),
        "provenance_paths": [provenance_path],
        "force_admit": True,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    result = dict(summary)
    result["enabled"] = True
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        result["filed"] = bool(body.get("ok"))
        result["response"] = body
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        result["filed"] = False
        result["error"] = str(exc)
    return result


def _sweep_summary(sweep: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "commits": len(sweep.get("commits", [])),
        "all_discrepancies": len(sweep.get("all_discrepancies", [])),
        "dedup_discrepancies": len(sweep.get("dedup_discrepancies", [])),
        "hot_files": len(sweep.get("hot_files", [])),
        "covered_hot": len(sweep.get("covered_hot", [])),
        "coverage_pct": sweep.get("coverage_pct", 0.0),
        "baseline_written": bool(sweep.get("baseline_written")),
    }


def run_scope_catchup(
    *,
    project_id: str = "aming-claw",
    repo_root: str | Path | None = None,
    base_ref: str = "HEAD",
    branch: Optional[str] = None,
    worktree_path: str | Path | None = None,
    since_baseline: Optional[str] = None,
    phases: Optional[List[str]] = None,
    dry_run: bool = True,
    output_path: str | Path | None = None,
    file_materialization_backlog: bool = False,
    materialization_api_base: str = "http://localhost:40000",
    materialization_bug_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Prepare a catch-up worktree and run scoped commit-sweep.

    The returned ``doc_update_mode`` is intentionally ``scan_only``: this path
    audits drift and writes commit-sweep baselines, but it does not mutate repo
    docs or ask PM/Dev to materialize documentation.
    """
    root = Path(repo_root).resolve() if repo_root else _repo_root()
    base_short = _rev_parse(root, base_ref, short=True)
    branch = branch or _default_branch(base_short)
    wt = Path(worktree_path) if worktree_path else _default_worktree(root, base_short)

    os.environ.setdefault("SHARED_VOLUME_PATH", str(root / "shared-volume"))

    worktree = ensure_catchup_worktree(
        root,
        worktree_path=wt,
        branch=branch,
        base_ref=base_ref,
    )

    effective_phases = phases or list(DEFAULT_PHASES)
    scan_dry_run = True if not dry_run else dry_run
    sweep = run_commit_sweep_orchestrated(
        project_id,
        worktree["worktree_path"],
        since_baseline=since_baseline,
        phases=effective_phases,
        dry_run=scan_dry_run,
    )

    mode = "dry_run" if dry_run else "apply"
    if output_path is None:
        output = Path(worktree["worktree_path"]) / "docs" / "dev" / "scratch" / (
            "scope-catchup-{base}-{mode}.json".format(base=worktree["base_short"], mode=mode)
        )
    else:
        output = Path(output_path)
        if not output.is_absolute():
            output = Path(worktree["worktree_path"]) / output
    output.parent.mkdir(parents=True, exist_ok=True)

    summary = _sweep_summary(sweep)
    resolved_since_baseline = sweep.get("since_baseline") or since_baseline
    materialization = _summarize_materialization_backlog(
        project_id=project_id,
        worktree=worktree,
        sweep=sweep,
        bug_id=materialization_bug_id,
        since_baseline=resolved_since_baseline,
    )

    result = {
        "ok": True,
        "project_id": project_id,
        "mode": mode,
        "phases": effective_phases,
        "since_baseline": since_baseline,
        "resolved_since_baseline": resolved_since_baseline,
        "worktree": worktree,
        "summary": summary,
        "result": sweep,
        "result_path": str(output),
        "no_redeploy": True,
        "doc_update_mode": "scan_only",
        "doc_update_note": (
            "scope-catchup runs commit-sweep scanning and optional baseline writes only; "
            "README/doc materialization must be filed as a separate chain/backlog task. "
            "Global chain-closure checks are opt-in via --phases when needed."
        ),
        "materialization_backlog": materialization,
    }
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if file_materialization_backlog:
        result["materialization_backlog"] = _file_materialization_backlog(
            project_id=project_id,
            summary=materialization,
            api_base=materialization_api_base,
            provenance_path=str(output),
        )
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if not dry_run:
        materialization_state = result.get("materialization_backlog", {})
        if materialization.get("actionable_count") and not materialization_state.get("filed"):
            result["baseline_blocked_reason"] = (
                "actionable materialization drift found; file materialization backlog "
                "or pass a waiver before advancing commit_sweep baseline"
            )
            result["summary"]["baseline_written"] = False
        else:
            apply_sweep = run_commit_sweep_orchestrated(
                project_id,
                worktree["worktree_path"],
                since_baseline=since_baseline,
                phases=effective_phases,
                dry_run=False,
            )
            result["result"] = apply_sweep
            result["summary"] = _sweep_summary(apply_sweep)
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
