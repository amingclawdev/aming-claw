"""reconcile_phases — pluggable phase interface for comprehensive reconcile.

PR1: Phase A adapter + Context loader + Discrepancy/PhaseBase types.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from . import phase_a
from . import phase_b
from . import phase_c
from . import phase_d
from . import phase_e
from . import phase_z
from . import aggregator
from . import orchestrator
from .context import ReconcileContext


@dataclass
class Discrepancy:
    """Single reconcile finding produced by a phase."""
    type: str                    # e.g. 'stale_ref', 'orphan_node', 'unmapped_file'
    node_id: Optional[str]       # affected node, or None for file-level issues
    field: Optional[str]         # 'primary'/'secondary'/'test' or None
    detail: str                  # human-readable description
    confidence: str              # 'high' | 'medium' | 'low'


class PhaseBase(ABC):
    """Abstract base for reconcile phases."""

    @abstractmethod
    def run(self, ctx: ReconcileContext) -> List[Discrepancy]:
        """Execute this phase and return findings."""
        ...


__all__ = [
    "Discrepancy", "PhaseBase", "ReconcileContext",
    "phase_a", "phase_b", "phase_c", "phase_d", "phase_e", "phase_z",
    "aggregator", "orchestrator",
]
