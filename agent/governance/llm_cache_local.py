"""Compatibility entrypoint for the local Phase Z v2 LLM cache."""
from __future__ import annotations

from agent.governance.llm_cache import LLMCache  # noqa: F401

__all__ = ["LLMCache"]
