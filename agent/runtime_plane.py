"""Pure project-plane endpoint resolution for host runtime consumers."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimePlane:
    project_id: str
    name: str
    governance_url: str
    manager_url: str
    executor_url: str


@dataclass(frozen=True)
class WorkspaceIdentity:
    root: str
    device: int
    inode: int


@dataclass(frozen=True)
class GitWorktreeIdentity:
    """Immutable physical and Git identity for one registered task worktree.

    A pathname alone is intentionally not authority.  A worktree can be
    removed and recreated at the same path, and linked worktrees share a
    common repository while retaining different git directories.  Effects are
    therefore bound to all three facts captured here.
    """

    logical_task_id: str
    workspace: WorkspaceIdentity
    git_root: WorkspaceIdentity
    git_common_dir: WorkspaceIdentity
    git_dir: WorkspaceIdentity


def bind_workspace_identity(workspace_root: str) -> WorkspaceIdentity:
    """Capture one explicit existing physical workspace without fallback."""
    if not isinstance(workspace_root, str) or not workspace_root or workspace_root != workspace_root.strip():
        raise ValueError("workspace root must be explicit and exact")
    candidate = Path(workspace_root)
    if not candidate.is_absolute() or not candidate.exists() or candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("workspace root must be an existing absolute non-symlink directory")
    resolved = candidate.resolve(strict=True)
    stat = resolved.stat(follow_symlinks=False)
    return WorkspaceIdentity(str(resolved), stat.st_dev, stat.st_ino)


def validate_current_workspace_identity(identity: WorkspaceIdentity) -> WorkspaceIdentity:
    """Fail closed if an already-bound workspace changed before an effect."""
    if not isinstance(identity, WorkspaceIdentity):
        raise ValueError("workspace identity is required")
    current = bind_workspace_identity(identity.root)
    if (current.device, current.inode) != (identity.device, identity.inode):
        raise ValueError("workspace identity changed")
    return identity


def _git_path(workspace_root: str, argument: str) -> str:
    """Resolve a Git path to one existing absolute non-symlink directory."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", argument],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("workspace is not a readable Git worktree") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ValueError("workspace is not a readable Git worktree")
    value = Path(proc.stdout.strip())
    if not value.is_absolute():
        value = Path(workspace_root) / value
    return str(value)


def bind_git_worktree_identity(workspace_root: str, logical_task_id: str) -> GitWorktreeIdentity:
    """Capture immutable worktree/repository facts for a logical task root."""
    if not isinstance(logical_task_id, str) or not logical_task_id.strip():
        raise ValueError("logical task authority is required")
    workspace = bind_workspace_identity(workspace_root)
    git_root = bind_workspace_identity(_git_path(workspace.root, "--show-toplevel"))
    if git_root != workspace:
        raise ValueError("registered workspace must be the Git worktree root")
    common_dir = bind_workspace_identity(_git_path(workspace.root, "--git-common-dir"))
    git_dir = bind_workspace_identity(_git_path(workspace.root, "--git-dir"))
    return GitWorktreeIdentity(
        logical_task_id=logical_task_id,
        workspace=workspace,
        git_root=git_root,
        git_common_dir=common_dir,
        git_dir=git_dir,
    )


def validate_current_git_worktree_identity(identity: GitWorktreeIdentity) -> GitWorktreeIdentity:
    """Fail closed when any registered Git/worktree fact changed before effect."""
    if not isinstance(identity, GitWorktreeIdentity):
        raise ValueError("Git worktree identity is required")
    current = bind_git_worktree_identity(identity.workspace.root, identity.logical_task_id)
    if current != identity:
        raise ValueError("registered Git worktree identity changed")
    return identity


def resolve_runtime_plane(project_id: str) -> RuntimePlane:
    """Resolve one exact canonical project identity without ambient defaults."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("runtime plane requires an explicit nonempty project_id")
    if project_id != project_id.strip() or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", project_id):
        raise ValueError("runtime plane project_id must be exact canonical form")
    if project_id == "aming-claw":
        return RuntimePlane(project_id, "dev", "http://127.0.0.1:40008", "http://127.0.0.1:40109", "http://127.0.0.1:40108")
    return RuntimePlane(project_id, "stable", "http://127.0.0.1:40000", "http://127.0.0.1:40101", "http://127.0.0.1:40100")
