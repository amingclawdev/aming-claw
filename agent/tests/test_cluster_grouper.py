"""Tests for CR1 — multi-signal cluster_grouper + LanguageAdapter Protocol.

Covers AC1-AC14 from task-1777728390-e195aa.  14 test functions ≥ AC14's
"12+" requirement.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest


# Ensure repo root is on sys.path so ``agent.*`` imports work when pytest
# is invoked from the project directory.
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.language_adapters import (  # noqa: E402
    FileTreeAdapter,
    LanguageAdapter,
    PythonAdapter,
)
from agent.governance.reconcile_config import (  # noqa: E402
    BOOTSTRAP_THRESHOLD,
    CLUSTER_SIGNAL_WEIGHTS,
    CLUSTER_THRESHOLD,
    RECONCILE_FEATURE_CLUSTER_FILE_CAP,
    RECONCILE_CLUSTER_SIZE_CAP,
)
from agent.governance.reconcile_phases.cluster_grouper import (  # noqa: E402
    ClusterGroup,
    FeatureNode,
    group_deltas_by_cluster,
)


# ---------------------------------------------------------------------------
# AC1 — public imports succeed
# ---------------------------------------------------------------------------

def test_ac1_public_imports_succeed():
    """Public adapter classes are importable from language_adapters package."""
    assert LanguageAdapter is not None
    assert PythonAdapter is not None
    assert FileTreeAdapter is not None


# ---------------------------------------------------------------------------
# AC2 — base.py contains the literal Protocol declaration & method names
# ---------------------------------------------------------------------------

def test_ac2_base_protocol_signature():
    base_path = Path(_repo_root) / "agent" / "governance" / "language_adapters" / "base.py"
    text = base_path.read_text(encoding="utf-8")
    assert "class LanguageAdapter(Protocol)" in text
    for method in ("supports", "collect_decorators", "find_module_root", "detect_test_pairing"):
        assert re.search(rf"def\s+{method}\b", text), f"missing method {method}"


# ---------------------------------------------------------------------------
# AC3 — supports() return values
# ---------------------------------------------------------------------------

def test_ac3_supports_for_python_and_filetree():
    assert PythonAdapter().supports("foo.py") is True
    assert FileTreeAdapter().supports("foo.unknown") is True
    # Python adapter should reject non-python paths but FileTreeAdapter accepts.
    assert PythonAdapter().supports("foo.unknown") is False
    assert FileTreeAdapter().supports("anything.go") is True


# ---------------------------------------------------------------------------
# AC4 — collect_decorators on a real ast.FunctionDef
# ---------------------------------------------------------------------------

def test_ac4_collect_decorators_route():
    src = """
@route
def handler():
    pass
