"""Pure project-plane endpoint resolution for host runtime consumers."""

from __future__ import annotations

import re
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


def bind_workspace_identity(workspace_root: str) -> WorkspaceIdentity:
    """Capture one explicit existing physical workspace without fallback."""
    if not isinstance(workspace_root, str) or not workspace_root or workspace_root != workspace_root.strip():
        raise ValueError("workspace root must be explicit and exact")
    candidate = Path(workspace_root)
    if not candidate.is_absolute() or not candidate.exists() or candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("workspace root must be an existing absolute non-symlink directory")
    resolved = candidate.resolve(strict=True)
    if resolved != candidate:
        raise ValueError("workspace root escaped its canonical path")
    stat = candidate.stat(follow_symlinks=False)
    return WorkspaceIdentity(str(candidate), stat.st_dev, stat.st_ino)


def resolve_runtime_plane(project_id: str) -> RuntimePlane:
    """Resolve one exact canonical project identity without ambient defaults."""
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("runtime plane requires an explicit nonempty project_id")
    if project_id != project_id.strip() or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", project_id):
        raise ValueError("runtime plane project_id must be exact canonical form")
    if project_id == "aming-claw":
        return RuntimePlane(project_id, "dev", "http://127.0.0.1:40008", "http://127.0.0.1:40109", "http://127.0.0.1:40108")
    return RuntimePlane(project_id, "stable", "http://127.0.0.1:40000", "http://127.0.0.1:40101", "http://127.0.0.1:40100")
