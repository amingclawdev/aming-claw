"""Shared dirty-worktree filtering for governance gates."""
from __future__ import annotations


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
    ".governance-cache/", ".governance-cache\\",
    ".observer-cache/", ".observer-cache\\",
    ".aming-claw/cache/", ".aming-claw\\cache\\",
)


def normalize_dirty_path(path: str) -> str:
    text = str(path or "").strip()
    if " -> " in text:
        text = text.rsplit(" -> ", 1)[1].strip()
    return text.replace("\\", "/").strip("/")


def is_ignored_dirty_path(path: str) -> bool:
    normalized = normalize_dirty_path(path)
    if not normalized:
        return False
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
