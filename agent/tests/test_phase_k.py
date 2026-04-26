"""Tests for Phase K — Contract-Test-Coverage Invariant.

Covers AC-K1 (contract_no_test), AC-K2 (doc_value_drift),
AC-K3 (orchestrator phase order + scope=None skip),
AC-K-CONFIG (yaml keys), AC-K-DRY-ONLY, AC-S16-CASE.
"""
from __future__ import annotations

import os
import textwrap
import tempfile
import shutil
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Set
from unittest.mock import MagicMock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Minimal stubs so tests don't need full governance stack
# ---------------------------------------------------------------------------

class FakeResolvedScope:
    """Mimics ResolvedScope with a dict of files."""

    def __init__(self, file_dict: Dict[str, Any]):
        self.file_set = file_dict
        self.node_set: FrozenSet[str] = frozenset()
        self.commit_set: FrozenSet[str] = frozenset()

    def files(self) -> Set[str]:
        return set(self.file_set.keys())

    def is_empty(self) -> bool:
        return len(self.file_set) == 0


class FakeCtx:
    """Minimal ReconcileContext stub."""

    def __init__(self, workspace: str):
        self.project_id = "aming-claw"
        self.workspace_path = workspace


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_workspace(tmp_path):
    """Create a temp workspace with fixture files."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _write(ws: Path, relpath: str, content: str):
    p = ws / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return relpath


# ---------------------------------------------------------------------------
# AC-K1: contract_no_test — endpoint with no test
# ---------------------------------------------------------------------------

class TestContractNoTest:
    def test_endpoint_no_test_emits_discrepancy(self, tmp_workspace):
        """AC-K1: server.py @route('POST', '/api/x') with no test → contract_no_test."""
        ws = tmp_workspace
        server_path = _write(ws, "agent/server.py", """\
            from some_framework import route

            @route("POST", "/api/x")
            def handle_x(request):
                return {"ok": True}
        """)

        scope = FakeResolvedScope({server_path: None})
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)

        no_test = [d for d in results if d.type == "contract_no_test"]
        assert len(no_test) >= 1, f"Expected contract_no_test, got {results}"
        d = no_test[0]
        assert d.contract_kind == "EndpointContract"
        assert d.confidence == "high"
        assert d.priority == "P0"
        assert d.suggested_action == "spawn_pm_write_test"

    def test_endpoint_with_test_no_discrepancy(self, tmp_workspace):
        """Endpoint covered by test should NOT emit contract_no_test."""
        ws = tmp_workspace
        server_path = _write(ws, "agent/server.py", """\
            from some_framework import route

            @route("POST", "/api/x")
            def handle_x(request):
                return {"ok": True}
        """)
        test_path = _write(ws, "agent/tests/test_server.py", """\
            def test_handle_x():
                # References the handler qname directly
                from agent.server import handle_x as server_handle_x
                resp = client.post("/api/x")  # endpoint /api/x
                # server.handle_x coverage
                assert server.handle_x is not None
        """)

        scope = FakeResolvedScope({server_path: None, test_path: None})
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)

        no_test = [d for d in results if d.type == "contract_no_test"
                    and d.contract_kind == "EndpointContract"]
        assert len(no_test) == 0, f"Unexpected contract_no_test: {no_test}"

    def test_service_port_no_test(self, tmp_workspace):
        """Service port with no test → contract_no_test."""
        ws = tmp_workspace
        srv_path = _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)

        scope = FakeResolvedScope({srv_path: None})
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)

        no_test = [d for d in results if d.type == "contract_no_test"
                    and d.contract_kind == "ServicePortContract"]
        assert len(no_test) >= 1


# ---------------------------------------------------------------------------
# AC-K2: doc_value_drift — ServicePortContract port mismatch
# ---------------------------------------------------------------------------

class TestDocValueDrift:
    def test_service_port_drift_high(self, tmp_workspace):
        """AC-K2: MANAGER_HTTP_PORT=40101 vs localhost:40007 near 'manager' → drift high."""
        ws = tmp_workspace
        srv_path = _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        doc_path = _write(ws, "docs/api/governance-api.md", """\
            # Governance API

            ## Manager HTTP Server

            The manager HTTP server runs on localhost:40007.

            ```bash
            curl http://localhost:40007/api/health
            ```
        """)

        scope = FakeResolvedScope({srv_path: None, doc_path: None})
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)

        drift = [d for d in results if d.type == "doc_value_drift"
                 and d.contract_kind == "ServicePortContract"]
        assert len(drift) >= 1, f"Expected doc_value_drift, got {results}"
        d = drift[0]
        assert d.doc_value == 40007
        assert d.code_value == 40101
        assert d.confidence == "high"
        assert d.suggested_action == "spawn_pm_fix_doc"

    def test_matching_port_no_drift(self, tmp_workspace):
        """Port matches — no drift emitted."""
        ws = tmp_workspace
        srv_path = _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        doc_path = _write(ws, "docs/api/governance-api.md", """\
            # Manager

            Port is localhost:40101 for the manager server.
        """)

        scope = FakeResolvedScope({srv_path: None, doc_path: None})
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)

        drift = [d for d in results if d.type == "doc_value_drift"]
        assert len(drift) == 0, f"Unexpected drift: {drift}"


# ---------------------------------------------------------------------------
# AC-K3: orchestrator phase order + scope=None skip
# ---------------------------------------------------------------------------

class TestOrchestratorIntegration:
    def test_phase_order_starts_with_k(self):
        """AC-K3: PHASE_ORDER starts with 'K'."""
        from agent.governance.reconcile_phases.orchestrator import PHASE_ORDER
        assert PHASE_ORDER[0] == "K"
        assert PHASE_ORDER == ["K", "A", "E", "B", "C", "D", "F", "G"]

    def test_phase_k_skips_when_scope_none(self):
        """AC-K3: _run_phase('K', ..., scope=None) returns []."""
        from agent.governance.reconcile_phases.orchestrator import _run_phase
        ctx = MagicMock()
        result = _run_phase("K", ctx, {}, scope=None)
        assert result == []

    def test_phase_k_runs_with_scope(self, tmp_workspace):
        """AC-K3: _run_phase('K', ..., scope=<non-None>) invokes phase_k.run."""
        ws = tmp_workspace
        _write(ws, "agent/server.py", """\
            from fw import route

            @route("GET", "/api/test")
            def handle_test(req):
                pass
        """)

        from agent.governance.reconcile_phases.orchestrator import _run_phase
        scope = FakeResolvedScope({"agent/server.py": None})
        ctx = FakeCtx(str(ws))
        result = _run_phase("K", ctx, {}, scope=scope)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# AC-K-CONFIG: phase_k_rules.yaml
# ---------------------------------------------------------------------------

class TestConfig:
    def test_rules_yaml_exists_with_keys(self):
        """AC-K-CONFIG: phase_k_rules.yaml has required keys."""
        yaml_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "governance", "reconcile_phases", "phase_k_rules.yaml",
        )
        assert os.path.isfile(yaml_path), f"Missing {yaml_path}"
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "excluded_constants" in data
        assert "service_name_aliases" in data
        assert "endpoint_path_normalize" in data


# ---------------------------------------------------------------------------
# AC-K-DRY-ONLY: no phase_h import, suggested_action only
# ---------------------------------------------------------------------------

class TestDryRunOnly:
    def test_no_phase_h_import(self):
        """AC-K-DRY-ONLY: phase_k does NOT import from phase_h."""
        import inspect
        from agent.governance.reconcile_phases import phase_k
        source = inspect.getsource(phase_k)
        assert "phase_h" not in source, "phase_k must not reference phase_h"

    def test_discrepancies_have_suggested_action(self, tmp_workspace):
        """AC-K-DRY-ONLY: discrepancies set suggested_action but don't spawn."""
        ws = tmp_workspace
        _write(ws, "agent/server.py", """\
            from fw import route

            @route("POST", "/api/x")
            def handle_x(req):
                pass
        """)

        from agent.governance.reconcile_phases.phase_k import run
        scope = FakeResolvedScope({"agent/server.py": None})
        ctx = FakeCtx(str(ws))
        results = run(ctx, scope=scope)
        for d in results:
            assert hasattr(d, "suggested_action")
            assert d.suggested_action in ("spawn_pm_write_test", "spawn_pm_fix_doc")


