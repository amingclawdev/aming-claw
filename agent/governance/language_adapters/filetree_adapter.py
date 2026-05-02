"""FileTreeAdapter — language-agnostic conservative fallback (CR1 R3).

Used when no language-specific adapter ``supports()`` the file under
analysis (e.g. ``.js``, ``.go``, ``.rs``, ``.unknown``).  Every method
returns the cheapest plausible answer so the cluster_grouper can still
operate without crashing on heterogeneous input.
"""
from __future__ import annotations

import os
from typing import Any, List, Optional


class FileTreeAdapter:
    """Conservative fallback implementation of :class:`LanguageAdapter`."""

    def supports(self, file_path: str) -> bool:
        """Always returns True — the fallback supports any non-empty path."""
        return bool(file_path)

    def collect_decorators(self, ast_node: Any) -> List[str]:
        """The filetree fallback knows nothing about AST decorators."""
        return []

    def find_module_root(self, file_path: str) -> str:
        """Module root degenerates to the file's parent directory."""
        if not file_path:
            return ""
        normalised = file_path.replace("\\", "/")
        return os.path.dirname(normalised)

    def detect_test_pairing(self, source_file: str) -> Optional[str]:
        """No conventional test pairing without language semantics."""
        return None


__all__ = ["FileTreeAdapter"]
