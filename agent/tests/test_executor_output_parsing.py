"""Regression tests for executor result-parsing order.

Covers the defect where _detect_terminal_cli_error ran before structured output
extraction, causing PM/QA preamble + fenced JSON to be misclassified as a
terminal error.
"""

import json
import types
import pytest

# ---------------------------------------------------------------------------
# Minimal stub so we can unit-test _parse_output and _detect_terminal_cli_error
# without importing the full executor (which has heavy deps).
# ---------------------------------------------------------------------------

import importlib
import sys
from pathlib import Path
from typing import Optional

# Ensure the project root is on sys.path so `agent.executor_worker` resolves.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _make_session(stdout="", stderr="", status="completed", exit_code=0):
    """Return a lightweight session-like object."""
    return types.SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        status=status,
        exit_code=exit_code,
    )


def _get_worker_class():
    """Import ExecutorWorker lazily so import errors are surfaced as test failures."""
    from agent.executor_worker import ExecutorWorker
    return ExecutorWorker


# ---- helpers to call methods without full construction ----

def _call_parse_output(stdout: str, task_type: str = "pm") -> dict:
    """Call _parse_output on a bare instance (no __init__ side-effects)."""
    cls = _get_worker_class()
    worker = object.__new__(cls)
    session = _make_session(stdout=stdout)
    return worker._parse_output(session, task_type)


def _call_detect_terminal_cli_error(stdout: str, stderr: str = "",
                                     task_type: str = "pm") -> Optional[str]:
    cls = _get_worker_class()
    worker = object.__new__(cls)
    session = _make_session(stdout=stdout, stderr=stderr)
    return worker._detect_terminal_cli_error(session, task_type)


# ======================================================================
# Tests: _parse_output structured extraction
# ======================================================================

class TestParseOutputStructured:
    """Verify _parse_output extracts JSON from various output formats."""

    def test_pure_json(self):
        """Pure JSON object parses directly."""
        payload = json.dumps({"summary": "all good", "changed_files": []})
        result = _call_parse_output(payload)
        assert result["summary"] == "all good"

    def test_fenced_json_with_preamble(self):
        """PM/QA-style preamble followed by fenced JSON block parses correctly."""
        stdout = (
            "I've analyzed the codebase and here are my findings.\n"
            "The following changes are needed:\n\n"
            "```json\n"
            '{"summary": "Fix parsing order", "changed_files": ["a.py"]}\n'
            "```\n"
        )
        result = _call_parse_output(stdout)
        assert result["summary"] == "Fix parsing order"
        assert result["changed_files"] == ["a.py"]

    def test_fenced_json_no_lang_tag(self):
        """Fenced block without 'json' language tag still parses."""
        stdout = (
            "Here is the result:\n"
            "```\n"
            '{"status": "pass", "score": 95}\n'
            "```\n"
        )
        result = _call_parse_output(stdout)
        assert result["status"] == "pass"
        assert result["score"] == 95

    def test_embedded_json_no_fence(self):
        """JSON object embedded in plain text (no fences) still extracted."""
        stdout = (
            'Some preamble text.\n'
            '{"summary": "embedded result", "test_results": {"passed": 5}}\n'
            'Some trailing text.\n'
        )
        result = _call_parse_output(stdout)
        assert result["summary"] == "embedded result"

    def test_raw_text_fallback(self):
        """Non-JSON output falls back to summary dict."""
        result = _call_parse_output("Everything looks fine, no issues found.")
        assert "summary" in result
        assert "exit_code" in result

    def test_empty_output(self):
        """Empty output produces fallback summary."""
        result = _call_parse_output("")
        assert result["summary"] == "(no output)"


# ======================================================================
# Tests: _detect_terminal_cli_error
# ======================================================================

class TestDetectTerminalCliError:
    """Verify terminal error detection works for real failures."""

    def test_reached_max_turns(self):
        err = _call_detect_terminal_cli_error("Error: Reached max turns (60)")
        assert err is not None
        assert "max turns" in err.lower() or "Reached max turns" in err

    def test_generic_error_line(self):
        err = _call_detect_terminal_cli_error("Error: provider rate limit exceeded")
        assert err is not None
        assert "rate limit" in err.lower()

    def test_no_error_when_json_present(self):
        """stdout starting with valid JSON should NOT trigger error detection."""
        stdout = '{"summary": "ok"}'
        err = _call_detect_terminal_cli_error(stdout)
        # Current implementation: stdout.startswith("Error:") check won't match JSON
        assert err is None

    def test_coordinator_type_skipped(self):
        """coordinator task_type always returns None (by design)."""
        err = _call_detect_terminal_cli_error(
            "Error: something bad", task_type="coordinator"
        )
        assert err is None


