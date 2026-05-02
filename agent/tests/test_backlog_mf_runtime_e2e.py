from __future__ import annotations

import json
import os
import time
import uuid
import urllib.error
import urllib.request

import pytest


GOV_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:40000").rstrip("/")
PROJECT_ID = os.environ.get("AMING_CLAW_E2E_PROJECT", "aming-claw")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{GOV_URL}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _expect_http_error(method: str, path: str, body: dict | None, status: int) -> dict:
    try:
        _request(method, path, body)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        parsed = json.loads(payload) if payload else {}
        assert exc.code == status
        return parsed
    raise AssertionError(f"expected HTTP {status} from {method} {path}")


@pytest.fixture(scope="module", autouse=True)
def require_live_governance():
    try:
        health = _request("GET", "/api/health")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"governance service unavailable at {GOV_URL}: {exc}")
    if health.get("status") != "ok":
        pytest.skip(f"governance service unhealthy at {GOV_URL}: {health}")


def _new_bug_id(suffix: str) -> str:
    return f"E2E-MF-{suffix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _create_backlog_row(bug_id: str) -> None:
    _request(
        "POST",
        f"/api/backlog/{PROJECT_ID}/{bug_id}",
        {
            "title": f"E2E MF runtime smoke {bug_id}",
            "status": "OPEN",
            "priority": "P3",
            "target_files": ["agent/governance/server.py"],
            "test_files": ["agent/tests/test_backlog_mf_runtime_e2e.py"],
            "acceptance_criteria": ["E2E smoke row"],
            "details_md": "Temporary E2E row; closed by test after assertions.",
            "force_admit": True,
            "actor": "pytest-e2e",
        },
    )


def _predeclare(bug_id: str, mf_id: str, mf_type: str = "chain_rescue") -> dict:
    return _request(
        "POST",
        f"/api/backlog/{PROJECT_ID}/{bug_id}/predeclare-mf",
        {
            "mf_id": mf_id,
            "mf_type": mf_type,
            "actor": "pytest-e2e",
            "reason": "E2E predeclare for MF runtime smoke path",
        },
    )


def _close_if_possible(bug_id: str) -> None:
    try:
        _request(
            "POST",
            f"/api/backlog/{PROJECT_ID}/{bug_id}/close",
            {"commit": "", "actor": "pytest-e2e"},
        )
    except Exception:
        pass


def test_system_recovery_mf_profile_bypasses_graph_governance():
    bug_id = _new_bug_id("SYSTEM")
    mf_id = "MF-2026-05-02-901"
    try:
        _create_backlog_row(bug_id)
        _predeclare(bug_id, mf_id, mf_type="system_recovery")
        started = _request(
            "POST",
            f"/api/backlog/{PROJECT_ID}/{bug_id}/start-mf",
            {
                "mf_id": mf_id,
                "mf_type": "system_recovery",
                "actor": "pytest-e2e",
                "observer_authorized": True,
                "reason": "E2E system recovery validates graph bypass profile",
            },
        )

        assert started["mf_type"] == "system_recovery"
        assert started["bypass_policy"]["graph_governance"] == "bypass"
        assert started["bypass_policy"]["bypass_graph_governance"] is True

        row = _request("GET", f"/api/backlog/{PROJECT_ID}/{bug_id}")
        assert row["status"] == "MF_IN_PROGRESS"
        assert row["runtime_state"] == "manual_fix_in_progress"
        assert row["mf_type"] == "system_recovery"
    finally:
        _close_if_possible(bug_id)


def test_chain_rescue_mf_profile_enforces_graph_and_rejects_bypass():
    bug_id = _new_bug_id("RESCUE")
    mf_id = "MF-2026-05-02-902"
    try:
        _create_backlog_row(bug_id)
        _predeclare(bug_id, mf_id)
        err = _expect_http_error(
            "POST",
            f"/api/backlog/{PROJECT_ID}/{bug_id}/start-mf",
            {
                "mf_id": mf_id,
                "mf_type": "chain_rescue",
                "bypass_graph_governance": True,
                "actor": "pytest-e2e",
            },
            422,
        )
        assert "system_recovery" in json.dumps(err)

        started = _request(
            "POST",
            f"/api/backlog/{PROJECT_ID}/{bug_id}/start-mf",
            {
                "mf_id": mf_id,
                "mf_type": "chain_rescue",
                "actor": "pytest-e2e",
            },
        )
        assert started["mf_type"] == "chain_rescue"
        assert started["bypass_policy"]["graph_governance"] == "enforce"
        assert started["bypass_policy"]["bypass_graph_governance"] is False
    finally:
        _close_if_possible(bug_id)


def test_mf_takeover_holds_current_unfinished_task():
    bug_id = _new_bug_id("TAKEOVER")
    mf_id = "MF-2026-05-02-903"
    try:
        _create_backlog_row(bug_id)
        task = _request(
            "POST",
            f"/api/task/{PROJECT_ID}/create",
            {
                "type": "task",
                "prompt": "E2E noop task for MF takeover smoke",
                "metadata": {"bug_id": bug_id},
            },
        )
        task_id = task["task_id"]
        _predeclare(bug_id, mf_id)

        started = _request(
            "POST",
            f"/api/backlog/{PROJECT_ID}/{bug_id}/start-mf",
            {
                "mf_id": mf_id,
                "mf_type": "chain_rescue",
                "takeover_action": "hold_current_chain",
                "taken_over_task_id": task_id,
                "takeover_reason": "E2E observer takeover smoke",
                "actor": "pytest-e2e",
            },
        )
        assert started["takeover"]["taken_over_task_id"] == task_id
        assert started["takeover"]["outcome"] == "observer_hold"

        row = _request("GET", f"/api/backlog/{PROJECT_ID}/{bug_id}")
        assert row["takeover"]["taken_over_task_id"] == task_id
        assert row["takeover"]["action"] == "hold_current_chain"

        tasks = _request("GET", f"/api/task/{PROJECT_ID}/list?limit=100")["tasks"]
        matched = [t for t in tasks if t["task_id"] == task_id]
        assert matched, f"created task {task_id} not found in task list"
        assert matched[0]["status"] == "observer_hold"
    finally:
        _close_if_possible(bug_id)
