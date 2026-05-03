"""Contract tests for the executor-gateway action executor cluster.

This test file pins down the public CLI / helper contracts of the four
executor scripts under ``executor-gateway/executors/``:

* ``code_change.py``
* ``plan_task.py``
* ``run_tests.py``
* ``take_screenshot.py``

The directory ``executor-gateway`` is *not* a valid Python package name
(hyphens are not legal identifiers), so the tests load each module by file
path via ``importlib.util.spec_from_file_location``. This mirrors how the
gateway dispatcher invokes the scripts as standalone commands.

Locked behaviors:

* AC2 -- ``safe_name()`` sanitization/truncation in ``code_change`` and
  ``take_screenshot``.
* AC3 -- ``plan_task.main()`` reads stdin JSON and emits ``ok=true`` JSON
  with a non-empty ``details.steps`` list.
* AC4 -- ``code_change.main()`` writes a proposal under
  ``WORKSPACE_PATH/.openclaw/proposals`` and exposes ``proposal_file`` in
  the JSON details payload.
* AC5 -- ``run_tests.main()`` honors ``SAFE_TEST_COMMAND`` via
  ``shlex.split``, runs in ``WORKSPACE_PATH``, and reflects subprocess
  ``returncode`` in the ``ok`` flag while preserving command/stdout/stderr
  details.
* AC6 -- ``take_screenshot.wants_all_screens()`` keyword detection plus
  ``resolve_output_dir()`` preference for ``SHARED_VOLUME_PATH`` over
  ``WORKSPACE_PATH``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Module-loader helpers
# ---------------------------------------------------------------------------

EXECUTORS_DIR = Path(__file__).resolve().parent.parent / "executors"


def _load_module(name: str, file_name: str) -> types.ModuleType:
    """Load an executor module by absolute file path.

    The ``executor-gateway`` directory cannot be imported as a Python package
    because the hyphen is not a legal identifier; this loader sidesteps that
    and is the only supported way to exercise the executor scripts in tests.
    """

    file_path = EXECUTORS_DIR / file_name
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    assert spec is not None and spec.loader is not None, (
        f"unable to build import spec for {file_path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def code_change_mod() -> types.ModuleType:
    return _load_module("contract_code_change", "code_change.py")


@pytest.fixture(scope="module")
def plan_task_mod() -> types.ModuleType:
    return _load_module("contract_plan_task", "plan_task.py")


@pytest.fixture(scope="module")
def run_tests_mod() -> types.ModuleType:
    return _load_module("contract_run_tests", "run_tests.py")


@pytest.fixture(scope="module")
def take_screenshot_mod() -> types.ModuleType:
    return _load_module("contract_take_screenshot", "take_screenshot.py")


# ---------------------------------------------------------------------------
# Stdio plumbing helper
# ---------------------------------------------------------------------------


def _drive_main(
    module: types.ModuleType,
    payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Pipe ``payload`` through ``module.main`` via stdin/stdout patching.

    Each script's contract is *stdin JSON in -> stdout JSON out -> exit 0*.
    This helper replays that contract and parses the resulting JSON payload
    for inspection.
    """

    in_buf = io.StringIO(json.dumps(payload))
    out_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdin", in_buf)
    monkeypatch.setattr(sys, "stdout", out_buf)
    rc = module.main()
    assert rc == 0, f"{module.__name__}.main() returned non-zero exit ({rc})"
    raw = out_buf.getvalue().strip()
    assert raw, f"{module.__name__}.main() produced no stdout"
    return json.loads(raw)


# ---------------------------------------------------------------------------
# AC1 -- file-path module loading sanity checks
# ---------------------------------------------------------------------------


def test_ac1_executors_load_by_file_path(
    code_change_mod: types.ModuleType,
    plan_task_mod: types.ModuleType,
    run_tests_mod: types.ModuleType,
    take_screenshot_mod: types.ModuleType,
) -> None:
    # Every executor must expose a ``main`` callable; that is the contract
    # the gateway dispatcher relies on when shelling out to the script.
    for mod in (code_change_mod, plan_task_mod, run_tests_mod, take_screenshot_mod):
        assert callable(getattr(mod, "main", None)), (
            f"{mod.__name__} is missing a callable main()"
        )


