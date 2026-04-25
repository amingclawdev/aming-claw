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
        """Load node state rows from DB for graph nodes (single batch query).

        Returns a dict mapping node_id → row dict with at least
        verify_status, updated_at fields.

        Uses all_db_node_state for a single batch query instead of N+1
        individual queries per graph node.
        """
        try:
            graph = self.graph
            if graph is None:
                return {}
            all_db = self.all_db_node_state
            graph_nodes = set(graph.list_nodes())
            return {nid: row for nid, row in all_db.items() if nid in graph_nodes}
        except Exception as exc:
            log.warning("Failed to load node_state: %s", exc)
            return {}

    @cached_property
    def all_db_node_state(self) -> Dict[str, dict]:
        """Load ALL node_state rows from DB, including orphans not in graph.

        Returns a dict mapping node_id → {verify_status, build_status, updated_by, updated_at}.
        """
        try:
            from ..db import get_connection
            import sqlite3 as _sqlite3
            conn = get_connection(self.project_id)
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                "SELECT node_id, verify_status, build_status, updated_by, updated_at "
                "FROM node_state WHERE project_id = ?",
                (self.project_id,),
            ).fetchall()
            return {r["node_id"]: dict(r) for r in rows}
        except Exception as exc:
            log.warning("Failed to load all_db_node_state: %s", exc)
            return {}

    @cached_property
    def graph_db_delta(self) -> Dict[str, Any]:
        """Compute graph vs DB node count delta.

        Returns summary dict with graph_count, db_count, orphan_db_count,
        missing_db_count (graph nodes with no DB record), and stuck_testing count.
        """
        graph = self.graph
        all_db = self.all_db_node_state
        graph_ids = set(graph.list_nodes()) if graph else set()
        db_ids = set(all_db.keys())

        orphan_db = sorted(db_ids - graph_ids)
        missing_db = sorted(graph_ids - db_ids)
        stuck = [
            nid for nid, row in all_db.items()
            if row.get("verify_status") == "testing"
        ]

        return {
            "graph_count": len(graph_ids),
            "db_count": len(db_ids),
            "orphan_db_count": len(orphan_db),
            "orphan_db_ids": orphan_db,
            "missing_db_count": len(missing_db),
            "missing_db_ids": missing_db,
            "stuck_testing_count": len(stuck),
            "stuck_testing_ids": stuck,
        }

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
