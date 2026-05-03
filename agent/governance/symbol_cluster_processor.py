"""Compatibility entrypoint for Phase Z v2 symbol cluster enrichment."""
from __future__ import annotations

from agent.governance.ai_cluster_processor import (  # noqa: F401
    ClusterReport,
    process_cluster_with_ai,
)

__all__ = ["ClusterReport", "process_cluster_with_ai"]
