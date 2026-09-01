from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent.governance import mcp_server
from agent.mcp import server as stdio_mcp_server


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
def test_governance_mcp_world_binding_has_no_ac_stable_fallback(monkeypatch):
    monkeypatch.setenv("AMING_CLAW_MCP_PROJECT_ID", "aming-claw")
    monkeypatch.delenv("GOVERNANCE_URL", raising=False)
    assert mcp_server._gov_url() == "http://127.0.0.1:40008"
    monkeypatch.setenv("GOVERNANCE_URL", "http://127.0.0.1:40000")
    with pytest.raises(ValueError, match="40008"):
        mcp_server._gov_url()


def test_governance_mcp_rejects_cross_project_request(monkeypatch):
    monkeypatch.setenv("AMING_CLAW_MCP_PROJECT_ID", "aming-claw")
    monkeypatch.delenv("GOVERNANCE_URL", raising=False)
    result = mcp_server._http(
        "GET",
        "/api/backlog/content-sys",
        {"project_id": "content-sys"},
    )
    assert result["error"] == "mcp_world_project_scope_mismatch"
    assert result["writes_performed"] is False


def test_actual_stdio_mcp_constructor_is_exactly_world_bound(monkeypatch, tmp_path):
    monkeypatch.delenv("GOVERNANCE_URL", raising=False)
    monkeypatch.delenv("AMING_CLAW_DEV_STORAGE_ROOT", raising=False)
    instance = stdio_mcp_server.AmingClawMCP(
        project_id="aming-claw",
        governance_url="",
        workspace=str(tmp_path),
        redis_url="redis://127.0.0.1:40079/0",
        max_workers=0,
    )
    assert instance.gov_url == "http://127.0.0.1:40008"
    assert instance.artifact_root is None
    assert instance.dispatcher._api.__self__ is instance
    assert instance._http(
        "POST",
        "/api/task/content-sys/claim",
        {"project_id": "content-sys"},
    ) == {
        "error": "mcp_world_project_scope_mismatch",
        "writes_performed": False,
        "mutation_performed": False,
    }
    with pytest.raises(ValueError, match="40008"):
        stdio_mcp_server.AmingClawMCP(
            project_id="aming-claw",
            governance_url="http://127.0.0.1:40000",
            workspace=str(tmp_path),
            redis_url="redis://127.0.0.1:40079/0",
            max_workers=0,
        )
    for project_id in ("aming_claw", "content-sys", ""):
        with pytest.raises(ValueError):
            stdio_mcp_server.AmingClawMCP(
                project_id=project_id,
                governance_url="http://127.0.0.1:40008",
                workspace=str(tmp_path),
                redis_url="redis://127.0.0.1:40079/0",
                max_workers=0,
            )

    stable = stdio_mcp_server.AmingClawMCP(
        project_id="charting-loop",
        governance_url="http://localhost:40000",
        workspace=str(tmp_path),
        redis_url="redis://127.0.0.1:40079/0",
        max_workers=0,
    )
    calls = []
    monkeypatch.setattr(
        stable,
        "_request_json",
        lambda method, url, data, timeout: calls.append(
            (method, url, data, timeout)
        ) or {"ok": True},
    )
    assert stable._http(
        "POST",
        "/api/graph-governance/content-sys/query",
        {
            "governance_project_id": "charting-loop",
            "target_project_id": "content-sys",
        },
    ) == {"ok": True}
    assert calls[0][1].startswith("http://127.0.0.1:40000/")


@pytest.mark.parametrize(
    ("project_id", "governance_url"),
    [
        ("stable-governance", "http://127.0.0.1:40000"),
        ("aming-claw", "http://127.0.0.1:40008"),
    ],
)
def test_control_only_stdio_starts_with_world_listener_offline_and_no_dev_storage(
    project_id,
    governance_url,
):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.pop("AMING_CLAW_DEV_STORAGE_ROOT", None)
    env.pop("SHARED_VOLUME_PATH", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent.mcp.server",
            "--project",
            project_id,
            "--workers",
            "0",
            "--governance-url",
            governance_url,
        ],
        cwd=root,
        env=env,
        input=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        + "\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    response = json.loads(proc.stdout.strip())
    assert response["id"] == 1
    assert response["result"]["tools"]


@pytest.mark.parametrize(
    ("max_workers", "autostart_executor"),
    [(1, False), (0, True)],
)
def test_ac_artifact_owning_mcp_requires_explicit_dev_storage(
    monkeypatch,
    tmp_path,
    max_workers,
    autostart_executor,
):
    monkeypatch.delenv("AMING_CLAW_DEV_STORAGE_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="explicit AMING_CLAW_DEV_STORAGE_ROOT"):
        stdio_mcp_server.AmingClawMCP(
            project_id="aming-claw",
            governance_url="http://127.0.0.1:40008",
            workspace=str(tmp_path),
            redis_url="redis://127.0.0.1:40079/0",
            max_workers=max_workers,
            autostart_executor=autostart_executor,
        )


def test_tools_call_world_scope_gate_rejects_before_first_http(monkeypatch, tmp_path):
    monkeypatch.delenv("AMING_CLAW_DEV_STORAGE_ROOT", raising=False)
    stable = stdio_mcp_server.AmingClawMCP(
        project_id="stable-governance",
        governance_url="http://127.0.0.1:40000",
        workspace=str(tmp_path),
        redis_url="redis://127.0.0.1:40079/0",
        max_workers=0,
    )
    dev = stdio_mcp_server.AmingClawMCP(
        project_id="aming-claw",
        governance_url="http://127.0.0.1:40008",
        workspace=str(tmp_path),
        redis_url="redis://127.0.0.1:40079/0",
        max_workers=0,
    )
    stable_calls = []
    dev_calls = []
    monkeypatch.setattr(
        stable,
        "_request_json",
        lambda *args, **kwargs: stable_calls.append((args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        dev,
        "_request_json",
        lambda *args, **kwargs: dev_calls.append((args, kwargs)) or {"ok": True},
    )

    expected = {
        "error": "mcp_world_project_scope_mismatch",
        "writes_performed": False,
        "mutation_performed": False,
    }
    assert stable._dispatch_tool_call(
        "runtime_status",
        {"project_id": "aming-claw"},
    ) == expected
    assert stable._http("GET", "/api/version-check/aming-claw") == expected
    assert stable_calls == []

    assert dev._dispatch_tool_call(
        "runtime_status",
        {"project_id": "drift-gym"},
    ) == expected
    assert dev_calls == []


@pytest.mark.parametrize("project_id", ["", " aming-claw", "aming-claw ", "amingClaw", "aming_claw"])
def test_mcp_plane_identity_rejects_raw_noncanonical_project_ids(project_id):
    with pytest.raises(ValueError):
        stdio_mcp_server._canonical_governance_url(project_id, "")
    with pytest.raises(ValueError):
        stdio_mcp_server.AmingClawMCP(
            project_id=project_id,
            governance_url="",
            workspace="/must-not-be-created",
            redis_url="redis://127.0.0.1:40079/0",
            max_workers=0,
        )