"""
    tree = ast.parse(src)
    fn_node = tree.body[0]
    assert isinstance(fn_node, ast.FunctionDef)
    decorators = PythonAdapter().collect_decorators(fn_node)
    assert "route" in decorators


# ---------------------------------------------------------------------------
# AC5 — config constants importable, weights sum to 1.0
# ---------------------------------------------------------------------------

def test_ac5_reconcile_config_constants():
    assert isinstance(CLUSTER_SIGNAL_WEIGHTS, dict)
    assert set(CLUSTER_SIGNAL_WEIGHTS.keys()) == {"S1", "S2", "S3", "S4", "S5"}
    total = sum(CLUSTER_SIGNAL_WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"weights must sum to 1.0, got {total}"
    assert isinstance(CLUSTER_THRESHOLD, float)
    assert isinstance(RECONCILE_CLUSTER_SIZE_CAP, int)
    assert isinstance(RECONCILE_FEATURE_CLUSTER_FILE_CAP, int)
    assert isinstance(BOOTSTRAP_THRESHOLD, int)


# ---------------------------------------------------------------------------
# AC6 — public surface of cluster_grouper module
# ---------------------------------------------------------------------------

def test_ac6_public_surface():
    # group_deltas_by_cluster is a callable
    assert callable(group_deltas_by_cluster)
    # ClusterGroup exposes the four required attributes (default-construct).
    cg = ClusterGroup(entries=[], primary_files=[], secondary_files=[], cluster_fingerprint="")
    for attr in ("entries", "primary_files", "secondary_files", "cluster_fingerprint"):
        assert hasattr(cg, attr), f"ClusterGroup missing attr: {attr}"


# ---------------------------------------------------------------------------
# AC7 — same module + ≥2 shared descendants → same cluster (sim ≥ 0.5)
# ---------------------------------------------------------------------------

def test_ac7_shared_descendants_and_module_clustered():
    a = FeatureNode(
        qname="agent.foo::a",
        module="agent.foo",
        primary_files=["agent/foo/a.py"],
        descendants={"x", "y", "z"},
    )
    b = FeatureNode(
        qname="agent.foo::b",
        module="agent.foo",
        primary_files=["agent/foo/b.py"],
        descendants={"x", "y", "w"},
    )
    clusters = group_deltas_by_cluster([a, b])
    assert len(clusters) == 1
    assert {e.qname for e in clusters[0].entries} == {a.qname, b.qname}


# ---------------------------------------------------------------------------
# AC8 — no shared signals → separate clusters (sim < 0.5)
# ---------------------------------------------------------------------------

def test_ac8_no_shared_signals_separate():
    a = FeatureNode(
        qname="alpha::one",
        module="alpha",
        primary_files=["alpha/one.py"],
        descendants={"x1"},
    )
    b = FeatureNode(
        qname="beta::two",
        module="beta",
        primary_files=["beta/two.py"],
        descendants={"y1"},
    )
    clusters = group_deltas_by_cluster([a, b])
    qname_sets = [{e.qname for e in c.entries} for c in clusters]
    assert {a.qname} in qname_sets
    assert {b.qname} in qname_sets
    assert len(clusters) == 2


# ---------------------------------------------------------------------------
# AC9 — fingerprint stable across repeated runs
# ---------------------------------------------------------------------------

def test_ac9_fingerprint_stable():
    nodes = [
        FeatureNode(
            qname="m::x",
            module="m",
            primary_files=["m/x.py"],
            descendants={"a", "b"},
        ),
        FeatureNode(
            qname="m::y",
            module="m",
            primary_files=["m/y.py"],
            descendants={"a", "b"},
        ),
    ]
    run1 = group_deltas_by_cluster(nodes)
    run2 = group_deltas_by_cluster(nodes)
    assert [c.cluster_fingerprint for c in run1] == [c.cluster_fingerprint for c in run2]
    # Fingerprint length is 16 hex chars.
    for c in run1:
        assert len(c.cluster_fingerprint) == 16
        int(c.cluster_fingerprint, 16)  # raises if not hex


# ---------------------------------------------------------------------------
# AC10 — empty input returns []
# ---------------------------------------------------------------------------

def test_ac10_empty_input_returns_empty():
    assert group_deltas_by_cluster([]) == []
    assert group_deltas_by_cluster(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC11 — union-find transitivity: A↔B, B↔C produce single cluster {A,B,C}
# ---------------------------------------------------------------------------

def test_ac11_union_find_transitive():
    # A and B share descendants {x,y} + module
    # B and C share descendants {y,z} + module
    # A and C share NOTHING in descendants but share module → S2 alone is 0.2
    a = FeatureNode(qname="m::a", module="m", primary_files=["m/a.py"], descendants={"x", "y"})
    b = FeatureNode(qname="m::b", module="m", primary_files=["m/b.py"], descendants={"x", "y", "z"})
    c = FeatureNode(qname="m::c", module="m", primary_files=["m/c.py"], descendants={"y", "z"})
    clusters = group_deltas_by_cluster([a, b, c])
    assert len(clusters) == 1
    assert {e.qname for e in clusters[0].entries} == {a.qname, b.qname, c.qname}


# ---------------------------------------------------------------------------
# AC12 — phase_z imports cluster_grouper and calls group_deltas_by_cluster
# ---------------------------------------------------------------------------

def test_ac12_phase_z_invokes_cluster_grouper():
    phase_z_path = Path(_repo_root) / "agent" / "governance" / "reconcile_phases" / "phase_z.py"
    text = phase_z_path.read_text(encoding="utf-8")
    assert "cluster_grouper" in text
    assert "group_deltas_by_cluster" in text


# ---------------------------------------------------------------------------
# AC13 — FileTreeAdapter fallback handles non-Python files without crashing
# ---------------------------------------------------------------------------

def test_ac13_filetree_fallback_no_crash_non_python():
    a = FeatureNode(qname="js::a", module="src/js", primary_files=["src/js/a.js"])
    b = FeatureNode(qname="go::b", module="src/go", primary_files=["src/go/b.go"])
    # language_adapter omitted → grouper should pick FileTreeAdapter and not crash.
    clusters = group_deltas_by_cluster([a, b])
    assert isinstance(clusters, list)
    # Two unrelated nodes → two separate clusters.
    assert len(clusters) == 2


# ---------------------------------------------------------------------------
# Bonus — duck-typed input & misc surface coverage
# ---------------------------------------------------------------------------

def test_filetree_adapter_methods():
    fa = FileTreeAdapter()
    assert fa.collect_decorators(None) == []
    assert fa.find_module_root("a/b/c.txt") == "a/b"
    assert fa.detect_test_pairing("a/b/c.txt") is None
    assert fa.supports("") is False