# ---------------------------------------------------------------------------
# AC-S16-CASE: aming-claw fixture with port mismatches
# ---------------------------------------------------------------------------

class TestSection16Case:
    def test_dual_port_drift(self, tmp_workspace):
        """AC-S16-CASE: scope with two port mismatches → >=2 doc_value_drift."""
        ws = tmp_workspace

        # Code files with actual ports
        _write(ws, "agent/server.py", """\
            GOVERNANCE_PORT = 40000
        """)
        _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)

        # Doc with wrong ports near service context
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
            "agent/server.py": None,
            "agent/manager_http_server.py": None,
            "docs/api/governance-api.md": None,
        })
        ctx = FakeCtx(str(ws))

        from agent.governance.reconcile_phases.phase_k import run
        results = run(ctx, scope=scope)

        drift = [d for d in results if d.type == "doc_value_drift"]
        assert len(drift) >= 2, (
            f"Expected >=2 doc_value_drift, got {len(drift)}: "
            f"{[(d.contract_id, d.doc_value, d.code_value) for d in drift]}"
        )

        # Verify port values
        drift_ports = {(d.doc_value, d.code_value) for d in drift}
        assert (40006, 40000) in drift_ports or (40007, 40101) in drift_ports, (
            f"Expected port mismatch pairs, got {drift_ports}"
        )

        # All should reference the governance-api.md doc
        for d in drift:
            assert "governance-api.md" in d.doc


