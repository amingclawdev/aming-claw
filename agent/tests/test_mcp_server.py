from __future__ import annotations

from agent.governance import mcp_server


def test_contract_runtime_bypass_mcp_exposes_only_opaque_cross_plane_authority(
    monkeypatch,
):
    properties = mcp_server._contract_runtime_bypass_line_schema_properties()
    assert {
        "observer_session_id",
        "observer_route_token_ref",
        "route_token_ref",
    }.issubset(properties)
    assert not {
        "namespace",
        "namespace_hash",
        "physical_namespace",
        "storage_contract_id",
        "runtime_port",
        "target_project_root",
        "runtime_world_authority",
        "world_hash",
        "authority_hash",
    }.intersection(properties)

    calls = []

    def request(method, path, body, *, gov_token="", **_kwargs):
        calls.append((method, path, body, gov_token))
        return {"ok": True, "decision": {"no_pass_claim": True}}

    monkeypatch.setattr(mcp_server, "_http_with_optional_gov_token", request)
    args = {
        "project_id": "aming-claw",
        "contract_execution_id": "cex-direct-main-exact",
        "bypass_identity": "bypass:cex-direct-main-exact:1",
        "stage_id": "route_gate",
        "line_id": "observer_bind_direct_scope",
        "execution_state_revision": 1,
        "runtime_guide_hash": "sha256:" + "1" * 64,
        "classification": "system_logic",
        "reason": "exact shared-plane blocker",
        "decision": "waive without PASS",
        "observer_session_id": "obs-exact",
        "observer_route_token_ref": "rtok-exact",
        "qa_session_token": "raw-credential-must-be-header-only",
    }
    result = mcp_server._dispatch_tool("contract_runtime_bypass_line", args)
    assert result == {"ok": True, "decision": {"no_pass_claim": True}}
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/contract-runtime/"
            "cex-direct-main-exact/line-bypasses",
            {
                key: value
                for key, value in args.items()
                if key not in {
                    "project_id",
                    "contract_execution_id",
                    "qa_session_token",
                }
            },
            "raw-credential-must-be-header-only",
        )
    ]
    assert "raw-credential" not in repr(calls[0][2])
