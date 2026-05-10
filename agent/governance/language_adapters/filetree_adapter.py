"""FileTreeAdapter — language-agnostic conservative fallback (CR1 R3).

Used when no language-specific adapter ``supports()`` the file under
analysis (e.g. ``.js``, ``.go``, ``.rs``, ``.unknown``).  Every method
returns the cheapest plausible answer so the cluster_grouper can still
operate without crashing on heterogeneous input.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY


class FileTreeAdapter:
    """Conservative fallback implementation of :class:`LanguageAdapter`."""

    def supports(self, file_path: str) -> bool:
        """Always returns True — the fallback supports any non-empty path."""
        return bool(file_path)

    def language(self) -> str:
        """The fallback derives language from the shared policy per file."""
        return ""

    def classify_file(self, file_path: str) -> Dict[str, Any]:
        """Return lightweight policy metadata for file-tree fallback nodes."""
        language = DEFAULT_LANGUAGE_POLICY.language_for_path(file_path)
        return {
            "file_kind": "source" if DEFAULT_LANGUAGE_POLICY.is_source_path(file_path) else "",
            "language": language,
            "adapter": "filetree",
        }

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

    def find_test_pairing(self, source_file: str) -> Optional[str]:
        """No conventional test pairing without language semantics."""
        return None

    def parse_symbols(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        """The fallback intentionally emits no symbols."""
        return []

    def parse_imports(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        """The fallback intentionally emits no imports."""
        return []

    def extract_relations(
        self,
        file_path: str,
        source: str = "",
        *,
        symbols: Optional[List[Dict[str, Any]]] = None,
        imports: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """The fallback intentionally emits no typed relations."""
        return []


__all__ = ["FileTreeAdapter"]
