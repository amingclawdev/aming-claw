"""Tests for Phase K extract_service_ports + attribution scoring.

Covers AC-EXT-1..5: os.environ.setdefault extraction, multi-port
attribution scoring, correct drift assignment, and regression safety.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, FrozenSet, Set
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs (same as test_phase_k.py)
# ---------------------------------------------------------------------------

class FakeResolvedScope:
    def __init__(self, file_dict: Dict[str, Any]):
        self.file_set = file_dict
        self.node_set: FrozenSet[str] = frozenset()
        self.commit_set: FrozenSet[str] = frozenset()

    def files(self) -> Set[str]:
        return set(self.file_set.keys())

    def is_empty(self) -> bool:
        return len(self.file_set) == 0


class FakeCtx:
    def __init__(self, workspace: str):
        self.project_id = "aming-claw"
        self.workspace_path = workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _write(ws: Path, relpath: str, content: str):
    p = ws / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return relpath


# ---------------------------------------------------------------------------
# AC-EXT-1: extract_service_ports detects os.environ.setdefault
# ---------------------------------------------------------------------------

class TestExtractSetdefault:
    def test_environ_setdefault_string_port(self, tmp_workspace):
        """AC-EXT-1: os.environ.setdefault('GOVERNANCE_PORT', '40000') yields port=40000."""
        ws = tmp_workspace
        path = _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
        """)
        from agent.governance.reconcile_phases.phase_k import extract_service_ports
        ports = extract_service_ports(path, str(ws))
        assert len(ports) >= 1
        gp = [p for p in ports if p.constant_name == "GOVERNANCE_PORT"]
        assert len(gp) == 1
        assert gp[0].port == 40000
        assert gp[0].service_name == "governance"

    def test_environ_setdefault_int_port(self, tmp_workspace):
        """os.environ.setdefault with int value also works."""
        ws = tmp_workspace
        path = _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', 40000)
        """)
        from agent.governance.reconcile_phases.phase_k import extract_service_ports
        ports = extract_service_ports(path, str(ws))
        gp = [p for p in ports if p.constant_name == "GOVERNANCE_PORT"]
        assert len(gp) == 1
        assert gp[0].port == 40000

    def test_non_port_env_ignored(self, tmp_workspace):
        """os.environ.setdefault for non-PORT/HOST names is ignored."""
        ws = tmp_workspace
        path = _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
            os.environ.setdefault('DEBUG_MODE', '1')
            os.environ.setdefault('LOG_LEVEL', 'INFO')
        """)
        from agent.governance.reconcile_phases.phase_k import extract_service_ports
        ports = extract_service_ports(path, str(ws))
        names = [p.constant_name for p in ports]
        assert "GOVERNANCE_PORT" in names
        assert "DEBUG_MODE" not in names
        assert "LOG_LEVEL" not in names

    def test_assign_still_works(self, tmp_workspace):
        """R4: top-level Assign extraction (PORT = 40101) still works."""
        ws = tmp_workspace
        path = _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        from agent.governance.reconcile_phases.phase_k import extract_service_ports
        ports = extract_service_ports(path, str(ws))
        assert len(ports) == 1
        assert ports[0].port == 40101
        assert ports[0].constant_name == "MANAGER_HTTP_PORT"

    def test_assign_and_setdefault_no_duplicate(self, tmp_workspace):
        """If both Assign and setdefault define same name, no duplicate."""
        ws = tmp_workspace
        path = _write(ws, "agent/server.py", """\
            import os
            GOVERNANCE_PORT = 40000
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
        """)
        from agent.governance.reconcile_phases.phase_k import extract_service_ports
        ports = extract_service_ports(path, str(ws))
        gp = [p for p in ports if p.constant_name == "GOVERNANCE_PORT"]
        assert len(gp) == 1


# ---------------------------------------------------------------------------
# AC-EXT-2: phase_k.run emits ServicePortContract for BOTH ports
# ---------------------------------------------------------------------------

