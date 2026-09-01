"""Pure project-plane endpoint resolution for host runtime consumers."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from dataclasses import dataclass


AC_PROJECT_ID = "aming-claw"
AC_DEV_STORAGE_NAMESPACE = ".aming-claw-dev-worlds"
GRAPH_ACTIVATION_POLICY_SCHEMA = "ac_graph_activation_policy.v1"


def graph_activation_policy(runtime_plane: str) -> dict[str, object]:
    """Return the one runtime-plane policy for active graph truth.

    This is deliberately a pure mapping: callers may report the policy, but
    they cannot use a request parameter or an environment variable to nominate
    the plane of a database effect.  Dev therefore stays denied here.  The
    graph store may admit only the narrower world-local exception re-derived
    by the opened-connection classifier from COW, inode, source, listener, and
    writer-custody evidence.
    """
    plane = str(runtime_plane or "").strip().lower()
    if plane not in {"stable", "dev", "unknown"}:
        raise ValueError("graph activation requires a stable, dev, or unknown runtime plane")
    return {
        "schema_version": GRAPH_ACTIVATION_POLICY_SCHEMA,
        "runtime_plane": plane,
        "active_graph_activation_allowed": plane == "stable",
    }


def _git_path(workspace_root: str, argument: str) -> str:
    """Resolve one Git-owned path from an existing physical workspace."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", argument],
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
    if not value.exists() or value.is_symlink() or value.resolve(strict=True) != value:
        raise ValueError("workspace Git path must be an existing physical directory")
    return str(value)


def _is_within(candidate: Path, container: Path) -> bool:
    return candidate == container or container in candidate.parents


def _is_ephemeral_path(path: Path) -> bool:
    """Reject only the host's known transient roots, not arbitrary test paths."""
    # Do not use ``tempfile.gettempdir()`` here: hermetic test fixtures may
    # live under a host-managed temporary parent while still modelling a
    # persistent sibling layout.  These two public temp roots are the
    # unsupported runtime locations and cover the legacy handoff lock path.
    temporary_roots = (Path("/tmp"), Path("/private/tmp"))
    return any(_is_within(path, root) for root in temporary_roots)


def resolve_ac_dev_storage_root(stable_shared_volume: str | Path) -> Path:
    """Derive AC's only dev world from the persistent stable-volume identity.

    This deliberately has no fallback to a worktree, ``/private/tmp``, session
    home, or an operator-supplied dev pathname.  The result is a sibling of the
    stable shared volume, in a reserved namespace, and is safe to create later.
    """
    candidate = Path(stable_shared_volume)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("canonical stable shared volume must be an existing absolute non-symlink directory")
    stable = candidate.resolve(strict=True)
    if stable != candidate:
        raise ValueError("canonical stable shared volume identity mismatch")
    git_root = Path(_git_path(str(stable), "--show-toplevel"))
    git_common_dir = Path(_git_path(str(stable), "--git-common-dir"))
    parent = git_root.parent
    if parent.is_symlink() or parent.resolve(strict=True) != parent:
        raise ValueError("canonical stable Git parent identity mismatch")
    root = parent / AC_DEV_STORAGE_NAMESPACE / AC_PROJECT_ID
    if _is_ephemeral_path(root):
        raise ValueError("AC dev storage root cannot be in a temporary directory")
    # Do not resolve a not-yet-created target: validate every existing parent.
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if probe.is_symlink() or probe.resolve(strict=True) != probe:
        raise ValueError("AC dev storage root cannot traverse a symlink")
    if root.exists() and (root.is_symlink() or root.resolve(strict=True) != root):
        raise ValueError("AC dev storage root cannot alias its canonical path")
    if _is_within(root, stable) or _is_within(stable, root):
        raise ValueError("AC dev storage root must be disjoint from stable shared volume")
    if _is_within(root, git_root) or _is_within(root, git_common_dir):
        raise ValueError("AC dev storage root must be outside stable Git checkout and common-dir")
    return root


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
