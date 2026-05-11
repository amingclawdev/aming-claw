"""Shared language and file-role policy for graph reconciliation."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


def _lowered(values: Iterable[str]) -> frozenset[str]:
    return frozenset(str(value).lower() for value in values if str(value))


@dataclass(frozen=True)
class LanguagePolicy:
    """Single source for source, test, config, doc, generated, and ignore rules."""

    source_extensions: frozenset[str] = frozenset({
        ".py", ".pyi",
        # Node.js variants: .js (default), .jsx (React), .mjs (ESM),
        # .cjs (CommonJS). All four are production source per ESM spec.
        # Adding .mjs/.cjs lets reconcile_file_inventory + graph adapter
        # discovery pick up node scripts that ship under frontend/.
        ".js", ".jsx", ".mjs", ".cjs",
        ".ts", ".tsx",
        ".go", ".rs",
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    })
    python_extensions: frozenset[str] = frozenset({".py", ".pyi"})
    test_dir_names: frozenset[str] = frozenset({"test", "tests", "__tests__"})
    doc_dir_names: frozenset[str] = frozenset({"doc", "docs", "documentation"})
    exclude_roots: frozenset[str] = frozenset({
        "__pycache__", ".git", "node_modules", ".venv", "venv", ".tox",
        ".aming-claw", ".claude", ".worktrees", "shared-volume", "runtime",
        ".mypy_cache", ".pytest_cache", ".observer-cache", ".governance-cache",
        "build", "dist", "target", "coverage", ".next", ".nuxt", ".eggs",
        "search-workspace",
    })
    manifest_language_hints: dict[str, str] = field(default_factory=lambda: {
        "pyproject.toml": "python",
        "setup.py": "python",
        "requirements.txt": "python",
        "package.json": "javascript",
        "tsconfig.json": "typescript",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "CMakeLists.txt": "cpp",
        "compile_commands.json": "cpp",
    })
    extension_languages: dict[str, str] = field(default_factory=lambda: {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".c": "cpp",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".h": "cpp",
        ".hpp": "cpp",
        ".sh": "shell",
        ".bash": "shell",
        ".ps1": "powershell",
        ".bat": "batch",
        ".cmd": "batch",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".toml": "toml",
        ".ini": "ini",
        ".cfg": "ini",
        ".md": "markdown",
        ".rst": "text",
        ".txt": "text",
        ".adoc": "text",
    })
    config_filenames: frozenset[str] = frozenset({
        ".env", ".env.example", "Dockerfile", "Makefile", "Pipfile",
        "pyproject.toml", "requirements.txt", "package.json", "tsconfig.json",
        "Cargo.toml", "go.mod", "CMakeLists.txt", "compile_commands.json",
        ".gitignore", ".mcp.json", "VERSION", "pipeline_config.yaml.example",
    })
    config_extensions: frozenset[str] = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"})
    script_extensions: frozenset[str] = frozenset({".sh", ".bash", ".ps1", ".bat", ".cmd"})
    doc_extensions: frozenset[str] = frozenset({".md", ".rst", ".txt", ".adoc"})
    index_doc_filenames: frozenset[str] = frozenset({
        "README.md", "WORKFLOW.md", "CONTRIBUTING.md", "CHANGELOG.md",
    })
    generated_filenames: frozenset[str] = frozenset({
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
        "Cargo.lock", ".coverage", "governance.db",
    })
    generated_extensions: frozenset[str] = frozenset({".log", ".db", ".sqlite", ".sqlite3", ".pyc"})
    generated_dir_markers: frozenset[str] = frozenset({"generated", "__generated__", "gen"})
    generated_dir_suffixes: tuple[str, ...] = (".egg-info",)
    generated_path_prefixes: tuple[str, ...] = ("docs/dev/scratch/", "docs/dev/observer/logs/")
    test_support_filenames: frozenset[str] = frozenset({"__init__.py", "conftest.py"})
    test_support_dirs: frozenset[str] = frozenset({
        "fixtures", "fixture", "testdata", "test_data", "snapshots", "__snapshots__",
    })

    def normalize_relpath(self, project_root: str, path: str) -> str:
        raw = str(path or "")
        try:
            if project_root and os.path.isabs(raw):
                raw = os.path.relpath(raw, project_root)
        except ValueError:
            pass
        rel = raw.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        return rel.strip("/")

    def is_under_any(self, rel_path: str, roots: Iterable[str]) -> bool:
        norm = str(rel_path or "").replace("\\", "/").strip("/")
        for root in roots or []:
            base = str(root or "").replace("\\", "/").strip("/")
            if not base:
                continue
            if norm == base or norm.startswith(base + "/"):
                return True
        return False

    def is_excluded_path(self, rel_path: str, extra_roots: Iterable[str] = ()) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        parts = _lowered(rel.split("/"))
        excludes = _lowered(self.exclude_roots) | _lowered(extra_roots)
        if parts & excludes:
            return True
        return self.is_under_any(rel, extra_roots)

    def is_doc_path(self, rel_path: str, doc_roots: Iterable[str] = ()) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        parts = _lowered(rel.split("/"))
        if parts & _lowered(self.doc_dir_names):
            return True
        return self.is_under_any(rel, doc_roots)

    def is_test_path(self, rel_path: str, test_roots: Iterable[str] = ()) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        parts = [part.lower() for part in rel.split("/") if part]
        name = parts[-1] if parts else ""
        if set(parts) & set(self.test_dir_names):
            return True
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
        if ".test." in name or ".spec." in name:
            return True
        return self.is_under_any(rel, test_roots)

    def is_source_path(self, rel_path: str) -> bool:
        return Path(str(rel_path or "")).suffix.lower() in self.source_extensions

    def is_production_source_path(
        self,
        rel_path: str,
        *,
        test_roots: Iterable[str] = (),
        doc_roots: Iterable[str] = (),
        exclude_roots: Iterable[str] = (),
    ) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        return (
            self.is_source_path(rel)
            and not self.is_config_path(rel)
            and not self.is_generated_path(rel)
            and not self.is_excluded_path(rel, exclude_roots)
            and not self.is_test_path(rel, test_roots)
            and not self.is_doc_path(rel, doc_roots)
        )

    def manifest_language(self, rel_path: str) -> str:
        return self.manifest_language_hints.get(Path(str(rel_path or "")).name, "")

    def language_for_path(self, rel_path: str, kind: str = "") -> str:
        suffix = Path(str(rel_path or "")).suffix.lower()
        language = self.extension_languages.get(suffix, "")
        if language:
            return language
        if kind in {"doc", "index_doc"}:
            return "text"
        return ""

    def is_generated_path(self, rel_path: str) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        name = Path(rel).name
        suffix = Path(rel).suffix.lower()
        parts = {part.lower() for part in rel.split("/") if part}
        return (
            name in self.generated_filenames
            or suffix in self.generated_extensions
            or bool(parts & set(self.generated_dir_markers))
            or any(part.endswith(self.generated_dir_suffixes) for part in parts)
            or any(rel.startswith(prefix) for prefix in self.generated_path_prefixes)
        )

    def is_config_path(self, rel_path: str) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        name = Path(rel).name
        lower_name = name.lower()
        suffix = Path(rel).suffix.lower()
        frontend_config_prefixes = (
            "vite.config.",
            "vitest.config.",
            "jest.config.",
            "eslint.config.",
            "prettier.config.",
            "next.config.",
            "webpack.config.",
            "rollup.config.",
        )
        return (
            name in self.config_filenames
            or name.startswith("Dockerfile")
            or suffix in self.config_extensions
            or lower_name in {".eslintrc", ".prettierrc"}
            or lower_name.startswith((".eslintrc.", ".prettierrc."))
            or lower_name.startswith(frontend_config_prefixes)
        )

    def is_script_path(self, rel_path: str) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        suffix = Path(rel).suffix.lower()
        return suffix in self.script_extensions or rel.startswith("scripts/")

    def is_index_doc_path(self, rel_path: str) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        name = Path(rel).name
        lower_name = name.lower()
        return name in self.index_doc_filenames or lower_name in {"readme.md", "index.md"}

    def is_test_support_path(self, rel_path: str) -> bool:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        name = Path(rel).name
        parts = {part.lower() for part in rel.split("/") if part}
        return name in self.test_support_filenames or bool(parts & set(self.test_support_dirs))

    def strip_source_suffix(self, rel_path: str) -> str:
        rel = str(rel_path or "").replace("\\", "/").strip("/")
        for suffix in sorted(self.source_extensions, key=len, reverse=True):
            if rel.lower().endswith(suffix):
                return rel[: -len(suffix)]
        return rel


DEFAULT_LANGUAGE_POLICY = LanguagePolicy()

__all__ = ["DEFAULT_LANGUAGE_POLICY", "LanguagePolicy"]
