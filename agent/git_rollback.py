"""
git_rollback.py - Git-based rollback mechanism for task execution.

Provides:
- pre_task_checkpoint: Check and auto-commit uncommitted changes before task runs
- rollback_to_checkpoint: Revert workspace to the checkpoint commit on rejection
- commit_after_acceptance: Commit task changes when accepted
- get_workspace_git_status: Check if workspace has uncommitted changes
"""
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from workspace import resolve_active_workspace


def _run_git(workspace: Path, *args: str, timeout: int = 15) -> Tuple[int, str, str]:
    """Run a git command in the given workspace directory."""
    cmd = ["git", "-C", str(workspace)] + list(args)
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", "git command timed out"
    except Exception as exc:
        return -1, "", str(exc)


def is_git_repo(workspace: Path) -> bool:
    """Check if the workspace is inside a git repository."""
    code, _, _ = _run_git(workspace, "rev-parse", "--is-inside-work-tree")
    return code == 0


def get_workspace_git_status(workspace: Optional[Path] = None) -> Dict:
    """Get the current git status of the workspace.

    Returns dict with keys:
    - is_git_repo: bool
    - has_uncommitted: bool
    - uncommitted_files: list of filenames
    - current_commit: str (short SHA)
    - current_branch: str
    """
    ws = workspace or resolve_active_workspace()
    if not is_git_repo(ws):
        return {
            "is_git_repo": False,
            "has_uncommitted": False,
            "uncommitted_files": [],
            "current_commit": "",
            "current_branch": "",
        }

    # Get current commit
    _, commit_sha, _ = _run_git(ws, "rev-parse", "--short", "HEAD")

    # Get current branch
    _, branch, _ = _run_git(ws, "rev-parse", "--abbrev-ref", "HEAD")

    # Get uncommitted changes (staged + unstaged + untracked)
    _, status_out, _ = _run_git(ws, "status", "--porcelain", "--", ".")
    files = []
    for line in status_out.splitlines():
        line = line.strip()
        if line and len(line) >= 3:
            files.append(line[3:].strip())

    return {
        "is_git_repo": True,
        "has_uncommitted": len(files) > 0,
        "uncommitted_files": files,
        "current_commit": commit_sha,
        "current_branch": branch,
    }


def pre_task_checkpoint(workspace: Optional[Path] = None, task_id: str = "") -> Dict:
    """Create a checkpoint before task execution.

    If there are uncommitted changes, auto-commit them with a checkpoint message.
    Returns checkpoint info dict:
    - checkpoint_commit: the commit SHA to rollback to
    - auto_committed: whether we auto-committed
    - committed_files: list of files that were committed
    - workspace: str path
    - error: str if something went wrong
    """
    ws = workspace or resolve_active_workspace()
    result = {
        "checkpoint_commit": "",
        "auto_committed": False,
        "committed_files": [],
        "workspace": str(ws),
        "error": "",
    }

    if not is_git_repo(ws):
        result["error"] = "workspace is not a git repository"
        return result

    status = get_workspace_git_status(ws)

    if status["has_uncommitted"]:
        # Auto-commit uncommitted changes as a checkpoint
        code, _, err = _run_git(ws, "add", "-A")
        if code != 0:
            result["error"] = "git add failed: {}".format(err)
            return result

        msg = "[checkpoint] auto-save before task {}".format(task_id or "execution")
        code, _, err = _run_git(ws, "commit", "-m", msg)
        if code != 0:
            # Could be nothing to commit (e.g. all ignored files)
            if "nothing to commit" not in err:
                result["error"] = "git commit failed: {}".format(err)
                return result
        else:
            result["auto_committed"] = True
            result["committed_files"] = status["uncommitted_files"]

    # Record the current HEAD as checkpoint
    _, sha, _ = _run_git(ws, "rev-parse", "HEAD")
    result["checkpoint_commit"] = sha
    return result


def rollback_to_checkpoint(checkpoint_commit: str, workspace: Optional[Path] = None) -> Dict:
    """Rollback workspace to a previous checkpoint commit.

    Uses git reset --hard to revert all changes back to the checkpoint.
    Returns:
    - success: bool
    - reverted_commit: str
    - current_commit: str (after rollback)
    - error: str
    """
    ws = workspace or resolve_active_workspace()
    result = {
        "success": False,
        "reverted_commit": "",
        "current_commit": "",
        "error": "",
    }

    if not checkpoint_commit:
        result["error"] = "no checkpoint commit provided"
        return result

    if not is_git_repo(ws):
        result["error"] = "workspace is not a git repository"
        return result

    # Get current HEAD before rollback
    _, current_sha, _ = _run_git(ws, "rev-parse", "--short", "HEAD")
    result["reverted_commit"] = current_sha

    # Reset to checkpoint
    code, _, err = _run_git(ws, "reset", "--hard", checkpoint_commit)
    if code != 0:
        result["error"] = "git reset failed: {}".format(err)
        return result

    # Clean untracked files created by the task
    _run_git(ws, "clean", "-fd")

    # Verify
    _, new_sha, _ = _run_git(ws, "rev-parse", "--short", "HEAD")
    result["current_commit"] = new_sha
    result["success"] = True
    return result


def commit_after_acceptance(task_id: str, task_code: str = "", task_text: str = "",
                            workspace: Optional[Path] = None) -> Dict:
    """Commit task changes after acceptance.

    Stages all changes and commits with a descriptive message.
    Returns:
    - success: bool
    - commit_sha: str
    - committed_files: list
    - error: str
    """
    ws = workspace or resolve_active_workspace()
    result = {
        "success": False,
        "commit_sha": "",
        "committed_files": [],
        "error": "",
    }

    if not is_git_repo(ws):
        result["error"] = "workspace is not a git repository"
        return result

    status = get_workspace_git_status(ws)
    if not status["has_uncommitted"]:
        # No changes to commit — that's fine, task might not have changed files
        _, sha, _ = _run_git(ws, "rev-parse", "--short", "HEAD")
        result["success"] = True
        result["commit_sha"] = sha
        return result

    # Stage all changes
    code, _, err = _run_git(ws, "add", "-A")
    if code != 0:
        result["error"] = "git add failed: {}".format(err)
        return result

    # Build commit message
    desc = task_text[:80] if task_text else "task execution"
    code_label = " [{}]".format(task_code) if task_code else ""
    msg = "[accepted]{} {}: {}".format(code_label, task_id, desc)

    code, _, err = _run_git(ws, "commit", "-m", msg)
    if code != 0:
        if "nothing to commit" in err:
            _, sha, _ = _run_git(ws, "rev-parse", "--short", "HEAD")
            result["success"] = True
            result["commit_sha"] = sha
            return result
        result["error"] = "git commit failed: {}".format(err)
        return result

    _, sha, _ = _run_git(ws, "rev-parse", "--short", "HEAD")
    result["success"] = True
    result["commit_sha"] = sha
    result["committed_files"] = status["uncommitted_files"]
    return result


def get_diff_summary(checkpoint_commit: str, workspace: Optional[Path] = None) -> str:
    """Get a summary of changes since the checkpoint."""
    ws = workspace or resolve_active_workspace()
    if not checkpoint_commit or not is_git_repo(ws):
        return ""

    # Get diffstat
    _, diff_stat, _ = _run_git(ws, "diff", "--stat", checkpoint_commit, "HEAD")
    if diff_stat:
        return diff_stat

    # If HEAD hasn't moved, check working tree changes
    _, diff_stat, _ = _run_git(ws, "diff", "--stat", checkpoint_commit)
    return diff_stat or "(no diff)"
