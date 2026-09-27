"""Shared dirty-worktree filtering for governance gates."""
from __future__ import annotations

import os
from dataclasses import dataclass


# Prefixes filtered from dirty_files before governance gate evaluation.
#
# These paths are local tool state or generated smoke/build artifacts. They
# should not block chain/version/scope gates as long as they remain untracked.
DIRTY_IGNORE_PREFIXES = (
    ".claude/", ".claude\\",
    ".codex/", ".codex\\",
    ".hypothesis/", ".hypothesis\\",
    ".venv/", ".venv\\",
    ".worktrees/", ".worktrees\\",
    "build/", "build\\",
    "docs/dev/", "docs/dev\\",
    ".recent-tasks.json",
    ".aming-claw-demo-environment.json",
    ".governance-cache/", ".governance-cache\\",
    ".observer-cache/", ".observer-cache\\",
    ".aming-claw/cache/", ".aming-claw\\cache\\",
)

# Python writes bytecode beneath a directory component with this exact name.
# Tests can create these files after a worker has claimed its evidence envelope,
# when recursive cleanup is no longer a legal host-safe action.  Keep the match
# component-exact so arbitrary ``.pyc`` files and lookalike directories remain
# governed dirty paths.
GENERATED_DIR_COMPONENTS = frozenset({"__pycache__"})


def normalize_dirty_path(path: str) -> str:
    text = str(path or "").strip()
    if " -> " in text:
        text = text.rsplit(" -> ", 1)[1].strip()
    return text.replace("\\", "/").strip("/")


def is_ignored_dirty_path(path: str) -> bool:
    normalized = normalize_dirty_path(path)
    if not normalized:
        return False
    if any(
        component in GENERATED_DIR_COMPONENTS
        for component in normalized.split("/")
    ):
        return True
    for prefix in DIRTY_IGNORE_PREFIXES:
        clean_prefix = normalize_dirty_path(prefix)
        if normalized == clean_prefix or normalized.startswith(f"{clean_prefix}/"):
            return True
    return False


def filter_dirty_files(paths: list[str] | tuple[str, ...]) -> list[str]:
    return sorted({
        normalize_dirty_path(path)
        for path in paths
        if normalize_dirty_path(path) and not is_ignored_dirty_path(path)
    })


def parse_git_porcelain_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        if len(line) < 4 or line[2] != " ":
            continue
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path:
            paths.append(path)
    return paths


class CandidateGitStatusError(ValueError):
    """A candidate clean decision cannot be made from this Git status."""


@dataclass(frozen=True)
class CandidateDirtyStatus:
    dirty_files: tuple[str, ...]
    dirty_file_count: int
    dirty_files_truncated: bool
    ignored_untracked_count: int


def _candidate_untracked_cache_path(path: str) -> bool:
    """Apply generated-cache exemptions only to an actual untracked child.

    Porcelain -z names are literal: on POSIX, a backslash is a filename byte,
    not a path separator or Git quoting. Directory exemptions require a child,
    so a bare metadata parent can never be silently exempted.
    """
    if not path:
        return False
    canonical = path.replace("\\", "/") if os.sep == "\\" else path
    components = canonical.split("/")
    if (canonical.startswith("/") or any(
        component in {"", ".", ".."} for component in components
    )):
        return False
    if any(component in GENERATED_DIR_COMPONENTS for component in components[:-1]):
        return True
    for prefix in DIRTY_IGNORE_PREFIXES:
        if "\\" in prefix:
            continue
        if prefix.endswith("/"):
            if canonical.startswith(prefix) and len(canonical) > len(prefix):
                return True
        elif canonical == prefix:
            return True
    return False


def candidate_dirty_status_from_porcelain_z(
    output: bytes, *, diagnostic_limit: int = 50,
) -> CandidateDirtyStatus:
    """Classify *all* Git v1 -z entries before bounding public diagnostics.

    Rename/copy records contain destination then source as two NUL-delimited
    paths. Both are governed because they are tracked changes, including when
    either endpoint has a generated-cache-looking name.
    """
    if type(output) is not bytes or diagnostic_limit < 0:
        raise CandidateGitStatusError("candidate_git_status_malformed")
    if not output:
        return CandidateDirtyStatus((), 0, False, 0)
    if not output.endswith(b"\0"):
        raise CandidateGitStatusError("candidate_git_status_malformed")
    records = output.split(b"\0")[:-1]
    governed: set[str] = set()
    ignored = 0
    index = 0
    allowed = b" MADRCUT"
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" ":
            raise CandidateGitStatusError("candidate_git_status_malformed")
        status = record[:2]
        if status == b"??":
            untracked = True
        elif (status == b"  " or status == b"!!"
              or any(char not in allowed for char in status)):
            raise CandidateGitStatusError("candidate_git_status_malformed")
        else:
            untracked = False
        paths = [record[3:]]
        if not untracked and (b"R" in status or b"C" in status):
            if index >= len(records):
                raise CandidateGitStatusError("candidate_git_status_malformed")
            paths.append(records[index])
            index += 1
        for raw_path in paths:
            if (not raw_path or raw_path.startswith(b"/")
                    or any(part in {b"", b".", b".."}
                           for part in raw_path.split(b"/"))):
                raise CandidateGitStatusError("candidate_git_status_malformed")
            try:
                path = raw_path.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CandidateGitStatusError("candidate_git_status_malformed") from exc
            if untracked and _candidate_untracked_cache_path(path):
                ignored += 1
            else:
                governed.add(path)
    ordered = tuple(sorted(governed))
    return CandidateDirtyStatus(
        ordered[:diagnostic_limit], len(ordered),
        len(ordered) > diagnostic_limit, ignored,
    )
