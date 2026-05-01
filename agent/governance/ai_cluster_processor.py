"""Phase Z v2 PR3 — AI cluster processor (Pass 5).

Given a *cluster* of functions (each an object exposing ``qname`` and
``lines`` attributes) and an *entry* function for the cluster, produce
a :class:`ClusterReport` that pairs the deterministic graph topology
(Passes 1–4) with semantic enrichment supplied by an LLM.

Authority model
---------------
* The LLM is the **suggester** — it proposes a human-readable feature
  name, a one-sentence purpose, expected test/doc paths, dead-code
  candidates, gap explanations, and doc-vs-code validation.
* The deterministic algorithm remains the **authority** — structure,
  dependency edges, and layer assignment are owned by Passes 1–4 and
  are not influenced by AI output.

3-call LLM pattern
------------------
1. Mandatory **semantic summarization** producing the core report.
2. Conditional **gap explanation** — only when ``missing_tests`` is
   non-empty after the first call.
3. Conditional **doc validation** — only when at least one expected
   doc path actually exists in the workspace.

Cache integration
-----------------
The cache key is::

    sha256(json.dumps(sorted([(f.qname, f.lines) for f in cluster])).encode()).hexdigest()

A cache hit short-circuits the entire pipeline (no ``ai_call``
invocation).  On miss, the resulting report is persisted via
:meth:`LLMCache.put`.

Graceful degradation
--------------------
* ``use_ai=False`` → returns an ``ai_unavailable`` placeholder.
* ``ai_call`` raising any ``Exception`` → same placeholder; the error
  is swallowed and the deterministic data is preserved.
* No partial state is ever returned: every error path produces a
  fully-populated :class:`ClusterReport`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Sequence

from agent.governance.llm_cache import LLMCache

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ClusterReport dataclass
# ---------------------------------------------------------------------------
ENRICHMENT_STATUSES = ("ai_complete", "ai_unavailable", "partial")


@dataclass
class ClusterReport:
    """Semantic + deterministic enrichment for a single cluster."""

    feature_name: str
    purpose: Optional[str]
    expected_test_files: List[str] = field(default_factory=list)
    expected_doc_sections: List[str] = field(default_factory=list)
    dead_code_candidates: List[str] = field(default_factory=list)
    missing_tests: List[str] = field(default_factory=list)
    gap_explanation: Optional[str] = None
    doc_validation: Optional[str] = None
    enrichment_status: str = "ai_unavailable"

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict of all 9 fields."""
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_cache_key(cluster: Sequence[Any]) -> str:
    """Compute a deterministic cache key for *cluster*.

    Cache key formula (per PRD R3)::

        sha256(json.dumps(sorted([(f.qname, f.lines) for f in cluster])).encode()).hexdigest()

    Order-independent: sorting the (qname, lines) tuples means clusters
    that differ only in iteration order produce the same key.
    """
    items = []
    for f in cluster:
        qname = getattr(f, "qname", None) or getattr(f, "qualified_name", "")
        lines = getattr(f, "lines", None)
        if lines is None:
            # Fall back to (lineno, end_lineno) when present.
            lo = getattr(f, "lineno", None)
            hi = getattr(f, "end_lineno", None)
            lines = [lo, hi] if lo is not None else []
        # Lists are JSON-serializable but not hashable; tuples are.
        items.append((str(qname), list(lines) if lines is not None else []))
    payload = json.dumps(sorted(items), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _placeholder(entry: Any) -> ClusterReport:
    """Return the canonical ``ai_unavailable`` placeholder."""
    qname = getattr(entry, "qname", None) or getattr(entry, "qualified_name", "")
    return ClusterReport(
        feature_name=str(qname or ""),
        purpose=None,
        expected_test_files=[],
        expected_doc_sections=[],
        dead_code_candidates=[],
        missing_tests=[],
        gap_explanation=None,
        doc_validation=None,
        enrichment_status="ai_unavailable",
    )


def _coerce_to_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return [str(value)]


def _default_ai_call(stage: str, payload: dict) -> dict:
    """Default LLM stub — returns an empty mapping so unit tests work offline.

    Real production callers must inject a concrete ``ai_call`` that
    talks to the model provider.  Returning ``{}`` here keeps the
    contract honest: the enrichment fields just stay empty rather than
    silently inventing data.
    """
    return {}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def process_cluster_with_ai(
    cluster: Sequence[Any],
    entry: Any,
    workspace,
    use_ai: bool = True,
    ai_call: Optional[Callable[[str, dict], dict]] = None,
    cache: Optional[LLMCache] = None,
) -> ClusterReport:
    """Produce a :class:`ClusterReport` for *cluster* (entry point = *entry*).

    Parameters
    ----------
    cluster : sequence
        Cluster members; each element must expose ``qname`` and ``lines``
        attributes (the cache key formula uses both).
    entry : object
        Designated entry-point function for the cluster.  Must expose
        ``qname``.  Used as the placeholder ``feature_name`` when AI is
        unavailable.
    workspace : str | pathlib.Path
        Project workspace root.  Used to (a) anchor doc-validation
        existence checks and (b) seat the on-disk LLM cache.
    use_ai : bool, default True
        Master switch.  When False, returns the placeholder report
        without invoking ``ai_call`` or the cache.
    ai_call : callable, optional
        Dependency-injected LLM caller of signature
        ``ai_call(stage: str, payload: dict) -> dict``.  Defaults to
        :func:`_default_ai_call` (which returns ``{}``).
    cache : LLMCache, optional
        Pre-built cache instance.  When ``None``, a fresh ``LLMCache``
        rooted at *workspace* is constructed.
    """
    # R5: hard short-circuit before any cache or LLM work.
    if not use_ai:
        return _placeholder(entry)

    if ai_call is None:
        ai_call = _default_ai_call

    if cache is None:
        try:
            cache = LLMCache(workspace) if workspace else None
        except Exception:  # pragma: no cover — defensive
            cache = None

    # ------------------------------------------------------------------
    # Cache key + cache lookup
    # ------------------------------------------------------------------
    try:
        key = _compute_cache_key(cluster)
    except Exception:  # pragma: no cover — defensive
        log.exception("ai_cluster_processor: cache key computation failed")
        return _placeholder(entry)

    if cache is not None:
        cached_payload = cache.get(key)
        if cached_payload is not None:
            try:
                return ClusterReport(**cached_payload)
            except TypeError:
                # Schema drift → ignore the stale entry and proceed.
                log.warning("ai_cluster_processor: stale cache entry for %s", key)

    # ------------------------------------------------------------------
    # 3-call LLM pattern (R2)
    # ------------------------------------------------------------------
    cluster_payload = {
        "entry": getattr(entry, "qname", None)
        or getattr(entry, "qualified_name", ""),
        "members": [
            {
                "qname": getattr(f, "qname", None)
                or getattr(f, "qualified_name", ""),
                "lines": getattr(f, "lines", None),
            }
            for f in cluster
        ],
    }

    try:
        # (1) Mandatory semantic summarization.
        summary = ai_call("summarize_cluster", cluster_payload) or {}
        if not isinstance(summary, dict):
            raise TypeError("ai_call(summarize_cluster) must return a dict")

        report = ClusterReport(
            feature_name=str(
                summary.get("feature_name")
                or getattr(entry, "qname", None)
                or getattr(entry, "qualified_name", "")
            ),
            purpose=summary.get("purpose"),
            expected_test_files=_coerce_to_list(summary.get("expected_test_files")),
            expected_doc_sections=_coerce_to_list(summary.get("expected_doc_sections")),
            dead_code_candidates=_coerce_to_list(summary.get("dead_code_candidates")),
            missing_tests=_coerce_to_list(summary.get("missing_tests")),
            gap_explanation=None,
            doc_validation=None,
            enrichment_status="ai_complete",
        )

        # (2) Conditional gap explanation.
        if report.missing_tests:
            gap = ai_call(
                "explain_gap",
                {
                    "feature_name": report.feature_name,
                    "missing_tests": list(report.missing_tests),
                },
            ) or {}
            if isinstance(gap, dict):
                report.gap_explanation = gap.get("explanation") or gap.get(
                    "gap_explanation"
                )

        # (3) Conditional doc validation — only when an expected doc
        #     path actually exists in the workspace.
        if report.expected_doc_sections and workspace:
            ws_root = str(workspace)
            existing_docs = []
            for rel in report.expected_doc_sections:
                if not rel:
                    continue
                candidate = os.path.join(ws_root, rel)
                if os.path.exists(candidate):
                    existing_docs.append(rel)
            if existing_docs:
                doc = ai_call(
                    "validate_docs",
                    {
                        "feature_name": report.feature_name,
                        "doc_paths": existing_docs,
                    },
                ) or {}
                if isinstance(doc, dict):
                    report.doc_validation = doc.get("validation") or doc.get(
                        "doc_validation"
                    )

    except Exception:
        log.exception("ai_cluster_processor: ai_call failed; falling back")
        return _placeholder(entry)

    # ------------------------------------------------------------------
    # Persist to cache (R3)
    # ------------------------------------------------------------------
    if cache is not None:
        try:
            cache.put(key, report)
        except Exception:  # pragma: no cover — cache write best-effort
            log.exception("ai_cluster_processor: cache.put failed for %s", key)

    return report