# ---------------------------------------------------------------------------
# AC2 -- safe_name sanitization / truncation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("simple-task_1.0", "simple-task_1.0"),
        ("path/with\\slashes:and?stars*", "path-with-slashes-and-stars-"),
        ("space and unicode 中文", "space-and-unicode---"),
        ("", ""),
    ],
)
def test_ac2_safe_name_sanitizes_unsafe_characters(
    code_change_mod: types.ModuleType,
    take_screenshot_mod: types.ModuleType,
    raw: str,
    expected: str,
) -> None:
    # Both modules implement an identical ``safe_name`` helper -- the
    # dispatcher relies on that consistency for filename safety.
    assert code_change_mod.safe_name(raw) == expected
    assert take_screenshot_mod.safe_name(raw) == expected


def test_ac2_safe_name_truncates_to_60_chars(
    code_change_mod: types.ModuleType,
    take_screenshot_mod: types.ModuleType,
) -> None:
    long_input = "a" * 200
    assert len(code_change_mod.safe_name(long_input)) == 60
    assert len(take_screenshot_mod.safe_name(long_input)) == 60
    # Truncation must keep only allowed characters and never grow the input.
    assert code_change_mod.safe_name(long_input) == "a" * 60


# ---------------------------------------------------------------------------
# AC3 -- plan_task.main() stdin JSON -> ok=true with non-empty steps
# ---------------------------------------------------------------------------


def test_ac3_plan_task_emits_ok_with_steps(
    plan_task_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _drive_main(
        plan_task_mod,
        {"task_id": "plan-1", "command_text": "Refactor login module"},
        monkeypatch,
    )

    assert result["ok"] is True
    assert "summary" in result and isinstance(result["summary"], str)
    steps = result.get("details", {}).get("steps")
    assert isinstance(steps, list) and len(steps) > 0, (
        "plan_task must emit a non-empty details.steps list"
    )
    # Every step entry must be a non-empty string -- callers render these.
    for step in steps:
        assert isinstance(step, str) and step.strip()


def test_ac3_plan_task_handles_missing_command_text(
    plan_task_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even without a command_text the planner must still emit a usable plan.
    result = _drive_main(plan_task_mod, {}, monkeypatch)
    assert result["ok"] is True
    assert result["details"]["steps"]


# ---------------------------------------------------------------------------
# AC4 -- code_change.main() writes proposal under WORKSPACE_PATH/.openclaw/proposals
# ---------------------------------------------------------------------------


def test_ac4_code_change_writes_proposal_file(
    code_change_mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))

    result = _drive_main(
        code_change_mod,
        {
            "task_id": "task/with*unsafe:chars",
            "command_text": "Add validation to user input",
        },
        monkeypatch,
    )

    assert result["ok"] is True
    proposal_file = result.get("details", {}).get("proposal_file")
    assert proposal_file, "code_change result must include details.proposal_file"

    proposal_path = Path(proposal_file)
    # Must live under WORKSPACE_PATH/.openclaw/proposals.
    expected_dir = tmp_path / ".openclaw" / "proposals"
    assert proposal_path.parent == expected_dir, (
        f"proposal must be under {expected_dir}, got {proposal_path.parent}"
    )
    assert proposal_path.exists(), "proposal file must be written to disk"

    # Filename must be the safe_name'd task_id with an .md extension.
    assert proposal_path.suffix == ".md"
    assert proposal_path.stem == code_change_mod.safe_name("task/with*unsafe:chars")

    body = proposal_path.read_text(encoding="utf-8")
    assert "Add validation to user input" in body
    assert "Suggested Steps" in body


# ---------------------------------------------------------------------------
# AC5 -- run_tests.main() honors SAFE_TEST_COMMAND, runs in WORKSPACE_PATH
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ac5_run_tests_uses_safe_test_command_and_workspace(
    run_tests_mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))
    monkeypatch.setenv("SAFE_TEST_COMMAND", 'pytest -k "smoke and not slow" -q')

    captured: dict[str, Any] = {}

    def fake_run(args, cwd, text, capture_output, check):  # type: ignore[no-untyped-def]
        captured["args"] = list(args)
        captured["cwd"] = cwd
        captured["text"] = text
        captured["capture_output"] = capture_output
        captured["check"] = check
        return _FakeCompletedProcess(0, stdout="3 passed", stderr="")

    monkeypatch.setattr(run_tests_mod.subprocess, "run", fake_run)

    result = _drive_main(run_tests_mod, {"task_id": "run-1"}, monkeypatch)

    # shlex.split must respect the quoted ``-k`` argument.
    assert captured["args"] == [
        "pytest",
        "-k",
        "smoke and not slow",
        "-q",
    ]
    assert captured["cwd"] == str(tmp_path)
    assert captured["text"] is True
    assert captured["capture_output"] is True
    assert captured["check"] is False

    assert result["ok"] is True
    details = result["details"]
    assert details["exit_code"] == 0
    assert details["command"] == captured["args"]
    assert details["stdout"] == "3 passed"
    assert details["stderr"] == ""


