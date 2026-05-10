"""LanguageAdapter Protocol — pluggable per-language analysis hook.

CR1 R1: Defines the abstract surface the cluster_grouper relies on for
language-aware similarity scoring.  Implementations live alongside in
``python_adapter.py`` (Python, AST-based) and ``filetree_adapter.py``
(conservative, language-agnostic fallback).

The Protocol keeps the legacy cluster-grouper hooks while exposing optional
graph-construction hooks:
    supports(file_path)          -- True if this adapter can analyse the file
    language()                   -- stable language key, or "" for fallback
    classify_file(file_path)     -- file kind/language metadata
    collect_decorators(node)     -- decorator names from an AST node
    find_module_root(file_path)  -- closest non-__init__ package boundary
    detect_test_pairing(source)  -- mapped test file path, or None
    find_test_pairing(source)    -- alias-style richer pairing hook
    parse_symbols(...)           -- adapter-native symbol facts
    parse_imports(...)           -- adapter-native import facts
    extract_relations(...)       -- typed language relations, if available

Adapters MUST be import-safe (no I/O at import time) and stateless.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class LanguageAdapter(Protocol):
    """Protocol describing the per-language analysis surface (CR1 R1)."""

    def supports(self, file_path: str) -> bool:
        """Return True if this adapter can analyse *file_path*."""
        ...

    def language(self) -> str:
        """Return a stable language key for this adapter, or ``""`` for fallback."""
        ...

    def classify_file(self, file_path: str) -> Dict[str, Any]:
        """Return lightweight file metadata for graph and inventory callers."""
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

    def find_test_pairing(self, source_file: str) -> Optional[str]:
        """Return the inferred test file path for *source_file*, or None."""
        ...

    def parse_symbols(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        """Return adapter-native symbol facts for *file_path*."""
        ...

    def parse_imports(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        """Return adapter-native import facts for *file_path*."""
        ...

    def extract_relations(
        self,
        file_path: str,
        source: str = "",
        *,
        symbols: Optional[List[Dict[str, Any]]] = None,
        imports: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Return adapter-native typed relations, or ``[]`` when unsupported."""
        ...


__all__ = ["LanguageAdapter"]
