"""Chain-Version commit trailer — git as single source of truth for chain state.

Phase A: 4-field trailer schema (Chain-Source-Task, Chain-Source-Stage,
Chain-Parent, Chain-Bug-Id) replaces single-field Chain-Version trailer.
See docs/dev/proposal-version-gate-as-commit-trailer.md §4.1.

Exports:
    get_chain_state       — read chain state from HEAD trailer + git status
    get_chain_version     — compat shim returning short-hash string
    validate_chain_lineage — first-parent range check for lineage gaps
    backfill_legacy_chain_history — tag pre-trailer commits with metadata
    write_merge_with_trailer — create merge commit with 4-field trailer
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any

log = logging.getLogger(__name__)

# 4-field trailer regexes per §4.1
_RE_SOURCE_TASK = re.compile(r"^Chain-Source-Task:\s*(\S+)", re.MULTILINE)
_RE_SOURCE_STAGE = re.compile(r"^Chain-Source-Stage:\s*(\S+)", re.MULTILINE)
_RE_PARENT = re.compile(r"^Chain-Parent:\s*(\S+)", re.MULTILINE)
_RE_BUG_ID = re.compile(r"^Chain-Bug-Id:\s*(\S+)", re.MULTILINE)

# Legacy single-field regex (for backfill detection)
_LEGACY_TRAILER_RE = re.compile(r"^Chain-Version:\s*(\S+)", re.MULTILINE)

# Prefixes filtered from dirty_files (mirrors auto_chain._DIRTY_IGNORE)
_DIRTY_IGNORE = (
    ".claude/", ".claude\\",
    ".worktrees/", ".worktrees\\",
    "docs/dev/", "docs/dev\\",
    ".recent-tasks.json",
    ".governance-cache/", ".governance-cache\\",
    ".observer-cache/", ".observer-cache\\",
)


def _repo_root() -> str:
    """Return the repository root directory."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git(args: list[str], cwd: str | None = None, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a git command and return the CompletedProcess."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd or _repo_root(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_trailer(message: str) -> str | None:
    """Extract Chain-Source-Stage value from a commit message, or None.

    Returns the stage value if a 4-field trailer is found, else falls back
    to Chain-Version for legacy detection, returning that value instead.
    """
    m = _RE_SOURCE_STAGE.search(message)
    if m:
        return m.group(1)
    # Legacy fallback
    m2 = _LEGACY_TRAILER_RE.search(message)
    return m2.group(1) if m2 else None


def _parse_4field_trailer(message: str) -> dict[str, str | None]:
    """Extract all 4 trailer fields from a commit message."""
    return {
        "task_id": (_m.group(1) if (_m := _RE_SOURCE_TASK.search(message)) else None),
        "stage": (_m2.group(1) if (_m2 := _RE_SOURCE_STAGE.search(message)) else None),
        "parent_sha": (_m3.group(1) if (_m3 := _RE_PARENT.search(message)) else None),
        "bug_id": (_m4.group(1) if (_m4 := _RE_BUG_ID.search(message)) else None),
    }


def get_chain_state(cwd: str | None = None) -> dict[str, Any]:
    """Read chain state from git log --first-parent, finding latest Chain-Source-Stage trailer.

    Walks first-parent history to find the most recent commit with a
    Chain-Source-Stage trailer. Returns dict with keys:
        chain_sha   — commit hash of the trailer commit (short)
        task_id     — Chain-Source-Task value
        stage       — Chain-Source-Stage value
        parent_sha  — Chain-Parent value
        version     — compat alias for chain_sha
        dirty       — bool, True if workspace has non-ignored uncommitted changes
        dirty_files — list of dirty file paths (filtered by _DIRTY_IGNORE)
        source      — 'trailer' if Chain-Source-Stage found, else 'head'
    """
    root = cwd or _repo_root()

    # Walk first-parent log to find latest commit with Chain-Source-Stage trailer
    log_proc = _git(["log", "--first-parent", "--max-count=50", "--format=%H%n%B%n---END-COMMIT---"], cwd=root)
    log_output = log_proc.stdout if log_proc.returncode == 0 else ""

    chain_sha = None
    task_id = None
    stage = None
    parent_sha = None
    source = "head"

    if log_output:
        chunks = log_output.split("---END-COMMIT---")
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            lines = chunk.split("\n", 1)
            if not lines:
                continue
            commit_hash = lines[0].strip()
            message = lines[1] if len(lines) > 1 else ""
            fields = _parse_4field_trailer(message)
            if fields["stage"]:
                # Found a commit with Chain-Source-Stage trailer
                short_proc = _git(["rev-parse", "--short", commit_hash], cwd=root)
                chain_sha = short_proc.stdout.strip() if short_proc.returncode == 0 else commit_hash[:7]
                task_id = fields["task_id"]
                stage = fields["stage"]
                parent_sha = fields["parent_sha"]
                source = "trailer"
                break
            # Also check legacy Chain-Version
            if _LEGACY_TRAILER_RE.search(message):
                short_proc = _git(["rev-parse", "--short", commit_hash], cwd=root)
                chain_sha = short_proc.stdout.strip() if short_proc.returncode == 0 else commit_hash[:7]
                source = "trailer"
                break

    # Fallback to HEAD short hash
    if not chain_sha:
        head_proc = _git(["rev-parse", "--short", "HEAD"], cwd=root)
        chain_sha = head_proc.stdout.strip() if head_proc.returncode == 0 else "unknown"

    # Check dirty status
    status_proc = _git(["status", "--porcelain"], cwd=root)
    raw_dirty = []
    if status_proc.returncode == 0:
        for line in status_proc.stdout.splitlines():
            if len(line) >= 4:
                filepath = line[3:].rstrip()
                if filepath:
                    raw_dirty.append(filepath)

    dirty_files = [
        f for f in raw_dirty
        if not any(f.startswith(p) for p in _DIRTY_IGNORE)
    ]

    return {
        "chain_sha": chain_sha,
        "task_id": task_id,
        "stage": stage,
        "parent_sha": parent_sha,
        "version": chain_sha,  # compat alias
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "source": source,
    }


def _read_runtime_version() -> str:
    """Read git short HEAD once at module import. Used to freeze RUNTIME_VERSION."""
    try:
        p = _git(["rev-parse", "--short", "HEAD"])
        return p.stdout.strip() if p.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


RUNTIME_VERSION = _read_runtime_version()  # frozen at process startup


def get_runtime_version() -> str:
    """Return chain version baked into this process at import time.

    Differs from get_chain_version() which reads live git on every call.
    Use to detect 'process running stale code' after a deploy.
    """
    return RUNTIME_VERSION


def get_chain_version(cwd: str | None = None) -> str:
    """Compat shim — returns short-hash string for existing auto_chain callers.

    Drop-in replacement for the DB-read pattern that previously fetched
    chain_version from project_version table.
    """
    state = get_chain_state(cwd=cwd)
    return state["chain_sha"]


def validate_chain_lineage(start: str, end: str, cwd: str | None = None) -> dict[str, Any]:
    """Validate first-parent commit range for lineage continuity.

    Per §4.3: performs first-parent range check and returns breaks[]
    for non-trailer commits.

    Args:
        start: Starting commit ref (exclusive, older)
        end: Ending commit ref (inclusive, newer)
        cwd: Optional working directory override

    Returns:
        dict with keys:
            valid   — bool
            reason  — descriptive string
            breaks  — list of commit hashes that lack Chain-Source-Stage trailers
            commits — total commits in range
    """
    root = cwd or _repo_root()

    # Verify both refs exist
    for ref_name, ref_val in [("start", start), ("end", end)]:
        check = _git(["rev-parse", "--verify", ref_val], cwd=root)
        if check.returncode != 0:
            return {
                "valid": False,
                "reason": f"Invalid ref '{ref_val}' for {ref_name}: {check.stderr.strip()}",
                "breaks": [],
                "commits": 0,
            }

    # Get first-parent commit range with messages
    range_proc = _git(
        ["log", "--first-parent", f"{start}..{end}", "--format=%H%n%B%n---END-COMMIT---"],
        cwd=root,
    )
    if range_proc.returncode != 0:
        return {
            "valid": False,
            "reason": f"rev-list failed: {range_proc.stderr.strip()}",
            "breaks": [],
            "commits": 0,
        }

    chunks = range_proc.stdout.split("---END-COMMIT---")
    breaks = []
    commit_count = 0

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.split("\n", 1)
        if not lines:
            continue
        commit_hash = lines[0].strip()
        if not commit_hash or len(commit_hash) < 7:
            continue
        commit_count += 1
        message = lines[1] if len(lines) > 1 else ""
        fields = _parse_4field_trailer(message)
        if not fields["stage"]:
            # Also check legacy trailer
            if not _LEGACY_TRAILER_RE.search(message):
                breaks.append(commit_hash[:12])

    if commit_count == 0:
        return {
            "valid": False,
            "reason": f"No commits in range {start}..{end}",
            "breaks": [],
            "commits": 0,
        }

    valid = len(breaks) == 0
    reason = (
        f"Valid lineage: {commit_count} commits from {start[:8]} to {end[:8]}"
        if valid
        else f"Lineage has {len(breaks)} break(s) in {commit_count} commits"
    )

    return {
        "valid": valid,
        "reason": reason,
        "breaks": breaks,
        "commits": commit_count,
    }


def backfill_legacy_chain_history(
    limit: int = 50,
    cwd: str | None = None,
    output_path: str | None = None,
) -> list[dict[str, Any]]:
    """Tag commits lacking Chain-Source-Stage trailer with legacy metadata.

    Scans up to `limit` recent commits and returns metadata dicts for those
    missing a 4-field trailer. Each dict contains:
        commit          — full hash
        short           — short hash
        legacy_inferred — True
        needs_audit     — True
        audit_note      — descriptive string
        has_legacy_trailer — True if commit has old Chain-Version trailer

    If output_path is provided, writes chain_history.json to that path.

    NOTE: This does NOT modify git history (no rebase/amend). The metadata
    is returned for the caller to store in governance DB or audit log.
    """
    root = cwd or _repo_root()

    log_proc = _git(
        ["log", f"--max-count={limit}", "--format=%H%n%B%n---END-COMMIT---"],
        cwd=root,
    )
    if log_proc.returncode != 0:
        log.warning("backfill: git log failed: %s", log_proc.stderr.strip())
        return []

    results: list[dict[str, Any]] = []
    chunks = log_proc.stdout.split("---END-COMMIT---")

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.split("\n", 1)
        if not lines:
            continue
        commit_hash = lines[0].strip()
        if not commit_hash or len(commit_hash) < 7:
            continue
        message = lines[1] if len(lines) > 1 else ""

        # Check for 4-field trailer
        fields = _parse_4field_trailer(message)
        if fields["stage"]:
            continue  # Already has new-style trailer

        short_proc = _git(["rev-parse", "--short", commit_hash], cwd=root)
        short_hash = short_proc.stdout.strip() if short_proc.returncode == 0 else commit_hash[:7]

        has_legacy = bool(_LEGACY_TRAILER_RE.search(message))

        results.append({
            "commit": commit_hash,
            "short": short_hash,
            "legacy_inferred": True,
            "needs_audit": True,
            "has_legacy_trailer": has_legacy,
            "audit_note": f"Pre-trailer commit {short_hash} — no Chain-Source-Stage trailer found; "
                          f"marked legacy_inferred during backfill scan",
        })

    log.info("backfill: scanned %d commits, %d lack Chain-Source-Stage trailer",
             min(limit, len(chunks)), len(results))

    # Write chain_history.json if output_path specified
    if output_path and results:
        try:
            with open(output_path, "w") as f:
                json.dump({"backfill_results": results, "total_scanned": min(limit, len(chunks))}, f, indent=2)
            log.info("backfill: wrote chain_history.json to %s", output_path)
        except Exception as e:
            log.warning("backfill: failed to write chain_history.json: %s", e)

    return results


def write_merge_with_trailer(
    message: str,
    branch: str | None = None,
    cwd: str | None = None,
    extra_args: list[str] | None = None,
    task_id: str | None = None,
    parent_chain_sha: str | None = None,
    bug_id: str | None = None,
) -> tuple[bool, str, str]:
    """Create a merge/commit with 4-field Chain trailer lines.

    Per §4.4: produces merge commits with Chain-Source-Task, Chain-Source-Stage,
    Chain-Parent, Chain-Bug-Id trailers.

    If `branch` is provided, performs `git merge --no-ff <branch>` first.
    Otherwise, commits staged changes with the trailers.

    Args:
        message: Base commit message (trailers will be appended)
        branch: Optional branch to merge (--no-ff)
        cwd: Optional working directory
        extra_args: Additional git args
        task_id: Chain-Source-Task value (task ID)
        parent_chain_sha: Chain-Parent value (parent chain commit SHA)
        bug_id: Chain-Bug-Id value (backlog bug ID)

    Returns:
        (success, commit_hash, error_message)
    """
    root = cwd or _repo_root()

    if branch:
        # Perform merge
        merge_args = ["merge", branch, "--no-ff", "--no-commit"]
        if extra_args:
            merge_args.extend(extra_args)
        merge_proc = _git(merge_args, cwd=root, timeout=30)
        if merge_proc.returncode != 0:
            # Abort the failed merge
            _git(["merge", "--abort"], cwd=root)
            return False, "", f"Merge failed: {merge_proc.stderr.strip()[:300]}"

    # Build trailer lines
    trailer_lines = []
    trailer_lines.append(f"Chain-Source-Task: {task_id or 'unknown'}")
    trailer_lines.append(f"Chain-Source-Stage: merge")
    trailer_lines.append(f"Chain-Parent: {parent_chain_sha or 'none'}")
    trailer_lines.append(f"Chain-Bug-Id: {bug_id or 'none'}")
    trailer_block = "\n".join(trailer_lines)

    # Build commit message with trailers
    full_msg = f"{message}\n\n{trailer_block}"
    commit_args = ["commit", "-m", full_msg]

    commit_proc = _git(commit_args, cwd=root, timeout=30)
    if commit_proc.returncode != 0:
        return False, "", f"Commit failed: {commit_proc.stderr.strip()[:300]}"

    # Read the new commit's short hash
    rev_proc = _git(["rev-parse", "--short", "HEAD"], cwd=root)
    short_hash = rev_proc.stdout.strip() if rev_proc.returncode == 0 else "unknown"

    log.info("write_merge_with_trailer: created commit %s with 4-field trailer (task=%s)",
             short_hash, task_id)
    return True, short_hash, ""
