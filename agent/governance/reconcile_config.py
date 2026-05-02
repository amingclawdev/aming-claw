"""Reconcile-cluster tuning constants (CR1 R5).

All constants are overridable via environment variables so the chain
operator can tweak behaviour without a code change:

    CLUSTER_SIGNAL_WEIGHT_S{1..5}   -- per-signal weight (must sum to 1.0)
    CLUSTER_THRESHOLD               -- pair similarity merge threshold
    RECONCILE_CLUSTER_SIZE_CAP      -- maximum nodes per cluster
    RECONCILE_FEATURE_CLUSTER_FILE_CAP -- max files per synthesized cluster
    BOOTSTRAP_THRESHOLD             -- bootstrap detector cluster floor

The defaults below were chosen to produce non-trivial clusters on the
aming-claw codebase while keeping unrelated nodes apart (AC7/AC8).
"""
from __future__ import annotations

import os
from typing import Dict


def _env_float(name: str, default: float) -> float:
    """Read *name* from os.environ as float; fall back to *default* on error."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Signal weights (must sum to 1.0)
# ---------------------------------------------------------------------------
CLUSTER_SIGNAL_WEIGHTS: Dict[str, float] = {
    "S1": _env_float("CLUSTER_SIGNAL_WEIGHT_S1", 0.40),  # DFS descendants overlap
    "S2": _env_float("CLUSTER_SIGNAL_WEIGHT_S2", 0.20),  # same module / package root
    "S3": _env_float("CLUSTER_SIGNAL_WEIGHT_S3", 0.15),  # graph dependency overlap
    "S4": _env_float("CLUSTER_SIGNAL_WEIGHT_S4", 0.15),  # test/doc proximity
    "S5": _env_float("CLUSTER_SIGNAL_WEIGHT_S5", 0.10),  # decorator overlap
}

# ---------------------------------------------------------------------------
# Cluster controls
# ---------------------------------------------------------------------------
CLUSTER_THRESHOLD: float = _env_float("CLUSTER_THRESHOLD", 0.50)
RECONCILE_CLUSTER_SIZE_CAP: int = _env_int("RECONCILE_CLUSTER_SIZE_CAP", 20)
RECONCILE_FEATURE_CLUSTER_FILE_CAP: int = _env_int("RECONCILE_FEATURE_CLUSTER_FILE_CAP", 6)
BOOTSTRAP_THRESHOLD: int = _env_int("BOOTSTRAP_THRESHOLD", 10)


__all__ = [
    "CLUSTER_SIGNAL_WEIGHTS",
    "CLUSTER_THRESHOLD",
    "RECONCILE_CLUSTER_SIZE_CAP",
    "RECONCILE_FEATURE_CLUSTER_FILE_CAP",
    "BOOTSTRAP_THRESHOLD",
]
