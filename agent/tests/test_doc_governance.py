"""Tests for doc governance: _infer_doc_associations (Step 3a, AC-L1.1)."""

import os
import tempfile
import shutil

import pytest

from agent.governance.graph_generator import _infer_doc_associations


class TestInferDocAssociations:
    """AC-L1.1: _infer_doc_associations returns list[dict] with inferred=True."""

    def _make_workspace(self, files: dict[str, str] | None = None) -> str:
        """Create a temp workspace with optional files (path -> content)."""
        ws = tempfile.mkdtemp(prefix="test_doc_gov_")
        if files:
            for path, content in files.items():
                full = os.path.join(ws, path.replace("/", os.sep))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(content)
        return ws

    def test_exact_stem_match_returns_confidence_09(self):
        """Exact stem match (reconcile.py ↔ reconcile.md) → confidence 0.9."""
        ws = self._make_workspace({
            "docs/reconcile.md": "# Reconcile flow design",
        })
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/reconcile.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            assert len(result) == 1
            assert result[0]["node_id"] == "L1.1"
            assert result[0]["doc_path"] == "docs/reconcile.md"
            assert result[0]["confidence"] == 0.9
            assert result[0]["inferred"] is True
            assert "reason" in result[0]
        finally:
            shutil.rmtree(ws)

    def test_no_docs_dir_returns_empty(self):
        """No docs/ directory → empty list, no crash."""
        ws = self._make_workspace()  # no docs/ created
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/reconcile.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            assert result == []
        finally:
            shutil.rmtree(ws)

    def test_all_results_have_inferred_true(self):
        """Every returned dict must have inferred=True."""
        ws = self._make_workspace({
            "docs/auto-chain.md": "# Auto chain design",
            "docs/governance/reconcile.md": "# Reconcile",
            "docs/db.md": "# DB schema",
        })
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/auto_chain.py"]},
                {"node_id": "L1.2", "primary": ["agent/governance/reconcile.py"]},
                {"node_id": "L1.3", "primary": ["agent/governance/db.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            assert len(result) >= 3
            for item in result:
                assert item["inferred"] is True
                assert isinstance(item["confidence"], float)
                assert 0.0 < item["confidence"] <= 1.0
                assert "node_id" in item
                assert "doc_path" in item
                assert "reason" in item
        finally:
            shutil.rmtree(ws)

    def test_partial_stem_overlap(self):
        """Partial overlap (auto_chain.py ↔ chain-design.md) → confidence 0.5."""
        ws = self._make_workspace({
            "docs/chain-design.md": "# Chain design doc",
        })
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/auto_chain.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            matches = [r for r in result if r["confidence"] == 0.5]
            assert len(matches) >= 1
            assert matches[0]["inferred"] is True
        finally:
            shutil.rmtree(ws)

    def test_keyword_match_in_content(self):
        """Keyword match in first 500 chars → confidence 0.3."""
        ws = self._make_workspace({
            "docs/architecture.md": "# Architecture\n\nThe reconcile module handles...",
        })
        try:
            nodes = [
                {"node_id": "L1.1", "primary": ["agent/governance/reconcile.py"]},
            ]
            result = _infer_doc_associations(nodes, ws)
            kw_matches = [r for r in result if r["confidence"] == 0.3]
            assert len(kw_matches) >= 1
            assert kw_matches[0]["inferred"] is True
        finally:
            shutil.rmtree(ws)
