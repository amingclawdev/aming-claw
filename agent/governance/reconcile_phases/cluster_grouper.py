"""Multi-signal delta cluster grouper (CR1 R6/R7).

Replaces the narrow ``group_into_epics`` heuristic with a 5-signal
weighted scoring + union-find pipeline:

    S1  DFS descendants overlap        weight 0.40
    S2  same module / package root     weight 0.20
    S3  graph dependency overlap       weight 0.15
    S4  test / doc proximity           weight 0.15
    S5  decorator overlap              weight 0.10

Pair similarity = sum(weight_i * signal_i).  Pairs above
``CLUSTER_THRESHOLD`` (default 0.5) are merged via union-find, so a
chain ``A↔B`` and ``B↔C`` collapses into a single cluster ``{A, B, C}``
even when ``A`` and ``C`` themselves score below threshold (AC11).

Public API:
    FeatureNode      -- minimal duck-typed input record
    ClusterGroup     -- output container with deterministic fingerprint
    group_deltas_by_cluster(feature_nodes, ...) -> List[ClusterGroup]
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Sequence, Set

from ..reconcile_config import CLUSTER_SIGNAL_WEIGHTS, CLUSTER_THRESHOLD

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class FeatureNode:
    """Minimal feature-node record consumed by the cluster grouper.

    The grouper relies only on attribute access, so callers are free to
    pass any duck-typed object exposing the same names; ``FeatureNode``
    is provided as the canonical concrete type for tests and the
    phase_z call site.
    """
    qname: str
    module: str = ""
    primary_files: List[str] = field(default_factory=list)
    secondary_files: List[str] = field(default_factory=list)
    descendants: Set[str] = field(default_factory=set)
    decorators: List[str] = field(default_factory=list)
    deps: List[str] = field(default_factory=list)


@dataclass
class ClusterGroup:
    """A merged group of feature nodes with a stable fingerprint.

    ``cluster_fingerprint`` is the first 16 hex chars of
    ``sha256(sorted_qnames | sorted_primary_files)`` — deterministic
    across runs (R8/AC9).
    """
    entries: List[FeatureNode]
    primary_files: List[str]
    secondary_files: List[str]
    cluster_fingerprint: str


# ---------------------------------------------------------------------------
# Internal: union-find
# ---------------------------------------------------------------------------


class _UnionFind:
    """Compact path-compression union-find over integer indices."""

    __slots__ = ("parent",)

    def __init__(self, n: int) -> None:
        self.parent: List[int] = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            # Deterministic merge: lower index becomes the root.
            if rx < ry:
                self.parent[ry] = rx
            else:
                self.parent[rx] = ry


# ---------------------------------------------------------------------------
# Internal: signal helpers
# ---------------------------------------------------------------------------


def _as_set(values: Iterable[str]) -> Set[str]:
    if not values:
        return set()
    return {v for v in values if v}


def _signal_s1_descendants(a: FeatureNode, b: FeatureNode) -> float:
    """Shared DFS descendants — saturates at 2 shared (per AC7)."""
    if not a.descendants or not b.descendants:
        return 0.0
    inter = a.descendants & b.descendants
    if not inter:
        return 0.0
    return min(1.0, len(inter) / 2.0)


def _signal_s2_module(a: FeatureNode, b: FeatureNode, adapter: Any) -> float:
    """Same module / package root."""
    if a.module and b.module and a.module == b.module:
        return 1.0
    if adapter is None or not a.primary_files or not b.primary_files:
        return 0.0
    try:
        ar = adapter.find_module_root(a.primary_files[0])
        br = adapter.find_module_root(b.primary_files[0])
    except Exception:
        return 0.0
    if ar and br and ar == br:
        return 1.0
    return 0.0


def _signal_s3_deps(a: FeatureNode, b: FeatureNode) -> float:
    """Jaccard overlap of declared graph dependencies."""
    sa = _as_set(a.deps)
    sb = _as_set(b.deps)
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    if not inter:
        return 0.0
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _signal_s4_proximity(a: FeatureNode, b: FeatureNode, adapter: Any) -> float:
    """Test / doc proximity — shared secondary files OR conventional test pair."""
    sa = _as_set(a.secondary_files)
    sb = _as_set(b.secondary_files)
    if sa & sb:
        return 1.0
    if adapter is None:
        return 0.0
    try:
        for pf in a.primary_files:
            paired = adapter.detect_test_pairing(pf)
            if paired and (paired in sb or paired in _as_set(b.primary_files)):
                return 1.0
        for pf in b.primary_files:
            paired = adapter.detect_test_pairing(pf)
            if paired and (paired in sa or paired in _as_set(a.primary_files)):
                return 1.0
    except Exception:
        return 0.0
    return 0.0


def _signal_s5_decorators(a: FeatureNode, b: FeatureNode) -> float:
    """Jaccard overlap of decorator names."""
    sa = _as_set(a.decorators)
    sb = _as_set(b.decorators)
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    if not inter:
        return 0.0
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _pair_similarity(a: FeatureNode, b: FeatureNode, adapter: Any) -> float:
    """Weighted sum of the 5 signals; clamped to [0, 1]."""
    w = CLUSTER_SIGNAL_WEIGHTS
    score = (
        w.get("S1", 0.0) * _signal_s1_descendants(a, b)
        + w.get("S2", 0.0) * _signal_s2_module(a, b, adapter)
        + w.get("S3", 0.0) * _signal_s3_deps(a, b)
        + w.get("S4", 0.0) * _signal_s4_proximity(a, b, adapter)
        + w.get("S5", 0.0) * _signal_s5_decorators(a, b)
    )
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


# ---------------------------------------------------------------------------
# Internal: adapter selection
# ---------------------------------------------------------------------------


def _resolve_adapter(language_adapter: Any, feature_nodes: Sequence[FeatureNode]) -> Any:
    """Pick PythonAdapter when most files look Python; otherwise FileTreeAdapter (AC13)."""
    if language_adapter is not None:
        return language_adapter

    # Local imports keep this module import-cheap.
    from ..language_adapters.filetree_adapter import FileTreeAdapter
    from ..language_adapters.python_adapter import PythonAdapter

    py = PythonAdapter()
    sample_files: List[str] = []
    for fn in feature_nodes:
        sample_files.extend(fn.primary_files or [])
        if len(sample_files) >= 8:
            break
    if sample_files and any(py.supports(f) for f in sample_files):
        return py
    return FileTreeAdapter()


# ---------------------------------------------------------------------------
# Internal: fingerprint
# ---------------------------------------------------------------------------


def _compute_fingerprint(entries: Sequence[FeatureNode], primary_files: Sequence[str]) -> str:
    """Deterministic 16-hex-char SHA-256 prefix of sorted qnames + primary files."""
    qnames = sorted(e.qname for e in entries if e.qname)
    pfiles = sorted(set(p for p in primary_files if p))
    payload = "|".join(qnames) + "||" + "|".join(pfiles)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def group_deltas_by_cluster(
    feature_nodes: Sequence[FeatureNode],
    candidate_graph: Optional[Any] = None,
    call_graph: Optional[Any] = None,
    language_adapter: Optional[Any] = None,
) -> List[ClusterGroup]:
    """Cluster *feature_nodes* via 5-signal weighted scoring + union-find.

    Parameters
    ----------
    feature_nodes : Sequence[FeatureNode]
        Input feature nodes (or duck-typed objects with the same fields).
        Empty input returns ``[]`` without error (R11/AC10).
    candidate_graph : Any, optional
        Reserved for future use — the candidate graph the deltas were
        derived from.  The default 5-signal scoring does not consult it.
    call_graph : Any, optional
        Reserved for future use — call graph for richer S3 scoring.
    language_adapter : LanguageAdapter, optional
        When omitted, an adapter is selected based on the file mix
        (PythonAdapter if any input file is ``.py``, else FileTreeAdapter).

    Returns
    -------
    List[ClusterGroup]
        Clusters in deterministic order (sorted by cluster_fingerprint).
    """
    nodes: List[FeatureNode] = list(feature_nodes or [])
    if not nodes:
        return []

    adapter = _resolve_adapter(language_adapter, nodes)

    # Stable input order: sort by qname so fingerprints are reproducible.
    nodes.sort(key=lambda n: (n.qname or "", tuple(sorted(n.primary_files or []))))
    n = len(nodes)
    uf = _UnionFind(n)

    # Pairwise scoring — O(n^2).  Acceptable for the typical reconcile
    # window (< a few hundred candidates); no early-termination optimisation
    # is attempted here.
    threshold = CLUSTER_THRESHOLD
    for i in range(n):
        for j in range(i + 1, n):
            sim = _pair_similarity(nodes[i], nodes[j], adapter)
            if sim >= threshold:
                uf.union(i, j)

    # Bucket by representative.
    buckets: dict[int, List[int]] = {}
    for idx in range(n):
        root = uf.find(idx)
        buckets.setdefault(root, []).append(idx)

    clusters: List[ClusterGroup] = []
    for _root, member_idxs in buckets.items():
        entries = [nodes[i] for i in sorted(member_idxs)]
        primary_files = sorted({pf for e in entries for pf in (e.primary_files or [])})
        secondary_files = sorted({sf for e in entries for sf in (e.secondary_files or [])})
        fingerprint = _compute_fingerprint(entries, primary_files)
        clusters.append(ClusterGroup(
            entries=entries,
            primary_files=primary_files,
            secondary_files=secondary_files,
            cluster_fingerprint=fingerprint,
        ))

    clusters.sort(key=lambda c: c.cluster_fingerprint)
    return clusters


__all__ = [
    "FeatureNode",
    "ClusterGroup",
    "group_deltas_by_cluster",
]
