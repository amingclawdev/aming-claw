"""Tests for B30: merge/deploy version gate exemption in _do_chain.

B30 replaced the old _version_cache invalidation approach with a direct
exemption: merge and deploy task types skip _gate_version_check entirely.

Verifies that:
1. Merge task completion skips _gate_version_check (version-advancing op)
2. Deploy task completion skips _gate_version_check (updates chain_version)
3. Non-exempt types (pm, dev) still run _gate_version_check
4. Exemption is gated by `if task_type in ("merge", "deploy"):` in source
"""

from __future__ import annotations

import os
import re
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _get_auto_chain_source_path() -> str:
    return os.path.join(
        os.path.dirname(__file__), "..", "governance", "auto_chain.py"
    )


class TestMergeInvalidatesVersionCache:
    """B30 AC1: merge task skips version gate (replaces old cache-invalidation approach)."""

    def test_merge_invalidates_cache(self):
        """B30: merge task type is exempt from _gate_version_check in source."""
        src_path = _get_auto_chain_source_path()
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        # B30: exemption block must exist: if task_type in ("merge", "deploy"):
        pattern = re.compile(
            r'if\s+task_type\s+in\s+\("merge",\s*"deploy"\)',
        )
        assert pattern.search(source), (
            'Expected `if task_type in ("merge", "deploy"):` exemption block in source'
        )

        # And the exemption must set ver_passed = True
        exempt_pattern = re.compile(
            r'if\s+task_type\s+in\s+\("merge",\s*"deploy"\).*?ver_passed\s*,\s*ver_reason\s*=\s*True',
            re.DOTALL,
        )
        assert exempt_pattern.search(source), (
            "Expected ver_passed = True inside merge/deploy exemption block"
        )


class TestNonMergeNoOp:
    """B30 AC2: non-exempt types still call _gate_version_check."""

    def test_source_only_invalidates_for_merge(self):
        """The _gate_version_check call is in the else branch of the exemption."""
        src_path = _get_auto_chain_source_path()
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        # The else branch must call _gate_version_check
        else_gate_pattern = re.compile(
            r'else\s*:\s*\n\s*#.*version check.*\n\s*ver_passed.*=\s*_gate_version_check\(',
            re.DOTALL,
        )
        assert else_gate_pattern.search(source), (
            "Expected _gate_version_check call in else branch of merge/deploy exemption"
        )


class TestSourcePositionCheck:
    """B30 AC3: exemption block appears before the stage gate check."""

    def test_cache_invalidation_before_gate_check(self):
        """In auto_chain.py source, the merge/deploy exemption precedes stage gate_fn call."""
        src_path = _get_auto_chain_source_path()
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        exempt_pos = source.find('if task_type in ("merge", "deploy"):')
        assert exempt_pos != -1, 'merge/deploy exemption block not found in source'

        gate_fn_pos = source.find("gate_fn = _GATES[gate_fn_name]")
        assert gate_fn_pos != -1, "_GATES lookup not found in source"

        assert exempt_pos < gate_fn_pos, (
            f"Exemption (pos {exempt_pos}) must appear before stage gate_fn call (pos {gate_fn_pos})"
        )


class TestTryExceptWrap:
    """B30 AC4: exemption block records a gate event for audit trail."""

    def test_cache_invalidation_has_try_except(self):
        """The merge/deploy exemption path calls _record_gate_event for auditability."""
        src_path = _get_auto_chain_source_path()
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        # After the exemption ver_passed=True, _record_gate_event must still be called
        audit_pattern = re.compile(
            r'if\s+task_type\s+in\s+\("merge",\s*"deploy"\).*?_record_gate_event\(',
            re.DOTALL,
        )
        assert audit_pattern.search(source), (
            "Expected _record_gate_event call inside merge/deploy exemption block (audit trail)"
        )
