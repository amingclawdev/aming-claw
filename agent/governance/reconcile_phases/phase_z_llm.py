"""Phase Z LLM enrichment — cheap-first Haiku→Sonnet cascade.

Provides optional LLM-based enrichment for Phase Z deltas.
Uses a two-tier approach: call Haiku first (cheap), only escalate
to Sonnet when Haiku confidence < 0.6.

Cache key = sha256(file_sha + prompt_version).
Cache stored as key→json files under ``phase_z_llm_cache/``.

R4 implementation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

PROMPT_VERSION = "2026-04-25"
HAIKU_CONFIDENCE_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_dir() -> str:
    """Return (and create) the LLM cache directory."""
    d = os.path.join(os.path.dirname(__file__), "..", "..", "..", "phase_z_llm_cache")
    d = os.path.normpath(d)
    os.makedirs(d, exist_ok=True)
    return d


def _cache_key(file_sha: str) -> str:
    """Compute cache key = sha256(file_sha + prompt_version)."""
    raw = file_sha + PROMPT_VERSION
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_cache(key: str) -> Optional[dict]:
    """Read cached result, or None if not present."""
    path = os.path.join(_cache_dir(), f"{key}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _write_cache(key: str, data: dict) -> None:
    """Write result to cache."""
    path = os.path.join(_cache_dir(), f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# LLM call stubs (R4)
# ---------------------------------------------------------------------------

def call_haiku(prompt: str, context: dict) -> dict:
    """Call Claude Haiku for cheap classification.

    Returns dict with at least ``confidence`` (float) and ``classification`` (str).
    In production this calls the Anthropic API; here we provide a stub
    that can be mocked in tests.
    """
    # Stub — real implementation would use anthropic SDK
    return {"confidence": 0.5, "classification": "unknown", "model": "haiku"}


def call_sonnet(prompt: str, context: dict) -> dict:
    """Call Claude Sonnet for higher-quality classification.

    Only called when Haiku confidence < 0.6.
    """
    # Stub — real implementation would use anthropic SDK
    return {"confidence": 0.8, "classification": "unknown", "model": "sonnet"}


# ---------------------------------------------------------------------------
# Cluster identification
# ---------------------------------------------------------------------------

def identify_ambiguous_clusters(deltas) -> List[dict]:
    """Identify clusters of deltas that would benefit from LLM classification.

    Returns list of cluster dicts with ``files``, ``file_sha``, ``delta_indices``.
    """
    clusters = []
    for i, d in enumerate(deltas):
        if d.delta_type in ("missing_node_high_conf", "missing_node_low_conf"):
            files = d.files or []
            # Compute a stable file_sha from sorted file list
            file_content = "|".join(sorted(files))
            file_sha = hashlib.sha256(file_content.encode("utf-8")).hexdigest()
            clusters.append({
                "files": files,
                "file_sha": file_sha,
                "delta_indices": [i],
            })
    return clusters


# ---------------------------------------------------------------------------
# Main enrichment entry point
# ---------------------------------------------------------------------------

def enrich_deltas(deltas, workspace_path: str = "") -> list:
    """Enrich deltas with LLM classification (cheap-first cascade).

    AC-Z5: call_haiku invoked first; call_sonnet only when Haiku
    confidence < 0.6.  Cache key = sha256(file_sha + prompt_version);
    re-run on unchanged code produces 0 LLM calls.

    AC-Z12: This function is only called when enable_llm_enrichment=True.
    """
    clusters = identify_ambiguous_clusters(deltas)

    for cluster in clusters:
        file_sha = cluster["file_sha"]
        key = _cache_key(file_sha)

        # Check cache first
        cached = _read_cache(key)
        if cached is not None:
            # Apply cached result
            for idx in cluster["delta_indices"]:
                deltas[idx].metadata["llm_classification"] = cached.get("classification", "unknown")
                deltas[idx].metadata["llm_confidence"] = cached.get("confidence", 0.0)
                deltas[idx].metadata["llm_model"] = cached.get("model", "cached")
            continue

        # Cheap-first: call Haiku
        prompt = f"Classify these files for graph inclusion: {cluster['files']}"
        context = {"workspace": workspace_path, "files": cluster["files"]}
        result = call_haiku(prompt, context)

        # Escalate to Sonnet if Haiku confidence < threshold
        if result.get("confidence", 0) < HAIKU_CONFIDENCE_THRESHOLD:
            result = call_sonnet(prompt, context)

        # Cache the result
        _write_cache(key, result)

        # Apply to deltas
        for idx in cluster["delta_indices"]:
            deltas[idx].metadata["llm_classification"] = result.get("classification", "unknown")
            deltas[idx].metadata["llm_confidence"] = result.get("confidence", 0.0)
            deltas[idx].metadata["llm_model"] = result.get("model", "unknown")

    return deltas
