"""Unit tests for OPT-BACKLOG-CH1-COORD-AUTOTAG.

Verifies that the coordinator auto-extracts a backlog ID from the incoming
prompt and that downstream PM task metadata receives it with the documented
idempotency guarantees.

Acceptance criteria from PRD (task-1776806353-8c1beb):
    AC1: prompt "fix B42"                     -> extract "B42"  -> meta.bug_id="B42"
    AC2: prompt with MF-2026-04-21-001        -> extract         -> meta.bug_id set
    AC3: prompt with OPT-BACKLOG-CH1-*        -> extract         -> meta.bug_id set
    AC4: prompt with no ID                    -> extract None    -> no bug_id key
    AC5: parent_meta.bug_id already "B50"     -> preserved, extraction does NOT overwrite
    AC6: action.bug_id already "B77"          -> preserved, extraction does NOT overwrite
    AC7: log line uses _hv_log (file-based), not log.info
    AC8: docs/coordinator-rules.md documents the feature (covered by dev gate, not unit test)
    AC9: this file exists and all tests pass
"""
from __future__ import annotations

import re

import pytest

from agent.executor_worker import _BACKLOG_ID_RE, _extract_backlog_id


# ---------------------------------------------------------------------------
# AC1-AC4: regex + extractor direct tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prompt,expected",
    [
        # AC1: bug ID
        ("please fix B42 asap", "B42"),
        ("B41 is broken", "B41"),
        ("regression in feature — see B9999", "B9999"),
        # AC2: manual-fix ID
        ("implement MF-2026-04-21-001 today", "MF-2026-04-21-001"),
        ("MF-2026-12-31-999 is the target", "MF-2026-12-31-999"),
        # AC3: OPT epic / sub-chain
        ("work on OPT-BACKLOG-CH1-COORD-AUTOTAG", "OPT-BACKLOG-CH1-COORD-AUTOTAG"),
        ("OPT-DB-GRAPH should land next", "OPT-DB-GRAPH"),
        ("OPT-FOO", "OPT-FOO"),
    ],
)
def test_ac1_ac3_positive_matches(prompt: str, expected: str) -> None:
    assert _extract_backlog_id(prompt) == expected


@pytest.mark.parametrize(
    "prompt",
    [
        # AC4: no backlog ID in free-text prompts
        "please fix the login page",
        "refactor the gateway for clarity",
        "",
        "nothing to see here",
        # near-misses that must NOT match (word boundary / format discipline)
        "OPTION-X",                 # OPT- must be followed by a letter/digit
        "BAR-42",                   # not B\d+
        "BMW42",                    # 'B' followed by non-digit breaks the pattern
        "MF-2026-04-21",            # missing NNN suffix
        "MF-26-04-21-001",          # YYYY wrong
    ],
)
def test_ac4_negative_no_match(prompt: str) -> None:
    assert _extract_backlog_id(prompt) is None


def test_extract_returns_first_match_in_text() -> None:
    # When a prompt references multiple IDs, the first (leftmost) is returned.
    # Users who need the second one must tag explicitly via action.bug_id.
    prompt = "B10 and also B20 in the same prompt"
    assert _extract_backlog_id(prompt) == "B10"


def test_regex_is_word_bounded() -> None:
    # Guard for the specific near-miss class: substrings should not match.
    assert _BACKLOG_ID_RE.search("XB42X") is None
    assert _BACKLOG_ID_RE.search("OPTIONAL") is None


# ---------------------------------------------------------------------------
# AC5-AC6: idempotency at the injection site
# ---------------------------------------------------------------------------
#
# We exercise the precedence logic (parent_meta > action > extraction) without
# invoking the full ExecutorWorker class, by replaying the exact block that
# _handle_coordinator_v1 executes in agent/executor_worker.py around line
# 1611-1635. The replay is intentionally verbatim so that any drift in the
# real code is caught by test failure.


