"""Tests for commit-sweep reconcile mode (orchestrator.py additions).

Covers: _dedup_key, run_commit_slice_orchestrated, run_commit_sweep_orchestrated,
rename map batching, baseline writes, and hot-file coverage.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

# ---------------------------------------------------------------------------
# Helpers — avoid importing heavy governance internals at module level
# ---------------------------------------------------------------------------

MODULE = "agent.governance.reconcile_phases.orchestrator"


def _import_orchestrator():
    """Import orchestrator lazily so patches can intercept subprocess."""
    import importlib
    mod = importlib.import_module(MODULE)
    return mod


# ---------------------------------------------------------------------------
# 1. test_slice_returns_changed_files
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.subprocess.run")
@patch(f"{MODULE}.run_orchestrated", return_value={"phases": {}})
@patch(f"{MODULE}._run_phase", return_value=[])
def test_slice_returns_changed_files(mock_run_phase, mock_orch, mock_subproc):
    """run_commit_slice_orchestrated returns files_in_slice from git diff-tree."""
    orch = _import_orchestrator()

    # Mock git diff-tree output
    diff_tree_result = MagicMock()
    diff_tree_result.returncode = 0
    diff_tree_result.stdout = "agent/foo.py\nagent/bar.py\n"
    mock_subproc.return_value = diff_tree_result

    # Mock ReconcileContext
    mock_ctx = MagicMock()
    mock_ctx.workspace_path = "/fake"
    mock_ctx.scan_depth = 3
    mock_ctx.graph_db_delta = {}

    with patch(f"{MODULE}.ReconcileContext", return_value=mock_ctx) if hasattr(orch, "ReconcileContext") else patch(f"agent.governance.reconcile_phases.context.ReconcileContext", return_value=mock_ctx):
        # Patch ReconcileScope.resolve
        with patch("agent.governance.reconcile_phases.scope.ReconcileScope.resolve", return_value=MagicMock()):
            result = orch.run_commit_slice_orchestrated(
                "aming-claw", "/fake", "abc123",
            )

    assert result["commit"] == "abc123"
    assert "agent/foo.py" in result["files_in_slice"]
    assert "agent/bar.py" in result["files_in_slice"]
    assert isinstance(result["discrepancies"], list)


# ---------------------------------------------------------------------------
# 2. test_sweep_no_merges
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.run_commit_slice_orchestrated")
@patch(f"{MODULE}._build_rename_map", return_value={})
@patch(f"{MODULE}.subprocess.run")
def test_sweep_no_merges(mock_subproc, mock_rename, mock_slice):
    """git log call in sweep uses --no-merges; --first-parent must NOT appear."""
    orch = _import_orchestrator()

    # Mock git log output (commit list)
    log_result = MagicMock()
    log_result.returncode = 0
    log_result.stdout = "aaa111\nbbb222\n"
    mock_subproc.return_value = log_result

    # Mock slice results
    mock_slice.return_value = {
        "commit": "aaa111",
        "discrepancies": [],
        "files_in_slice": ["f.py"],
    }

    # Mock DB for baseline resolution
    with patch(f"agent.governance.db.DBContext") as mock_db:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.return_value.__exit__ = MagicMock(return_value=False)

        result = orch.run_commit_sweep_orchestrated(
            "aming-claw", "/fake", since_baseline="base123",
        )

    # Verify --no-merges was in the git log call
    git_log_call = mock_subproc.call_args
    cmd = git_log_call[0][0] if git_log_call[0] else git_log_call[1].get("args", [])
    assert "--no-merges" in cmd, f"--no-merges not in {cmd}"
    assert "--first-parent" not in cmd, f"--first-parent found in {cmd}"


# ---------------------------------------------------------------------------
# 3. test_dedup_key_extracts
# ---------------------------------------------------------------------------

def test_dedup_key_extracts():
    """_dedup_key returns (affected_file, contract_id, type) correctly."""
    orch = _import_orchestrator()

    # unmapped_file → detail is file path
    d1 = {"type": "unmapped_file", "detail": "agent/foo.py", "node_id": "L1.3"}
    assert orch._dedup_key(d1) == ("agent/foo.py", "L1.3", "unmapped_file")

    # stale_ref → first token of detail
    d2 = {"type": "stale_ref", "detail": "agent/bar.py references L2.1", "contract_id": "C42"}
    assert orch._dedup_key(d2) == ("agent/bar.py", "C42", "stale_ref")

    # doc_value_drift → d["doc"]
    d3 = {"type": "doc_value_drift", "doc": "docs/api.md", "constant_name": "PORT"}
    assert orch._dedup_key(d3) == ("docs/api.md", "PORT", "doc_value_drift")

    # unmapped_doc
    d4 = {"type": "unmapped_doc", "detail": "docs/readme.md"}
    assert orch._dedup_key(d4) == ("docs/readme.md", "", "unmapped_doc")

    # fallback
    d5 = {"type": "other", "detail": "something", "node_id": "N1"}
    assert orch._dedup_key(d5) == ("something", "N1", "other")


# ---------------------------------------------------------------------------
# 4. test_sweep_dedup_newest
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.run_commit_slice_orchestrated")
@patch(f"{MODULE}._build_rename_map", return_value={})
@patch(f"{MODULE}.subprocess.run")
def test_sweep_dedup_newest(mock_subproc, mock_rename, mock_slice):
    """Dedup keeps the newest (last-processed) discrepancy per key."""
    orch = _import_orchestrator()

    log_result = MagicMock()
    log_result.returncode = 0
    # git log returns newest first, so the implementation must replay oldest
    # first before applying last-write-wins dedup.
    log_result.stdout = "commit2\ncommit1\n"
    mock_subproc.return_value = log_result

    # commit1 produces a discrepancy
    slice1 = {
        "commit": "commit1",
        "discrepancies": [
            {"type": "unmapped_file", "detail": "agent/foo.py", "node_id": "L1", "old": True},
        ],
        "files_in_slice": ["agent/foo.py"],
    }
    # commit2 produces the same key but different payload
    slice2 = {
        "commit": "commit2",
        "discrepancies": [
            {"type": "unmapped_file", "detail": "agent/foo.py", "node_id": "L1", "old": False},
        ],
        "files_in_slice": ["agent/foo.py"],
    }
    mock_slice.side_effect = [slice1, slice2]

    with patch(f"agent.governance.db.DBContext") as mock_db:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.return_value.__exit__ = MagicMock(return_value=False)

        result = orch.run_commit_sweep_orchestrated(
            "aming-claw", "/fake", since_baseline="base",
        )

    # Should have 2 total but 1 deduped
    assert len(result["all_discrepancies"]) == 2
    assert len(result["dedup_discrepancies"]) == 1
    # The kept one should be from commit2 (newest = last write wins)
    assert result["dedup_discrepancies"][0]["attribution_commit"] == "commit2"
    assert [call.args[2] for call in mock_slice.call_args_list] == ["commit1", "commit2"]


# ---------------------------------------------------------------------------
# 5. test_sweep_hot_file_coverage
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.run_commit_slice_orchestrated")
@patch(f"{MODULE}._build_rename_map", return_value={})
@patch(f"{MODULE}.subprocess.run")
def test_sweep_hot_file_coverage(mock_subproc, mock_rename, mock_slice):
    """Coverage counts scanned hot files, not only files with discrepancies."""
    orch = _import_orchestrator()

    log_result = MagicMock()
    log_result.returncode = 0
    log_result.stdout = "c1\nc2\n"
    mock_subproc.return_value = log_result

    slice1 = {
        "commit": "c1",
        "discrepancies": [
            {"type": "unmapped_file", "detail": "a.py", "node_id": ""},
        ],
        "files_in_slice": ["a.py", "b.py"],
    }
    slice2 = {
        "commit": "c2",
        "discrepancies": [],
        "files_in_slice": ["c.py"],
    }
    mock_slice.side_effect = [slice1, slice2]

    with patch(f"agent.governance.db.DBContext") as mock_db:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.return_value.__exit__ = MagicMock(return_value=False)

        result = orch.run_commit_sweep_orchestrated(
            "aming-claw", "/fake", since_baseline="base",
            coverage_target="hot_files",
        )

    # hot_files = {a.py, b.py, c.py} → 3 files
    assert len(result["hot_files"]) == 3
    # All three hot files were scanned, even though only a.py had a discrepancy.
    assert result["covered_hot"] == ["a.py", "b.py", "c.py"]
    assert result["coverage_pct"] == 1.0


# ---------------------------------------------------------------------------
# 6. test_sweep_baseline_write
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.run_commit_slice_orchestrated")
@patch(f"{MODULE}._build_rename_map", return_value={})
@patch(f"{MODULE}.subprocess.run")
def test_sweep_baseline_write(mock_subproc, mock_rename, mock_slice):
    """When dry_run=False and walk succeeds, baseline_service.create_baseline is called
    with scope_kind='commit_sweep'."""
    orch = _import_orchestrator()

    # git log for commits
    log_result = MagicMock()
    log_result.returncode = 0
    log_result.stdout = "c1\n"

    # git rev-parse for HEAD
    head_result = MagicMock()
    head_result.returncode = 0
    head_result.stdout = "abc1234"

    mock_subproc.side_effect = [log_result, head_result]

    mock_slice.return_value = {
        "commit": "c1",
        "discrepancies": [],
        "files_in_slice": ["f.py"],
    }

    with patch(f"agent.governance.db.DBContext") as mock_db, \
         patch(f"agent.governance.baseline_service.create_baseline") as mock_create_bl:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_create_bl.return_value = {"baseline_id": 1}

        result = orch.run_commit_sweep_orchestrated(
            "aming-claw", "/fake", since_baseline="base",
            dry_run=False,
        )

    assert result["baseline_written"] is True
    mock_create_bl.assert_called_once()
    call_kwargs = mock_create_bl.call_args
    # Check scope_kind='commit_sweep'
    if call_kwargs[1]:
        assert call_kwargs[1].get("scope_kind") == "commit_sweep" or \
               call_kwargs[0][0] if len(call_kwargs[0]) > 0 else True
    else:
        # positional args
        pass
    # Verify scope_kind in either positional or keyword args
    all_args = call_kwargs
    assert "commit_sweep" in str(all_args), f"scope_kind='commit_sweep' not found in {all_args}"


# ---------------------------------------------------------------------------
# 7. test_sweep_since_baseline_default
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.run_commit_slice_orchestrated")
@patch(f"{MODULE}._build_rename_map", return_value={})
@patch(f"{MODULE}.subprocess.run")
def test_sweep_since_baseline_default(mock_subproc, mock_rename, mock_slice):
    """When since_baseline=None, resolves from version_baselines WHERE scope_kind='commit_sweep'."""
    orch = _import_orchestrator()

    log_result = MagicMock()
    log_result.returncode = 0
    log_result.stdout = "c1\n"
    mock_subproc.return_value = log_result

    mock_slice.return_value = {
        "commit": "c1",
        "discrepancies": [],
        "files_in_slice": ["f.py"],
    }

    with patch(f"agent.governance.db.DBContext") as mock_db:
        mock_conn = MagicMock()
        # Return a row with chain_version for the baseline query
        mock_row = {"chain_version": "resolved_base_sha"}
        mock_conn.execute.return_value.fetchone.return_value = mock_row
        mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.return_value.__exit__ = MagicMock(return_value=False)

        result = orch.run_commit_sweep_orchestrated(
            "aming-claw", "/fake",
            # since_baseline=None → should auto-resolve
        )

    # The SQL query should have been called with scope_kind='commit_sweep'
    sql_call = mock_conn.execute.call_args_list[0]
    sql_text = sql_call[0][0]
    assert "scope_kind" in sql_text
    assert "commit_sweep" in sql_text

    # Should have proceeded to git log with the resolved baseline
    assert mock_subproc.called
    git_log_cmd = mock_subproc.call_args[0][0]
    assert "resolved_base_sha..HEAD" in " ".join(git_log_cmd)


# ---------------------------------------------------------------------------
# 8. test_rename_map_batched
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.subprocess.run")
def test_rename_map_batched(mock_subproc):
    """Rename detection uses exactly 1 subprocess call with --name-status and -M."""
    orch = _import_orchestrator()

    rename_result = MagicMock()
    rename_result.returncode = 0
    rename_result.stdout = "R100\told/path.py\tnew/path.py\nM\tmodified.py\n"
    mock_subproc.return_value = rename_result

    rmap = orch._build_rename_map("base123", "/fake")

    # Should be exactly 1 subprocess call
    assert mock_subproc.call_count == 1

    cmd = mock_subproc.call_args[0][0]
    assert "--name-status" in cmd
    assert "-M" in cmd

    # Should have parsed the rename
    assert rmap == {"old/path.py": "new/path.py"}
