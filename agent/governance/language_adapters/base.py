"""LanguageAdapter Protocol — pluggable per-language analysis hook.

CR1 R1: Defines the abstract surface the cluster_grouper relies on for
language-aware similarity scoring.  Implementations live alongside in
``python_adapter.py`` (Python, AST-based) and ``filetree_adapter.py``
(conservative, language-agnostic fallback).

The Protocol is intentionally narrow:
    supports(file_path)         -- True if this adapter can analyse the file
    collect_decorators(node)    -- decorator names from an AST node
    find_module_root(file_path) -- closest non-__init__ package boundary
    detect_test_pairing(source) -- mapped test file path, or None

Adapters MUST be import-safe (no I/O at import time) and stateless.
"""
from __future__ import annotations

from typing import Any, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class LanguageAdapter(Protocol):
    """Protocol describing the per-language analysis surface (CR1 R1)."""

    def supports(self, file_path: str) -> bool:
        """Return True if this adapter can analyse *file_path*."""
        ...

    def collect_decorators(self, ast_node: Any) -> List[str]:
        """Return the list of decorator names attached to *ast_node*.

        Implementations should tolerate non-AST inputs by returning ``[]``.
        """
        ...

    def find_module_root(self, file_path: str) -> str:
        """Return the closest module-root directory for *file_path*.

        For Python: walks up to the first non-``__init__`` package boundary.
        For the filetree fallback: returns ``os.path.dirname(file_path)``.
        """
        ...

    def detect_test_pairing(self, source_file: str) -> Optional[str]:
        """Return the inferred test file path for *source_file*, or None."""
        ...


__all__ = ["LanguageAdapter"]
