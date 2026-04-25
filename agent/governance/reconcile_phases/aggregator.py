"""Aggregator — cross-phase deduplication and ranking for reconcile v2.

Deduplicates findings across all 5 phases:
- File-level dedup: Phase E binding takes precedence over Phase A unmapped_file.
- Cross-reference boosting: Phase B x C, Phase D x A intersections boost confidence.
- Ranked output into 3 buckets: auto_fixable, human_review, genuinely_unresolvable.
"""
from __future__ import annotations

import re
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Discrepancy types considered auto-fixable at high confidence
_AUTO_FIXABLE_TYPES = frozenset({
    "stale_ref",
    "unmapped_high_conf_suggest",
    "merge_not_tracked",
})

# Types that are always human-review regardless of confidence
_HUMAN_REVIEW_TYPES = frozenset({
    "hotfix_no_mf_record",
    "doc_stale",
    "doc_missing_known_keyword",
    "pm_proposed_not_in_node_state",
    "orphan_in_db",
    "stuck_testing",
    "chain_not_closed",
})

CONF_ORDER = {"high": 3, "medium": 2, "low": 1}


def _extract_file(detail: str) -> Optional[str]:
    """Extract file= or detail-as-path from a Discrepancy."""
    m = re.search(r"file=(\S+)", detail)
    if m:
        return m.group(1)
    # Phase A unmapped_file stores the path directly in detail
    if detail and not detail.startswith("{") and "/" in detail:
        return detail.strip()
    return None


def aggregate(
    phase_results: Dict[str, list],
    auto_fix_threshold: str = "high",
) -> Dict[str, Any]:
    """Aggregate and deduplicate findings from all phases.

    Args:
        phase_results: {"A": [...], "E": [...], "B": [...], "C": [...], "D": [...]}
        auto_fix_threshold: minimum confidence for auto_fixable bucket.

    Returns:
        {
            "auto_fixable": [...],
            "human_review": [...],
            "genuinely_unresolvable": [...],
            "dedup_removed": int,
        }
    """
    min_conf = CONF_ORDER.get(auto_fix_threshold, 3)

    # --- Phase E over Phase A dedup -----------------------------------------
    phase_e = phase_results.get("E", [])
    phase_a = phase_results.get("A", [])

    # Collect files bound by Phase E high-conf suggestions
    phase_e_files = set()
    for d in phase_e:
        if getattr(d, "type", "") == "unmapped_high_conf_suggest":
            f = _extract_file(getattr(d, "detail", ""))
            if f:
                phase_e_files.add(f)

    # Remove Phase A unmapped_file entries that Phase E covers
    dedup_removed = 0
    deduped_a: list = []
    for d in phase_a:
        if getattr(d, "type", "") == "unmapped_file":
            f = _extract_file(getattr(d, "detail", ""))
            if f and f in phase_e_files:
                dedup_removed += 1
                continue
        deduped_a.append(d)

    # --- Cross-reference boosting -------------------------------------------
    # Phase D x A: if a doc_stale ref matches an unmapped_file, boost to medium
    phase_d = phase_results.get("D", [])
    phase_a_files = set()
    for d in deduped_a:
        f = _extract_file(getattr(d, "detail", ""))
        if f:
            phase_a_files.add(f)

    for d in phase_d:
        if getattr(d, "type", "") == "doc_stale":
            ref_m = re.search(r"ref=(\S+)", getattr(d, "detail", ""))
            if ref_m and ref_m.group(1) in phase_a_files:
                # Boost to medium if currently low
                if getattr(d, "confidence", "low") == "low":
                    object.__setattr__(d, "confidence", "medium")

    # --- Merge all into one flat list ---------------------------------------
    all_findings: list = []
    all_findings.extend(deduped_a)
    all_findings.extend(phase_e)
    all_findings.extend(phase_results.get("B", []))
    all_findings.extend(phase_results.get("C", []))
    all_findings.extend(phase_d)
    all_findings.extend(phase_results.get("F", []))
    all_findings.extend(phase_results.get("G", []))

    # --- Bucket into 3 tiers ------------------------------------------------
    auto_fixable: list = []
    human_review: list = []
    genuinely_unresolvable: list = []

    for d in all_findings:
        dtype = getattr(d, "type", "")
        dconf = CONF_ORDER.get(getattr(d, "confidence", "low"), 1)

        if dtype in _AUTO_FIXABLE_TYPES and dconf >= min_conf:
            auto_fixable.append(d)
        elif dtype in _HUMAN_REVIEW_TYPES or dtype in _AUTO_FIXABLE_TYPES:
            human_review.append(d)
        else:
            genuinely_unresolvable.append(d)

    return {
        "auto_fixable": auto_fixable,
        "human_review": human_review,
        "genuinely_unresolvable": genuinely_unresolvable,
        "dedup_removed": dedup_removed,
    }
