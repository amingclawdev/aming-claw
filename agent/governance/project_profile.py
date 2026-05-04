"""Project profile discovery for reconcile bootstrap boundaries.

The profile is intentionally conservative: it discovers source, test, doc,
and excluded roots before symbol scanning so Phase Z can build a production
code graph while keeping tests/docs as downstream consumers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List


SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".cc",
    ".cpp", ".cxx", ".h", ".hpp",
}
PYTHON_EXTENSIONS = {".py", ".pyi"}
TEST_DIR_NAMES = {"test", "tests", "__tests__"}
DOC_DIR_NAMES = {"doc", "docs", "documentation"}
DEFAULT_EXCLUDE_ROOTS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv", ".tox",
    ".claude", ".worktrees", "shared-volume", "runtime", ".mypy_cache",
    ".pytest_cache", ".observer-cache", ".governance-cache", "build", "dist",
    "target", "coverage", ".next", ".nuxt", ".eggs",
}
MANIFEST_LANGUAGE_HINTS = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "package.json": "javascript",
    "tsconfig.json": "typescript",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "CMakeLists.txt": "cpp",
    "compile_commands.json": "cpp",
}


@dataclass(frozen=True)
class ProjectProfile:
    """Discovered source/test/doc boundaries for a project root."""

    project_root: str
    languages: List[str] = field(default_factory=list)
    source_roots: List[str] = field(default_factory=list)
    test_roots: List[str] = field(default_factory=list)
    doc_roots: List[str] = field(default_factory=list)
    exclude_roots: List[str] = field(default_factory=list)
    manifest_files: List[str] = field(default_factory=list)

    def normalize_relpath(self, path: str) -> str:
        raw = str(path or "")
        try:
            if os.path.isabs(raw):
                raw = os.path.relpath(raw, self.project_root)
        except ValueError:
            pass
        return raw.replace("\\", "/").strip("/")

    def is_excluded_path(self, path: str) -> bool:
        rel = self.normalize_relpath(path)
        parts = [p for p in rel.split("/") if p]
        if any(part in DEFAULT_EXCLUDE_ROOTS for part in parts):
            return True
        return _is_under_any(rel, self.exclude_roots)

    def is_doc_path(self, path: str) -> bool:
        rel = self.normalize_relpath(path)
        parts = [p for p in rel.split("/") if p]
        if any(part.lower() in DOC_DIR_NAMES for part in parts):
            return True
        return _is_under_any(rel, self.doc_roots)

    def is_test_path(self, path: str) -> bool:
        rel = self.normalize_relpath(path)
        parts = [p.lower() for p in rel.split("/") if p]
        name = parts[-1] if parts else ""
        if any(part in TEST_DIR_NAMES for part in parts):
            return True
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
        if ".test." in name or ".spec." in name:
            return True
        return _is_under_any(rel, self.test_roots)

    def is_production_source_path(self, path: str) -> bool:
        rel = self.normalize_relpath(path)
        suffix = Path(rel).suffix.lower()
        return (
            suffix in SOURCE_EXTENSIONS
            and not self.is_excluded_path(rel)
            and not self.is_test_path(rel)
            and not self.is_doc_path(rel)
        )


def discover_project_profile(project_root: str) -> ProjectProfile:
    """Discover a minimal language/profile boundary map for *project_root*."""
    root = Path(project_root).resolve()
    manifests = _discover_manifests(root)
    languages = _discover_languages(root, manifests)
    test_roots = _discover_named_dirs(root, TEST_DIR_NAMES)
    doc_roots = _discover_named_dirs(root, DOC_DIR_NAMES)
    exclude_roots = _discover_existing_excludes(root)
    source_roots = _discover_source_roots(root, test_roots, doc_roots, exclude_roots)

    if not source_roots:
        source_roots = ["."]

    return ProjectProfile(
        project_root=str(root),
        languages=languages,
        source_roots=source_roots,
        test_roots=test_roots,
        doc_roots=doc_roots,
        exclude_roots=exclude_roots,
        manifest_files=manifests,
    )


def _discover_manifests(root: Path) -> List[str]:
    found = []
    for path in _iter_files(root):
        if path.name in MANIFEST_LANGUAGE_HINTS:
            found.append(_rel(root, path))
    return sorted(found)


def _discover_languages(root: Path, manifests: Iterable[str]) -> List[str]:
    langs = {
        MANIFEST_LANGUAGE_HINTS[Path(name).name]
        for name in manifests
        if Path(name).name in MANIFEST_LANGUAGE_HINTS
    }
    for path in _iter_files(root):
        suffix = path.suffix.lower()
        if suffix in PYTHON_EXTENSIONS:
            langs.add("python")
        elif suffix in {".js", ".jsx"}:
            langs.add("javascript")
        elif suffix in {".ts", ".tsx"}:
            langs.add("typescript")
        elif suffix == ".go":
            langs.add("go")
        elif suffix == ".rs":
            langs.add("rust")
        elif suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
            langs.add("cpp")
    return sorted(langs)


def _discover_named_dirs(root: Path, names: set[str]) -> List[str]:
    found = set()
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_ROOTS]
        for dirname in list(dirnames):
            if dirname.lower() in names:
                found.add(_rel(root, Path(dirpath) / dirname))
    return sorted(found)


def _discover_existing_excludes(root: Path) -> List[str]:
    found = set()
    for name in DEFAULT_EXCLUDE_ROOTS:
        if (root / name).exists():
            found.add(name)
    return sorted(found)


def _discover_source_roots(
    root: Path,
    test_roots: List[str],
    doc_roots: List[str],
    exclude_roots: List[str],
) -> List[str]:
    roots = set()
    if _contains_root_source_file(root):
        roots.add(".")
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        rel = _rel(root, child)
        lower = child.name.lower()
        if lower in TEST_DIR_NAMES or lower in DOC_DIR_NAMES:
            continue
        if _is_under_any(rel, test_roots) or _is_under_any(rel, doc_roots):
            continue
        if child.name in DEFAULT_EXCLUDE_ROOTS or _is_under_any(rel, exclude_roots):
            continue
        if _contains_source_file(child, root):
            roots.add(rel)
    return sorted(roots)


def _contains_root_source_file(root: Path) -> bool:
    for child in root.iterdir():
        if not child.is_file():
            continue
        if child.name in DEFAULT_EXCLUDE_ROOTS:
            continue
        if child.suffix.lower() in SOURCE_EXTENSIONS:
            return True
    return False


def _contains_source_file(path: Path, root: Path) -> bool:
    for file_path in _iter_files(path):
        rel = _rel(root, file_path)
        parts = [p.lower() for p in rel.split("/") if p]
        if any(part in TEST_DIR_NAMES or part in DOC_DIR_NAMES for part in parts):
            continue
        if file_path.suffix.lower() in SOURCE_EXTENSIONS:
            return True
    return False


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_ROOTS]
        for fname in filenames:
            yield Path(dirpath) / fname


def _rel(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root)).replace("\\", "/")


def _is_under_any(rel: str, roots: Iterable[str]) -> bool:
    norm = str(rel or "").replace("\\", "/").strip("/")
    for root in roots or []:
        base = str(root or "").replace("\\", "/").strip("/")
        if not base:
            continue
        if norm == base or norm.startswith(base + "/"):
            return True
    return False


__all__ = ["ProjectProfile", "discover_project_profile"]
