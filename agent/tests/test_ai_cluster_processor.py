"""Tests for agent/governance/ai_cluster_processor.py — Phase Z v2 PR3 Pass 5."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

# Ensure agent package is importable from the repo root
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.ai_cluster_processor import (  # noqa: E402
    ClusterReport,
    process_cluster_with_ai,
    _compute_cache_key,
)
from agent.governance.llm_cache import LLMCache  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class _Fn:
    qname: str
    lines: List[int]


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def cluster():
    return [
        _Fn(qname="agent.foo::do_thing", lines=[1, 10]),
        _Fn(qname="agent.foo::helper", lines=[12, 20]),
    ]


@pytest.fixture
def entry():
    return _Fn(qname="agent.foo::do_thing", lines=[1, 10])


# ---------------------------------------------------------------------------
# AC10 — five required test functions
# ---------------------------------------------------------------------------

def test_use_ai_false_produces_placeholder_report(workspace, cluster, entry):
    """When use_ai=False, return ai_unavailable placeholder with feature_name=entry.qname."""
    report = process_cluster_with_ai(cluster, entry, str(workspace), use_ai=False)

    assert isinstance(report, ClusterReport)
    assert report.feature_name == entry.qname
    assert report.enrichment_status == "ai_unavailable"
    assert report.purpose is None
    assert report.expected_test_files == []
    assert report.expected_doc_sections == []
    assert report.dead_code_candidates == []
    assert report.missing_tests == []
    assert report.gap_explanation is None
    assert report.doc_validation is None


def test_use_ai_true_with_mock_calls_three_endpoints(workspace, cluster, entry):
    """3-call LLM pattern: summarize + gap + doc-validate when conditions met."""
    # Pre-create the doc so doc-validation triggers.
    doc_rel = "docs/feature.md"
    doc_abs = workspace / doc_rel
    doc_abs.parent.mkdir(parents=True, exist_ok=True)
    doc_abs.write_text("placeholder", encoding="utf-8")

    def fake_ai_call(stage, payload):
        if stage == "summarize_cluster":
            return {
                "feature_name": "Do Thing Feature",
                "purpose": "Performs a thing.",
                "expected_test_files": ["agent/tests/test_foo.py"],
                "expected_doc_sections": [doc_rel],
                "dead_code_candidates": [],
                "missing_tests": ["agent/tests/test_foo_edge_cases.py"],
            }
        if stage == "explain_gap":
            return {"explanation": "Edge cases not yet covered."}
        if stage == "validate_docs":
            return {"validation": "doc references current API."}
        raise AssertionError(f"unexpected stage: {stage}")

    mock = MagicMock(side_effect=fake_ai_call)

    report = process_cluster_with_ai(
        cluster, entry, str(workspace), use_ai=True, ai_call=mock
    )

    assert report.enrichment_status == "ai_complete"
    assert report.feature_name == "Do Thing Feature"
    assert report.purpose == "Performs a thing."
    assert report.expected_test_files == ["agent/tests/test_foo.py"]
    assert report.missing_tests == ["agent/tests/test_foo_edge_cases.py"]
    assert report.gap_explanation == "Edge cases not yet covered."
    assert report.doc_validation == "doc references current API."

    stages_called = [c.args[0] for c in mock.call_args_list]
    assert stages_called == [
        "summarize_cluster",
        "explain_gap",
        "validate_docs",
    ]
    assert mock.call_count == 3


def test_cache_hit_skips_llm_call(workspace, cluster, entry):
    """Pre-populated cache → ai_call is never invoked."""
    cache = LLMCache(str(workspace))
    key = _compute_cache_key(cluster)

    cached_report = ClusterReport(
        feature_name="cached-name",
        purpose="cached-purpose",
        expected_test_files=["t.py"],
        expected_doc_sections=[],
        dead_code_candidates=[],
        missing_tests=[],
        gap_explanation=None,
        doc_validation=None,
        enrichment_status="ai_complete",
    )
    cache.put(key, cached_report)

    mock_ai_call = MagicMock()
    report = process_cluster_with_ai(
        cluster, entry, str(workspace),
        use_ai=True, ai_call=mock_ai_call, cache=cache,
    )

    mock_ai_call.assert_not_called()
    assert report.feature_name == "cached-name"
    assert report.enrichment_status == "ai_complete"


def test_ai_503_falls_back_to_placeholder(workspace, cluster, entry):
    """When ai_call raises any Exception → ai_unavailable placeholder."""
    attempts = []
    sleeps = []

    def boom(stage, payload):
        attempts.append(stage)
        raise RuntimeError("503 Service Unavailable")

    report = process_cluster_with_ai(
        cluster,
        entry,
        str(workspace),
        use_ai=True,
        ai_call=boom,
        retry_sleep=sleeps.append,
    )

    assert isinstance(report, ClusterReport)
    assert report.enrichment_status == "ai_unavailable"
    assert report.feature_name == entry.qname
    assert report.purpose is None
    assert report.gap_explanation is None
    assert report.doc_validation is None
    assert attempts == ["summarize_cluster"] * 4
    assert sleeps == [1.0, 4.0, 16.0]


def test_transient_ai_failure_retries_then_succeeds(workspace, cluster, entry):
    """The 1s/4s/16s backoff path recovers from transient LLM failures."""
    attempts = []
    sleeps = []

    def flaky(stage, payload):
        attempts.append(stage)
        if len(attempts) < 3:
            raise RuntimeError("temporary 503")
        return {
            "feature_name": "Recovered Feature",
            "purpose": "Recovered after retry.",
            "expected_test_files": [],
            "expected_doc_sections": [],
            "dead_code_candidates": [],
            "missing_tests": [],
        }

    report = process_cluster_with_ai(
        cluster,
        entry,
        str(workspace),
        use_ai=True,
        ai_call=flaky,
        retry_sleep=sleeps.append,
    )

    assert report.enrichment_status == "ai_complete"
    assert report.feature_name == "Recovered Feature"
    assert attempts == ["summarize_cluster"] * 3
    assert sleeps == [1.0, 4.0]


def test_cluster_report_serializable():
    """ClusterReport.to_dict() round-trips through json.dumps."""
    report = ClusterReport(
        feature_name="x",
        purpose=None,
        expected_test_files=["a.py"],
        expected_doc_sections=["docs/x.md"],
        dead_code_candidates=["y"],
        missing_tests=[],
        gap_explanation=None,
        doc_validation=None,
        enrichment_status="ai_complete",
    )
    payload = report.to_dict()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    expected_keys = {
        "feature_name", "purpose", "expected_test_files",
        "expected_doc_sections", "dead_code_candidates",
        "missing_tests", "gap_explanation", "doc_validation",
        "enrichment_status",
    }
    assert expected_keys.issubset(set(decoded))
    assert decoded["feature_name"] == "x"
    assert decoded["enrichment_status"] == "ai_complete"


# ---------------------------------------------------------------------------
# Extra coverage
# ---------------------------------------------------------------------------

def test_only_summarize_called_when_no_gaps_and_no_docs(workspace, cluster, entry):
    """Only the summarize stage runs when missing_tests is empty AND no doc paths exist."""
    def fake_ai_call(stage, payload):
        return {
            "feature_name": "f",
            "purpose": "p",
            "expected_test_files": [],
            "expected_doc_sections": ["docs/missing.md"],  # path does NOT exist
            "dead_code_candidates": [],
            "missing_tests": [],
        }

    mock = MagicMock(side_effect=fake_ai_call)
    report = process_cluster_with_ai(
        cluster, entry, str(workspace), use_ai=True, ai_call=mock
    )

    assert mock.call_count == 1
    assert report.enrichment_status == "ai_complete"
    assert report.gap_explanation is None
    assert report.doc_validation is None


def test_cache_miss_writes_to_cache(workspace, cluster, entry):
    """Cache-miss path persists the report so subsequent calls are hits."""
    cache = LLMCache(str(workspace))

    def fake_ai_call(stage, payload):
        return {
            "feature_name": "F",
            "purpose": "P",
            "expected_test_files": [],
            "expected_doc_sections": [],
            "dead_code_candidates": [],
            "missing_tests": [],
        }

    mock1 = MagicMock(side_effect=fake_ai_call)
    process_cluster_with_ai(
        cluster, entry, str(workspace), use_ai=True, ai_call=mock1, cache=cache,
    )
    assert mock1.call_count == 1

    # Second call should be a cache hit.
    mock2 = MagicMock(side_effect=fake_ai_call)
    report = process_cluster_with_ai(
        cluster, entry, str(workspace), use_ai=True, ai_call=mock2, cache=cache,
    )
    mock2.assert_not_called()
    assert report.feature_name == "F"


def test_compatibility_entrypoints_export_existing_implementation():
    from agent.governance.llm_cache_local import LLMCache as CompatCache
    from agent.governance.symbol_cluster_processor import (
        ClusterReport as CompatClusterReport,
        process_cluster_with_ai as compat_process,
    )

    assert CompatCache is LLMCache
    assert CompatClusterReport is ClusterReport
    assert compat_process is process_cluster_with_ai
