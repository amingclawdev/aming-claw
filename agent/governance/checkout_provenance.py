"""Checkout provenance helpers for graph materialization."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _git_output(root: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _resolve_under(root: Path, raw: str) -> str:
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_ephemeral_root(root: Path) -> bool:
    candidates = {
        Path(tempfile.gettempdir()),
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/tmp"),
    }
    return any(_is_under(root, candidate) for candidate in candidates)


def _identity_hash(parts: list[str]) -> str:
    raw = "\n".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def describe_checkout(
    project_root: str | Path,
    *,
    project_id: str = "",
    commit_sha: str = "",
) -> dict[str, Any]:
    """Describe the checkout used to materialize graph state.

    ``execution_root`` is intentionally identified as the local root that ran
    the scan. The stable ``canonical_project_identity`` is separate so linked
    or temporary worktrees can be audited without pretending their filesystem
    path is the project identity.
    """
    root = Path(project_root).resolve()
    commit = commit_sha or _git_output(root, ["rev-parse", "--verify", "HEAD"])
    top = _git_output(root, ["rev-parse", "--show-toplevel"])
    common_dir = _git_output(root, ["rev-parse", "--git-common-dir"])
    git_dir = _git_output(root, ["rev-parse", "--git-dir"])
    remote_url = _git_output(root, ["config", "--get", "remote.origin.url"])
    is_git = bool(top)

    top_path = Path(top).resolve() if top else root
    common_abs = _resolve_under(root, common_dir)
    git_dir_abs = _resolve_under(root, git_dir)
    is_linked = bool(is_git and common_abs and not _is_under(Path(common_abs), top_path))
    ephemeral = _is_ephemeral_root(root)
    warnings: list[dict[str, str]] = []
    if ephemeral:
        warnings.append({
            "code": "ephemeral_execution_root",
            "message": "graph snapshot was materialized from a temporary execution root",
        })
    if is_linked:
        warnings.append({
            "code": "linked_worktree_execution_root",
            "message": "graph snapshot was materialized from a linked git worktree",
        })

    identity_parts = [project_id, remote_url, common_abs, top_path.name]
    return {
        "project_id": project_id,
        "execution_root": str(root),
        "execution_root_role": "execution_root",
        "execution_root_is_ephemeral": ephemeral,
        "commit_sha": commit,
        "is_git_worktree": is_git,
        "git": {
            "worktree_root": str(top_path) if is_git else "",
            "git_dir": git_dir_abs,
            "git_common_dir": common_abs,
            "is_linked_worktree": is_linked,
            "remote_url": remote_url,
        },
        "canonical_project_identity": {
            "type": "git" if is_git else "path",
            "project_id": project_id,
            "remote_url": remote_url,
            "commit_sha": commit,
            "identity_hash": _identity_hash(identity_parts),
        },
        "warnings": warnings,
    }


__all__ = ["describe_checkout"]
