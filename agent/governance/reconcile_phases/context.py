"""ReconcileContext — shared, cached data loader for reconcile phases."""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from functools import cached_property
from typing import Any, Dict, Optional, Set, Tuple

from ..reconcile import phase_scan
from ..project_service import load_project_graph
from ..graph import AcceptanceGraph

log = logging.getLogger(__name__)


class ReconcileContext:
    """Immutable context shared across phases; heavy loads happen at most once."""

    def __init__(
        self,
        project_id: str,
        workspace_path: str,
        scan_depth: int = 3,
        exclude_patterns: Optional[list] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.project_id = project_id
        self.workspace_path = workspace_path
        self._scan_depth = scan_depth
        self._exclude_patterns = exclude_patterns
        self.options: Dict[str, Any] = options or {}

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

    @cached_property
    def node_state(self) -> Dict[str, dict]:
        """Load node state rows from state_service.

        Returns a dict mapping node_id → row dict with at least
        verify_status, updated_at fields.
        """
        try:
            from ..state_service import get_node_status, _get_conn
            conn = _get_conn(self.project_id)
            graph = self.graph
            if graph is None:
                return {}
            result: Dict[str, dict] = {}
            for node_id in graph.list_nodes():
                status = get_node_status(conn, self.project_id, node_id)
                if status:
                    result[node_id] = status
            return result
        except Exception as exc:
            log.warning("Failed to load node_state: %s", exc)
            return {}

    # -- git helpers ---------------------------------------------------------

    def git_log_per_file_last_commit_date(self, file_path: str) -> Optional[datetime]:
        """Get the last git commit date for a file.

        Runs ``git log -1 --format=%ct -- <file>`` from workspace_path,
        converts the unix timestamp to a timezone-aware datetime.
        Returns None if the file has no git history.
        """
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%ct", "--", file_path],
                cwd=self.workspace_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout.strip()
            if not output:
                return None
            timestamp = int(output)
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            log.debug("git log failed for %s: %s", file_path, exc)
            return None
