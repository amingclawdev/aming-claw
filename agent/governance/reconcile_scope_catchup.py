"""Branch/worktree wrapper for scoped reconcile commit-sweep catch-up.

This module keeps the operational rule explicit: scoped reconcile can chase
main-line MF drift in a dedicated worktree without redeploying runtime services.
Only runtime MF commits on main require gov/sm redeploy.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .reconcile_phases.orchestrator import run_commit_sweep_orchestrated


# Phase G is a global chain-closure audit, not a commit/file scoped check. Keep
# it opt-in so MF catch-up does not drown hot-file drift in historical queue noise.
DEFAULT_PHASES = ["K", "A", "E", "D", "F"]


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
    sweep = run_commit_sweep_orchestrated(
        project_id,
        worktree["worktree_path"],
        since_baseline=since_baseline,
        phases=effective_phases,
        dry_run=dry_run,
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

    summary = {
        "commits": len(sweep.get("commits", [])),
        "all_discrepancies": len(sweep.get("all_discrepancies", [])),
        "dedup_discrepancies": len(sweep.get("dedup_discrepancies", [])),
        "hot_files": len(sweep.get("hot_files", [])),
        "covered_hot": len(sweep.get("covered_hot", [])),
        "coverage_pct": sweep.get("coverage_pct", 0.0),
        "baseline_written": bool(sweep.get("baseline_written")),
    }

    result = {
        "ok": True,
        "project_id": project_id,
        "mode": mode,
        "phases": effective_phases,
        "since_baseline": since_baseline,
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
    }
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