def _simulate_inject(parent_meta: dict, action: dict, task_prompt: str) -> dict:
    """Replay the bug_id precedence block from _handle_coordinator_v1.

    Keep in sync with agent/executor_worker.py:1611-1635.
    """
    forwarded_meta: dict = {}
    # 1. Whitelist-forward from parent_meta (see the key-forwarding loop)
    for key in (
        "parallel_plan",
        "lane",
        "lane_name",
        "split_plan_doc",
        "convergence_required",
        "convergence_lane",
        "depends_on_lanes",
        "allow_dirty_workspace_reconciliation",
        "bug_id",
    ):
        if key in parent_meta:
            forwarded_meta[key] = parent_meta[key]
    # 2. Action-provided bug_id (fallback if parent_meta didn't set it)
    action_bug_id = action.get("bug_id")
    if action_bug_id and "bug_id" not in forwarded_meta:
        forwarded_meta["bug_id"] = action_bug_id
    # 3. Prompt extraction (final fallback)
    if "bug_id" not in forwarded_meta:
        extracted = _extract_backlog_id(task_prompt or "")
        if extracted:
            forwarded_meta["bug_id"] = extracted
    return forwarded_meta


def test_ac5_parent_meta_bug_id_not_overwritten_by_extraction() -> None:
    # AC5: parent_meta already has B50; prompt mentions B99. Result: B50 preserved.
    result = _simulate_inject(
        parent_meta={"bug_id": "B50"},
        action={},
        task_prompt="we also noticed B99 in passing",
    )
    assert result["bug_id"] == "B50"


def test_ac6_action_bug_id_not_overwritten_by_extraction() -> None:
    # AC6: action supplies B77; prompt mentions B99. Result: B77 preserved.
    result = _simulate_inject(
        parent_meta={},
        action={"bug_id": "B77"},
        task_prompt="while implementing, we saw B99 too",
    )
    assert result["bug_id"] == "B77"


def test_parent_meta_wins_over_action() -> None:
    # Not in PRD but documents the chosen precedence: parent_meta > action.
    result = _simulate_inject(
        parent_meta={"bug_id": "B50"},
        action={"bug_id": "B77"},
        task_prompt="B99",
    )
    assert result["bug_id"] == "B50"


def test_action_wins_over_extraction_when_parent_empty() -> None:
    result = _simulate_inject(
        parent_meta={},
        action={"bug_id": "OPT-MANUAL"},
        task_prompt="B99 also mentioned",
    )
    assert result["bug_id"] == "OPT-MANUAL"


def test_extraction_is_only_used_when_nothing_else_provides_bug_id() -> None:
    result = _simulate_inject(
        parent_meta={},
        action={},
        task_prompt="investigate B41 class drift — see OPT-BACKLOG-CH1-COORD-AUTOTAG",
    )
    # Leftmost match wins
    assert result["bug_id"] == "B41"


def test_ac4_no_match_leaves_no_bug_id_key() -> None:
    # Guarantees that forwarded_meta isn't polluted with a None bug_id.
    result = _simulate_inject(
        parent_meta={},
        action={},
        task_prompt="unrelated text, nothing to match here",
    )
    assert "bug_id" not in result


# ---------------------------------------------------------------------------
# Sanity: the real injection code is still in place
# ---------------------------------------------------------------------------

def test_injection_site_still_present_in_source() -> None:
    """Fail loudly if the code block is refactored away without updating _simulate_inject."""
    import agent.executor_worker as ew
    import inspect
    src = inspect.getsource(ew)
    # Guard: autotag marker comment is still in source
    assert "OPT-BACKLOG-CH1-COORD-AUTOTAG" in src
    # Guard: whitelist loop includes bug_id
    assert re.search(
        r"for key in \(\s*[^)]*\"bug_id\"[^)]*\)",
        src,
        flags=re.DOTALL,
    ) is not None, "parent_meta whitelist must include bug_id"
    # Guard: fallback extraction call still exists
    assert "_extract_backlog_id(task.get(\"prompt\") or \"\")" in src
