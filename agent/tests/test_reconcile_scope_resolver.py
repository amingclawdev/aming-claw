"""Tests for ReconcileScope resolver — scope.py unit tests."""
from __future__ import annotations

import os
import sys
import types
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root on path
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from agent.governance.reconcile_phases.scope import (
    ReconcileScope, ResolvedScope, FileOrigin, EmptyScopeError,
    _expand_test_siblings, _expand_doc_refs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeCtx:
    """Minimal ReconcileContext stub."""
    def __init__(self, project_id="test-proj", workspace_path="/tmp/ws", graph=None):
        self.project_id = project_id
        self.workspace_path = workspace_path
        self.graph = graph
        self.options = {}


class FakeNode:
    def __init__(self, primary=None, secondary=None, test=None):
        self.primary = primary or []
        self.secondary = secondary or []
        self.test = test or []


class FakeGraph:
    def __init__(self, nodes=None):
        self._nodes = nodes or {}
    def get_node(self, nid):
        return self._nodes.get(nid)
    def list_nodes(self):
        return list(self._nodes.keys())


# ---------------------------------------------------------------------------
# AC-S4: UNION semantics (bug_id + commit → union, not intersection)
# ---------------------------------------------------------------------------

def test_union_semantics_bug_id_and_commit():
    """AC-S4: Two resolvers produce union of both outputs."""
    scope = ReconcileScope(bug_id="BUG-1", commit="abc123")
    ctx = FakeCtx()

    bug_files = {"agent/governance/server.py"}
    commit_files = {"agent/governance/reconcile.py"}

    def fake_bug_resolver(bug_id, ctx, file_set):
        for f in bug_files:
            file_set[f] = FileOrigin(source="bug_id", detail=bug_id)

    def fake_commit_resolver(commits, ctx, file_set):
        for f in commit_files:
            file_set[f] = FileOrigin(source="commit", detail="abc123")

    with patch("agent.governance.reconcile_phases.scope._resolve_bug_id", fake_bug_resolver), \
         patch("agent.governance.reconcile_phases.scope._resolve_commits", fake_commit_resolver), \
         patch("agent.governance.reconcile_phases.scope._expand_test_siblings"), \
         patch("agent.governance.reconcile_phases.scope._expand_doc_refs"):
        resolved = scope.resolve(ctx)

    # Both sets should be present (UNION)
    assert "agent/governance/server.py" in resolved.files()
    assert "agent/governance/reconcile.py" in resolved.files()
    assert len(resolved.files()) >= 2


# ---------------------------------------------------------------------------
# AC-S5: strict=True + empty → EmptyScopeError; strict=False → warning
# ---------------------------------------------------------------------------

def test_strict_empty_raises():
    """AC-S5: strict=True with empty resolution raises EmptyScopeError."""
    scope = ReconcileScope(bug_id="NONEXISTENT", strict=True)
    ctx = FakeCtx()

    with patch("agent.governance.reconcile_phases.scope._resolve_bug_id") as mock_resolve:
        mock_resolve.side_effect = lambda *a, **kw: None  # resolves nothing
        with pytest.raises(EmptyScopeError):
            scope.resolve(ctx)


def test_nonstrict_empty_returns_empty():
    """AC-S5: strict=False with empty resolution returns empty ResolvedScope."""
    scope = ReconcileScope(bug_id="NONEXISTENT", strict=False)
    ctx = FakeCtx()

    with patch("agent.governance.reconcile_phases.scope._resolve_bug_id") as mock_resolve:
        mock_resolve.side_effect = lambda *a, **kw: None
        resolved = scope.resolve(ctx)

    assert resolved.is_empty()
    assert resolved.files() == set()


# ---------------------------------------------------------------------------
# AC-S1 / AC-S2: Individual resolver outputs
# ---------------------------------------------------------------------------

def test_paths_resolver():
    """Explicit paths are added with 'path' origin."""
    scope = ReconcileScope(paths=["a.py", "b.py"], include_tests=False, include_docs=False)
    ctx = FakeCtx()
    resolved = scope.resolve(ctx)
    assert resolved.files() == {"a.py", "b.py"}
    assert resolved.file_set["a.py"].source == "path"


def test_nodes_resolver():
    """Node resolver extracts files from graph nodes."""
    graph = FakeGraph({
        "L1.1": FakeNode(primary=["agent/foo.py"], secondary=["agent/bar.py"]),
    })
    scope = ReconcileScope(nodes=["L1.1"], include_tests=False, include_docs=False)
    ctx = FakeCtx(graph=graph)
    resolved = scope.resolve(ctx)
    assert "agent/foo.py" in resolved.files()
    assert "agent/bar.py" in resolved.files()
    assert "L1.1" in resolved.node_set


def test_commit_set_populated():
    """Commit resolver populates commit_set."""
    scope = ReconcileScope(commit="abc123", include_tests=False, include_docs=False)
    ctx = FakeCtx()

    def fake_resolve(commits, ctx, file_set):
        for f in ["changed.py"]:
            file_set[f] = FileOrigin(source="commit", detail="abc123")

    with patch("agent.governance.reconcile_phases.scope._resolve_commits", fake_resolve):
        resolved = scope.resolve(ctx)

    assert "abc123" in resolved.commit_set
    assert "changed.py" in resolved.files()


# ---------------------------------------------------------------------------
# ResolvedScope basics
# ---------------------------------------------------------------------------

def test_resolved_scope_empty():
    rs = ResolvedScope()
    assert rs.is_empty()
    assert rs.files() == set()


def test_resolved_scope_with_files():
    rs = ResolvedScope(
        file_set={"a.py": FileOrigin(source="path")},
        node_set=frozenset(["L1.1"]),
    )
    assert not rs.is_empty()
    assert "a.py" in rs.files()


# ---------------------------------------------------------------------------
# FileOrigin
# ---------------------------------------------------------------------------

def test_file_origin_creation():
    fo = FileOrigin(source="bug_id", detail="BUG-123")
    assert fo.source == "bug_id"
    assert fo.detail == "BUG-123"


# ---------------------------------------------------------------------------
# AC2: bug_id resolver — backlog_bugs row → resolved file_set
# ---------------------------------------------------------------------------

def test_bug_id_resolver_returns_target_files():
    """bug_id resolver reads backlog_bugs row and populates file_set with both paths."""
    import json
    import sqlite3
    from contextlib import contextmanager
    from agent.governance.reconcile_phases.scope import _resolve_bug_id

    bug_id = "TEST-BUG-SCOPE-001"
    target_files = ["a.py", "b.py"]

    # In-memory DB with backlog_bugs table and a test row
    mem_conn = sqlite3.connect(":memory:")
    mem_conn.execute(
        "CREATE TABLE backlog_bugs (bug_id TEXT PRIMARY KEY, target_files TEXT)"
    )
    mem_conn.execute(
        "INSERT INTO backlog_bugs (bug_id, target_files) VALUES (?, ?)",
        (bug_id, json.dumps(target_files)),
    )
    mem_conn.commit()

    @contextmanager
    def fake_db_context(project_id):
        yield mem_conn

    ctx = FakeCtx()
    file_set = {}

    # Patch DBContext in the db module so the lazy import inside _resolve_bug_id picks it up
    with patch("agent.governance.db.DBContext", fake_db_context):
        _resolve_bug_id(bug_id, ctx, file_set)

    assert "a.py" in file_set, f"a.py missing from file_set: {file_set}"
    assert "b.py" in file_set, f"b.py missing from file_set: {file_set}"
    assert file_set["a.py"].source == "bug_id"
    assert file_set["b.py"].source == "bug_id"
    assert file_set["a.py"].detail == bug_id

    mem_conn.close()


def test_bug_id_resolve_integration():
    """ReconcileScope(bug_id=...).resolve() returns file_set with both target paths."""
    import json
    import sqlite3
    from contextlib import contextmanager

    bug_id = "TEST-BUG-SCOPE-002"
    target_files = ["a.py", "b.py"]

    mem_conn = sqlite3.connect(":memory:")
    mem_conn.execute(
        "CREATE TABLE backlog_bugs (bug_id TEXT PRIMARY KEY, target_files TEXT)"
    )
    mem_conn.execute(
        "INSERT INTO backlog_bugs (bug_id, target_files) VALUES (?, ?)",
        (bug_id, json.dumps(target_files)),
    )
    mem_conn.commit()

    @contextmanager
    def fake_db_context(project_id):
        yield mem_conn

    scope = ReconcileScope(bug_id=bug_id, include_tests=False, include_docs=False)
    ctx = FakeCtx()

    with patch("agent.governance.db.DBContext", fake_db_context):
        resolved = scope.resolve(ctx)

    assert "a.py" in resolved.files(), f"a.py missing: {resolved.files()}"
    assert "b.py" in resolved.files(), f"b.py missing: {resolved.files()}"

    mem_conn.close()
