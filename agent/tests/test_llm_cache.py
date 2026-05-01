"""Tests for agent/governance/llm_cache.py — Phase Z v2 PR3 LLMCache."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import List

import pytest

# Ensure agent package is importable from the repo root
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.llm_cache import LLMCache  # noqa: E402
from agent.governance.ai_cluster_processor import (  # noqa: E402
    ClusterReport,
    _compute_cache_key,
)


@dataclass
class _Fn:
    qname: str
    lines: List[int]


# ---------------------------------------------------------------------------
# AC11 — four required test functions
# ---------------------------------------------------------------------------

def test_put_get_roundtrip(tmp_path):
    """put() then get() returns the same payload."""
    cache = LLMCache(str(tmp_path))
    report = ClusterReport(
        feature_name="rt",
        purpose="p",
        expected_test_files=["t.py"],
        expected_doc_sections=["d.md"],
        dead_code_candidates=[],
        missing_tests=[],
        gap_explanation=None,
        doc_validation=None,
        enrichment_status="ai_complete",
    )
    cache.put("abc123", report)

    got = cache.get("abc123")
    assert isinstance(got, dict)
    assert got["feature_name"] == "rt"
    assert got["enrichment_status"] == "ai_complete"

    # Storage layout sanity check.
    expected = tmp_path / "llm_cache" / "cluster_summaries" / "abc123.json"
    assert expected.exists()


def test_get_missing_returns_none(tmp_path):
    """get() on an unknown key returns None (no exception)."""
    cache = LLMCache(str(tmp_path))
    assert cache.get("nope") is None
    assert cache.get("does-not-exist") is None


def test_atomic_write_no_partial_files(tmp_path):
    """Successful put() leaves no .tmp residue alongside the final file."""
    cache = LLMCache(str(tmp_path))
    cache.put("k1", {"a": 1, "b": [1, 2, 3]})

    root = tmp_path / "llm_cache" / "cluster_summaries"
    files = list(root.iterdir())
    # Exactly one file: the final committed JSON.  No .tmp leftovers.
    assert len(files) == 1
    assert files[0].name == "k1.json"
    assert not any(p.name.endswith(".tmp") for p in files)

    # And the file is valid JSON.
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload == {"a": 1, "b": [1, 2, 3]}


def test_cache_key_deterministic_for_same_cluster():
    """Cache key is deterministic regardless of the cluster's input order."""
    a = _Fn(qname="m::a", lines=[1, 5])
    b = _Fn(qname="m::b", lines=[10, 20])
    c = _Fn(qname="m::c", lines=[30, 40])

    key1 = _compute_cache_key([a, b, c])
    key2 = _compute_cache_key([c, a, b])
    key3 = _compute_cache_key([b, c, a])
    assert key1 == key2 == key3

    # And different clusters → different keys.
    d = _Fn(qname="m::d", lines=[50, 60])
    assert _compute_cache_key([a, b, c]) != _compute_cache_key([a, b, d])


# ---------------------------------------------------------------------------
# Extra coverage
# ---------------------------------------------------------------------------

def test_invalidate_returns_true_then_false(tmp_path):
    cache = LLMCache(str(tmp_path))
    cache.put("k", {"x": 1})
    assert cache.invalidate("k") is True
    assert cache.invalidate("k") is False
    assert cache.get("k") is None


def test_put_accepts_plain_dict(tmp_path):
    cache = LLMCache(str(tmp_path))
    cache.put("k", {"hello": "world"})
    assert cache.get("k") == {"hello": "world"}


def test_lazy_directory_creation(tmp_path):
    """Cache dir does not exist until first put."""
    sub = tmp_path / "deep" / "nested" / "wsroot"
    cache = LLMCache(str(sub))
    target = sub / "llm_cache" / "cluster_summaries"
    assert not target.exists()

    cache.put("k", {"v": 1})
    assert target.exists()
    assert (target / "k.json").exists()
