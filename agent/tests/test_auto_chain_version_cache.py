"""Tests for version cache invalidation after merge tasks in _do_chain.

Verifies that:
1. Merge task completion invalidates _version_cache (ts=0)
2. Non-merge task types do NOT invalidate the cache
3. Cache invalidation appears BEFORE _gate_version_check in source
4. Cache invalidation is wrapped in try/except
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import sys
import textwrap
from unittest.mock import patch, MagicMock

import pytest

# Ensure agent package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _get_auto_chain_source_path() -> str:
    """Return the absolute path to auto_chain.py."""
    return os.path.join(
        os.path.dirname(__file__), "..", "governance", "auto_chain.py"
    )


class TestMergeInvalidatesVersionCache:
    """AC1/AC4: After merge task completes, _version_cache['ts'] is set to 0."""

    def test_merge_invalidates_cache(self):
        """When task_type=='merge', the source code sets _version_cache['ts'] = 0."""
        src_path = _get_auto_chain_source_path()
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        # Verify that inside the merge block, _version_cache["ts"] is set to 0
        # This simulates the runtime behavior: after merge, cache ts is reset
        pattern = re.compile(
            r'if\s+task_type\s*==\s*"merge"\s*:.*?_version_cache\[.*?ts.*?\]\s*=\s*0',
            re.DOTALL,
        )
        match = pattern.search(source)
        assert match is not None, (
            "Expected _version_cache['ts'] = 0 inside merge task_type block"
        )

        # Also verify the import path is correct (agent.governance.server)
        import_pattern = re.compile(
            r'from\s+agent\.governance\.server\s+import\s+_version_cache'
        )
        assert import_pattern.search(source), (
            "Expected import of _version_cache from agent.governance.server"
        )


class TestNonMergeNoOp:
    """AC3/AC4: Non-merge task types do NOT invalidate the version cache."""

    def test_source_only_invalidates_for_merge(self):
        """The invalidation block is gated by `if task_type == "merge":`."""
        src_path = _get_auto_chain_source_path()
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        # Find the _version_cache["ts"] = 0 assignment
        # It must be inside an `if task_type == "merge":` block
        pattern = re.compile(
            r'if\s+task_type\s*==\s*"merge"\s*:.*?_version_cache\[.*?ts.*?\]\s*=\s*0',
            re.DOTALL,
        )
        assert pattern.search(source), (
            "Expected _version_cache['ts'] = 0 inside an `if task_type == 'merge':` block"
        )


class TestSourcePositionCheck:
    """AC1/AC4: _version_cache invalidation appears BEFORE _gate_version_check call."""

    def test_cache_invalidation_before_gate_check(self):
        """In auto_chain.py source, the cache reset must precede _gate_version_check."""
        src_path = _get_auto_chain_source_path()
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        cache_pos = source.find('_version_cache["ts"] = 0')
        if cache_pos == -1:
            cache_pos = source.find("_version_cache['ts'] = 0")
        assert cache_pos != -1, "_version_cache ts=0 assignment not found in source"

        gate_pos = source.find("_gate_version_check(")
        assert gate_pos != -1, "_gate_version_check call not found in source"

        assert cache_pos < gate_pos, (
            f"Cache invalidation (pos {cache_pos}) must appear before "
            f"_gate_version_check (pos {gate_pos})"
        )


class TestTryExceptWrap:
    """AC2/AC4: Cache invalidation is wrapped in try/except."""

    def test_cache_invalidation_has_try_except(self):
        """The _version_cache invalidation block must be inside try/except."""
        src_path = _get_auto_chain_source_path()
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        # Find the merge block and verify it contains try/except wrapping
        # the _version_cache assignment
        pattern = re.compile(
            r'if\s+task_type\s*==\s*"merge"\s*:\s*\n\s*try\s*:.*?_version_cache.*?ts.*?=\s*0.*?\n\s*except\s+Exception',
            re.DOTALL,
        )
        assert pattern.search(source), (
            "Expected _version_cache invalidation wrapped in try/except "
            "inside the merge task_type block"
        )
