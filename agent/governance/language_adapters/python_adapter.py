"""PythonAdapter — AST-backed implementation of LanguageAdapter (CR1 R2).

Uses the standard-library ``ast`` module for decorator extraction and a
filesystem walk for ``find_module_root``.  Test pairing follows the
conventional ``foo.py`` → ``test_foo.py`` mapping.

The adapter is stateless and side-effect free; it never reads files
during attribute access.
"""
from __future__ import annotations

import ast
import os
from typing import Any, Dict, List, Optional

from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY


class PythonAdapter:
    """Python-specific implementation of the :class:`LanguageAdapter` Protocol."""

    # ------------------------------------------------------------------
    # supports
    # ------------------------------------------------------------------
    def supports(self, file_path: str) -> bool:
        """Return True for ``.py`` (and ``.pyi``) source files."""
        if not file_path:
            return False
        lower = file_path.lower()
        return any(lower.endswith(ext) for ext in DEFAULT_LANGUAGE_POLICY.python_extensions)

    def language(self) -> str:
        """Return the stable policy language key for Python files."""
        return "python"

    def classify_file(self, file_path: str) -> Dict[str, Any]:
        """Return lightweight policy metadata for *file_path*."""
        language = DEFAULT_LANGUAGE_POLICY.language_for_path(file_path)
        return {
            "file_kind": "source" if self.supports(file_path) else "",
            "language": language,
            "adapter": "python",
        }

    # ------------------------------------------------------------------
    # collect_decorators
    # ------------------------------------------------------------------
    def collect_decorators(self, ast_node: Any) -> List[str]:
        """Extract decorator names from an AST FunctionDef/AsyncFunctionDef/ClassDef.

        Handles the three decorator shapes encountered in practice:
        - ``@route``                  → ``ast.Name``         → ``"route"``
        - ``@app.route``              → ``ast.Attribute``    → ``"app.route"``
        - ``@route("/x")``            → ``ast.Call``         → name of ``func``
        Returns ``[]`` for anything that lacks a ``decorator_list`` attribute.
        """
        if ast_node is None:
            return []
        decorators = getattr(ast_node, "decorator_list", None)
        if not decorators:
            return []
        names: List[str] = []
        for dec in decorators:
            name = self._decorator_name(dec)
            if name:
                names.append(name)
        return names

    @staticmethod
    def _decorator_name(dec: Any) -> str:
        """Best-effort textual rendering of a decorator AST node."""
        # @name
        if isinstance(dec, ast.Name):
            return dec.id
        # @pkg.name (or deeper attribute chains)
        if isinstance(dec, ast.Attribute):
            parts: List[str] = []
            cur: Any = dec
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            return ".".join(reversed(parts)) if parts else ""
        # @name(...) — recurse into the called node
        if isinstance(dec, ast.Call):
            return PythonAdapter._decorator_name(dec.func)
        return ""

    # ------------------------------------------------------------------
    # find_module_root
    # ------------------------------------------------------------------
    def find_module_root(self, file_path: str) -> str:
        """Walk up to the first non-``__init__`` package boundary.

        Concretely: starting from ``dirname(file_path)``, climb until
        ``__init__.py`` is no longer present in the candidate directory
        (or the filesystem root is reached).  When the file is not under
        any ``__init__`` package, the immediate parent directory is
        returned — matching the filetree fallback behaviour.
        """
        if not file_path:
            return ""
        # Normalise to forward slashes for portability inside the function.
        normalised = file_path.replace("\\", "/")
        directory = os.path.dirname(normalised)
        if not directory:
            return ""

        # Climb while parent has an __init__.py (i.e. we're still inside a package).
        current = directory
        while True:
            init_marker = os.path.join(current, "__init__.py")
            parent = os.path.dirname(current)
            # Stop at filesystem root.
            if not parent or parent == current:
                return current
            # If current has __init__ but parent does NOT, current IS the root.
            parent_init = os.path.join(parent, "__init__.py")
            if os.path.exists(init_marker) and not os.path.exists(parent_init):
                return current
            # If current itself does not have __init__, it is the boundary.
            if not os.path.exists(init_marker):
                return current
            current = parent

    # ------------------------------------------------------------------
    # detect_test_pairing
    # ------------------------------------------------------------------
    def detect_test_pairing(self, source_file: str) -> Optional[str]:
        """Map ``foo.py`` → conventional ``tests/test_foo.py`` location.

        Returns ``None`` if *source_file* is itself a test, is empty, or
        is not a Python source file.  The returned path is **conventional**
        — callers should treat it as a hint and verify existence before
        relying on it.
        """
        if not source_file:
            return None
        normalised = source_file.replace("\\", "/")
        base = os.path.basename(normalised)
        if not base.endswith(".py"):
            return None
        # Already a test file — nothing to pair.
        if base.startswith("test_") or base.endswith("_test.py"):
            return None
        return f"tests/test_{base}"

    def find_test_pairing(self, source_file: str) -> Optional[str]:
        """Compatibility alias for graph builders that use richer adapter hooks."""
        return self.detect_test_pairing(source_file)

    def parse_symbols(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        """Parse Python classes/functions into neutral symbol dictionaries."""
        try:
            tree = ast.parse(source or "", filename=file_path or "<unknown>")
        except (SyntaxError, ValueError):
            return []
        symbols: List[Dict[str, Any]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append({
                    "name": node.name,
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                    "lineno": getattr(node, "lineno", 0),
                    "end_lineno": getattr(node, "end_lineno", getattr(node, "lineno", 0)),
                    "decorators": self.collect_decorators(node),
                })
        return symbols

    def parse_imports(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        """Parse Python imports into neutral import dictionaries."""
        try:
            tree = ast.parse(source or "", filename=file_path or "<unknown>")
        except (SyntaxError, ValueError):
            return []
        imports: List[Dict[str, Any]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({
                        "local": alias.asname or alias.name,
                        "imported": alias.name,
                        "kind": "import",
                    })
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imported = f"{module}.{alias.name}" if module else alias.name
                    imports.append({
                        "local": alias.asname or alias.name,
                        "imported": imported,
                        "kind": "from_import",
                    })
        return imports

    def extract_relations(
        self,
        file_path: str,
        source: str = "",
        *,
        symbols: Optional[List[Dict[str, Any]]] = None,
        imports: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Python typed relations are extracted by Phase Z's richer AST pass."""
        return []


__all__ = ["PythonAdapter"]
