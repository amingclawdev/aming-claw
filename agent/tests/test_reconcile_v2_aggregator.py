"""Tests for Aggregator — cross-phase dedup and ranking (AC5.3)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# Local Discrepancy stand-in (avoid importing full package in tests)
# ---------------------------------------------------------------------------

@dataclass
class _Disc:
    type: str
    node_id: Optional[str]
    field: Optional[str]
    detail: str
    confidence: str


# ---------------------------------------------------------------------------
# AC5.3: Aggregator deduplicates Phase A x Phase E
# ---------------------------------------------------------------------------

class TestAggregatorDedup:
    def test_phase_e_overlays_phase_a(self):
        """AC5.3: file in both Phase A unmapped_files AND Phase E unmapped_high_conf_suggest
        appears exactly ONCE with Phase E binding taking precedence."""
        from agent.governance.reconcile_phases.aggregator import aggregate

        shared_file = "agent/governance/new_module.py"

        phase_a = [
            _Disc(type="unmapped_file", node_id=None, field=None,
                  detail=shared_file, confidence="low"),
            _Disc(type="stale_ref", node_id="L1.1", field="primary",
                  detail="old.py -> new.py", confidence="high"),
        ]
        phase_e = [
            _Disc(type="unmapped_high_conf_suggest", node_id="L7.3", field="secondary",
                  detail=f"file={shared_file} suggested_node=L7.3 field=secondary score=0.95 top2=0.10 gap=0.85",
                  confidence="high"),
        ]

        result = aggregate({"A": phase_a, "E": phase_e, "B": [], "C": [], "D": []})

        # The Phase A unmapped_file for shared_file should be removed
        assert result["dedup_removed"] == 1

        # Count total occurrences of shared_file across all buckets
        all_items = result["auto_fixable"] + result["human_review"] + result["genuinely_unresolvable"]
        file_mentions = [d for d in all_items
                         if shared_file in getattr(d, "detail", "")]
        # Exactly ONE entry (the Phase E one)
        assert len(file_mentions) == 1
        assert file_mentions[0].type == "unmapped_high_conf_suggest"

    def test_no_dedup_when_no_overlap(self):
        """No dedup when Phase A and E reference different files."""
        from agent.governance.reconcile_phases.aggregator import aggregate

        phase_a = [
            _Disc(type="unmapped_file", node_id=None, field=None,
                  detail="agent/foo.py", confidence="low"),
        ]
        phase_e = [
            _Disc(type="unmapped_high_conf_suggest", node_id="L1.1", field="test",
                  detail="file=agent/bar.py suggested_node=L1.1 field=test score=0.90 top2=0.10 gap=0.80",
                  confidence="high"),
        ]

        result = aggregate({"A": phase_a, "E": phase_e, "B": [], "C": [], "D": []})
        assert result["dedup_removed"] == 0


class TestAggregatorBuckets:
    def test_three_bucket_keys(self):
        """Output always has auto_fixable, human_review, genuinely_unresolvable."""
        from agent.governance.reconcile_phases.aggregator import aggregate

        result = aggregate({"A": [], "E": [], "B": [], "C": [], "D": []})
        assert "auto_fixable" in result
        assert "human_review" in result
        assert "genuinely_unresolvable" in result

    def test_auto_fixable_threshold(self):
        """Only high-confidence auto-fixable types end up in auto_fixable bucket."""
        from agent.governance.reconcile_phases.aggregator import aggregate

        findings = {
            "A": [
                _Disc(type="stale_ref", node_id="L1.1", field="primary",
                      detail="old -> new", confidence="high"),
                _Disc(type="stale_ref", node_id="L1.2", field="primary",
                      detail="old2 -> new2", confidence="low"),
            ],
            "E": [], "B": [], "C": [], "D": [],
        }

        result = aggregate(findings, auto_fix_threshold="high")
        assert len(result["auto_fixable"]) == 1
        assert result["auto_fixable"][0].node_id == "L1.1"

    def test_human_review_types(self):
        """doc_stale and pm_proposed types go to human_review."""
        from agent.governance.reconcile_phases.aggregator import aggregate

        findings = {
            "A": [], "E": [], "B": [],
            "C": [],
            "D": [
                _Disc(type="doc_stale", node_id=None, field=None,
                      detail="doc=x.md ref=y.py age_days=30", confidence="medium"),
            ],
        }

        result = aggregate(findings)
        assert len(result["human_review"]) == 1
        assert result["human_review"][0].type == "doc_stale"