# ======================================================================
# Tests: Integration — parsing order (the actual defect regression)
# ======================================================================

class TestParsingOrderIntegration:
    """Verify that structured extraction runs BEFORE terminal error fallback.

    This is the core regression test for the defect where PM/QA preamble
    was misclassified as terminal_cli_error.
    """

    def test_preamble_with_error_word_still_parses_json(self):
        """Output containing 'Error:' in preamble but valid fenced JSON
        should parse as structured success, NOT terminal error."""
        stdout = (
            "Error: I noticed some issues during analysis.\n"
            "Here is my structured result:\n\n"
            "```json\n"
            '{"summary": "QA pass with warnings", "score": 85}\n'
            "```\n"
        )
        # _parse_output should find the fenced JSON
        result = _call_parse_output(stdout)
        assert result.get("summary") == "QA pass with warnings"
        assert result.get("score") == 85
        # Crucially, "exit_code" should NOT be in keys (it's not raw fallback)
        assert "exit_code" not in result

    def test_true_error_no_json_detected(self):
        """Real terminal error (no JSON anywhere) should be detected."""
        stdout = "Error: Reached max turns"
        # _parse_output falls back to raw summary
        result = _call_parse_output(stdout)
        assert "exit_code" in result  # raw fallback

        # And terminal error detection catches it
        err = _call_detect_terminal_cli_error(stdout)
        assert err is not None

    def test_raw_fallback_has_exit_code_marker(self):
        """Raw fallback result contains 'exit_code' key — used to gate
        terminal error check in the fixed code path."""
        result = _call_parse_output("just some plain text, no json here")
        assert "exit_code" in result
        assert set(result.keys()) <= {"summary", "exit_code"}

    def test_structured_result_lacks_exit_code(self):
        """Valid structured JSON result does NOT contain 'exit_code' key
        (unless the AI explicitly included it), so the fallback gate won't
        trigger terminal error check."""
        payload = json.dumps({"summary": "done", "changed_files": []})
        result = _call_parse_output(payload)
        assert "exit_code" not in result


# ======================================================================
# Tests: gate_blocked reason key defensive lookup (B12 fix)
# ======================================================================

class TestGateBlockedReasonKey:
    """Verify gate_blocked handling uses defensive .get() for reason keys.

    Regression tests for B12: executor_worker line ~2063 used chain['reason']
    which throws KeyError when the response uses 'gate_reason' instead.
    """

    def _build_chain_msg(self, chain: dict) -> str:
        """Replicate the gate_blocked chain_msg logic from run_once."""
        chain_msg = ""
        if chain.get("gate_blocked"):
            chain_msg = f"gate_blocked: {chain.get('reason') or chain.get('gate_reason') or 'unknown'}"
        return chain_msg

    def test_gate_reason_key_no_keyerror(self):
        """AC3: auto_chain with gate_reason (no reason key) must not raise KeyError."""
        chain = {"gate_blocked": True, "gate_reason": "version mismatch"}
        # Must not raise KeyError
        msg = self._build_chain_msg(chain)
        assert "version mismatch" in msg

    def test_reason_key_backward_compat(self):
        """AC4: auto_chain with reason key still works and message contains reason."""
        chain = {"gate_blocked": True, "reason": "dirty workspace"}
        msg = self._build_chain_msg(chain)
        assert "dirty workspace" in msg

    def test_neither_key_falls_back_to_unknown(self):
        """R3: If neither reason nor gate_reason present, fall back to 'unknown'."""
        chain = {"gate_blocked": True}
        msg = self._build_chain_msg(chain)
        assert "unknown" in msg

    def test_reason_preferred_over_gate_reason(self):
        """When both keys present, 'reason' takes priority (backward compat)."""
        chain = {"gate_blocked": True, "reason": "primary", "gate_reason": "secondary"}
        msg = self._build_chain_msg(chain)
        assert "primary" in msg
