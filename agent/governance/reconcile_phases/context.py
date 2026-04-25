"""ReconcileContext — shared, cached data loader for reconcile phases."""
from __future__ import annotations

from functools import cached_property
from typing import Dict, Optional, Set, Tuple

from ..reconcile import phase_scan
from ..project_service import load_project_graph
from ..graph import AcceptanceGraph


class ReconcileContext:
    """Immutable context shared across phases; heavy loads happen at most once."""

    def __init__(
        self,
        project_id: str,
        workspace_path: str,
        scan_depth: int = 3,
        exclude_patterns: Optional[list] = None,
    ) -> None:
        self.project_id = project_id
        self.workspace_path = workspace_path
        self._scan_depth = scan_depth
        self._exclude_patterns = exclude_patterns

    # -- cached heavy loads --------------------------------------------------

    @cached_property
    def _scan_result(self) -> Tuple[Set[str], Dict[str, dict]]:
        return phase_scan(
            self.workspace_path, self._scan_depth, self._exclude_patterns,
        )

    @cached_property
    def file_set(self) -> Set[str]:
        return self._scan_result[0]

    @cached_property
    def file_metadata(self) -> Dict[str, dict]:
        return self._scan_result[1]

    @cached_property
    def graph(self) -> Optional[AcceptanceGraph]:
        return load_project_graph(self.project_id)
