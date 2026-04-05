"""Tests for agent.governance.doc_policy — unified doc-gate policy module."""

import pytest
from agent.governance.doc_policy import (
    is_governed_doc,
    is_dev_artifact,
    is_test_fixture,
    is_doc_related,
    is_governance_internal_repair,
    should_require_docs,
)


# ---------------------------------------------------------------------------
# AC4: governed doc classification
# ---------------------------------------------------------------------------

class TestGovernedDocClassification:
    def test_docs_md_is_governed(self):
        assert is_governed_doc("docs/architecture.md") is True

    def test_docs_nested_md_is_governed(self):
        assert is_governed_doc("docs/api/endpoints.md") is True

    def test_docs_dev_not_governed(self):
        assert is_governed_doc("docs/dev/notes.md") is False

    def test_non_docs_not_governed(self):
        assert is_governed_doc("agent/governance/auto_chain.py") is False

    def test_docs_non_md_not_governed(self):
        assert is_governed_doc("docs/image.png") is False

    def test_is_doc_related_for_governed(self):
        assert is_doc_related("docs/architecture.md") is True


# ---------------------------------------------------------------------------
# AC2: dev artifact classification
# ---------------------------------------------------------------------------

class TestDevArtifactClassification:
    def test_docs_dev_is_artifact(self):
        assert is_dev_artifact("docs/dev/roadmap.md") is True

    def test_docs_dev_nested_is_artifact(self):
        assert is_dev_artifact("docs/dev/notes/scratch.md") is True

    def test_docs_root_not_artifact(self):
        assert is_dev_artifact("docs/architecture.md") is False

    def test_is_doc_related_false_for_dev_artifact(self):
        """AC2: dev artifacts return False from is_doc_related."""
        assert is_doc_related("docs/dev/roadmap.md") is False

    def test_backslash_normalization(self):
        assert is_dev_artifact("docs\\dev\\notes.md") is True


# ---------------------------------------------------------------------------
# AC3: test fixture classification
# ---------------------------------------------------------------------------

class TestTestFixtureClassification:
    def test_agent_tests_is_fixture(self):
        assert is_test_fixture("agent/tests/test_doc_policy.py") is True

    def test_agent_tests_conftest_is_fixture(self):
        assert is_test_fixture("agent/tests/conftest.py") is True

    def test_agent_tests_nested(self):
        assert is_test_fixture("agent/tests/fixtures/data.json") is True

    def test_non_test_path(self):
        assert is_test_fixture("agent/governance/auto_chain.py") is False

    def test_is_doc_related_true_for_tests(self):
        """AC3: test fixtures are always-related."""
        assert is_doc_related("agent/tests/test_doc_policy.py") is True

    def test_is_doc_related_true_for_test_conftest(self):
        assert is_doc_related("agent/tests/conftest.py") is True


# ---------------------------------------------------------------------------
# AC5 (partial): governance internal repair detection
# ---------------------------------------------------------------------------

class TestGovernanceInternalRepair:
    def test_all_governance_files(self):
        meta = {"target_files": ["agent/governance/auto_chain.py"]}
        changed = ["agent/governance/doc_policy.py"]
        assert is_governance_internal_repair(meta, changed) is True

    def test_with_test_files(self):
        meta = {"target_files": ["agent/governance/auto_chain.py"]}
        changed = ["agent/governance/doc_policy.py", "agent/tests/test_doc_policy.py"]
        assert is_governance_internal_repair(meta, changed) is True

    def test_non_governance_file_returns_false(self):
        meta = {"target_files": ["agent/governance/auto_chain.py"]}
        changed = ["docs/architecture.md"]
        assert is_governance_internal_repair(meta, changed) is False

    def test_empty_files_returns_false(self):
        assert is_governance_internal_repair({}, []) is False

    def test_role_permissions_is_governance(self):
        meta = {"target_files": ["agent/role_permissions.py"]}
        changed = ["agent/role_permissions.py"]
        assert is_governance_internal_repair(meta, changed) is True


# ---------------------------------------------------------------------------
# should_require_docs
# ---------------------------------------------------------------------------

class TestShouldRequireDocs:
    def test_empty_changed_files(self):
        ok, missing = should_require_docs([], {})
        assert ok is False
        assert missing == set()

    def test_governance_internal_bypass(self):
        meta = {"target_files": ["agent/governance/auto_chain.py"]}
        ok, missing = should_require_docs(["agent/governance/doc_policy.py"], meta)
        assert ok is False

    def test_doc_impact_empty_files_list(self):
        meta = {"doc_impact": {"files": []}}
        ok, missing = should_require_docs(["src/app.py"], meta)
        assert ok is False
        assert missing == set()

    def test_dev_artifact_filtered_from_expected(self):
        """Dev artifacts should never be required as missing docs."""
        meta = {"doc_impact": {"files": ["docs/dev/notes.md"]}}
        ok, missing = should_require_docs(["src/app.py"], meta)
        assert ok is False
        assert missing == set()