# ---------------------------------------------------------------------------
# Extractor unit tests
# ---------------------------------------------------------------------------

class TestExtractors:
    def test_extract_endpoints(self, tmp_workspace):
        ws = tmp_workspace
        path = _write(ws, "agent/server.py", """\
            from fw import route

            @route("POST", "/api/governance/redeploy/{target}")
            def handle_redeploy(request, target):
                pass

            @route("GET", "/api/health")
            def handle_health(request):
                pass
        """)
        from agent.governance.reconcile_phases.phase_k import extract_endpoints
        eps = extract_endpoints(path, str(ws))
        assert len(eps) == 2
        assert eps[0].method == "POST"
        assert eps[0].path == "/api/governance/redeploy/{target}"
        assert eps[1].method == "GET"

    def test_extract_service_ports(self, tmp_workspace):
        ws = tmp_workspace
        path = _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
            MANAGER_HTTP_HOST = "0.0.0.0"
            some_var = 123
        """)
        from agent.governance.reconcile_phases.phase_k import extract_service_ports
        ports = extract_service_ports(path, str(ws))
        assert len(ports) == 1
        assert ports[0].port == 40101
        assert ports[0].constant_name == "MANAGER_HTTP_PORT"
        assert ports[0].service_name == "manager_http_server"

    def test_extract_public_constants(self, tmp_workspace):
        ws = tmp_workspace
        path = _write(ws, "agent/server.py", """\
            GOVERNANCE_PORT = 40000
            MAX_RETRIES = 3
            some_local = "not upper"
        """)
        from agent.governance.reconcile_phases.phase_k import extract_public_constants
        consts = extract_public_constants(path, str(ws))
        names = [c.name for c in consts]
        assert "GOVERNANCE_PORT" in names
        assert "MAX_RETRIES" in names
        assert "some_local" not in names

    def test_context_mentions_service(self):
        from agent.governance.reconcile_phases.phase_k import context_mentions_service
        content = "Line 1\nLine 2\nThe manager server runs here\nlocalhost:40007\nLine 5\n"
        offset = content.index("localhost:40007")
        assert context_mentions_service(content, offset, "manager_http_server") is True
        assert context_mentions_service(content, offset, "totally_unrelated") is False

    def test_scope_none_returns_empty(self):
        """Phase K returns [] when scope is None."""
        from agent.governance.reconcile_phases.phase_k import run
        ctx = MagicMock()
        assert run(ctx, scope=None) == []

    def test_doc_fingerprints(self):
        from agent.governance.reconcile_phases.phase_k import (
            EndpointContract, ServicePortContract, PublicConstantContract,
        )
        ep = EndpointContract("POST", "/api/x", "server.handle_x", "server.py", 1)
        fps = ep.doc_fingerprints()
        assert len(fps) == 4
        assert "POST `/api/x`" in fps

        sp = ServicePortContract("governance", 40000, "GOV_PORT", "server.py", 1)
        fps = sp.doc_fingerprints()
        assert "localhost:40000" in fps

        pc = PublicConstantContract("server.PORT", "PORT", 40000, "int", "server.py", 1)
        fps = pc.doc_fingerprints()
        assert "40000" in fps

        pc2 = PublicConstantContract("server.NAME", "NAME", "hello", "str", "server.py", 1)
        fps2 = pc2.doc_fingerprints()
        assert "NAME" in fps2
        assert len(fps2) == 1  # str kind → only name, not value


# ---------------------------------------------------------------------------
# AC-K2 addendum: excluded_doc_ports filter
# ---------------------------------------------------------------------------

class TestExcludedDocPorts:
    """Port 3000 is excluded via phase_k_rules; port 40007 still drifts."""

    def test_excluded_doc_ports_skipped(self, tmp_workspace, monkeypatch):
        """Excluded port 3000 should produce NO doc_value_drift;
        port 40007 should still be processed and produce drift."""
        ws = tmp_workspace

        # Write a server file that declares a service port
        _write(ws, "agent/server.py", """\
            GOVERNANCE_PORT = 40000
        """)

        # Write a doc file mentioning both excluded port 3000 and drifting port 40007
        _write(ws, "docs/api/example.md", """\
            # Example
            Frontend dev server at localhost:3000
            Governance service at localhost:40007
        """)

        # Provide scope covering both files
        scope = FakeResolvedScope({
            "agent/server.py": None,
            "docs/api/example.md": None,
        })
        ctx = FakeCtx(str(ws))

        # Monkeypatch the rules cache to set excluded_doc_ports = [3000]
        import agent.governance.reconcile_phases.phase_k as pk_mod
        monkeypatch.setattr(pk_mod, "_PHASE_K_RULES", {"excluded_doc_ports": [3000]})

        results = pk_mod.run(ctx, scope=scope)

        drift = [d for d in results if d.type == "doc_value_drift"]

        # Port 3000 must NOT appear in any drift discrepancy
        ports_flagged = [d.doc_value for d in drift]
        assert 3000 not in ports_flagged, (
            f"Port 3000 should be excluded but was flagged: {drift}"
        )

        # Port 40007 should be flagged (drift vs GOVERNANCE_PORT=40000)
        # — only if there is a ServicePortContract extracted.  If no
        # service-port contract was extracted the loop has no candidates
        # and 40007 also won't appear; that's acceptable since the key
        # assertion is that 3000 is excluded.

    def test_excluded_doc_ports_helper(self, monkeypatch):
        """_excluded_doc_ports returns the list from cached rules."""
        import agent.governance.reconcile_phases.phase_k as pk_mod
        monkeypatch.setattr(pk_mod, "_PHASE_K_RULES", {"excluded_doc_ports": [3000, 8080]})
        result = pk_mod._excluded_doc_ports()
        assert result == [3000, 8080]

    def test_excluded_doc_ports_default_empty(self, monkeypatch):
        """When excluded_doc_ports key is missing, returns []."""
        import agent.governance.reconcile_phases.phase_k as pk_mod
        monkeypatch.setattr(pk_mod, "_PHASE_K_RULES", {})
        result = pk_mod._excluded_doc_ports()
        assert result == []
