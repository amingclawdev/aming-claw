from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.governance.reconcile_semantic_config import (
    DEFAULT_CONFIG_PATH,
    PROJECT_OVERRIDE_PATH,
    SemanticConfigValidationError,
    load_semantic_enrichment_config,
)
from agent.governance import reconcile_semantic_ai
from agent.governance.reconcile_semantic_ai import build_semantic_ai_call, resolve_semantic_ai_route


def test_default_semantic_config_loads_state_only_profile():
    config = load_semantic_enrichment_config()

    assert config.analyzer == "reconcile_semantic"
    assert config.provider == "pipeline"
    assert config.role == "pm"
    assert config.use_ai_default is False
    assert "modify_code" not in config.permissions_can
    assert "mutate_graph_topology" in config.permissions_cannot
    payload = config.to_instruction_payload()
    assert payload["mutate_project_files"] is False
    assert payload["mutate_graph_topology"] is False
    assert payload["prompt_template"]
    assert Path(DEFAULT_CONFIG_PATH).exists()


def test_project_override_merges_with_default(tmp_path):
    project = tmp_path / "project"
    override_path = project / PROJECT_OVERRIDE_PATH
    override_path.parent.mkdir(parents=True)
    override_path.write_text(
        "\n".join(
            [
                'model: "gpt-test-semantic"',
                "use_ai_default: true",
                "input_policy:",
                "  max_excerpt_chars: 77",
                "prompt_template: |-",
                "  Custom project semantic analyzer prompt.",
            ]
        ),
        encoding="utf-8",
    )

    config = load_semantic_enrichment_config(project_root=project)

    assert config.model == "gpt-test-semantic"
    assert config.use_ai_default is True
    assert config.input_policy.max_excerpt_chars == 77
    assert config.prompt_template == "Custom project semantic analyzer prompt."
    assert "read_graph_snapshot" in config.permissions_can
    assert config.override_path == str(override_path)


def test_semantic_ai_route_can_be_enabled_by_env(monkeypatch):
    config = load_semantic_enrichment_config()
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_PROVIDER", "openai")
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_MODEL", "gpt-test-semantic")

    route = resolve_semantic_ai_route(config)

    assert route["provider"] == "openai"
    assert route["model"] == "gpt-test-semantic"
    assert route["source"] == "env"


def test_semantic_ai_openai_call_streams_prompt_on_stdin(monkeypatch, tmp_path):
    config = load_semantic_enrichment_config()
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_PROVIDER", "openai")
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_MODEL", "gpt-test-semantic")
    monkeypatch.setenv("CODEX_BIN", "codex-test")
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        calls.append({"cmd": cmd, "input": kwargs.get("input")})
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            '{"feature_name":"Governance Trace","semantic_summary":"Trace is auditable."}',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_semantic_ai.subprocess, "run", fake_run)
    ai_call = build_semantic_ai_call(
        semantic_config=config,
        project_id="aming-claw",
        snapshot_id="full-test",
        project_root=tmp_path,
    )

    result = ai_call("reconcile_semantic_feature", {"feature": {"node_id": "L7.1"}})

    assert calls
    assert calls[0]["cmd"][-1] == "-"
    assert "Payload:" in calls[0]["input"]
    assert result["feature_name"] == "Governance Trace"
    assert result["_ai_route"]["provider"] == "openai"


def test_semantic_ai_rejects_error_only_json(monkeypatch, tmp_path):
    config = load_semantic_enrichment_config()
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_PROVIDER", "openai")
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_MODEL", "gpt-test-semantic")
    monkeypatch.setenv("CODEX_BIN", "codex-test")

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"error":"Cannot read supplied payload."}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_semantic_ai.subprocess, "run", fake_run)
    ai_call = build_semantic_ai_call(
        semantic_config=config,
        project_id="aming-claw",
        snapshot_id="full-test",
        project_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="error response"):
        ai_call("reconcile_semantic_feature", {"feature": {"node_id": "L7.1"}})


def test_semantic_config_rejects_mutation_permissions(tmp_path):
    cfg = tmp_path / "semantic.yaml"
    cfg.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "analyzer: reconcile_semantic",
                "permissions:",
                "  can:",
                "    - modify_code",
                "prompt_template: unsafe",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SemanticConfigValidationError, match="mutation permissions"):
        load_semantic_enrichment_config(config_path=cfg)
