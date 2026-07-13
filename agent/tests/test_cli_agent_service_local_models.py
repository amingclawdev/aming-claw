import json
import sys
from pathlib import Path

import pytest


AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))


def _executable(tmp_path, name):
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _codex_endpoint(*, auth_mode="none"):
    from cli_agent_service.models import InferenceEndpoint

    return InferenceEndpoint(
        endpoint_id="endpoint-ollama",
        provider="ollama",
        model="qwen3-coder",
        backend_mode="codex_oss",
        auth_mode=auth_mode,
        endpoint_kind="local_model",
    )


def _claude_endpoint(*, auth_mode="credential_ref"):
    from cli_agent_service.models import InferenceEndpoint

    return InferenceEndpoint(
        endpoint_id="endpoint-claude-gateway",
        provider="anthropic_compatible",
        model="gateway-sonnet",
        backend_mode="claude_gateway",
        auth_mode=auth_mode,
        endpoint_kind="claude_compatible_gateway",
    )


def test_codex_oss_is_an_endpoint_adapter_without_an_account_profile(tmp_path):
    from cli_agent_service.adapters.codex_oss import CodexOssAdapter

    executable = _executable(tmp_path, "codex")
    launch = CodexOssAdapter(executable=str(executable)).build_launch_spec(
        _codex_endpoint(),
        worktree=tmp_path,
        output_path=tmp_path / "last.txt",
    )

    assert launch.command[:3] == (str(executable), "exec", "--oss")
    assert "--local-provider" in launch.command
    assert "ollama" in launch.command
    assert "qwen3-coder" in launch.command
    assert "--sandbox" in launch.command
    assert "--dangerously-bypass-approvals-and-sandbox" not in launch.command
    assert launch.environment is None
    assert "profile" not in json.dumps(launch.to_public_dict(), sort_keys=True)


def test_codex_oss_rejects_subscription_auth(tmp_path):
    from cli_agent_service.adapters.codex_oss import (
        CodexOssAdapter,
        CodexOssAdapterError,
    )

    with pytest.raises(CodexOssAdapterError, match="cannot use subscription"):
        CodexOssAdapter(executable=str(_executable(tmp_path, "codex"))).build_launch_spec(
            _codex_endpoint(auth_mode="cli_auth"),
            worktree=tmp_path,
            output_path=tmp_path / "last.txt",
        )


def test_claude_gateway_spec_never_contains_credential_ref_or_value(tmp_path):
    from cli_agent_service.adapters.claude_gateway import ClaudeGatewayAdapter
    from cli_agent_service.models import CredentialRef

    executable = _executable(tmp_path, "claude")
    credential_ref = "credential:claude-gateway:local"
    secret_value = "private-gateway-value"
    adapter = ClaudeGatewayAdapter(
        executable=str(executable),
        endpoint_url="http://127.0.0.1:8080/v1",
        credential_ref=CredentialRef(
            ref_id=credential_ref,
            provider="claude_gateway",
            ref_kind="host_owned",
        ),
    )
    launch = adapter.build_launch_spec(_claude_endpoint(), worktree=tmp_path)
    rendered = "\n".join(
        (
            repr(adapter),
            repr(launch),
            json.dumps(launch.to_public_dict(), sort_keys=True),
            " ".join(launch.command),
        )
    )

    assert credential_ref not in rendered
    assert secret_value not in rendered
    assert launch.environment == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8080/v1"}
    assert "ANTHROPIC_AUTH_TOKEN" not in launch.environment
    assert "--model" in launch.command
    assert "gateway-sonnet" in launch.command

    private_environment = dict(launch.environment)
    seen_refs = []

    def resolve(ref):
        seen_refs.append(ref)
        return secret_value

    adapter.apply_credential(private_environment, resolve)
    assert seen_refs == [credential_ref]
    assert private_environment["ANTHROPIC_AUTH_TOKEN"] == secret_value
    assert "ANTHROPIC_AUTH_TOKEN" not in launch.environment
    assert secret_value not in repr(launch)


def test_gateway_errors_and_urls_do_not_echo_credential_material(tmp_path):
    from cli_agent_service.adapters.claude_gateway import (
        ClaudeGatewayAdapter,
        ClaudeGatewayAdapterError,
    )

    raw = "private-gateway-value"
    with pytest.raises(ClaudeGatewayAdapterError) as invalid_url:
        ClaudeGatewayAdapter(endpoint_url="https://user:{}@gateway.invalid".format(raw))
    assert raw not in str(invalid_url.value)

    adapter = ClaudeGatewayAdapter(
        executable=str(_executable(tmp_path, "claude")),
        endpoint_url="http://127.0.0.1:8080",
        credential_ref="credential:claude-gateway:local",
    )
    with pytest.raises(ClaudeGatewayAdapterError) as resolution:
        adapter.apply_credential({}, lambda _ref: (_ for _ in ()).throw(RuntimeError(raw)))
    assert raw not in str(resolution.value)
    assert raw not in repr(resolution.value)


def test_gateway_without_auth_needs_no_fake_profile_or_credential(tmp_path):
    from cli_agent_service.adapters.claude_gateway import ClaudeGatewayAdapter

    launch = ClaudeGatewayAdapter(
        executable=str(_executable(tmp_path, "claude")),
        endpoint_url="http://127.0.0.1:8080",
    ).build_launch_spec(
        _claude_endpoint(auth_mode="none"),
        worktree=tmp_path,
    )

    assert "profile" not in json.dumps(launch.to_public_dict(), sort_keys=True)
    assert "credential" not in " ".join(launch.command).lower()


def test_adapter_package_exports_local_endpoint_adapters():
    from cli_agent_service import adapters

    assert adapters.CodexOssEndpointAdapter is adapters.CodexOssAdapter
    assert adapters.ClaudeCompatibleGatewayAdapter is adapters.ClaudeGatewayAdapter
