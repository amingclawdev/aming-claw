"""Tests for scripts/reconcile-scoped.py CLI wrapper."""
from __future__ import annotations

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)


# ---------------------------------------------------------------------------
# AC-CLI: --bug-id X --dry-run prints JSON
# ---------------------------------------------------------------------------

def test_cli_dry_run_outputs_json(capsys):
    """AC-CLI: --bug-id X --dry-run produces JSON output."""
    fake_result = {
        "report_path": "docs/dev/scratch/reconcile-comprehensive-2026-04-25.md",
        "summary": {"auto_fixable": 0, "human_review": 0},
        "phases": {},
    }

    with patch("agent.governance.reconcile_phases.orchestrator.run_orchestrated", return_value=fake_result):
        from scripts import __init__  # noqa: F401 — just ensure scripts is a package-like path
    # We need to import the main function
    sys.path.insert(0, os.path.join(_root, "scripts"))
    import importlib

    # Import the CLI module
    spec = importlib.util.spec_from_file_location(
        "reconcile_scoped",
        os.path.join(_root, "scripts", "reconcile-scoped.py"),
    )
    mod = importlib.util.module_from_spec(spec)

    with patch("agent.governance.reconcile_phases.orchestrator.run_orchestrated", return_value=fake_result), \
         patch("agent.governance.reconcile_phases.scope.ReconcileScope.resolve") as mock_resolve:
        mock_resolve.return_value = MagicMock(
            files=lambda: set(),
            is_empty=lambda: True,
            file_set={}, node_set=frozenset(), commit_set=frozenset(),
        )
        spec.loader.exec_module(mod)
        ret = mod.main(["--bug-id", "TEST-BUG", "--dry-run"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "summary" in output or "report_path" in output


# ---------------------------------------------------------------------------
# AC-CLI: --strict + empty resolution exits non-zero
# ---------------------------------------------------------------------------

def test_cli_strict_empty_exits_nonzero():
    """AC-CLI: --strict with empty resolution exits non-zero."""
    from agent.governance.reconcile_phases.scope import EmptyScopeError

    spec = __import__("importlib").util.spec_from_file_location(
        "reconcile_scoped",
        os.path.join(_root, "scripts", "reconcile-scoped.py"),
    )
    mod = __import__("importlib").util.module_from_spec(spec)

    with patch("agent.governance.reconcile_phases.orchestrator.run_orchestrated") as mock_run:
        mock_run.side_effect = EmptyScopeError("empty scope")
        spec.loader.exec_module(mod)
        with pytest.raises(SystemExit) as exc_info:
            mod.main(["--bug-id", "NONEXISTENT", "--strict"])
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# CLI parses arguments correctly
# ---------------------------------------------------------------------------

def test_cli_parses_phases():
    """CLI --phases flag splits comma-separated values."""
    spec = __import__("importlib").util.spec_from_file_location(
        "reconcile_scoped",
        os.path.join(_root, "scripts", "reconcile-scoped.py"),
    )
    mod = __import__("importlib").util.module_from_spec(spec)

    fake_result = {"report_path": "", "summary": {}, "phases": {}}

    with patch("agent.governance.reconcile_phases.orchestrator.run_orchestrated", return_value=fake_result) as mock_run, \
         patch("agent.governance.reconcile_phases.scope.ReconcileScope.resolve") as mock_resolve:
        mock_resolve.return_value = MagicMock(
            files=lambda: set(),
            is_empty=lambda: True,
            file_set={}, node_set=frozenset(), commit_set=frozenset(),
        )
        spec.loader.exec_module(mod)
        mod.main(["--bug-id", "TEST", "--phases", "A,E,B", "--dry-run"])

    call_kwargs = mock_run.call_args
    assert call_kwargs[1]["phases"] == ["A", "E", "B"]


# ---------------------------------------------------------------------------
# Server reconcile-v2 scope field
# ---------------------------------------------------------------------------

def test_reconcile_v2_scope_field_forwarded():
    """AC-COMPAT: /api/wf/{pid}/reconcile-v2 forwards scope to metadata."""
    # Just verify the server code pattern exists
    import importlib
    server_path = os.path.join(_root, "agent", "governance", "server.py")
    with open(server_path, "r") as f:
        content = f.read()
    assert 'scope_data = body.get("scope")' in content
    assert 'metadata["scope"] = scope_data' in content
