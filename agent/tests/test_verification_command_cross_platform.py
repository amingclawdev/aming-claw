"""B41 AC3: Cross-platform verification command guard tests."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from executor_worker import _assert_portable_verification_command


# --- Banned first-token cases (9 Unix commands) ---
@pytest.mark.parametrize("token", ["grep", "sed", "awk", "find", "head", "tail", "cat", "cut", "xargs"])
def test_banned_unix_command_rejected(token):
    result = _assert_portable_verification_command(f"{token} foo bar.txt")
    assert result is not None
    assert result["status"] == "failed"
    assert result["result"]["test_report"]["tool"] == "b41-guard"
    assert result["result"]["test_report"]["passed"] == 0
    assert result["result"]["test_report"]["failed"] == 1
    assert "banned Unix command" in result["result"]["test_report"]["summary"]


# --- Banned shell chaining operators (4 cases) ---
@pytest.mark.parametrize("op", [" && ", " || ", " | ", " ; "])
def test_banned_operator_rejected(op):
    cmd = f"python -m pytest foo.py{op}echo done"
    result = _assert_portable_verification_command(cmd)
    assert result is not None
    assert result["status"] == "failed"
    assert result["result"]["test_report"]["tool"] == "b41-guard"
    assert "shell chaining operator" in result["result"]["test_report"]["summary"]


# --- Empty / whitespace ---
def test_empty_command_rejected():
    result = _assert_portable_verification_command("")
    assert result is not None
    assert result["status"] == "failed"


def test_whitespace_command_rejected():
    result = _assert_portable_verification_command("   ")
    assert result is not None
    assert result["status"] == "failed"


# --- Valid pass-throughs ---
def test_pytest_allowed():
    assert _assert_portable_verification_command("pytest agent/tests/test_foo.py -v") is None


def test_python_c_allowed():
    assert _assert_portable_verification_command('python -c "assert True"') is None


def test_python_m_pytest_allowed():
    assert _assert_portable_verification_command("python -m pytest agent/tests/ -v") is None
