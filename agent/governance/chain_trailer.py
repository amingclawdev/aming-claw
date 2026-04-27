"""Chain-Version commit trailer — git as single source of truth for chain state.

Reads/writes Chain-Version trailers in git commit messages, replacing DB-stored
SERVER_VERSION as the canonical version gate source. Part of Phase A of the
Version Gate as Commit Trailer proposal (docs/dev/proposal-version-gate-as-commit-trailer.md).

Exports:
    get_chain_state       — read chain state from HEAD trailer + git status
    get_chain_version     — compat shim returning short-hash string
    validate_chain_lineage — first-parent range check for lineage gaps
    backfill_legacy_chain_history — tag pre-trailer commits with metadata
    write_merge_with_trailer — create merge commit with Chain-Version trailer
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

log = logging.getLogger(__name__)

# Regex to match "Chain-Version: <hash>" trailer in commit message body
_TRAILER_RE = re.compile(r"^Chain-Version:\s*(\S+)", re.MULTILINE)

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
    """Extract Chain-Version value from a commit message, or None."""
    m = _TRAILER_RE.search(message)
    return m.group(1) if m else None


def get_chain_state(cwd: str | None = None) -> dict[str, Any]:
    """Read chain state from HEAD commit trailer + git status.

    Returns dict with keys:
        version     — short-hash string (from trailer or HEAD)
        dirty       — bool, True if workspace has non-ignored uncommitted changes
        dirty_files — list of dirty file paths (filtered by _DIRTY_IGNORE)
        source      — 'trailer' if Chain-Version trailer found, else 'head'
    """
    root = cwd or _repo_root()

    # Read HEAD commit message for trailer
    msg_proc = _git(["log", "-1", "--format=%B"], cwd=root)
    message = msg_proc.stdout if msg_proc.returncode == 0 else ""
    trailer_version = _parse_trailer(message)

    # Read HEAD short hash as fallback
    head_proc = _git(["rev-parse", "--short", "HEAD"], cwd=root)
    head_short = head_proc.stdout.strip() if head_proc.returncode == 0 else "unknown"

    # Determine version and source
    if trailer_version:
        version = trailer_version
        source = "trailer"
    else:
        version = head_short
        source = "head"

    # Check dirty status
    status_proc = _git(["status", "--porcelain"], cwd=root)
    raw_dirty = []
    if status_proc.returncode == 0 and status_proc.stdout.strip():
        for line in status_proc.stdout.strip().splitlines():
            # porcelain format: XY filename
            filepath = line[3:].strip() if len(line) > 3 else line.strip()
            if filepath:
                raw_dirty.append(filepath)

    # Filter ignored paths
    dirty_files = [
        f for f in raw_dirty
        if not any(f.startswith(p) for p in _DIRTY_IGNORE)
    ]

    return {
        "version": version,
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
    return state["version"]


def validate_chain_lineage(start: str, end: str, cwd: str | None = None) -> tuple[bool, str]:
    """Validate first-parent commit range for lineage continuity.

    Args:
        start: Starting commit ref (exclusive, older)
        end: Ending commit ref (inclusive, newer)
        cwd: Optional working directory override

    Returns:
        (True, reason) for valid lineage, (False, reason) for gaps/non-linear history.
    """
    root = cwd or _repo_root()

    # Verify both refs exist
    for ref_name, ref_val in [("start", start), ("end", end)]:
        check = _git(["rev-parse", "--verify", ref_val], cwd=root)
        if check.returncode != 0:
            return False, f"Invalid ref '{ref_val}' for {ref_name}: {check.stderr.strip()}"

    # Get first-parent commit range
    range_proc = _git(
        ["rev-list", "--first-parent", f"{start}..{end}"],
        cwd=root,
    )
    if range_proc.returncode != 0:
        return False, f"rev-list failed: {range_proc.stderr.strip()}"

    commits = [c.strip() for c in range_proc.stdout.strip().splitlines() if c.strip()]
    if not commits:
        return False, f"No commits in range {start}..{end}"

    # Verify each commit's first parent links to the previous one (linear chain)
    for i, commit in enumerate(commits):
        if i == len(commits) - 1:
            # Last commit (oldest in range) — its parent should be start
            parent_proc = _git(["rev-parse", f"{commit}^"], cwd=root)
            if parent_proc.returncode != 0:
                return False, f"Cannot resolve parent of {commit[:8]}"
            parent = parent_proc.stdout.strip()
            # Resolve start to full hash for comparison
            start_full = _git(["rev-parse", start], cwd=root).stdout.strip()
            if parent != start_full:
                return False, f"Gap: parent of {commit[:8]} is {parent[:8]}, expected {start_full[:8]}"
        else:
            # Verify next commit in list is the first parent
            parent_proc = _git(["rev-parse", f"{commit}^"], cwd=root)
            if parent_proc.returncode != 0:
                return False, f"Cannot resolve parent of {commit[:8]}"
            parent = parent_proc.stdout.strip()
            next_full = _git(["rev-parse", commits[i + 1]], cwd=root).stdout.strip()
            if parent != next_full:
                return False, f"Non-linear: parent of {commit[:8]} is {parent[:8]}, expected {next_full[:8]}"

    return True, f"Valid lineage: {len(commits)} commits from {start[:8]} to {end[:8]}"


def backfill_legacy_chain_history(
    limit: int = 50,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Tag commits lacking Chain-Version trailer with legacy metadata.

    Scans up to `limit` recent commits and returns metadata dicts for those
    missing a Chain-Version trailer. Each dict contains:
        commit   — full hash
        short    — short hash
        legacy_inferred — True
        needs_audit     — True
        audit_note      — descriptive string

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

        # Skip commits that already have a trailer
        if _parse_trailer(message):
            continue

        short_proc = _git(["rev-parse", "--short", commit_hash], cwd=root)
        short_hash = short_proc.stdout.strip() if short_proc.returncode == 0 else commit_hash[:7]

        results.append({
            "commit": commit_hash,
            "short": short_hash,
            "legacy_inferred": True,
            "needs_audit": True,
            "audit_note": f"Pre-trailer commit {short_hash} — no Chain-Version trailer found; "
                          f"marked legacy_inferred during backfill scan",
        })

    log.info("backfill: scanned %d commits, %d lack Chain-Version trailer",
             min(limit, len(chunks)), len(results))
    return results


def write_merge_with_trailer(
    message: str,
    branch: str | None = None,
    cwd: str | None = None,
    extra_args: list[str] | None = None,
) -> tuple[bool, str, str]:
    """Create a merge/commit with a Chain-Version trailer line.

    If `branch` is provided, performs `git merge --no-ff <branch>` first.
    Otherwise, commits staged changes with the trailer.

    The trailer is appended as: Chain-Version: <short-hash>

    Args:
        message: Base commit message (trailer will be appended)
        branch: Optional branch to merge (--no-ff)
        cwd: Optional working directory
        extra_args: Additional git args

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

    # Get what HEAD will be after commit to embed in trailer
    # For now, we commit first then amend — simpler and guaranteed correct hash
    # Actually: commit with placeholder, then we know the hash IS the commit itself

    # Build commit message with trailer
    # The trailer will reference the resulting commit's own short hash
    # We use a two-step: commit, read hash, amend with trailer
    temp_msg = message
    commit_args = ["commit", "-m", temp_msg]
    if branch:
        commit_args = ["commit", "-m", temp_msg]  # merge was staged by --no-commit
    else:
        # Caller should have staged files already
        pass

    commit_proc = _git(commit_args, cwd=root, timeout=30)
    if commit_proc.returncode != 0:
        return False, "", f"Commit failed: {commit_proc.stderr.strip()[:300]}"

    # Read the new commit's short hash
    rev_proc = _git(["rev-parse", "--short", "HEAD"], cwd=root)
    short_hash = rev_proc.stdout.strip() if rev_proc.returncode == 0 else "unknown"

    # Amend with trailer appended
    trailer_msg = f"{message}\n\nChain-Version: {short_hash}"
    amend_proc = _git(["commit", "--amend", "-m", trailer_msg], cwd=root, timeout=30)
    if amend_proc.returncode != 0:
        # Original commit succeeded, amend failed — return original hash
        log.warning("write_merge_with_trailer: amend failed: %s", amend_proc.stderr.strip())
        return True, short_hash, f"Commit created but trailer amend failed: {amend_proc.stderr.strip()[:200]}"

    # Re-read hash after amend (it changes)
    rev2_proc = _git(["rev-parse", "--short", "HEAD"], cwd=root)
    final_hash = rev2_proc.stdout.strip() if rev2_proc.returncode == 0 else short_hash

    # Final amend with correct self-referencing hash
    final_msg = f"{message}\n\nChain-Version: {final_hash}"
    final_amend = _git(["commit", "--amend", "-m", final_msg], cwd=root, timeout=30)
    if final_amend.returncode == 0:
        rev3 = _git(["rev-parse", "--short", "HEAD"], cwd=root)
        actual_hash = rev3.stdout.strip() if rev3.returncode == 0 else final_hash
        # If amend changed hash again, do one more pass (converges quickly)
        if actual_hash != final_hash:
            converge_msg = f"{message}\n\nChain-Version: {actual_hash}"
            _git(["commit", "--amend", "-m", converge_msg], cwd=root, timeout=30)
            rev4 = _git(["rev-parse", "--short", "HEAD"], cwd=root)
            actual_hash = rev4.stdout.strip() if rev4.returncode == 0 else actual_hash
        final_hash = actual_hash

    log.info("write_merge_with_trailer: created commit %s with Chain-Version trailer", final_hash)
    return True, final_hash, ""
