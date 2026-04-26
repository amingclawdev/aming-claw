"""Tests for Phase K confidence threshold ladder.

Covers OPT-BACKLOG-PHASE-K-HIGH-CONFIDENCE-THRESHOLD:
  - doc_value_drift score-floor + gap ladder
  - contract_no_test handler_qname qualification + fingerprint evidence
  - endpoint drift (Step 3a) unchanged high confidence
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, FrozenSet, Set

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
# doc_value_drift confidence ladder
# ---------------------------------------------------------------------------

class TestDocDriftConfidence:
    def test_doc_drift_high_requires_min_score(self, tmp_workspace, monkeypatch):
        """doc_value_drift confidence='low' when best_score < _HIGH_CONFIDENCE_MIN_SCORE
        and multiple candidates exist (single candidate is always high)."""
        ws = tmp_workspace
        # Two service ports with low-context doc (no /api/ path prefix, no constant)
        # → both score low but >0, triggering multi-candidate low-score path
        _write(ws, "agent/myservice.py", """\
            MY_PORT = 9000
        """)
        _write(ws, "agent/otherservice.py", """\
            OTHER_PORT = 9001
        """)
        doc_path = _write(ws, "docs/notes.md", """\
            # Notes
            Some server at localhost:9999
        """)

        import agent.governance.reconcile_phases.phase_k as pk_mod
        monkeypatch.setattr(pk_mod, "_PHASE_K_RULES", {"excluded_doc_ports": []})

        scope = FakeResolvedScope({
            "agent/myservice.py": None,
            "agent/otherservice.py": None,
            doc_path: None,
        })
        ctx = FakeCtx(str(ws))
        results = pk_mod.run(ctx, scope=scope)

        drift = [d for d in results if d.type == "doc_value_drift"
                 and d.contract_kind == "ServicePortContract"]
        # With multiple candidates and low scores, confidence must NOT be 'high'
        for d in drift:
            assert d.confidence in ("low", "medium"), (
                f"Expected low/medium confidence for low-score multi-candidate match, got {d.confidence}"
            )

    def test_doc_drift_high_requires_gap(self, tmp_workspace, monkeypatch):
        """doc_value_drift confidence='medium' when gap < _HIGH_CONFIDENCE_MIN_GAP."""
        ws = tmp_workspace
        # Two services with similar context → both score high but close gap
        _write(ws, "agent/alpha_server.py", """\
            ALPHA_PORT = 8001
        """)
        _write(ws, "agent/alpha_worker.py", """\
            ALPHA_WORKER_PORT = 8002
        """)
        doc_path = _write(ws, "docs/alpha.md", """\
            # Alpha Service

            The alpha server runs on localhost:7777

            ```bash
            curl http://localhost:7777/api/alpha/health
            ```
        """)

        import agent.governance.reconcile_phases.phase_k as pk_mod
        monkeypatch.setattr(pk_mod, "_PHASE_K_RULES", {"excluded_doc_ports": []})

        scope = FakeResolvedScope({
            "agent/alpha_server.py": None,
            "agent/alpha_worker.py": None,
            doc_path: None,
        })
        ctx = FakeCtx(str(ws))
        results = pk_mod.run(ctx, scope=scope)

        drift = [d for d in results if d.type == "doc_value_drift"
                 and d.contract_kind == "ServicePortContract"
                 and d.doc_value == 7777]
        # With two close candidates, confidence should be medium (gap < 1.5)
        for d in drift:
            assert d.confidence in ("medium", "low"), (
                f"Expected medium/low when gap is small, got {d.confidence}"
            )

    def test_doc_drift_high_with_clear_winner(self, tmp_workspace, monkeypatch):
        """doc_value_drift confidence='high' when score>=5.0 and clear gap to runner-up."""
        ws = tmp_workspace
        # Manager service with strong context (path prefix match → +5.0)
        srv_path = _write(ws, "agent/manager_http_server.py", """\
            MANAGER_HTTP_PORT = 40101
        """)
        # Second service with weaker context
        _write(ws, "agent/other_server.py", """\
            OTHER_PORT = 40200
        """)
        doc_path = _write(ws, "docs/api/manager.md", """\
            # Manager HTTP Server

            The manager HTTP server runs on localhost:40007.

            ```bash
            curl http://localhost:40007/api/manager/health
            ```
        """)

        import agent.governance.reconcile_phases.phase_k as pk_mod
        monkeypatch.setattr(pk_mod, "_PHASE_K_RULES", {"excluded_doc_ports": []})

        scope = FakeResolvedScope({
            srv_path: None,
            "agent/other_server.py": None,
            doc_path: None,
        })
        ctx = FakeCtx(str(ws))
        results = pk_mod.run(ctx, scope=scope)

        drift = [d for d in results if d.type == "doc_value_drift"
                 and d.contract_kind == "ServicePortContract"
                 and d.doc_value == 40007]
        assert len(drift) >= 1, f"Expected drift for port 40007, got {results}"
        assert drift[0].confidence == "high", (
            f"Expected high confidence for clear winner, got {drift[0].confidence}"
        )

    def test_doc_drift_high_with_tie(self, tmp_workspace, monkeypatch):
        """doc_value_drift confidence='medium' when top two candidates tie."""
        ws = tmp_workspace
        # Two services with identical scoring context
        _write(ws, "agent/svc_a.py", """\
            SVC_A_PORT = 5001
        """)
        _write(ws, "agent/svc_b.py", """\
            SVC_B_PORT = 5002
        """)
        doc_path = _write(ws, "docs/services.md", """\
            # Services

            A generic service runs at localhost:5555

            ```bash
            curl http://localhost:5555/api/svc_a/health
            curl http://localhost:5555/api/svc_b/health
            ```
        """)

        import agent.governance.reconcile_phases.phase_k as pk_mod
        monkeypatch.setattr(pk_mod, "_PHASE_K_RULES", {"excluded_doc_ports": []})

        scope = FakeResolvedScope({
            "agent/svc_a.py": None,
            "agent/svc_b.py": None,
            doc_path: None,
        })
        ctx = FakeCtx(str(ws))
        results = pk_mod.run(ctx, scope=scope)

        drift = [d for d in results if d.type == "doc_value_drift"
                 and d.contract_kind == "ServicePortContract"
                 and d.doc_value == 5555]
        for d in drift:
            assert d.confidence in ("medium", "low"), (
                f"Expected medium/low for tied candidates, got {d.confidence}"
            )


# ---------------------------------------------------------------------------
# contract_no_test confidence ladder
# ---------------------------------------------------------------------------

class TestContractNoTestConfidence:
    def test_contract_no_test_low_for_short_fingerprint(self):
        """contract_no_test confidence='low' for unqualified qname + short fingerprints."""
        from agent.governance.reconcile_phases.phase_k import _contract_no_test_confidence
        # No dots, short fingerprints
        result = _contract_no_test_confidence("handle_x", ["POST", "x"])
        assert result == "low"

    def test_contract_no_test_high_for_qualified_qname(self):
        """contract_no_test confidence='high' for qualified qname (>=1 dot) + long fingerprint."""
        from agent.governance.reconcile_phases.phase_k import _contract_no_test_confidence
        result = _contract_no_test_confidence(
            "agent.server.handle_x",
            ["POST `/api/governance/redeploy`", "endpoint.*/api/x"],
        )
        assert result == "high"

    def test_contract_no_test_medium_for_simple_qname(self):
        """contract_no_test confidence='medium' for unqualified qname but long fingerprint."""
        from agent.governance.reconcile_phases.phase_k import _contract_no_test_confidence
        # No dots but has long fingerprint
        result = _contract_no_test_confidence(
            "handle_x",
            ["POST `/api/governance/redeploy`"],
        )
        assert result == "medium"


# ---------------------------------------------------------------------------
# Endpoint drift (Step 3a) still high
# ---------------------------------------------------------------------------

class TestEndpointDriftStillHigh:
    def test_existing_high_confidence_endpoint_drift_still_high(self, tmp_workspace):
        """Endpoint drift (Step 3a) still emits confidence='high' — explicit method+path match."""
        ws = tmp_workspace
        # Server with endpoint and port
        _write(ws, "agent/server.py", """\
            from fw import route
            GOVERNANCE_PORT = 40000

            @route("POST", "/api/governance/redeploy/{target}")
            def handle_redeploy(request, target):
                pass
        """)
        # Doc referencing the endpoint with wrong port
        _write(ws, "docs/api/governance-api.md", """\
            # Governance API

            ## Redeploy

            ```bash
            curl -X POST http://localhost:99999/api/governance/redeploy/{target}
            ```
        """)

        import agent.governance.reconcile_phases.phase_k as pk_mod
        monkeypatch_rules = {"excluded_doc_ports": []}

        scope = FakeResolvedScope({
            "agent/server.py": None,
            "docs/api/governance-api.md": None,
        })
        ctx = FakeCtx(str(ws))

        # Temporarily set rules
        old_rules = pk_mod._PHASE_K_RULES
        pk_mod._PHASE_K_RULES = monkeypatch_rules
        try:
            results = pk_mod.run(ctx, scope=scope)
        finally:
            pk_mod._PHASE_K_RULES = old_rules

        ep_drift = [d for d in results if d.type == "doc_value_drift"
                    and d.contract_kind == "EndpointContract"]
        assert len(ep_drift) >= 1, f"Expected endpoint drift, got {results}"
        for d in ep_drift:
            assert d.confidence == "high", (
                f"Endpoint drift must remain high confidence, got {d.confidence}"
            )