class TestMultiPortExtraction:
    def test_both_ports_extracted(self, tmp_workspace):
        """AC-EXT-2: run on scope with setdefault + Assign emits both ports."""
        ws = tmp_workspace
        _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
        """)
        _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        _write(ws, "docs/api/governance-api.md", """\
            # API Reference
            Some text about the API.
        """)

        scope = FakeResolvedScope({
            "start_governance.py": None,
            "agent/manager_http_server.py": None,
            "docs/api/governance-api.md": None,
        })
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        # We need to check that both ports are extracted - inspect via no_test discrepancies
        # or by directly checking extract_service_ports
        from agent.governance.reconcile_phases.phase_k import extract_service_ports
        gov_ports = extract_service_ports("start_governance.py", str(ws))
        mgr_ports = extract_service_ports("agent/manager_http_server.py", str(ws))

        gov_names = [p.constant_name for p in gov_ports]
        mgr_names = [p.constant_name for p in mgr_ports]
        assert "GOVERNANCE_PORT" in gov_names
        assert "MANAGER_HTTP_PORT" in mgr_names

        # Also run the full phase and check no_test for both
        results = run(ctx, scope=scope)
        no_test = [d for d in results if d.type == "contract_no_test"
                   and d.contract_kind == "ServicePortContract"]
        contract_ids = [d.contract_id for d in no_test]
        assert "GOVERNANCE_PORT" in contract_ids
        assert "MANAGER_HTTP_PORT" in contract_ids


# ---------------------------------------------------------------------------
# AC-EXT-3 & AC-EXT-4: Attribution scoring
# ---------------------------------------------------------------------------

class TestAttributionScoring:
    def test_drift_attributed_to_governance(self, tmp_workspace):
        """AC-EXT-3: localhost:40006 in governance context → GOVERNANCE_PORT."""
        ws = tmp_workspace
        _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
        """)
        _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        _write(ws, "docs/api/governance-api.md", """\
            # Governance API Reference

            ## Governance Server

            The governance server listens on localhost:40006 for API requests.
        """)

        scope = FakeResolvedScope({
            "start_governance.py": None,
            "agent/manager_http_server.py": None,
            "docs/api/governance-api.md": None,
        })
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)
        drift = [d for d in results if d.type == "doc_value_drift"
                 and d.doc_value == 40006]
        assert len(drift) >= 1, f"Expected drift for 40006, got {results}"
        assert drift[0].contract_id == "GOVERNANCE_PORT"
        assert drift[0].code_value == 40000

    def test_drift_attributed_to_manager(self, tmp_workspace):
        """AC-EXT-4: localhost:40007 in manager context → MANAGER_HTTP_PORT."""
        ws = tmp_workspace
        _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
        """)
        _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        _write(ws, "docs/api/governance-api.md", """\
            # API Reference

            ## Manager HTTP Server

            The manager HTTP server provides task management at localhost:40007.
        """)

        scope = FakeResolvedScope({
            "start_governance.py": None,
            "agent/manager_http_server.py": None,
            "docs/api/governance-api.md": None,
        })
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)
        drift = [d for d in results if d.type == "doc_value_drift"
                 and d.doc_value == 40007]
        assert len(drift) >= 1, f"Expected drift for 40007, got {results}"
        assert drift[0].contract_id == "MANAGER_HTTP_PORT"
        assert drift[0].code_value == 40101

    def test_dual_drift_correct_attribution(self, tmp_workspace):
        """Both 40006 and 40007 in same doc, each attributed correctly."""
        ws = tmp_workspace
        _write(ws, "start_governance.py", """\
            import os
            os.environ.setdefault('GOVERNANCE_PORT', '40000')
        """)
        _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        _write(ws, "docs/api/governance-api.md", """\
            # Governance API Reference

            ## Governance Server

            The governance server listens on localhost:40006 for API requests.

            ```bash
            curl http://localhost:40006/api/health
            ```

            ## Manager HTTP Server

            The manager HTTP server provides task management at localhost:40007.

            ```bash
            curl http://localhost:40007/api/task/list
            ```
        """)

        scope = FakeResolvedScope({
            "start_governance.py": None,
            "agent/manager_http_server.py": None,
            "docs/api/governance-api.md": None,
        })
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)
        drift = [d for d in results if d.type == "doc_value_drift"]
        # Should have drifts for both ports
        drift_map = {d.doc_value: d for d in drift}

        # 40006 → GOVERNANCE_PORT
        assert 40006 in drift_map, f"No drift for 40006 in {[(d.doc_value, d.contract_id) for d in drift]}"
        assert drift_map[40006].contract_id == "GOVERNANCE_PORT"
        assert drift_map[40006].code_value == 40000

        # 40007 → MANAGER_HTTP_PORT
        assert 40007 in drift_map, f"No drift for 40007 in {[(d.doc_value, d.contract_id) for d in drift]}"
        assert drift_map[40007].contract_id == "MANAGER_HTTP_PORT"
        assert drift_map[40007].code_value == 40101


# ---------------------------------------------------------------------------
# R3: Score factors unit test
# ---------------------------------------------------------------------------

class TestScoreServicePortMatch:
    def test_constant_name_in_context(self):
        """constant_name within ±10 lines → +3."""
        from agent.governance.reconcile_phases.phase_k import (
            score_service_port_match, ServicePortContract,
        )
        sp = ServicePortContract("governance", 40000, "GOVERNANCE_PORT", "server.py", 1)
        doc = "Line1\nGOVERNANCE_PORT is set here\nlocalhost:40006\nLine4\n"
        offset = doc.index("localhost:40006")
        score = score_service_port_match(sp, doc, offset)
        assert score >= 3.0

    def test_service_name_in_context(self):
        """service_name within ±5 lines → +1."""
        from agent.governance.reconcile_phases.phase_k import (
            score_service_port_match, ServicePortContract,
        )
        sp = ServicePortContract("governance", 40000, "GOV_PORT", "server.py", 1)
        doc = "Line1\nThe governance server\nlocalhost:40006\nLine4\n"
        offset = doc.index("localhost:40006")
        score = score_service_port_match(sp, doc, offset)
        assert score >= 1.0

    def test_tie_gives_medium_confidence(self, tmp_workspace):
        """R3: Tied scores → confidence='medium'."""
        ws = tmp_workspace
        _write(ws, "svc_a.py", """\
            import os
            os.environ.setdefault('A_PORT', '1000')
        """)
        _write(ws, "svc_b.py", """\
            import os
            os.environ.setdefault('B_PORT', '2000')
        """)
        # Doc with equal context for both (neither name mentioned)
        _write(ws, "docs/test.md", """\
            # Test
            some generic text
            localhost:9999
        """)
        scope = FakeResolvedScope({
            "svc_a.py": None,
            "svc_b.py": None,
            "docs/test.md": None,
        })
        ctx = FakeCtx(str(ws))
        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)
        drift = [d for d in results if d.type == "doc_value_drift" and d.doc_value == 9999]
        # If both score equally, confidence should not be 'high'
        # (could be 'medium' or 'ambiguous attribution' depending on scores)
        if drift:
            assert drift[0].confidence in ("medium", "ambiguous attribution")


# ---------------------------------------------------------------------------
# R5: _infer_service_name preserved
# ---------------------------------------------------------------------------

class TestInferServiceName:
    def test_start_governance_infers_governance(self):
        """R5: start_governance.py → 'governance'."""
        from agent.governance.reconcile_phases.phase_k import _infer_service_name
        assert _infer_service_name("start_governance.py") == "governance"

    def test_server_infers_governance(self):
        from agent.governance.reconcile_phases.phase_k import _infer_service_name
        assert _infer_service_name("server.py") == "governance"

    def test_manager_http_server(self):
        from agent.governance.reconcile_phases.phase_k import _infer_service_name
        assert _infer_service_name("agent/manager_http_server.py") == "manager_http_server"


# ---------------------------------------------------------------------------
# AC-EXT-5: Existing tests still pass (covered by running both test files)
# ---------------------------------------------------------------------------

class TestRegressionSafety:
    def test_existing_assign_extraction_unchanged(self, tmp_workspace):
        """R4: top-level GOVERNANCE_PORT = 40000 still extracted."""
        ws = tmp_workspace
        path = _write(ws, "agent/server.py", """\
            GOVERNANCE_PORT = 40000
        """)
        from agent.governance.reconcile_phases.phase_k import extract_service_ports
        ports = extract_service_ports(path, str(ws))
        assert len(ports) == 1
        assert ports[0].port == 40000
        assert ports[0].constant_name == "GOVERNANCE_PORT"
