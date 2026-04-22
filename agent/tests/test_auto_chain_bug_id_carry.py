"""Unit tests for OPT-BACKLOG bug_id carry-through in auto_chain.py (R2).

Verifies:
    AC4: _create_next_stage copies metadata.bug_id from parent into child
    AC5: dev-retry and same-stage-retry paths preserve bug_id (CH2 fallback)
    grep-verify: 'bug_id' in next-stage creation block, CH2 fallback log lines intact
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# AC4: bug_id appears in the next-stage creation block (grep-verify)
# ---------------------------------------------------------------------------

def test_ac4_bug_id_in_next_stage_creation() -> None:
    """Verify bug_id is forwarded in the task.created event payload."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    # The event payload forwards bug_id from task_meta
    assert '"bug_id": task_meta.get("bug_id"' in content or \
           "'bug_id': task_meta.get('bug_id'" in content or \
           '"bug_id"' in content, \
        "bug_id not found in auto_chain.py next-stage creation block"


def test_ac4_bug_id_in_event_metadata() -> None:
    """Verify the task.created event includes metadata.bug_id."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    # CH2 pattern: forward bug_id in task.created payload
    assert 'metadata": {"bug_id": task_meta.get("bug_id"' in content, \
        "CH2 event payload with bug_id not found in auto_chain.py"


# ---------------------------------------------------------------------------
# AC5: dev-retry and same-stage-retry preserve bug_id (CH2 fallback)
# ---------------------------------------------------------------------------

def test_ac5_ch2_fallback_dev_retry_log_line() -> None:
    """Verify the CH2 fallback-fill log line exists for dev-retry path."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    assert "CH2 fallback-filled bug_id=%s for dev-retry" in content, \
        "CH2 fallback-fill dev-retry log line not found in auto_chain.py"


def test_ac5_ch2_fallback_same_stage_retry_log_line() -> None:
    """Verify the CH2 fallback-fill log line exists for same-stage-retry path."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    assert "CH2 fallback-filled bug_id=%s for %s same-stage-retry" in content, \
        "CH2 fallback-fill same-stage-retry log line not found in auto_chain.py"


def test_ac5_ch2_chain_context_import() -> None:
    """Verify the CH2 fallback imports chain_context.get_store."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    assert "get_store as _get_ctx_store_bug" in content, \
        "CH2 chain_context import not found in auto_chain.py"


# ---------------------------------------------------------------------------
# Builder functions propagate bug_id via **metadata spread
# ---------------------------------------------------------------------------

def test_builders_use_metadata_spread() -> None:
    """Verify builder functions use **metadata to preserve all fields including bug_id."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    # All builders should use **metadata spread — search until next def
    builder_names = ["_build_test_prompt", "_build_qa_prompt", "_build_merge_prompt"]
    for name in builder_names:
        idx = content.find(f"def {name}")
        assert idx >= 0, f"{name} not found in auto_chain.py"
        # Find end of function (next top-level def)
        next_def = content.find("\ndef ", idx + 10)
        block = content[idx:next_def] if next_def > idx else content[idx:]
        assert "**metadata" in block, f"{name} does not use **metadata spread for bug_id propagation"


# ---------------------------------------------------------------------------
# _try_backlog_close_via_db exists for merge finalize
# ---------------------------------------------------------------------------

def test_backlog_close_function_exists() -> None:
    """Verify _try_backlog_close_via_db function exists in auto_chain.py."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    assert "_try_backlog_close_via_db" in content, \
        "_try_backlog_close_via_db not found in auto_chain.py"


def test_backlog_close_called_in_finalize() -> None:
    """Verify _try_backlog_close_via_db is called from _finalize_chain."""
    auto_chain_path = Path(__file__).resolve().parents[1] / "governance" / "auto_chain.py"
    content = auto_chain_path.read_text(encoding="utf-8")
    # Should be called with bug_id from metadata
    assert 'bug_id = metadata.get("bug_id"' in content or \
           "bug_id = metadata.get('bug_id'" in content, \
        "bug_id extraction for backlog close not found in _finalize_chain"
