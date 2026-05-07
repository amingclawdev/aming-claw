from __future__ import annotations

from pathlib import Path

import pytest

from agent.governance.reconcile_semantic_config import (
    DEFAULT_CONFIG_PATH,
    PROJECT_OVERRIDE_PATH,
    SemanticConfigValidationError,
    load_semantic_enrichment_config,
)


def test_default_semantic_config_loads_state_only_profile():
    config = load_semantic_enrichment_config()

    assert config.analyzer == "reconcile_semantic"
    assert config.provider == "injected"
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
