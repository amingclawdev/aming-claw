"""Tests for documentation restructuring Phase 3 — README, config docs, CODE_DOC_MAP, redirect stubs."""

import os
import re

# All paths relative to project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(relpath):
    full = os.path.join(PROJECT_ROOT, relpath)
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


def _exists(relpath):
    return os.path.isfile(os.path.join(PROJECT_ROOT, relpath))


# ---------- AC1: No operational facts in README ----------

def test_ac1_no_operational_facts_in_readme():
    """No port numbers, no REST API endpoint paths, no env config values.

    Note: docs/api/ folder references are doc links, not REST endpoints.
    AC1 intent is to remove operational details like :40000, POST /api/task, PORT=, etc.
    """
    content = _read("README.md")
    # Port numbers
    assert not re.search(r':\d{4,5}', content), "README.md contains port numbers"
    # REST endpoint paths (preceded by space, HTTP method, or line-start — not 'docs')
    rest_endpoints = re.findall(r'(?<!docs)(?<!doc)/api/\w+', content)
    assert len(rest_endpoints) == 0, f"README.md contains REST endpoint paths: {rest_endpoints}"
    # Env var config values
    assert "PORT=" not in content
    assert "MEMORY_BACKEND" not in content
    # curl commands / localhost references
    assert "localhost" not in content
    assert "curl" not in content


# ---------- AC2: Get Started links ----------

def test_ac2_get_started_links():
    content = _read("README.md")
    assert "## Get Started" in content
    assert "docs/architecture.md" in content
    assert "docs/deployment.md" in content
    assert "observer" in content.lower()


# ---------- AC3: Deep Dive section ----------

def test_ac3_deep_dive_section():
    content = _read("README.md")
    assert "## Deep Dive" in content or "## Deep dive" in content
    assert "docs/roles/README.md" in content
    assert "docs/governance/README.md" in content
    assert "docs/config/README.md" in content
    assert "docs/api/README.md" in content


# ---------- AC4: Config schema docs exist with content ----------

def test_ac4_config_aming_claw_yaml():
    content = _read("docs/config/aming-claw-yaml.md")
    assert ".aming-claw.yaml" in content
    assert "project_id" in content


def test_ac4_config_mcp_json():
    content = _read("docs/config/mcp-json.md")
    assert ".mcp.json" in content
    assert "mcpServers" in content


def test_ac4_config_role_permissions():
    content = _read("docs/config/role-permissions.md")
    assert "# " in content
    assert "Permission" in content


# ---------- AC5: Config README links ----------

def test_ac5_config_readme_links():
    content = _read("docs/config/README.md")
    assert "aming-claw-yaml.md" in content
    assert "mcp-json.md" in content
    assert "role-permissions.md" in content


# ---------- AC6: CODE_DOC_MAP canonical mappings ----------

def test_ac6_code_doc_map_mappings():
    content = _read("agent/governance/impact_analyzer.py")
    assert '"docs/governance/auto-chain.md"' in content
    assert '"docs/api/governance-api.md"' in content
    assert '"docs/api/executor-api.md"' in content
    assert '"docs/governance/memory.md"' in content
    assert '"docs/governance/conflict-rules.md"' in content
    assert '"docs/config/role-permissions.md"' in content
    assert '"docs/governance/gates.md"' in content
    assert '"docs/governance/acceptance-graph.md"' in content


# ---------- AC7: No old doc references ----------

def test_ac7_no_old_doc_references():
    content = _read("agent/governance/impact_analyzer.py")
    assert "p0-3-design" not in content
    assert "ai-agent-integration-guide" not in content
    assert "human-intervention-guide" not in content


# ---------- AC8: Roles README entries ----------

def test_ac8_roles_readme_entries():
    content = _read("docs/roles/README.md")
    assert "tester.md" in content
    assert "qa.md" in content
    assert "gatekeeper.md" in content


# ---------- AC9: Gatekeeper doc exists ----------

def test_ac9_gatekeeper_exists():
    content = _read("docs/roles/gatekeeper.md")
    assert "# " in content


# ---------- AC10: Redirect stubs ----------

def test_ac10_guide_coordinator_redirect():
    content = _read("docs/guide-coordinator.md")
    assert "roles/coordinator.md" in content
    assert "moved" in content.lower() or "see" in content.lower()


def test_ac10_observer_feature_guide_redirect():
    content = _read("docs/observer-feature-guide.md")
    assert "roles/observer.md" in content
    assert "moved" in content.lower() or "see" in content.lower()


def test_ac10_human_intervention_redirect():
    content = _read("docs/human-intervention-guide.md")
    assert "governance/" in content
    assert "moved" in content.lower() or "see" in content.lower()


# ---------- AC11: PM role reachable in 2 clicks ----------

def test_ac11_pm_reachable():
    readme = _read("README.md")
    assert "roles/README" in readme
    roles_readme = _read("docs/roles/README.md")
    assert "pm.md" in roles_readme


# ---------- AC12: All CODE_DOC_MAP files exist ----------

def test_ac12_code_doc_map_files_exist():
    content = _read("agent/governance/impact_analyzer.py")
    doc_paths = re.findall(r'"(docs/[^"]+)"', content)
    missing = [p for p in doc_paths if not _exists(p)]
    assert len(missing) == 0, f"Missing doc files referenced in CODE_DOC_MAP: {missing}"
