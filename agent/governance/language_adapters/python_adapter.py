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
from typing import Any, List, Optional


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
        return lower.endswith(".py") or lower.endswith(".pyi")

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


__all__ = ["PythonAdapter"]
