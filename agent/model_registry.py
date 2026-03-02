"""
model_registry.py - Fetch available AI models from Anthropic and OpenAI APIs.

Returns a unified list: [{"id": "...", "provider": "anthropic"|"openai", "label": "..."}]
Results are in-memory cached for CACHE_TTL seconds per process lifetime.
"""
import os
import time
from typing import Dict, List, Optional

import requests

CACHE_TTL = 300  # 5 minutes

_cache: Dict[str, object] = {}  # key -> (ts, value)


def _cached(key: str, ttl: int, fn):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < ttl:
        return entry[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


# ── Anthropic ──────────────────────────────────────────────────────────────────

_ANTHROPIC_PREFER = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]

def fetch_anthropic_models() -> List[Dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        resp = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception:
        return []

    models = []
    for m in data:
        mid = m.get("id", "")
        if not mid or "claude" not in mid.lower():
            continue
        models.append({"id": mid, "provider": "anthropic",
                        "created": m.get("created_at", "")})

    # Sort: preferred first, then by created_at desc
    def _sort_key(m):
        for i, p in enumerate(_ANTHROPIC_PREFER):
            if m["id"].startswith(p):
                return (0, i, "")
        return (1, 99, m["id"])

    models.sort(key=_sort_key)
    return models


# ── OpenAI ────────────────────────────────────────────────────────────────────

_OPENAI_PREFIXES = ("gpt-4o", "gpt-4", "o1", "o3", "gpt-3.5-turbo")
_OPENAI_SKIP = ("instruct", "vision-preview", "0301", "0314", "0613")

def fetch_openai_models() -> List[Dict]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        resp = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": "Bearer " + api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception:
        return []

    models = []
    for m in data:
        mid = m.get("id", "")
        if not any(mid.startswith(p) for p in _OPENAI_PREFIXES):
            continue
        if any(s in mid for s in _OPENAI_SKIP):
            continue
        models.append({"id": mid, "provider": "openai",
                        "created": m.get("created", 0)})

    models.sort(key=lambda m: -m["created"])
    return models


# ── Combined ──────────────────────────────────────────────────────────────────

def get_available_models(force_refresh: bool = False) -> List[Dict]:
    """Return unified model list from all configured providers."""
    if force_refresh:
        _cache.clear()

    def _fetch():
        result = []
        result.extend(fetch_anthropic_models())
        result.extend(fetch_openai_models())
        return result

    return _cached("all_models", CACHE_TTL, _fetch)


def make_label(m: Dict) -> str:
    """Short display label for a model entry."""
    provider_tag = "[C]" if m["provider"] == "anthropic" else "[O]"
    return "{} {}".format(provider_tag, m["id"])
