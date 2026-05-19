from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.governance.graph_enrich_config_ops import SCHEMA_VERSION
from agent.governance.reconcile_semantic_config import PROJECT_OVERRIDE_PATH


@dataclass(frozen=True)
class ExternalSemanticProject:
    root: Path

    @property
    def override_path(self) -> Path:
        return self.root / PROJECT_OVERRIDE_PATH


def create_external_semantic_project(root: Path) -> ExternalSemanticProject:
    """Create a small user-owned project for semantic config isolation tests."""
    root.mkdir(parents=True, exist_ok=True)
    _write(root / "agent" / "__init__.py", "")
    _write(
        root / "agent" / "storage.py",
        "def load_state():\n"
        "    return {'status': 'ok'}\n",
    )
    _write(
        root / "agent" / "service.py",
        "from agent.storage import load_state\n\n"
        "def service_entry():\n"
        "    return load_state()['status']\n",
    )
    _write(
        root / "agent" / "tests" / "test_service.py",
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
    )
    return ExternalSemanticProject(root=root)


def project_local_policy_payload(*, rule_id: str = "project.calls.import_only.downgrade") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "upsert_edge_evidence_policy",
                "rule_id": rule_id,
                "edge": "calls",
                "source_evidence": "import_only",
                "action": "downgrade",
                "downgrade_to": "imports",
                "confidence": 0.94,
                "evidence": {
                    "reason": "Project-local imports should not become calls edges.",
                },
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": [
                "schema_version",
                "op_supported",
                "edge_supported_or_canonical_alias",
                "config_patch_previewed",
                "observer_approval_required",
            ],
            "known_risks": [],
        },
    }


def register_function_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "register_function",
                "rule_id": "project.python.custom_call_resolver",
                "edge": "calls",
                "source_evidence": "semantic_feedback",
                "action": "add",
                "confidence": 0.72,
                "function_name": "resolve_project_specific_calls",
                "proposal_scope": "upstream",
                "evidence": {
                    "reason": (
                        "The project appears to need a new analyzer function. "
                        "This must become an upstream proposal, not executable project config."
                    ),
                },
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": [
                "schema_version",
                "function_registration_requires_upstream_review",
                "no_project_code_execution",
            ],
            "known_risks": ["register_function is not an executable project-local config op"],
        },
    }


def register_function_semantic_payload() -> dict[str, Any]:
    return {
        "graph_enrich_config_suggestions": [
            register_function_payload()["operations"][0],
        ],
        "open_issues": [
            {
                "type": "upstream_function_proposal",
                "summary": "Project-specific call resolver may be generally useful upstream.",
            }
        ],
    }


def core_semantic_config_texts(repo_root: Path) -> dict[str, str | None]:
    paths = [
        repo_root / "config" / "reconcile" / "semantic_enrichment.yaml",
        repo_root / PROJECT_OVERRIDE_PATH,
    ]
    return {str(path): _read_text_or_none(path) for path in paths}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_text_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")