def test_ac5_run_tests_marks_failure_when_returncode_nonzero(
    run_tests_mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))
    monkeypatch.setenv("SAFE_TEST_COMMAND", "pytest -q")

    long_stdout = "X" * 5000
    long_stderr = "Y" * 5000

    def fake_run(args, cwd, text, capture_output, check):  # type: ignore[no-untyped-def]
        return _FakeCompletedProcess(2, stdout=long_stdout, stderr=long_stderr)

    monkeypatch.setattr(run_tests_mod.subprocess, "run", fake_run)

    result = _drive_main(run_tests_mod, {}, monkeypatch)

    assert result["ok"] is False
    details = result["details"]
    assert details["exit_code"] == 2
    # stdout/stderr are tail-truncated to 3000 chars to keep payloads small.
    assert len(details["stdout"]) == 3000
    assert len(details["stderr"]) == 3000
    assert details["stdout"] == long_stdout[-3000:]
    assert details["stderr"] == long_stderr[-3000:]
    assert details["command"] == ["pytest", "-q"]


# ---------------------------------------------------------------------------
# AC6 -- take_screenshot helpers: wants_all_screens + resolve_output_dir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command_text",
    [
        "please capture all screens",
        "Capture ALL MONITORS now",
        "multi screen snapshot",
        "I need multiple screens",
        "请截取双屏",  # 双屏
        "多屏截图",  # 多屏
        "抓取所有屏幕",  # 所有屏幕
        "全部屏幕拍一下",  # 全部屏幕
    ],
)
def test_ac6_wants_all_screens_detects_keywords(
    take_screenshot_mod: types.ModuleType, command_text: str
) -> None:
    assert take_screenshot_mod.wants_all_screens(command_text) is True


@pytest.mark.parametrize(
    "command_text",
    ["", None, "primary screen only", "just one monitor please"],
)
def test_ac6_wants_all_screens_returns_false_otherwise(
    take_screenshot_mod: types.ModuleType, command_text: Any
) -> None:
    assert take_screenshot_mod.wants_all_screens(command_text) is False


def test_ac6_resolve_output_dir_prefers_shared_volume(
    take_screenshot_mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_root = tmp_path / "shared"
    workspace_root = tmp_path / "workspace"
    monkeypatch.setenv("SHARED_VOLUME_PATH", str(shared_root))
    monkeypatch.setenv("WORKSPACE_PATH", str(workspace_root))

    out_dir, returned_root = take_screenshot_mod.resolve_output_dir()

    assert out_dir == shared_root / "screenshots"
    assert returned_root == shared_root
    assert out_dir.is_dir(), "shared screenshots dir must be created"
    # Workspace fallback must NOT have been touched when SHARED_VOLUME_PATH wins.
    assert not (workspace_root / ".openclaw" / "screenshots").exists()


def test_ac6_resolve_output_dir_falls_back_to_workspace(
    take_screenshot_mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("SHARED_VOLUME_PATH", raising=False)
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))

    out_dir, returned_root = take_screenshot_mod.resolve_output_dir()

    assert out_dir == tmp_path / ".openclaw" / "screenshots"
    assert returned_root is None
    assert out_dir.is_dir()


def test_ac6_resolve_output_dir_blank_shared_path_falls_back(
    take_screenshot_mod: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Whitespace-only SHARED_VOLUME_PATH must be treated as unset.
    monkeypatch.setenv("SHARED_VOLUME_PATH", "   ")
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))

    out_dir, returned_root = take_screenshot_mod.resolve_output_dir()

    assert returned_root is None
    assert out_dir == tmp_path / ".openclaw" / "screenshots"
