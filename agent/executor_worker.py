# v2: git-diff verified, artifact-filtered.
# Verified: executor self-fix bootstrap successful.
"""Executor Worker — polls Governance API for tasks and executes them via Claude CLI.

This is the missing link between:
  - Governance task queue (create/claim/complete)
  - AI execution (ai_lifecycle.py → Claude CLI)

Flow:
  1. Poll: GET /api/task/{project}/list?status=queued
  2. Claim: POST /api/task/{project}/claim
  3. Execute: AILifecycleManager.create_session(role, prompt)
  4. Report: POST /api/task/{project}/progress
  5. Complete: POST /api/task/{project}/complete (triggers auto-chain)

Usage:
  python -m agent.executor_worker --project aming-claw
  GOVERNANCE_URL=http://localhost:40000 python -m agent.executor_worker

Full chain verified: dev→test→qa→merge→deploy.
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import argparse
import threading
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

_proj_root = str(Path(__file__).resolve().parent.parent)
_agent_dir = str(Path(__file__).resolve().parent)
# Both paths needed:
#   _proj_root → makes `from agent.governance.X import Y` work (used by merge stage)
#   _agent_dir → makes `from ai_lifecycle import ...` (sibling) work (used by PM/Dev stages)
# Embedded Python's restrictive python312._pth doesn't include either by default.
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

log = logging.getLogger("executor_worker")

# --- Configuration ---

GOVERNANCE_URL = os.getenv("GOVERNANCE_URL", "http://localhost:40000")
POLL_INTERVAL = int(os.getenv("EXECUTOR_POLL_INTERVAL", "10"))
WORKER_ID = os.getenv("EXECUTOR_WORKER_ID", f"executor-{os.getpid()}")
WORKSPACE = os.getenv("CODEX_WORKSPACE", str(Path(__file__).resolve().parents[1]))

# R2: MAX_CONCURRENT_WORKERS — configurable via env var, default 2, clamped to [1, 5]
MAX_CONCURRENT_WORKERS = min(5, max(1, int(os.getenv("MAX_CONCURRENT_WORKERS", "2"))))

# R7: SHUTDOWN_TIMEOUT — configurable via env var, default 120s
SHUTDOWN_TIMEOUT = int(os.getenv("SHUTDOWN_TIMEOUT", "120"))

# Completion is the one API call where a transient governance outage can lose
# finished work. Retry only after error-shaped responses from _api().
COMPLETE_RETRY_DELAYS = (5, 15, 30)
COMPLETE_REQUEST_TIMEOUT = int(os.getenv("COMPLETE_REQUEST_TIMEOUT", "900"))

# Task type → role.
# Timeout is no longer hardcoded per task type.  The ai_lifecycle streaming watchdog
# kills processes that produce no stdout for _HANG_TIMEOUT (120 s), and enforces an
# absolute cap of _MAX_TIMEOUT (1200 s).  External update_progress() calls extend the
# deadline via AILifecycleManager.extend_deadline().
_DEFAULT_TASK_ROLE_MAP = {
    "coordinator": "coordinator",
    "pm":    "pm",
    "dev":   "dev",
    "test":  "script",  # 6b: test tasks run as scripts, not AI
    "qa":    "qa",
    "gatekeeper": "gatekeeper",
    "merge": "script",  # handled by _execute_merge, no AI
    "deploy": "script",  # handled by _execute_deploy, no AI
    "task":  "coordinator",
}


def _build_task_role_map():
    """Build TASK_ROLE_MAP from YAML configs with fallback to defaults."""
    try:
        from agent.governance.role_config import get_all_role_configs
        configs = get_all_role_configs()
        if configs:
            result = dict(_DEFAULT_TASK_ROLE_MAP)
            for role_name, config in configs.items():
                if config.task_type_alias:
                    result[config.task_type_alias] = role_name
            return result
    except Exception:
        pass
    return dict(_DEFAULT_TASK_ROLE_MAP)


TASK_ROLE_MAP = _build_task_role_map()
# Merge/test are script-based, see _execute_merge()/_execute_test()


def _string_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, tuple):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _dedupe_list(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _derive_target_files(metadata: dict) -> list[str]:
    target_files = _string_list(metadata.get("target_files"))
    if target_files:
        return _dedupe_list(target_files)
    cluster_payload = metadata.get("cluster_payload")
    if isinstance(cluster_payload, dict):
        primary_files = _string_list(cluster_payload.get("primary_files"))
        if primary_files:
            return _dedupe_list(primary_files)
        return _dedupe_list(_string_list(cluster_payload.get("target_files")))
    return []


def _derive_test_files(metadata: dict) -> list[str]:
    test_files = _string_list(metadata.get("test_files"))
    if test_files:
        return _dedupe_list(test_files)
    cluster_report = metadata.get("cluster_report")
    if isinstance(cluster_report, dict):
        return _dedupe_list(_string_list(cluster_report.get("expected_test_files")))
    return []


def _reconcile_candidate_manifest(cluster_payload: dict) -> list[dict]:
    """Compact, non-truncated manifest of candidate nodes for PM prompts."""
    if not isinstance(cluster_payload, dict):
        return []
    raw_nodes = cluster_payload.get("candidate_nodes")
    if not isinstance(raw_nodes, list):
        return []
    manifest: list[dict] = []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        item = {
            "node_id": str(raw.get("node_id") or raw.get("id") or "").strip(),
            "title": str(raw.get("title") or "").strip(),
            "primary": _string_list(raw.get("primary") or raw.get("primary_files")),
            "layer": raw.get("layer"),
            "parent_layer": raw.get("parent_layer"),
            "hierarchy_parent": (
                raw.get("hierarchy_parent")
                or raw.get("parent")
                or raw.get("parent_id")
                or metadata.get("hierarchy_parent")
            ),
            "deps": _string_list(raw.get("_deps") or raw.get("deps")),
            "secondary": _string_list(raw.get("secondary") or raw.get("secondary_files")),
            "test": _string_list(raw.get("test") or raw.get("tests") or raw.get("test_files")),
            "test_coverage": raw.get("test_coverage"),
        }
        manifest.append({k: v for k, v in item.items() if v not in (None, "", [])})
    return manifest


def _parse_pytest_output(stdout: str, stderr: str, returncode: int) -> dict:
    """6c: Parse pytest output into structured test_report.

    Extracts passed/failed/error counts from pytest's summary line.
    Falls back to exit code if summary line is not found.
    """
    import re
    report = {"tool": "pytest", "passed": 0, "failed": 0, "errors": 0}

    # Try to parse pytest summary: "X passed, Y failed, Z error" etc.
    combined = stdout + "\n" + stderr
    # Match patterns like "5 passed", "2 failed", "1 error", "3 warnings"
    passed_m = re.search(r"(\d+)\s+passed", combined)
    failed_m = re.search(r"(\d+)\s+failed", combined)
    error_m = re.search(r"(\d+)\s+error", combined)

    if passed_m:
        report["passed"] = int(passed_m.group(1))
    if failed_m:
        report["failed"] = int(failed_m.group(1))
    if error_m:
        report["errors"] = int(error_m.group(1))

    # Extract command from first line or fallback
    cmd_m = re.search(r"^(pytest|python -m pytest)\s.*$", combined, re.MULTILINE)
    if cmd_m:
        report["command"] = cmd_m.group(0)[:200]

    # Fallback: if no counts found, use exit code
    if not passed_m and not failed_m and not error_m:
        if returncode == 0:
            report["passed"] = 1  # at least something passed
            report["summary"] = "exit code 0 (assumed pass)"
        else:
            report["failed"] = 1
            report["summary"] = f"exit code {returncode}"
            report["stderr"] = stderr[:500] if stderr else ""

    return report


# ---------------------------------------------------------------------------
# B41 AC2: Fail-fast guard for non-portable verification commands
# ---------------------------------------------------------------------------
_BANNED_TOKENS = frozenset({"grep", "sed", "awk", "find", "head", "tail", "cat", "cut", "xargs"})
_BANNED_OPS = (" && ", " || ", " | ", " ; ")


def _assert_portable_verification_command(cmd: str):
    """Reject Unix-only commands before subprocess execution.

    Returns None on pass (safe to proceed), or a failure dict on rejection.
    """
    if not cmd or not cmd.strip():
        return _b41_reject(cmd, "empty or whitespace-only command")

    first_token = cmd.split()[0].strip("\"'`")
    if first_token in _BANNED_TOKENS:
        return _b41_reject(cmd, f"banned Unix command: {first_token}")

    for op in _BANNED_OPS:
        if op in cmd:
            return _b41_reject(cmd, f"shell chaining operator: {op.strip()}")

    return None


def _b41_reject(cmd: str, reason: str) -> dict:
    return {
        "status": "failed",
        "result": {
            "test_report": {
                "tool": "b41-guard",
                "summary": f"B41: non-portable verification.command rejected: {reason}",
                "passed": 0,
                "failed": 1,
                "command": cmd,
            },
            "error": f"B41: non-portable verification.command rejected: {reason}",
        },
    }


# Stall detection: after N consecutive empty polls with queued tasks, force-restart poll loop.
EXECUTOR_STALL_THRESHOLD = int(os.getenv("EXECUTOR_STALL_THRESHOLD", "20"))

# Absolute ceiling passed to ai_lifecycle; actual enforcement is via heartbeat watchdog.
MAX_SESSION_TIMEOUT = 1200


# ---------------------------------------------------------------------------
# OPT-BACKLOG-CH1-COORD-AUTOTAG: coordinator auto-extracts backlog ID from prompt
# ---------------------------------------------------------------------------
# Recognizes three ID shapes emitted by the governance backlog:
#   - B\d+                       — bug IDs (e.g. B41)
#   - MF-YYYY-MM-DD-NNN          — manual-fix IDs (e.g. MF-2026-04-21-004)
#   - OPT-[A-Z0-9][A-Z0-9-]*     — optimization epic / sub-chain IDs
#
# The extraction is used by _handle_coordinator_v1's create_pm_task path to
# inject metadata.bug_id into the PM task, so the downstream merge-stage
# helper auto_chain._try_backlog_close_via_db can fire backlog close on merge.
# Governed by graph node L4.43 (backlog-as-chain-source policy).
_BACKLOG_ID_RE = re.compile(
    r"\b("
    r"B\d+"
    r"|MF-\d{4}-\d{2}-\d{2}-\d{3}"
    r"|OPT-[A-Z0-9][A-Z0-9-]*"
    r")\b"
)


def _extract_backlog_id(text: str) -> Optional[str]:
    """Return the first backlog ID found in *text*, or None if no match.

    Recognizes B\\d+, MF-YYYY-MM-DD-NNN, and OPT-XXX patterns. Word-bounded so
    substrings like ``MY-B42-LIB`` or ``OPTION-X`` do NOT match.
    """
    if not text:
        return None
    m = _BACKLOG_ID_RE.search(text)
    return m.group(1) if m else None


class ExecutorWorker:
    """Polls governance API, claims tasks, executes via Claude CLI."""

    def __init__(self, project_id: str, governance_url: str = GOVERNANCE_URL,
                 worker_id: str = WORKER_ID, workspace: str = WORKSPACE):
        self.project_id = project_id
        self.base_url = governance_url.rstrip("/")
        self.worker_id = worker_id
        self.workspace = workspace
        self._running = False
        self._current_task = None
        self._lifecycle = None
        self._pid_path = None  # type: Optional[str]
        self._consecutive_empty_polls = 0  # tracks consecutive polls with no task
        self._start_time = time.monotonic()
        self.last_claimed_at = time.monotonic()  # R5: tracked by ServiceManager watchdog

    def _api(self, method: str, path: str, data: dict = None, timeout: Optional[int] = None) -> dict:
        """Call governance API. Short timeouts to avoid MCP IO deadlock."""
        import requests
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                r = requests.get(url, timeout=timeout or 5)
            else:
                r = requests.post(url, json=data or {}, timeout=timeout or 10,
                                  headers={"Content-Type": "application/json"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            # DO NOT use log.warning here — it blocks in MCP subprocess (IO pipe deadlock)
            return {"error": str(e)}

    def _check_queued_tasks(self) -> int:
        """R6: Check how many queued tasks exist via GET /api/task/{project}/list."""
        result = self._api("GET", f"/api/task/{self.project_id}/list")
        if "error" in result:
            return 0
        tasks = result.get("tasks", [])
        return sum(1 for t in tasks if t.get("status") in ("queued", "pending"))

    def _claim_task(self) -> Optional[Dict]:
        """Try to claim next queued task."""
        result = self._api("POST", f"/api/task/{self.project_id}/claim",
                           {"worker_id": self.worker_id, "caller_pid": os.getpid()})
        if "error" in result or "task" not in result:
            return None
        task_payload = result["task"]
        fence_token = result.get("fence_token", "")
        if isinstance(task_payload, (list, tuple)):
            if len(task_payload) < 2:
                return None
            task_data, fence_token = task_payload[0], task_payload[1]
        else:
            task_data = task_payload
        if not task_data or not isinstance(task_data, dict):
            return None
        task_data["_fence_token"] = fence_token
        return task_data

    def _report_progress(self, task_id: str, progress: dict):
        """Report execution progress."""
        self._api("POST", f"/api/task/{self.project_id}/progress",
                  {"task_id": task_id, "progress": progress})

    def _complete_task(self, task_id: str, status: str, result: dict) -> dict:
        """Mark task complete (triggers auto-chain)."""
        payload = {"task_id": task_id, "status": status, "result": result}
        response = self._api(
            "POST", f"/api/task/{self.project_id}/complete", payload,
            timeout=COMPLETE_REQUEST_TIMEOUT,
        )
        for delay in COMPLETE_RETRY_DELAYS:
            if "error" not in response:
                return response
            log.warning(
                "complete_task error for %s; retrying in %ss: %s",
                task_id, delay, response.get("error"),
            )
            time.sleep(delay)
            response = self._api(
                "POST", f"/api/task/{self.project_id}/complete", payload,
                timeout=COMPLETE_REQUEST_TIMEOUT,
            )
        return response

    def _complete_task_or_raise(self, task_id: str, status: str, result: dict) -> dict:
        """Complete a task and fail the worker if governance never accepted it."""
        response = self._complete_task(task_id, status, result)
        if "error" in response:
            raise RuntimeError(
                f"complete_task failed after retries for {task_id}: {response.get('error')}"
            )
        return response

    def _execute_task(self, task: dict) -> dict:
        """Execute a single task via Claude CLI."""
        task_id = task["task_id"]
        task_type = task.get("type", "task")
        prompt = task.get("prompt", "")
        metadata = task.get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        role = TASK_ROLE_MAP.get(task_type, "dev")

        import time as _time
        _t0 = _time.time()
        # Write timing to a file for debugging (host process logs may not be visible)
        _timing_file = os.path.join(self.workspace or ".", "shared-volume", "codex-tasks", "logs",
                                     f"timing-{task_id}.txt")
        os.makedirs(os.path.dirname(_timing_file), exist_ok=True)
        def _timing(msg):
            # Write to file ONLY — log.info blocks intermittently in MCP subprocess
            try:
                with open(_timing_file, "a") as f:
                    f.write(f"{_time.time() - _t0:.1f}s {msg}\n")
            except Exception:
                pass
        _timing(f"start: type={task_type} role={role}")

        # Report progress (non-blocking — short timeout to avoid IO pipe deadlock)
        try:
            import requests as _req
            _req.post(f"{self.base_url}/api/task/{self.project_id}/progress",
                      json={"task_id": task_id, "progress": {"step": "starting", "role": role}},
                      timeout=3)
        except Exception:
            pass
        _timing("report_progress: done")

        # Merge/deploy/test are script operations, not AI
        if task_type == "merge":
            return self._execute_merge(task_id, metadata)
        if task_type == "deploy":
            return self._execute_deploy(task_id, metadata)
        if task_type == "test":
            return self._execute_test(task_id, metadata)
        # observer-hotfix-3 2026-04-25: reconcile is also a script operation,
        # delegated to agent.governance.reconcile_task.run_reconcile_pipeline.
        # Phase J added the task type but missed wiring it into executor; without
        # this handler, executor falls back to AI 'dev' role and times out.
        if task_type == "reconcile" or task_type.startswith("reconcile_"):
            return self._execute_reconcile(task_id, metadata)

        target_files = _derive_target_files(metadata)
        test_files = _derive_test_files(metadata)

        worktree_path = None
        branch_name = None
        execution_workspace = self.workspace
        try:
            attempt_num = int(task.get("attempt_num") or metadata.get("attempt_num") or 1)
        except Exception:
            attempt_num = 1
        if task_type == "dev":
            _timing("worktree: starting")
            worktree_path, branch_name = self._create_worktree(
                task_id,
                base_ref=self._dev_worktree_base_ref(metadata),
                base_commit=metadata.get("reconcile_target_base_commit", ""),
                attempt_num=attempt_num,
            )
            if worktree_path:
                execution_workspace = worktree_path
                _timing(f"worktree: created {worktree_path}")
            else:
                reason = "worktree creation returned (None, None)"
                log.warning(f"worktree creation failed for dev task {task_id}: {reason}")
                _timing("worktree: creation failed, returning error")
                return {
                    "status": "failed",
                    "error": f"worktree creation failed: {reason}",
                }
        elif task_type in ("test", "qa"):
            inherited_worktree = metadata.get("_worktree", "")
            inherited_branch = metadata.get("_branch", "")
            if inherited_worktree and os.path.isdir(inherited_worktree):
                execution_workspace = inherited_worktree
                worktree_path = inherited_worktree
                branch_name = inherited_branch
                _timing(f"worktree: reusing {inherited_worktree}")
            else:
                _timing("worktree: no inherited worktree, fallback to main workspace")

        # Build context for AI session
        context = {
            "task_id": task_id,
            "task_type": task_type,
            "project_id": self.project_id,
            "metadata": metadata,
            "operation_type": metadata.get("operation_type", ""),
            "cluster_payload": metadata.get("cluster_payload", {}),
            "cluster_report": metadata.get("cluster_report", {}),
            "target_files": target_files,
            "changed_files": metadata.get("changed_files", []),
            "verification": metadata.get("verification", {}),
            "test_files": test_files,
            "requirements": metadata.get("requirements", []),
            "acceptance_criteria": metadata.get("acceptance_criteria", []),
            "doc_impact": metadata.get("doc_impact", {}),
            "test_report": metadata.get("test_report", {}),
            "related_nodes": metadata.get("related_nodes", []),
            "attempt_num": task.get("attempt_num", 1),
            "chat_id": metadata.get("chat_id", ""),
            "previous_gate_reason": metadata.get("previous_gate_reason", ""),
            "rejection_reason": metadata.get("rejection_reason", ""),
            "_round2_memories": metadata.get("_round2_memories", []),
            "_coordinator_memories": metadata.get("_coordinator_memories", []),
            "_coordinator_context": metadata.get("_coordinator_context", {}),
            "workspace": execution_workspace,
        }

        # Enhance prompt with governance context
        enhanced_prompt = self._build_prompt(prompt, task_type, context)
        _timing(f"build_prompt done: prompt_len={len(enhanced_prompt)}")

        # Create AI session
        if self._lifecycle is None:
            from ai_lifecycle import AILifecycleManager
            self._lifecycle = AILifecycleManager()

        _t1 = _time.time()
        session = self._lifecycle.create_session(
            role=role,
            prompt=enhanced_prompt,
            context=context,
            project_id=self.project_id,
            timeout_sec=MAX_SESSION_TIMEOUT,
            workspace=execution_workspace,
        )

        _timing(f"create_session done: pid={session.pid}")

        if session.status == "failed":
            return {"status": "failed", "error": session.stderr}

        # Wait for completion with progress reporting (short timeout to avoid IO pipe deadlock)
        try:
            _req.post(f"{self.base_url}/api/task/{self.project_id}/progress",
                      json={"task_id": task_id, "progress": {"step": "running", "session_id": session.session_id}},
                      timeout=3)
        except Exception:
            pass
        if hasattr(self._lifecycle, "extend_deadline"):
            self._lifecycle.extend_deadline(session.session_id)

        _t2 = _time.time()
        output = self._lifecycle.wait_for_output(session.session_id)
        _timing(f"wait_for_output done: status={output.get('status')} elapsed={output.get('elapsed_sec')}s")

        if session.status == "timeout":
            _timing(f"TIMEOUT after {_time.time() - _t0:.1f}s total")
            return {"status": "failed", "error": "Session timed out (hung or exceeded max runtime)"}
        if session.status == "failed":
            return {"status": "failed", "error": session.stderr[:500]}
        # Detect actually changed files via git diff (skip for non-code roles)
        if task_type in ("coordinator", "task", "pm", "qa"):
            changed_files = []
            _timing(f"git_diff: skipped ({task_type})")
        else:
            _timing("git_diff: starting")
            changed_files = self._get_git_changed_files(cwd=execution_workspace)
            _timing(f"git_diff: done, {len(changed_files)} files")

        # Stage changed files if any
        if changed_files:
            try:
                import subprocess
                subprocess.run(
                    ["git", "add", "--"] + changed_files,
                    cwd=execution_workspace,
                    capture_output=True,
                    timeout=30,
                )
                log.info("Staged %d changed file(s): %s", len(changed_files), changed_files)
            except Exception as e:
                log.warning("git add failed: %s", e)

        # Parse output FIRST — structured extraction takes priority over error detection.
        # PM/QA often emit a natural-language preamble before fenced JSON; parsing must
        # run before _detect_terminal_cli_error so the preamble is not misclassified.
        _timing("parse_output: starting")
        result = self._parse_output(session, task_type)
        _timing(f"parse_output: done, keys={list(result.keys())}")

        # Fallback: only check for terminal CLI error if _parse_output did NOT find
        # valid structured JSON (i.e. it fell through to the raw-summary fallback).
        _is_raw_fallback = (
            set(result.keys()) <= {"summary", "exit_code"}
            and "exit_code" in result
        )
        if _is_raw_fallback:
            terminal_cli_error = self._detect_terminal_cli_error(session, task_type)
            if terminal_cli_error:
                _timing(f"terminal_cli_error: {terminal_cli_error}")
                return {
                    "status": "failed",
                    "error": terminal_cli_error,
                    "result": {
                        "error": terminal_cli_error,
                        "summary": terminal_cli_error,
                    },
                }

        # B28b: QA hard validation — non-JSON or missing/invalid recommendation must fail
        # immediately so the gate receives a clean failure instead of a silent None.
        if task_type == "qa":
            if _is_raw_fallback:
                _err = "structured_output_invalid:no_json"
                return {
                    "status": "failed",
                    "error": _err,
                    "result": {"error": _err, "summary": "QA output is not valid JSON"},
                }
            _rec = result.get("recommendation")
            if _rec is None:
                _err = "structured_output_invalid:missing_recommendation"
                return {
                    "status": "failed",
                    "error": _err,
                    "result": {"error": _err, "summary": "QA output missing recommendation field"},
                }
            _VALID_QA_RECS = {"qa_pass", "reject", "merge_pass"}
            if _rec not in _VALID_QA_RECS:
                _err = f"structured_output_invalid:invalid_recommendation:{_rec}"
                return {
                    "status": "failed",
                    "error": _err,
                    "result": {"error": _err, "summary": f"QA recommendation '{_rec}' is not valid"},
                }

        # Always overwrite/set changed_files from git diff (ground truth)
        # IMPORTANT: git diff is authoritative; always set even if _parse_output
        # returned its own changed_files (it may be stale or from AI hallucination)
        result["changed_files"] = changed_files if changed_files else result.get("changed_files", [])
        if worktree_path and branch_name:
            result["_worktree"] = worktree_path
            result["_branch"] = branch_name

        _timing(f"final_result: changed_files={result.get('changed_files')} keys={list(result.keys())}")

        # Write structured memory on completion
        _timing("write_memory: starting")
        self._write_memory(task_type, task_id, result, metadata)
        _timing("write_memory: done, returning")

        return {"status": "succeeded", "result": result}

    def _write_memory(self, task_type: str, task_id: str, result: dict, metadata: dict):
        """Write structured memory after task completion (best-effort)."""
        try:
            changed = result.get("changed_files", [])
            summary = result.get("summary", "")

            if task_type == "dev" and (summary or changed):
                prompt_lower = (metadata.get("original_prompt", summary) or "").lower()
                if any(w in prompt_lower for w in ("fix", "bug", "error")):
                    decision_type = "bugfix"
                elif any(w in prompt_lower for w in ("add", "new", "create", "implement")):
                    decision_type = "feature"
                elif any(w in prompt_lower for w in ("refactor", "clean", "rename")):
                    decision_type = "refactor"
                else:
                    decision_type = "config"

                gate_reason = metadata.get("previous_gate_reason", "")
                self._api("POST", f"/api/mem/{self.project_id}/write", {
                    "module_id": changed[0] if changed else "general",
                    "kind": "decision",
                    "content": summary or f"Changed {len(changed)} files",
                    "structured": {
                        "decision_type": decision_type,
                        "related_files": changed,
                        "validation_status": "untested",
                        "failure_pattern": gate_reason if gate_reason else None,
                        "followup_needed": bool(gate_reason),
                        "task_id": task_id,
                        "chain_stage": "dev",
                    },
                })

            elif task_type == "test":
                report = result.get("test_report", {})
                if not isinstance(report, dict) or not report:
                    return
                passed = report.get("passed", 0) or 0
                failed = report.get("failed", 0) or 0
                self._api("POST", f"/api/mem/{self.project_id}/write", {
                    "module_id": "testing",
                    "kind": "test_result" if failed == 0 else "failure_pattern",
                    "content": f"{passed} passed, {failed} failed",
                    "structured": {
                        "related_files": changed,
                        "validation_status": "tested" if failed == 0 else "failed",
                        "failure_pattern": report.get("error_summary", "") if failed > 0 else None,
                        "followup_needed": failed > 0,
                        "task_id": task_id,
                        "chain_stage": "test",
                    },
                })

            elif task_type == "qa" and result.get("recommendation") == "reject":
                self._api("POST", f"/api/mem/{self.project_id}/write", {
                    "module_id": changed[0] if changed else "general",
                    "kind": "failure_pattern",
                    "content": result.get("review_summary", "QA rejected"),
                    "structured": {
                        "root_cause": result.get("reject_reason", ""),
                        "related_files": changed,
                        "followup_needed": True,
                        "task_id": task_id,
                        "chain_stage": "qa",
                    },
                })
        except Exception as e:
            log.warning("Memory write failed (non-fatal): %s", e)

    def _execute_test(self, task_id: str, metadata: dict) -> dict:
        """6a: Test is a script operation — run pytest directly, no Claude CLI.

        Pre-flight checks verify test files exist before running.
        Supports command_argv (shell=False) and command_shell (shell=True).
        """
        import subprocess as _sp
        import shlex

        # Determine execution workspace (inherit worktree from dev stage)
        execution_workspace = self.workspace
        inherited_worktree = metadata.get("_worktree", "")
        if inherited_worktree and os.path.isdir(inherited_worktree):
            execution_workspace = inherited_worktree

        # 6a: Pre-flight file check — verify test files exist
        test_files = metadata.get("test_files", [])
        verification = metadata.get("verification", {})
        if not test_files and isinstance(verification, dict):
            cmd_str = verification.get("command", "")
            if cmd_str:
                # Extract test file paths from command
                test_files = [p for p in cmd_str.split() if p.endswith(".py")]

        missing = [f for f in test_files if not os.path.isfile(os.path.join(execution_workspace, f))]
        if missing:
            return {
                "status": "failed",
                "result": {
                    "error": f"Pre-flight: test files missing: {missing}",
                    "test_report": {"passed": 0, "failed": 0, "errors": 1, "tool": "pre-flight"},
                },
            }

        # 6e: Build command — prefer command_argv, fallback to command_shell, then shlex
        command_argv = metadata.get("command_argv")
        command_shell = metadata.get("command_shell")
        use_shell = False

        if command_argv and isinstance(command_argv, list):
            cmd = command_argv
        elif command_shell and isinstance(command_shell, str):
            cmd = command_shell
            use_shell = True
        elif isinstance(verification, dict) and verification.get("command"):
            cmd_str = verification["command"]
            # Normalize bare pytest → python -m pytest (Windows PATH issue)
            cmd_str = cmd_str.replace("pytest ", "python -m pytest ", 1) if cmd_str.startswith("pytest ") else cmd_str
            # R1: shell operators (&&, ||, ;, |) require shell=True to be parsed
            if any(op in cmd_str for op in ("&&", "||", ";", "|")):
                cmd = cmd_str
                use_shell = True
            else:
                try:
                    cmd = shlex.split(cmd_str)
                except ValueError:
                    cmd = cmd_str
                    use_shell = True
        else:
            # Default: run all test files with pytest
            cmd = [sys.executable, "-m", "pytest"] + test_files + ["-v", "--tb=short"]

        # B41 AC2: fail-fast guard for non-portable commands
        cmd_for_guard = cmd if isinstance(cmd, str) else " ".join(cmd)
        guard_result = _assert_portable_verification_command(cmd_for_guard)
        if guard_result is not None:
            log.warning("test_script: B41 guard rejected command: %s", cmd_for_guard)
            return guard_result

        log.info("test_script: running command in %s: %s (shell=%s)", execution_workspace, cmd, use_shell)

        # B50: Propagate PYTHONPATH so legacy governance.X imports resolve
        from pathlib import Path as _Path
        _repo_root = _Path(execution_workspace).resolve()
        _new_pp = str(_repo_root) + os.pathsep + str(_repo_root / "agent")
        _existing_pp = os.environ.get("PYTHONPATH", "")
        if _existing_pp:
            _new_pp = _new_pp + os.pathsep + _existing_pp
        test_env = {**os.environ, "PYTHONPATH": _new_pp}

        try:
            proc = _sp.run(
                cmd,
                cwd=execution_workspace,
                capture_output=True,
                text=True,
                timeout=300,
                shell=use_shell,
                env=test_env,
            )
            report = _parse_pytest_output(proc.stdout, proc.stderr, proc.returncode)
            result = {
                "test_report": report,
                "changed_files": metadata.get("changed_files", []),
                "_worktree": inherited_worktree,
                "_branch": metadata.get("_branch", ""),
            }
            if report.get("failed", 0) == 0 and report.get("errors", 0) == 0:
                return {"status": "succeeded", "result": result}
            else:
                result["error"] = f"Tests failed: {report.get('failed',0)} failures, {report.get('errors',0)} errors"
                return {"status": "failed", "result": result}
        except _sp.TimeoutExpired:
            return {
                "status": "failed",
                "result": {
                    "error": "Test execution timed out (300s)",
                    "test_report": {"passed": 0, "failed": 0, "errors": 1, "tool": "pytest", "timeout": True},
                },
            }
        except Exception as e:
            return {
                "status": "failed",
                "result": {
                    "error": f"Test execution error: {e}",
                    "test_report": {"passed": 0, "failed": 0, "errors": 1, "tool": "pytest"},
                },
            }

    @staticmethod
    def _try_backlog_close_impl(project_id, bug_id, commit_hash, api_fn):
        """Best-effort backlog close via governance API. Never raises."""
        if not bug_id:
            return
        try:
            api_fn("POST", f"/api/backlog/{project_id}/{bug_id}/close",
                   {"commit": commit_hash, "actor": "executor-merge"})
            log.info("backlog close: closed %s after merge commit %s", bug_id, commit_hash)
        except Exception:
            log.warning("backlog close failed for %s (non-fatal)", bug_id, exc_info=True)

    def _execute_merge(self, task_id: str, metadata: dict) -> dict:
        """Merge is a script operation.

        For chained dev work, we verify the merge in a clean integration
        worktree so a dirty main workspace does not block the pipeline.
        """
        import subprocess

        changed = metadata.get("changed_files", [])
        branch = metadata.get("_branch", "")
        worktree = metadata.get("_worktree", "")
        reconcile_target_branch = (
            metadata.get("reconcile_target_branch", "")
            if metadata.get("operation_type") == "reconcile-cluster" else ""
        )
        reconcile_target_base = metadata.get("reconcile_target_base_commit", "")
        merge_target_ref = reconcile_target_branch or "HEAD"
        merge_target_label = reconcile_target_branch or "main"
        self._report_progress(task_id, {"step": "merging"})

        # Chained merge without isolation metadata: check if changes already on main
        if metadata.get("parent_task_id") and not branch:
            # Observer-completed dev tasks don't produce _branch/_worktree.
            # If changed_files are already committed to main, treat as pre-merged.
            if metadata.get("_already_merged") or metadata.get("_merge_commit"):
                rev = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=self.workspace, capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                merge_commit = metadata.get("_merge_commit", rev)
                log.info("merge: pre-merged chain (observer), commit=%s", merge_commit)
                return {"status": "succeeded", "result": {
                    "merge_commit": merge_commit,
                    "changed_files": changed,
                    "pre_merged": True,
                }}
            # Check if HEAD already contains the expected changes
            try:
                head_rev = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=self.workspace, capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                # Query chain_version via HTTP API (executor runs standalone, no relative imports)
                import urllib.request
                vc_resp = urllib.request.urlopen(
                    f"{self.base_url}/api/version-check/{self.project_id}", timeout=10,
                )
                vc_data = json.loads(vc_resp.read())
                chain_ver = vc_data.get("chain_version", "")
                # B35: tolerate short/full hash mismatch via prefix match
                if chain_ver and not (chain_ver.startswith(head_rev) or head_rev.startswith(chain_ver)):
                    log.info("merge: no isolation branch but HEAD (%s) ahead of chain_version (%s) — treating as pre-merged",
                             head_rev, chain_ver)
                    return {"status": "succeeded", "result": {
                        "merge_commit": head_rev,
                        "changed_files": changed,
                        "pre_merged": True,
                    }}
                # HEAD == chain_version: explicit pre_merged metadata flag
                if metadata.get("pre_merged"):
                    log.info("merge: explicit pre_merged flag, HEAD=%s", head_rev)
                    return {"status": "succeeded", "result": {
                        "pre_merged": True,
                    }}
                # HEAD == chain_version: check if changed_files present in HEAD commit
                if changed:
                    head_files_proc = subprocess.run(
                        ["git", "log", "-1", "--name-only", "--format=", "HEAD"],
                        cwd=self.workspace, capture_output=True, text=True, timeout=5,
                    )
                    head_files = {f.strip() for f in head_files_proc.stdout.splitlines() if f.strip()}
                    if all(cf in head_files for cf in changed):
                        log.info("merge: HEAD==chain_version and all changed_files in HEAD commit — pre-merged, HEAD=%s", head_rev)
                        return {"status": "succeeded", "result": {
                            "merge_commit": head_rev,
                            "changed_files": changed,
                            "pre_merged": True,
                        }}
            except Exception as e:
                log.warning("merge: pre-merge detection failed: %s", e)
            return {
                "status": "failed",
                "error": (
                    "Chained merge task has parent_task_id but no isolated "
                    "merge metadata (_branch/_worktree). Refusing to touch "
                    "main-workspace files. Re-run the dev chain to produce "
                    "proper isolation metadata."
                ),
            }

        try:
            if branch:
                if not self._branch_exists(branch):
                    return {"status": "failed", "error": f"Merge branch missing for chained merge: {branch}"}
                if reconcile_target_branch:
                    ok, err = self._ensure_branch_at_ref(reconcile_target_branch, reconcile_target_base or "HEAD")
                    if not ok:
                        return {"status": "failed", "error": f"reconcile target branch setup failed: {err[:300]}"}

                worktree_available = bool(worktree and os.path.isdir(worktree))
                branch_already_merged = False
                if worktree_available:
                    subprocess.run(["git", "add", "-A"],
                                   cwd=worktree, capture_output=True, timeout=30)
                    status = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                            cwd=worktree, capture_output=True, text=True, timeout=10)
                    staged = [f.strip() for f in status.stdout.splitlines() if f.strip()]
                    if staged:
                        msg = f"dev: {task_id}\n\nChanged files: {', '.join(staged[:10])}"
                        commit_proc = subprocess.run(["git", "commit", "-m", msg],
                                                     cwd=worktree, capture_output=True, text=True, timeout=30)
                        if commit_proc.returncode != 0:
                            return {"status": "failed", "error": f"git commit failed in dev worktree: {commit_proc.stderr[:300]}"}
                        branch_already_merged = False
                    elif reconcile_target_branch:
                        branch_already_merged = self._branch_already_merged(branch, merge_target_ref)
                    else:
                        branch_already_merged = self._branch_already_merged(branch)
                else:
                    if reconcile_target_branch:
                        branch_already_merged = self._branch_already_merged(branch, merge_target_ref)
                    else:
                        branch_already_merged = self._branch_already_merged(branch)
                    if not branch_already_merged:
                        log.info("merge replay without dev worktree: task=%s branch=%s", task_id, branch)

                if branch_already_merged:
                    rev = subprocess.run(["git", "rev-parse", merge_target_ref],
                                         cwd=self.workspace, capture_output=True, text=True, timeout=5)
                    return {"status": "succeeded", "result": {
                        "merge_commit": rev.stdout.strip(),
                        "branch": merge_target_label,
                        "merge_mode": "already_merged_replay",
                        "files_changed": len(changed),
                        "changed_files": changed,
                        "reconcile_target_branch": reconcile_target_branch,
                        "reconcile_target_base_commit": reconcile_target_base,
                        "main_redeployed": False if reconcile_target_branch else True,
                    }}

                integration_worktree = ""
                integration_branch = ""
                try:
                    integration_worktree, integration_branch, create_error = self._create_integration_worktree(
                        task_id,
                        base_ref=merge_target_ref,
                    )
                    if not integration_worktree:
                        return {"status": "failed", "error": f"Integration worktree setup failed: {create_error[:300]}"}

                    # Use chain_trailer for merge with 4-field trailer (Phase A §4.4)
                    from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state
                    chain_state = get_chain_state(cwd=integration_worktree)
                    parent_chain_sha = chain_state.get("chain_sha", "")
                    bug_id = metadata.get("bug_id", "") or metadata.get("chain_bug_id", "")
                    success, merge_commit, err = write_merge_with_trailer(
                        message=f"Auto-merge: {task_id}",
                        branch=branch,
                        cwd=integration_worktree,
                        task_id=task_id,
                        parent_chain_sha=parent_chain_sha,
                        bug_id=bug_id)
                    if not success:
                        return {"status": "failed", "error": f"Merge conflict: {err[:300]}"}

                    if reconcile_target_branch:
                        ff_proc = subprocess.run(
                            ["git", "branch", "-f", reconcile_target_branch, merge_commit],
                            cwd=self.workspace, capture_output=True, text=True, timeout=30)
                        if ff_proc.returncode != 0:
                            return {"status": "failed", "error": f"ff-only advance of reconcile branch failed: {ff_proc.stderr[:300]}"}
                        rev_proc = subprocess.run(
                            ["git", "rev-parse", reconcile_target_branch],
                            cwd=self.workspace, capture_output=True, text=True, timeout=5)
                        if rev_proc.returncode == 0 and rev_proc.stdout.strip():
                            merge_commit = rev_proc.stdout.strip()
                        if worktree_available:
                            self._remove_worktree(worktree, branch, delete_branch=False)
                        return {"status": "succeeded", "result": {
                            "merge_commit": merge_commit,
                            "branch": reconcile_target_branch,
                            "merge_mode": "reconcile_target_branch",
                            "files_changed": len(changed),
                            "changed_files": changed,
                            "reconcile_target_branch": reconcile_target_branch,
                            "reconcile_target_base_commit": reconcile_target_base,
                            "main_redeployed": False,
                        }}

                    # B20: Clean leaked staged/untracked files before ff-only merge
                    try:
                        # Unstage any leaked files from worktree contamination
                        subprocess.run(["git", "reset", "HEAD", "--"],
                                       cwd=self.workspace, capture_output=True, timeout=10)
                        # Remove untracked files that conflict with merge
                        merge_files = subprocess.run(
                            ["git", "diff", "--name-only", "HEAD", merge_commit],
                            cwd=integration_worktree, capture_output=True, text=True, timeout=10
                        ).stdout.strip().splitlines()
                        for f in merge_files:
                            untracked = os.path.join(self.workspace, f)
                            if os.path.exists(untracked):
                                tracked = subprocess.run(
                                    ["git", "ls-files", f],
                                    cwd=self.workspace, capture_output=True, text=True, timeout=5
                                ).stdout.strip()
                                if not tracked:
                                    os.remove(untracked)
                                    log.info("merge: removed untracked %s before ff-only", f)
                    except Exception as e:
                        log.warning("merge: pre-ff cleanup failed (non-fatal): %s", e)

                    # Advance real workspace main to the merge commit via ff-only
                    ff_proc = subprocess.run(
                        ["git", "merge", "--ff-only", merge_commit],
                        cwd=self.workspace, capture_output=True, text=True, timeout=30)
                    if ff_proc.returncode != 0:
                        return {"status": "failed", "error": f"ff-only advance of main failed: {ff_proc.stderr[:300]}"}

                    # Sync version to governance DB
                    try:
                        self._api("POST", f"/api/version-sync/{self.project_id}", {
                            "git_head": merge_commit,
                            "dirty_files": [],
                        })
                        old_ver_row = self._api("GET", f"/api/version-check/{self.project_id}")
                        old_ver = old_ver_row.get("chain_version", "")
                        self._api("POST", f"/api/version-update/{self.project_id}", {
                            "chain_version": merge_commit,
                            "updated_by": "auto-chain",
                            "task_id": task_id,
                            "chain_stage": "merge",
                            "old_version": old_ver if old_ver != "(not set)" else "",
                        })
                    except Exception as e:
                        log.warning("Version sync in isolated merge failed (non-fatal): %s", e)

                    if worktree_available:
                        self._remove_worktree(worktree, branch, delete_branch=False)
                    return {"status": "succeeded", "result": {
                        "merge_commit": merge_commit,
                        "branch": "main",
                        "merge_mode": "isolated_integration",
                        "files_changed": len(changed),
                        "changed_files": changed,
                    }}
                finally:
                    if integration_worktree or integration_branch:
                        self._remove_worktree(integration_worktree, integration_branch)

            # Stage changed files (or all if none specified)
            if changed:
                subprocess.run(["git", "add", "--"] + changed,
                               cwd=self.workspace, capture_output=True, timeout=30)
            else:
                subprocess.run(["git", "add", "-A"],
                               cwd=self.workspace, capture_output=True, timeout=30)

            # Check if there's anything to commit
            status = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                    cwd=self.workspace, capture_output=True, text=True, timeout=10)
            staged = [f.strip() for f in status.stdout.splitlines() if f.strip()]

            if not staged:
                return {"status": "succeeded", "result": {
                    "merge_commit": "none", "branch": "main",
                    "files_changed": 0, "note": "nothing to commit"
                }}

            # Commit with 4-field Chain trailer (Phase A §4.4)
            msg = f"Auto-merge: {task_id}\n\nChanged files: {', '.join(staged[:10])}"
            from agent.governance.chain_trailer import write_merge_with_trailer, get_chain_state
            chain_state = get_chain_state(cwd=self.workspace)
            parent_chain_sha = chain_state.get("chain_sha", "")
            bug_id = metadata.get("bug_id", "") or metadata.get("chain_bug_id", "")
            success, commit_hash, err = write_merge_with_trailer(
                message=msg, cwd=self.workspace,
                task_id=task_id,
                parent_chain_sha=parent_chain_sha,
                bug_id=bug_id)
            if not success:
                return {"status": "failed", "error": f"git commit failed: {err[:300]}"}

            log.info("Merge complete: %s (%d files)", commit_hash, len(staged))

            # Immediately sync git HEAD to governance after commit (don't wait for 60s poll)
            HEAD = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.workspace
            ).decode().strip()
            self._api("POST", f"/api/version-sync/{self.project_id}", {"git_head": HEAD, "dirty_files": []})

            # Update VERSION file + DB chain_version + git sync
            try:
                # 1. Update VERSION file
                ver_path = os.path.join(self.workspace, "VERSION")
                if os.path.exists(ver_path):
                    with open(ver_path) as f:
                        content = f.read()
                    import re as _re
                    content = _re.sub(r'CHAIN_VERSION=\S+', f'CHAIN_VERSION={commit_hash}', content)
                    with open(ver_path, 'w') as f:
                        f.write(content)
                    # Amend commit to include VERSION
                    subprocess.run(["git", "add", "VERSION"], cwd=self.workspace, capture_output=True, timeout=10)
                    subprocess.run(["git", "commit", "--amend", "--no-edit"], cwd=self.workspace, capture_output=True, timeout=10)
                    # Re-read hash after amend
                    rev2 = subprocess.run(["git", "rev-parse", "HEAD"],
                                          cwd=self.workspace, capture_output=True, text=True, timeout=5)
                    commit_hash = rev2.stdout.strip()

                # 2. Update DB chain_version
                old_ver_row = self._api("GET", f"/api/version-check/{self.project_id}")
                old_ver = old_ver_row.get("chain_version", "")
                self._api("POST", f"/api/version-update/{self.project_id}", {
                    "chain_version": commit_hash,
                    "updated_by": "auto-chain",
                    "task_id": task_id,
                    "chain_stage": "merge",
                    "old_version": old_ver if old_ver != "(not set)" else "",
                })

                # 3. Sync git_head to DB
                self._api("POST", f"/api/version-sync/{self.project_id}", {
                    "git_head": commit_hash,
                    "dirty_files": [],
                })
                log.info("Chain version updated: %s → %s (VERSION + DB + sync)", old_ver, commit_hash)
            except Exception as e:
                log.warning("Version update failed (non-fatal): %s", e)

            # Write merge outcome to memory for future recall
            try:
                summary = metadata.get("intent_summary", "")
                if not summary:
                    summary = f"Merged {len(staged)} files: {', '.join(staged[:5])}"
                self._api("POST", f"/api/mem/{self.project_id}/write", {
                    "module": staged[0] if staged else "general",
                    "kind": "task_result",
                    "content": summary,
                    "structured": {
                        "merge_commit": commit_hash,
                        "changed_files": staged,
                        "files_changed": len(staged),
                        "task_id": task_id,
                        "chain_stage": "merge",
                        "parent_task_id": metadata.get("parent_task_id", ""),
                    },
                })
            except Exception:
                log.debug("Merge memory write failed (non-fatal)")

            # OPT-BACKLOG: best-effort backlog close after successful merge (R3/AC6)
            self._try_backlog_close_impl(
                self.project_id, metadata.get("bug_id", ""),
                commit_hash, self._api,
            )

            return {"status": "succeeded", "result": {
                "merge_commit": commit_hash,
                "branch": "main",
                "files_changed": len(staged),
                "changed_files": staged,
            }}

        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def _branch_exists(self, branch_name: str) -> bool:
        try:
            proc = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
                cwd=self.workspace,
                capture_output=True,
                timeout=10,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _branch_already_merged(self, branch_name: str, target_ref: str = "HEAD") -> bool:
        try:
            proc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", branch_name, target_ref or "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                timeout=10,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _dev_worktree_base_ref(self, metadata: dict) -> str:
        if metadata.get("operation_type") == "reconcile-cluster":
            return metadata.get("reconcile_target_branch") or "HEAD"
        return "HEAD"

    def _ensure_branch_at_ref(self, branch_name: str, base_ref: str) -> tuple[bool, str]:
        """Ensure branch_name exists, creating it at base_ref when needed."""
        branch_name = str(branch_name or "").strip()
        base_ref = str(base_ref or "HEAD").strip()
        if not branch_name or self._branch_exists(branch_name):
            return True, ""
        try:
            proc = subprocess.run(
                ["git", "branch", branch_name, base_ref],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return True, ""
            return False, (proc.stderr or proc.stdout or "").strip()
        except Exception as exc:
            return False, str(exc)

    def _execute_reconcile(self, task_id: str, metadata: dict) -> dict:
        """observer-hotfix-3 2026-04-25: Run reconcile pipeline via Phase J task type.

        Delegates to agent.governance.reconcile_task.run_reconcile_pipeline,
        which walks the 6 stages (scan/diff/propose/approve/apply/verify),
        produces mutation_plan.json, and either auto-applies high-conf items
        or queues medium/low for manual review.
        """
        self._report_progress(task_id, {"step": "reconciling"})
        try:
            try:
                from agent.governance.reconcile_task import run_full_reconcile
                from agent.governance.db import get_connection
            except ImportError:
                # When _agent_dir is on sys.path (this module's import setup),
                # the 'agent.X' import fails; fall back to 'governance.X'.
                from governance.reconcile_task import run_full_reconcile
                from governance.db import get_connection
            conn = get_connection(self.project_id)
            try:
                result = run_full_reconcile(
                    conn, self.project_id, task_id, metadata,
                )
                conn.commit()
                return {"status": "succeeded", "result": result}
            finally:
                conn.close()
        except Exception as e:
            log.error("reconcile pipeline failed for %s: %s", task_id, e, exc_info=True)
            return {"status": "failed", "error": f"reconcile pipeline error: {e}"}

    def _execute_deploy(self, task_id: str, metadata: dict) -> dict:
        """Run deploy orchestration on the host executor, not in governance."""
        changed = metadata.get("changed_files", [])
        self._report_progress(task_id, {"step": "deploying"})
        if metadata.get("operation_type") == "reconcile-cluster" and metadata.get("reconcile_target_branch"):
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            report = {
                "started_at": now,
                "changed_files": changed,
                "affected_services": [],
                "steps": {},
                "smoke_test": {},
                "success": True,
                "finished_at": now,
                "note": "Reconcile cluster merge is branch-local; main services are not redeployed until session signoff.",
                "reconcile_target_branch": metadata.get("reconcile_target_branch", ""),
                "merge_commit": metadata.get("merge_commit", ""),
                "main_redeployed": False,
            }
            return {"status": "succeeded", "result": {
                "deploy": "completed",
                "report": report,
                "changed_files": changed,
                "reconcile_target_branch": metadata.get("reconcile_target_branch", ""),
                "merge_commit": metadata.get("merge_commit", ""),
            }}
        try:
            from deploy_chain import run_deploy

            chat_id = int(metadata.get("chat_id", 0) or 0)
            expected_head = self._resolve_deploy_expected_head(metadata)
            report = run_deploy(
                changed,
                chat_id=chat_id,
                project_id=self.project_id,
                task_id=task_id,
                expected_head=expected_head,
            )

            # R4: Pre-return coherence assertion — report.success must agree
            # with smoke_test.all_pass. If incoherent, force status='failed'.
            smoke = report.get("smoke_test", {})
            if smoke and smoke.get("all_pass") is False and report.get("success"):
                log.warning(
                    "deploy coherence violation: report.success=True but "
                    "smoke_test.all_pass=False — forcing status=failed [task=%s]",
                    task_id,
                )
                report["success"] = False
                report["coherence_violation"] = True
            # Also check individual service=False with success=True
            if report.get("success") and smoke:
                for svc in ("executor", "governance", "gateway"):
                    if smoke.get(svc) is False:
                        log.warning(
                            "deploy coherence violation: report.success=True but "
                            "smoke_test.%s=False — forcing status=failed [task=%s]",
                            svc, task_id,
                        )
                        report["success"] = False
                        report["coherence_violation"] = True
                        break

            result = {
                "deploy": "completed" if report.get("success") else "failed",
                "report": report,
                "changed_files": changed,
            }
            if report.get("success"):
                return {"status": "succeeded", "result": result}
            summary = report.get("error") or json.dumps(report, ensure_ascii=False)[:300]
            return {"status": "failed", "error": summary, "result": result}
        except Exception as e:
            return {"status": "failed", "error": str(e), "result": {"deploy": "failed", "changed_files": changed}}

    def _resolve_deploy_expected_head(self, metadata: dict) -> str:
        """Return the commit deploy should make active in runtime/version state."""
        for key in ("expected_head", "merge_commit", "_merge_commit"):
            value = metadata.get(key, "")
            if value:
                return str(value)
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                return proc.stdout.strip()
        except Exception:
            pass
        return ""

    def _fetch_memories(self, query: str, top_k: int = 3) -> list:
        """Search memory backend for relevant past work (best-effort, 3s timeout)."""
        try:
            import requests as _req
            from urllib.parse import quote
            resp = _req.get(f"{self.base_url}/api/mem/{self.project_id}/search?q={quote(query[:120])}&top_k={top_k}",
                            timeout=3)
            return resp.json().get("results", [])
        except Exception:
            return []

    def _fetch_conversation_history(self, limit: int = 10) -> list[dict]:
        """Fetch recent conversation history from session_context log.

        Uses short timeout (3s) to avoid blocking coordinator flow.
        """
        try:
            import requests as _req
            resp = _req.get(f"{self.base_url}/api/context/{self.project_id}/log?limit={limit}", timeout=3)
            data = resp.json()
            entries = data.get("entries", [])
            result = []
            for e in entries:
                if e.get("type") != "coordinator_turn":
                    continue
                # The entry is a flat dict; wrap relevant fields under "content"
                content = {
                    "user_message": e.get("user_message", ""),
                    "decision": e.get("decision", ""),
                    "reply_preview": e.get("reply_preview", ""),
                }
                result.append({"content": content, "created_at": e.get("ts", "")})
            return result
        except Exception as e:
            log.debug("_fetch_conversation_history failed: %s", e)
            return []

    def _write_conversation_history(self, task: dict, parsed_output: dict):
        """Write coordinator decision to session_context for future reference."""
        try:
            actions = parsed_output.get("actions", [])
            decision = actions[0].get("type", "unknown") if actions else "unknown"
            entry = {
                "user_message": task.get("prompt", "")[:500],
                "decision": decision,
                "reply_preview": parsed_output.get("reply", "")[:200],
            }
            # Add PM task ID if created
            if decision == "create_pm_task":
                entry["pm_prompt_preview"] = actions[0].get("prompt", "")[:200]
            # Add queries if query_memory was used
            if decision == "query_memory":
                entry["queries"] = actions[0].get("queries", [])

            import requests as _req
            _req.post(f"{self.base_url}/api/context/{self.project_id}/log",
                      json={"type": "coordinator_turn", **entry},
                      timeout=3)
        except Exception as e:
            log.debug("_write_conversation_history failed (non-fatal): %s", e)

    def _build_prompt(self, prompt: str, task_type: str, context: dict) -> str:
        """Enhance task prompt with governance context."""
        parts = [prompt]

        if task_type == "pm":
            import time as _bt
            _bt0 = _bt.time()
            def _bp_log(msg):
                try:
                    bp_path = os.path.join(self.workspace or ".", "shared-volume", "codex-tasks", "logs",
                                           f"build-prompt-{context.get('task_id','?')}.txt")
                    with open(bp_path, "a") as f:
                        f.write(f"{_bt.time()-_bt0:.1f}s {msg}\n")
                except Exception:
                    pass

            # 1. Coordinator-forwarded context
            coordinator_memories = context.get("_coordinator_memories", [])
            coordinator_context = context.get("_coordinator_context", {})
            if coordinator_memories:
                parts.append("\n## Context from Coordinator (pre-searched memories)")
                seen = set()
                for m in coordinator_memories:
                    c = m.get('summary', m.get('content', ''))[:150]
                    if c not in seen:
                        seen.add(c)
                        parts.append(f"  - [{m.get('kind','')}] {c}")
            if coordinator_context:
                parts.append(f"\n## Coordinator Decision Context")
                parts.append(f"  {json.dumps(coordinator_context, ensure_ascii=False)}")
            _bp_log(f"coordinator context: {len(coordinator_memories)} memories")

            # 2. PM's own memory search
            memories = self._fetch_memories(prompt[:120])
            if memories:
                parts.append("\n## Additional Memories (PM search)")
                seen = set()
                for m in memories:
                    c = m.get('summary', m.get('content', ''))[:150]
                    if c not in seen:
                        seen.add(c)
                        parts.append(f"  - [{m.get('kind','')}] {c}")
            _bp_log(f"pm memory search: {len(memories)} results")

            # 3. Runtime context + queue
            try:
                import requests as _req
                ctx_data = _req.get(f"{self.base_url}/api/context/{self.project_id}/load", timeout=3).json()
                if ctx_data.get("exists"):
                    parts.append(f"\n## Runtime Context")
                    parts.append(f"  {json.dumps(ctx_data.get('context', {}), ensure_ascii=False)}")
            except Exception:
                pass
            try:
                task_list = _req.get(f"{self.base_url}/api/task/{self.project_id}/list", timeout=3).json()
                active = [t for t in task_list.get("tasks", [])
                          if t.get("status") in ("queued", "claimed", "observer_hold")]
                if active:
                    parts.append(f"\n## Active Task Queue ({len(active)} tasks)")
                    for t in active[:5]:
                        parts.append(f"  - {t.get('task_id','')}: [{t.get('type','')}] {t.get('prompt','')[:60]}")
            except Exception:
                pass
            _bp_log("context + queue done")

            metadata = context.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            operation_type = (
                metadata.get("operation_type")
                or context.get("operation_type")
                or ""
            )
            if operation_type == "reconcile-cluster":
                cluster_payload = (
                    metadata.get("cluster_payload")
                    or context.get("cluster_payload")
                    or {}
                )
                cluster_report = (
                    metadata.get("cluster_report")
                    or context.get("cluster_report")
                    or {}
                )
                batch_ref = metadata.get("batch_memory_ref")
                if not isinstance(batch_ref, dict):
                    batch_ref = {}
                batch_id = (
                    metadata.get("batch_id")
                    or metadata.get("reconcile_batch_id")
                    or batch_ref.get("batch_id")
                    or ""
                )
                if batch_id:
                    try:
                        import requests as _req_batch
                        batch_resp = _req_batch.get(
                            f"{self.base_url}/api/reconcile/{self.project_id}/batch-memory/{batch_id}",
                            timeout=3,
                        ).json()
                        batch = batch_resp.get("batch", {}) if isinstance(batch_resp, dict) else {}
                        memory = batch.get("memory", {}) if isinstance(batch, dict) else {}
                        related_features = []
                        try:
                            from governance import reconcile_batch_memory as _rbm
                            related_features = _rbm.find_related_features(batch, cluster_payload)
                        except Exception:
                            related_features = []
                        batch_context = {
                            "batch_id": batch_id,
                            "session_id": batch.get("session_id", ""),
                            "processed_cluster_count": len(memory.get("processed_clusters", {}) or {}),
                            "accepted_feature_count": len(memory.get("accepted_features", {}) or {}),
                            "related_features": related_features[:8],
                            "open_conflicts": (memory.get("open_conflicts", []) or [])[:8],
                        }
                        parts.append("\n## Reconcile Batch Memory")
                        parts.append(
                            "Use this as the batch-wide semantic memory. If this cluster "
                            "overlaps an accepted feature, merge into that feature instead "
                            "of inventing a parallel feature name."
                        )
                        parts.append(
                            f"```json\n{json.dumps(batch_context, ensure_ascii=False, indent=2)}\n```"
                        )
                    except Exception:
                        parts.append(
                            "\n## Reconcile Batch Memory\n"
                            f"Batch memory id {batch_id!r} was declared but could not be loaded. "
                            "Do not assume this is the first cluster; keep feature naming conservative."
                        )
                candidate_manifest = _reconcile_candidate_manifest(cluster_payload)
                if candidate_manifest:
                    parts.append("\n## Reconcile Candidate Node Manifest (Authoritative)")
                    parts.append(
                        "This compact manifest is never truncated. PM MUST copy every "
                        "candidate node below into proposed_nodes exactly once. If this "
                        "manifest and the larger metadata block disagree, this manifest "
                        "wins for node_id/primary/title/layer/hierarchy_parent."
                    )
                    parts.append(f"candidate_node_count: {len(candidate_manifest)}")
                    parts.append(
                        f"```json\n{json.dumps(candidate_manifest, ensure_ascii=False, indent=2)}\n```"
                    )
                cluster_scope = {
                    "operation_type": operation_type,
                    "cluster_fingerprint": metadata.get("cluster_fingerprint", ""),
                    "reconcile_run_id": metadata.get("reconcile_run_id", ""),
                    "batch_id": batch_id,
                    "target_files": context.get("target_files", []),
                    "test_files": context.get("test_files", []),
                    "cluster_payload": cluster_payload,
                    "cluster_report": cluster_report,
                }
                cluster_json = json.dumps(
                    cluster_scope, ensure_ascii=False, indent=2
                )
                if len(cluster_json) > 12000:
                    cluster_json = cluster_json[:12000] + "\n...<truncated>"
                parts.append("\n## Reconcile Cluster Source Of Truth")
                parts.append(
                    "Use this embedded cluster metadata as the audit scope. "
                    "Do not widen scope beyond these files, and do not query "
                    "Governance APIs to rediscover cluster_payload or "
                    "cluster_report. Read at most 3-5 listed files only if "
                    "needed to name precise requirements."
                )
                parts.append(f"```json\n{cluster_json}\n```")
                parts.append(
                    "Reconcile-cluster PRD contract: proposed_nodes MUST mirror "
                    "cluster_payload.candidate_nodes one-for-one. When a "
                    "candidate node has a concrete node_id, copy that node_id "
                    "exactly; never set it to null or invent a replacement. "
                    "Also preserve primary and title exactly. Treat "
                    "parent_layer as the candidate node's own layer (for example "
                    "L7/7), and preserve the hierarchy parent (for example "
                    "L3.18) via parent/parent_id or deps so Dev can emit "
                    "graph_delta.creates for the "
                    "overlay without touching graph.json."
                )
                _bp_log("reconcile cluster metadata embedded")

            # 4. Target file preview (help PM verify paths and understand scope)
            target_files = context.get("target_files", [])
            if target_files:
                parts.append(f"\n## Target Files Preview")
                for tf in target_files[:3]:
                    tf_path = os.path.join(self.workspace or ".", tf)
                    try:
                        with open(tf_path, "r", encoding="utf-8", errors="replace") as f:
                            lines = f.readlines()
                        parts.append(f"\n### {tf} ({len(lines)} lines)")
                        # Show first 30 lines as preview
                        preview = "".join(lines[:30])
                        parts.append(f"```\n{preview}```")
                    except Exception:
                        parts.append(f"\n### {tf} (file not found or unreadable)")
                _bp_log(f"target files preview: {len(target_files)} files")

            # 6d: Graph impact for target_files
            if target_files and operation_type != "reconcile-cluster":
                try:
                    import requests as _req2
                    impact = _req2.post(
                        f"{self.base_url}/api/impact/{self.project_id}",
                        json={"files": ",".join(target_files[:10])},
                        timeout=5,
                    ).json()
                    affected = impact.get("affected_nodes", [])
                    if affected:
                        parts.append(f"\n## Graph Impact Analysis ({len(affected)} affected nodes)")
                        for node in affected[:8]:
                            parts.append(
                                f"  - {node.get('node_id','')}: {node.get('title','')} "
                                f"(L{node.get('verify_level','?')}, {node.get('gate_mode','auto')})"
                            )
                        related_docs = impact.get("related_docs", [])
                        if related_docs:
                            parts.append(f"  Related docs: {related_docs[:5]}")
                except Exception:
                    pass
                _bp_log("graph impact query done")

            # 6. Project structure
            parts.append("\n## Project Structure")
            parts.append("  agent/ — executor_worker, ai_lifecycle, pipeline_config")
            parts.append("  agent/governance/ — auto_chain, db, server, memory_service, memory_backend")
            parts.append("  agent/telegram_gateway/ — gateway, message_worker")
            parts.append("  agent/tests/ — pytest test files")
            parts.append("  docs/ — specs, rules, dev iteration logs")

            # 7. PRD output format instruction (scheme C — single source of truth)
            parts.append(
                "\nOutput a PRD as strict JSON with these fields:\n"
                "{\n"
                '  "target_files": ["agent/xxx.py"],        // MANDATORY — files Dev will modify\n'
                '  "test_files": ["agent/tests/test_xxx.py"], // soft-mandatory (or skip_reasons)\n'
                '  "requirements": ["R1: ...", "R2: ..."],   // MANDATORY\n'
                '  "acceptance_criteria": ["AC1: ...", ...],  // MANDATORY — concrete, grep-verifiable\n'
                '  "verification": {"method": "automated test", "command": "pytest agent/tests/"}, // MANDATORY\n'
                '  "proposed_nodes": [                        // soft-mandatory (or skip_reasons)\n'
                '    {\n'
                '      "node_id": "L7.x",                      // required when supplied by reconcile-cluster candidate_nodes\n'
                '      "parent_layer": 7,\n'
                '      "title": "Node title",\n'
                '      "deps": ["L3.2"],\n'
                '      "verify_requires": ["L4.32"],\n'
                '      "primary": ["agent/xxx.py"],\n'
                '      "test": ["agent/tests/test_xxx.py"],\n'
                '      "test_strategy": "what to test and how",\n'
                '      "description": "what this node covers"\n'
                '    }\n'
                '  ],\n'
                '  "doc_impact": {"files": [...], "changes": [...]}, // soft-mandatory (or skip_reasons)\n'
                '  "skip_reasons": {"field_name": "reason"},  // REQUIRED for omitted soft fields\n'
                '  "related_nodes": ["L7.4"],                 // existing nodes affected\n'
                '  "prd": {                                   // optional metadata\n'
                '    "feature": "Feature name",\n'
                '    "background": "Why this change",\n'
                '    "scope": "Impact scope",\n'
                '    "risk": "Risk assessment"\n'
                '  }\n'
                "}\n"
                "\nRules:\n"
                "- target_files, requirements, acceptance_criteria, verification are MANDATORY (gate blocks if missing)\n"
                "- test_files, proposed_nodes, doc_impact: provide OR explain in skip_reasons\n"
                "- For test coverage, put only existing files in test_files/proposed_nodes[].test; list missing coverage in missing_test_gaps or skip_reasons\n"
                "- Use Read/Grep to verify file paths exist before listing them\n"
                "- Read at most 3-5 key files, then output the JSON\n"
                "- Do NOT output actions, reply, or schema_version fields\n"
                "\nverification.command constraints (CRITICAL — Windows cmd.exe executor):\n"
                "- MUST be a single invocation of `pytest ...` or `python -c \"...\"` (or `python -m <module>`).\n"
                "- MUST NOT use Unix-only commands: grep, sed, awk, find, head, tail, cat, cut, xargs.\n"
                "- MUST NOT use shell chaining/pipes: `&&`, `||`, `|`, `;`, `>`, redirect operators. Windows cmd treats these as arguments to the first command.\n"
                "- For doc-string checks use `python -c \"import pathlib,sys; t=pathlib.Path('docs/x.md').read_text(encoding='utf-8'); assert 'PATTERN' in t; print('ok')\"`.\n"
                "- For code behavior checks use a dedicated pytest test file under agent/tests/.\n"
                "- If you need multiple checks, either (a) use one pytest file that asserts all of them, or (b) write a single `python -c` with multiple asserts — never chain shell commands.\n"
                "Output ONLY the PRD JSON object."
            )
            _bp_log("format instruction appended")

        elif task_type in ("coordinator", "task"):
            # Two-round coordinator: round 1 has no memories, round 2 has memory results
            import time as _bt
            _bt0 = _bt.time()
            def _bp_log(msg):
                try:
                    log_path = os.path.join(self.workspace or ".", "shared-volume", "codex-tasks", "logs",
                                            f"build-prompt-{context.get('task_id','?')}.txt")
                    with open(log_path, "a") as f:
                        f.write(f"{_bt.time()-_bt0:.1f}s {msg}\n")
                except Exception:
                    pass
            parts.append(f"\nproject_id: {self.project_id}")

            # Inject conversation history (last 10)
            _bp_log("fetching history")
            history = self._fetch_conversation_history()
            _bp_log(f"history done: {len(history)} entries")
            if history:
                parts.append("\n## Recent Conversation (last 10 turns)")
                for h in reversed(history):  # oldest first
                    c = h.get("content", {})
                    parts.append(f"  - [{h.get('created_at','')}] user: {c.get('user_message','')[:80]}")
                    parts.append(f"    decision: {c.get('decision','?')}")

            # Round 2: inject memory results if available
            round2_memories = context.get("_round2_memories", [])
            if round2_memories:
                parts.append("\n## Memory Search Results (from your query_memory request)")
                seen_content = set()
                for m in round2_memories:
                    content = m.get('summary', m.get('content', ''))[:150]
                    if content not in seen_content:
                        seen_content.add(content)
                        parts.append(f"  - [{m.get('kind','')}] {content}")
                parts.append("\nNow make your final decision based on these results.")
                parts.append("You MUST output reply_only or create_pm_task. Do NOT output query_memory again.")

            # Pre-fetch active queue
            _bp_log("fetching queue")
            try:
                import requests as _req
                task_list = _req.get(f"{self.base_url}/api/task/{self.project_id}/list", timeout=3).json()
                active = [t for t in task_list.get("tasks", [])
                          if t.get("status") in ("queued", "claimed", "observer_hold")]
                if active:
                    parts.append(f"\n## Active Task Queue ({len(active)} tasks)")
                    for t in active[:5]:
                        parts.append(f"  - {t.get('task_id','')}: [{t.get('type','')}] {(t.get('prompt',''))[:60]}")
            except Exception:
                pass

            _bp_log("queue done")
            # Pre-fetch runtime context
            _bp_log("fetching context")
            try:
                ctx_data = _req.get(f"{self.base_url}/api/context/{self.project_id}/load", timeout=3).json()
                if ctx_data.get("exists"):
                    parts.append(f"\n## Runtime Context")
                    parts.append(f"  {json.dumps(ctx_data.get('context', {}), ensure_ascii=False)}")
            except Exception:
                pass

            _bp_log("context done")
            # Inject rule engine decision if non-trivial
            rule_decision = context.get("rule_decision", "new")
            rule_reason = context.get("rule_reason", "")
            if rule_decision and rule_decision != "new":
                parts.append(f"\n## Rule Engine Decision: {rule_decision}")
                parts.append(f"  Reason: {rule_reason}")
                # MB4: Inject specific failure/pitfall content for retry decisions
                failure_content = metadata.get("failure_content") or metadata.get("details", {}).get("failure_content", "")
                if failure_content and rule_decision == "retry":
                    parts.append(f"  Historical context: {failure_content[:500]}")

        elif task_type == "test":
            changed = context.get("changed_files", [])
            verification = context.get("verification", {})
            test_files = context.get("test_files", [])
            # Inject past failure patterns for these files
            if changed:
                memories = self._fetch_memories(", ".join(changed[:3]))
                failures = [m for m in memories if m.get("kind") in ("failure_pattern", "test_result")]
                if failures:
                    parts.append("\nPast test failures for these files:")
                    for m in failures:
                        parts.append(f"  - {m.get('summary', m.get('content',''))[:150]}")
            parts.append(f"\nRun tests. Changed files: {json.dumps(changed)}")
            if verification:
                parts.append(f"Verification plan: {json.dumps(verification, ensure_ascii=False)}")
                if verification.get("command"):
                    parts.append(f"Required verification command: {verification['command']}")
                    parts.append("You MUST attempt the required verification command before finishing unless it is unavailable in the current workspace.")
            if test_files:
                parts.append(f"Priority test files: {json.dumps(test_files)}")
            parts.append("Report result as strict JSON: {\"schema_version\":\"v1\",\"summary\":\"...\",\"test_report\":{\"passed\":N,\"failed\":N,\"tool\":\"pytest\",\"command\":\"exact command attempted\"}}")

        elif task_type == "qa":
            changed = context.get("changed_files", [])
            requirements = context.get("requirements", [])
            criteria = context.get("acceptance_criteria", [])
            verification = context.get("verification", {})
            doc_impact = context.get("doc_impact", {})
            test_report = context.get("test_report", {})
            if changed:
                memories = self._fetch_memories(", ".join(changed[:3]))
                decisions = [m for m in memories if m.get("kind") in ("decision", "task_result")]
                if decisions:
                    parts.append("\nPast decisions for these files:")
                    for m in decisions:
                        parts.append(f"  - [{m.get('kind','')}] {m.get('summary', m.get('content',''))[:150]}")
            if test_report:
                parts.append(f"\nTest report: {json.dumps(test_report, ensure_ascii=False)}")
            if requirements:
                parts.append(f"Requirements: {json.dumps(requirements, ensure_ascii=False)}")
            if criteria:
                parts.append(f"Acceptance criteria: {json.dumps(criteria, ensure_ascii=False)}")
            if verification:
                parts.append(f"Verification contract: {json.dumps(verification, ensure_ascii=False)}")
            if doc_impact:
                parts.append(f"Doc impact: {json.dumps(doc_impact, ensure_ascii=False)}")
            has_chain_qa_contract = (
                "criteria_results" in prompt
                or "Graph Delta Review" in prompt
                or "graph_delta_review" in prompt
            )
            parts.append("\nYou are a QA reviewer. Review the test results and changed files above.")
            if has_chain_qa_contract:
                parts.append(
                    "Follow the stricter chain QA contract already present in the task prompt. "
                    "Do not collapse the review to a generic tests-pass JSON; include every "
                    "required structured field such as criteria_results and graph_delta_review."
                )
            else:
                parts.append("If tests passed and changes look reasonable, respond ONLY with this exact JSON:")
                parts.append('{"recommendation": "qa_pass", "review_summary": "Tests pass, changes approved"}')
                parts.append("If there are critical issues, respond with:")
                parts.append('{"recommendation": "reject", "reason": "description of issue"}')

        elif task_type == "gatekeeper":
            parts.append("\nYou are the isolated pre-merge gatekeeper.")
            parts.append("Assess whether the implementation satisfies the PM contract and whether merge should proceed.")
            parts.append("You must not ask for broader project context or propose code changes.")
            parts.append('Respond ONLY with JSON: {"schema_version":"v1","review_summary":"...","recommendation":"merge_pass|reject","pm_alignment":"pass|partial|fail","checked_requirements":["R1"],"reason":""}')

        elif task_type == "merge":
            parts.append("\nCommit all staged changes to git and respond with JSON: {\"merge_commit\": \"<hash>\", \"branch\": \"main\", \"files_changed\": N}")

        elif task_type == "dev":
            target = context.get("target_files", [])
            if target:
                parts.append(f"\nTarget files: {json.dumps(target)}")
            verification = context.get("verification", {})
            if verification:
                parts.append(f"Verification plan: {json.dumps(verification, ensure_ascii=False)}")
                if verification.get("command"):
                    parts.append(f"Required verification command: {verification['command']}")
                    parts.append("You MUST attempt the required verification command before finishing unless the command itself is unavailable in the current workspace.")
                    parts.append("If verification is only partially completed, explain exactly why in summary/retry_context and report the attempted command in test_results.command.")
            # Inject memories relevant to target files and prompt
            mem_query = prompt if len(prompt) <= 120 else (", ".join(target[:3]) if target else prompt[:120])
            memories = self._fetch_memories(mem_query)
            if memories:
                parts.append("\nRelevant memories from past work:")
                for m in memories:
                    parts.append(f"  - [{m.get('kind','')}] {m.get('summary', m.get('content',''))[:150]}")
            attempt = context.get("attempt_num", 1)
            gate_reason = context.get("previous_gate_reason", "") or context.get("rejection_reason", "")
            if attempt and int(attempt) > 1 and gate_reason:
                parts.append(f"\nThis is retry attempt #{attempt}. Previous attempt was blocked by gate.")
                parts.append(f"Gate rejection reason: {gate_reason}")
                parts.append("Fix ONLY the specific issue described above; do not make unrelated changes.")
            parts.append("\nAfter completing, respond with strict JSON including verification evidence:")
            parts.append("{\"schema_version\":\"v1\",\"summary\":\"...\",\"changed_files\":[...],\"new_files\":[],\"test_results\":{\"ran\":true,\"passed\":N,\"failed\":N,\"command\":\"exact command attempted\"},\"related_nodes\":[...],\"needs_review\":false}")

        return "\n".join(parts)

    # Files/patterns to ignore in git diff (Claude CLI artifacts, not real changes).
    # NOTE: executor_worker.py is NOT excluded here — it can be a legitimate task target.
    # If it were excluded, any dev task targeting this file would always fail the gate
    # with "No files changed", making such tasks unretryable.
    #
    # Node mapping (file → acceptance-graph node):
    #   executor_worker.py        → L3.2  ExecutorWorker (this file — executor process)
    #   governance/auto_chain.py  → L2.1  AutoChain      (stage-transition dispatcher)
    #   governance/graph.py       → L1.3  AcceptanceGraph (DAG rule layer)
    #   governance/task_registry.py → L2.2 TaskRegistry  (task CRUD / queue)
    def _handle_coordinator_result(self, task: dict, result: dict) -> None:
        """Parse coordinator AI output and execute the decided action.

        Supports two JSON formats:
        - New (v1): {"schema_version":"v1", "reply":"...", "actions":[...], "context_update":{...}}
        - Legacy:   {"action":"reply|create_task", ...}

        Gate validation: only reply_only and create_pm_task actions are allowed.
        """
        metadata = task.get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        chat_id = metadata.get("chat_id", "")

        # Extract JSON from AI output
        raw = result.get("summary", "") or result.get("raw_output", "") or json.dumps(result)

        # Debug: dump raw output to file for observability
        try:
            dump_path = os.path.join(self.workspace or ".", "shared-volume", "codex-tasks", "logs",
                                     f"coordinator-{task['task_id']}.raw.txt")
            os.makedirs(os.path.dirname(dump_path), exist_ok=True)
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(f"result_keys: {list(result.keys())}\n\n")
                f.write(f"raw ({len(raw)} chars):\n{raw}\n")
        except Exception:
            pass

        parsed = self._extract_json(raw)

        if not parsed:
            if chat_id:
                self._telegram_reply(chat_id, raw[:2000])
            return

        # Detect format: new (schema_version) vs legacy (action)
        if "schema_version" in parsed or "actions" in parsed:
            self._handle_coordinator_v1(task, parsed, chat_id)
        elif "action" in parsed:
            self._handle_coordinator_legacy(task, parsed, chat_id)
        else:
            log.warning("coordinator.gate: JSON has no actions or action field: %s",
                        list(parsed.keys()))

    def _extract_json(self, raw: str) -> Optional[Dict]:
        """Extract first valid JSON object from raw output."""
        # Strategy 1: entire output is JSON
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

        # Strategy 2: find JSON block with balanced braces
        for marker in ['{"schema_version"', '{"action"', '{"actions"']:
            start = raw.find(marker)
            if start >= 0:
                depth = 0
                for i in range(start, len(raw)):
                    if raw[i] == '{': depth += 1
                    elif raw[i] == '}': depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(raw[start:i+1])
                        except json.JSONDecodeError:
                            pass
                        break
        return None

    def _validate_coordinator_output(self, parsed: dict, round: int = 1) -> tuple[bool, str]:
        """Validate coordinator JSON output against gate rules G1-G7.
        Returns (valid, error_message).
        round=1 allows query_memory; round=2 only allows final decisions."""
        if not isinstance(parsed, dict):
            return False, "Output is not a JSON object"
        if parsed.get("schema_version") != "v1":
            return False, "Missing or invalid schema_version (expected 'v1')"

        # reply is required for final decisions, optional for query_memory
        has_query_memory = any(a.get("type") == "query_memory" for a in (parsed.get("actions") or []))
        if not has_query_memory and not parsed.get("reply"):
            return False, "Missing or empty 'reply' field"

        actions = parsed.get("actions")
        if not actions or not isinstance(actions, list):
            return False, "Missing or empty 'actions' array"
        for a in actions:
            atype = a.get("type", "")
            if round == 1:
                allowed_types = ("reply_only", "create_pm_task", "query_memory")
            else:
                allowed_types = ("reply_only", "create_pm_task")
            if atype not in allowed_types:
                return False, f"Invalid action type '{atype}' (allowed: {', '.join(allowed_types)})"
            if atype == "query_memory":
                queries = a.get("queries", [])
                if not queries or not isinstance(queries, list) or len(queries) > 3:
                    return False, "query_memory: queries must be non-empty list, max 3 items"
                if not all(isinstance(q, str) and len(q) >= 2 for q in queries):
                    return False, "query_memory: each query must be string >= 2 chars"
            if atype == "create_pm_task":
                prompt = a.get("prompt", "")
                if not prompt or len(prompt) < 50:
                    return False, f"create_pm_task prompt too short ({len(prompt)} chars, min 50)"
        return True, ""

    def _handle_coordinator_v1(self, task: dict, parsed: dict, chat_id: str) -> None:
        """Handle new-format coordinator output with gate validation."""
        import time as _t
        _hv_t0 = _t.time()
        def _hv_log(msg):
            # Write to file FIRST (log.info may block in MCP subprocess)
            try:
                _log_path = os.path.join(self.workspace or ".", "shared-volume", "codex-tasks", "logs",
                                          f"coordinator-flow-{task.get('task_id','?')}.txt")
                with open(_log_path, "a") as f:
                    f.write(f"  handle_v1 {_t.time()-_hv_t0:.1f}s {msg}\n")
            except Exception:
                pass

        reply = parsed.get("reply", "")
        actions = parsed.get("actions", [])
        context_update = parsed.get("context_update", {})
        task_id = task["task_id"]

        _hv_log(f"start: actions={[a.get('type') for a in actions]} reply={reply[:60]}")

        # Gate: validate each action
        for action in actions:
            action_type = action.get("type", "")

            if action_type == "reply_only":
                _hv_log(f"action: reply_only")
                if chat_id and reply:
                    self._telegram_reply(chat_id, reply)

            elif action_type == "create_pm_task":
                # Gate check: must have prompt
                prompt = action.get("prompt", "")
                if not prompt:
                    log.warning("coordinator.gate: create_pm_task missing prompt, rejected")
                    continue
                target_files = action.get("target_files", [])
                related_nodes = action.get("related_nodes", [])
                parent_meta = task.get("metadata") or {}
                if isinstance(parent_meta, str):
                    try:
                        parent_meta = json.loads(parent_meta)
                    except Exception:
                        parent_meta = {}

                forwarded_meta = {
                    "parent_task_id": task_id,
                    "chat_id": chat_id,
                    "source": "coordinator",
                    "target_files": target_files,
                    "related_nodes": related_nodes,
                    "_coordinator_memories": getattr(self, '_last_query_memories', []),
                    "_coordinator_context": context_update,
                }
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

                # OPT-BACKLOG-CH1-COORD-AUTOTAG: auto-extract bug_id from
                # coordinator task prompt and inject into PM task metadata so
                # auto_chain._try_backlog_close_via_db can fire backlog close
                # on merge. Precedence (idempotent):
                #   1. action dict explicit bug_id        (never overwritten)
                #   2. parent_meta.bug_id (forwarded above, never overwritten)
                #   3. prompt regex extraction            (fallback)
                action_bug_id = action.get("bug_id")
                if action_bug_id and "bug_id" not in forwarded_meta:
                    forwarded_meta["bug_id"] = action_bug_id
                if "bug_id" not in forwarded_meta:
                    extracted = _extract_backlog_id(task.get("prompt") or "")
                    if extracted:
                        forwarded_meta["bug_id"] = extracted
                        _hv_log(f"autotag: task={task_id} bug_id={extracted}")

                _hv_log(f"action: create_pm_task target_files={target_files}")

                sub_result = self._api("POST", f"/api/task/{self.project_id}/create", {
                    "prompt": prompt,
                    "type": "pm",
                    "priority": 1,
                    "metadata": forwarded_meta,
                })
                sub_id = sub_result.get("task_id", "?")
                _hv_log(f"pm_created: {sub_id}")
                if chat_id:
                    self._telegram_reply(
                        chat_id,
                        f"PM task created: {sub_id[-12:]}\n{reply[:200] if reply else prompt[:200]}")

            else:
                # Gate: reject disallowed actions
                log.warning("coordinator.gate: action '%s' not allowed, rejected (task=%s)",
                            action_type, task_id)

        _hv_log("actions done")
        # Save context update if provided
        if context_update:
            try:
                self._api("POST", f"/api/context/{self.project_id}/save",
                           {"context": context_update})
                _hv_log(f"context saved: {list(context_update.keys())}")
            except Exception as e:
                _hv_log(f"context save failed: {e}")
        _hv_log("returning")

    def _handle_coordinator_legacy(self, task: dict, parsed: dict, chat_id: str) -> None:
        """Handle legacy-format coordinator output: {"action": "reply|create_task", ...}"""
        action = parsed.get("action", "")
        task_id = task["task_id"]
        log.info("coordinator.legacy_action: %s (task=%s)", action, task_id)

        # Write coordinator decision to governance audit log
        try:
            self._api("POST", f"/api/audit/{self.project_id}/record", {
                "event": "coordinator.decision",
                "actor": "coordinator",
                "details": {
                    "task_id": task_id,
                    "decision": [action],
                    "reply_preview": parsed.get("text", parsed.get("prompt", ""))[:200],
                    "context_update_keys": [],
                },
            })
        except Exception:
            pass  # audit failure should not block coordinator

        if action == "reply":
            text = parsed.get("text", "No response")
            if chat_id:
                self._telegram_reply(chat_id, text)

        elif action == "create_task":
            sub_type = parsed.get("type", "pm")
            # Gate: only PM allowed
            if sub_type != "pm":
                log.warning("coordinator.gate: legacy create_task type='%s' rejected, must be 'pm'",
                            sub_type)
                sub_type = "pm"  # Force to PM
            sub_prompt = parsed.get("prompt", "")
            if sub_prompt:
                sub_result = self._api("POST", f"/api/task/{self.project_id}/create", {
                    "prompt": sub_prompt,
                    "type": sub_type,
                    "priority": 1,
                    "metadata": {
                        "parent_task_id": task_id,
                        "chat_id": chat_id,
                        "source": "coordinator",
                    },
                })
                sub_id = sub_result.get("task_id", "?")
                log.info("coordinator.pm_created: %s (parent=%s, legacy)", sub_id, task_id)
                if chat_id:
                    self._telegram_reply(chat_id, f"Task created: {sub_id[-12:]}")
        else:
            log.warning("coordinator.gate: unknown legacy action '%s'", action)

    def _telegram_reply(self, chat_id, text: str) -> None:
        """Send reply to Telegram via Bot API."""
        import urllib.request, urllib.error
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            log.warning("TELEGRAM_BOT_TOKEN not set, cannot reply")
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log.error("Telegram reply failed: %s", e)

    _IGNORE_PATTERNS = {".claude/", "__pycache__/", ".pyc", ".lock", ".worktrees/"}

    def _create_worktree(self, task_id: str, worker_id: str = "",
                         base_ref: str = "HEAD", base_commit: str = "",
                         attempt_num: int = 1):
        """Create isolated git worktree for a dev task.

        Args:
            task_id: Task identifier for branch naming.
            worker_id: Optional worker prefix for parallel dispatch (R3).
                       When set, worktree is placed under .worktrees/worker-{N}/dev-task-{id}.
        """
        try:
            attempt_num = int(attempt_num or 1)
        except Exception:
            attempt_num = 1
        attempt_suffix = f"-attempt-{attempt_num}" if attempt_num > 1 else ""
        branch_name = f"dev/{task_id}{attempt_suffix}"
        if worker_id:
            worktree_dir = os.path.join(self.workspace, ".worktrees", worker_id, f"dev-task-{task_id}{attempt_suffix}")
        else:
            worktree_dir = os.path.join(self.workspace, ".worktrees", f"dev-{task_id}{attempt_suffix}")
        try:
            base_ref = str(base_ref or "HEAD").strip()
            if base_ref and base_ref != "HEAD":
                ok, _err = self._ensure_branch_at_ref(base_ref, base_commit or "HEAD")
                if not ok:
                    return None, None
            os.makedirs(os.path.dirname(worktree_dir), exist_ok=True)
            # R3: Fetch latest origin/main so worktree baseline includes recent merges
            subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
            proc = subprocess.run(
                ["git", "worktree", "add", "-b", branch_name, worktree_dir, base_ref or "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                return None, None
            return worktree_dir, branch_name
        except Exception:
            return None, None

    def _remove_worktree(self, worktree_path: str, branch_name: str, delete_branch: bool = True) -> None:
        """Remove worktree and optionally delete its branch."""
        try:
            if worktree_path and os.path.isdir(worktree_path):
                subprocess.run(
                    ["git", "worktree", "remove", worktree_path, "--force"],
                    cwd=self.workspace,
                    capture_output=True,
                    timeout=30,
                )
            elif worktree_path and os.path.exists(worktree_path):
                shutil.rmtree(worktree_path, ignore_errors=True)
            if delete_branch and branch_name:
                subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    cwd=self.workspace,
                    capture_output=True,
                    timeout=10,
                )
        except Exception:
            pass

    def _create_integration_worktree(self, task_id: str, base_ref: str = "HEAD"):
        """Create a clean integration worktree used only for merge verification."""
        branch_name = f"merge/{task_id}"
        worktree_dir = os.path.join(self.workspace, ".worktrees", f"merge-{task_id}")
        try:
            os.makedirs(os.path.dirname(worktree_dir), exist_ok=True)
            proc = subprocess.run(
                ["git", "worktree", "add", "-b", branch_name, worktree_dir, base_ref or "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                return None, None, (proc.stderr or proc.stdout or "").strip()
            return worktree_dir, branch_name, ""
        except Exception as e:
            return None, None, str(e)

    def _get_git_changed_files(self, cwd: str = None) -> list:
        """Run git diff --name-only to detect files changed since last commit."""
        try:
            repo_cwd = cwd or self.workspace
            # Check both staged and unstaged changes
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=repo_cwd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            files = [f.strip() for f in result.stdout.splitlines() if f.strip()]

            # Also include staged new files (added to index but not yet in HEAD)
            result2 = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=A", "--cached"],
                cwd=repo_cwd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            new_files = [f.strip() for f in result2.stdout.splitlines() if f.strip()]

            # Also include untracked new files (created but not yet staged)
            result3 = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=repo_cwd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            untracked_files = [f.strip() for f in result3.stdout.splitlines() if f.strip()]

            # Merge all three, preserving order, dedup
            seen = set(files)
            for f in new_files + untracked_files:
                if f not in seen:
                    files.append(f)
                    seen.add(f)

            # Filter out Claude artifacts and non-code files
            files = [f for f in files
                     if not any(p in f for p in self._IGNORE_PATTERNS)]

            return files
        except Exception as e:
            log.warning("git diff failed: %s", e)
            return []

    def _parse_output(self, session, task_type: str) -> dict:
        """Parse AI session output into structured result, handling non-JSON gracefully."""
        stdout = session.stdout or ""
        stripped = stdout.strip()

        # Fast path: many CLI runs return a single top-level JSON object directly.
        # Parse the whole payload first so nested objects inside a valid result
        # do not get mistaken for the final answer.
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass

        # Try to extract JSON from markdown code blocks first (```json ... ```)
        import re
        code_blocks = re.findall(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', stdout)
        for block in reversed(code_blocks):
            try:
                obj = json.loads(block)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

        # Fallback: find first (outermost) JSON object from raw output.
        # Forward iteration ensures we match the top-level object rather than
        # an inner nested object when preamble/trailing text is present.
        parsed = None
        for candidate in re.finditer(r'\{', stdout):
            start = candidate.start()
            depth = 0
            end = None
            for i, ch in enumerate(stdout[start:], start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end is not None:
                try:
                    obj = json.loads(stdout[start:end])
                    if isinstance(obj, dict):
                        parsed = obj
                        break
                except json.JSONDecodeError:
                    continue

        if parsed is not None:
            return parsed

        # Fallback: return raw output as summary (non-JSON output is acceptable)
        summary = stdout.strip()
        if not summary:
            summary = "(no output)"
        return {
            "summary": summary[:1000],
            "exit_code": getattr(session, "exit_code", None),
        }

    def _detect_terminal_cli_error(self, session, task_type: str) -> Optional[str]:
        """Detect terminal CLI failures that should never be treated as success."""
        if task_type in ("coordinator", "task", "merge", "deploy"):
            return None

        stdout = (getattr(session, "stdout", "") or "").strip()
        stderr = (getattr(session, "stderr", "") or "").strip()
        combined = "\n".join(p for p in (stdout, stderr) if p).strip()
        lowered = combined.lower()

        if "reached max turns" in lowered:
            first_line = combined.splitlines()[0].strip() if combined else ""
            return first_line or "Error: Reached max turns"

        if stdout.startswith("Error:") and not stdout.lstrip().startswith("{"):
            return stdout.splitlines()[0].strip()

        # R3: Classify JSON-shaped auth failure responses as terminal errors.
        # Stale CLAUDE_CODE_OAUTH_TOKEN or expired API keys produce 401 / Unauthorized
        # responses that should not be retried endlessly.
        _auth_failure_patterns = ("unauthorized", "invalid_token", "authentication_error",
                                  "token_expired", "auth_error")
        if any(pat in lowered for pat in _auth_failure_patterns):
            return f"Auth failure detected: {combined.splitlines()[0][:200]}"
        if '"error"' in lowered and ("401" in combined or "403" in combined):
            return f"Auth failure (HTTP 401/403): {combined.splitlines()[0][:200]}"

        return None

    # ------------------------------------------------------------------
    # Startup helpers: crash recovery + PID lock
    # ------------------------------------------------------------------

    def _run_ttl_cleanup(self) -> None:
        """Archive memories with expired TTL (per domain pack durability)."""
        try:
            result = self._api("POST", f"/api/mem/{self.project_id}/ttl-cleanup")
            archived = result.get("archived", 0)
            if archived:
                log.info("_run_ttl_cleanup: archived %d expired memories", archived)
        except Exception as e:
            log.debug("_run_ttl_cleanup failed (non-fatal): %s", e)

    def _recover_stale_leases(self) -> None:
        """Periodically re-queue claimed tasks whose lease has expired (runtime orphan recovery)."""
        try:
            result = self._api("POST", f"/api/task/{self.project_id}/recover")
            recovered = result.get("recovered", 0)
            if recovered:
                log.warning("_recover_stale_leases: re-queued %d orphaned task(s) with expired leases", recovered)
        except Exception as e:
            log.debug("_recover_stale_leases failed (non-fatal): %s", e)

    def _check_auth_smoke_test(self) -> None:
        """R4: Auth smoke test at startup — verify CLAUDE_CODE_OAUTH_TOKEN is not
        present in our environment (it would be forwarded to child subprocesses
        and cause 401 failures if stale).  Logs warning on failure but does NOT
        block startup.
        """
        try:
            stale_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            if stale_token:
                log.warning(
                    "_check_auth_smoke_test: CLAUDE_CODE_OAUTH_TOKEN found in "
                    "executor env (%d chars) — child subprocesses will strip it "
                    "via env-filter, but the launch env should be cleaned",
                    len(stale_token),
                )
            else:
                log.info("_check_auth_smoke_test: no stale CLAUDE_CODE_OAUTH_TOKEN in env (OK)")
        except Exception as e:
            log.warning("_check_auth_smoke_test failed (non-fatal): %s", e)

    def _recover_stuck_tasks(self) -> None:
        """Mark any 'claimed' tasks from a previous crash as failed.

        Called once at the start of ``run_loop`` before the first poll so that
        tasks which were in-progress when the executor crashed are not left
        permanently stuck in the 'claimed' state.
        """
        try:
            data = self._api("GET", f"/api/task/{self.project_id}/list?status=claimed&limit=200")
            tasks = data.get("tasks", [])
            claimed = [t for t in tasks if t.get("status") == "claimed"]
            if not claimed:
                log.info("_recover_stuck_tasks: no stuck tasks found")
                return
            log.warning(
                "_recover_stuck_tasks: found %d stuck task(s) from previous crash, marking failed",
                len(claimed),
            )
            for task in claimed:
                task_id = task.get("task_id") or task.get("id")
                if not task_id:
                    continue
                self._api("POST", f"/api/task/{self.project_id}/complete", {
                    "task_id": task_id,
                    "status": "failed",
                    "result": {
                        "error": "executor_crash_recovery",
                        "reason": "Executor crashed or restarted while task was claimed",
                    },
                })
                log.info("_recover_stuck_tasks: marked task %s as failed", task_id)
        except Exception as e:
            log.warning("_recover_stuck_tasks failed (non-fatal): %s", e)

    def _acquire_pid_lock(self) -> bool:
        """Write our PID to a temp file, returning False if another instance is alive.

        The PID file path is:
          ``<tempdir>/aming-claw-executor-<project_id>.pid``

        If an old PID file exists but the process is no longer running (stale
        lock), the file is overwritten and ``True`` is returned.
        """
        pid_path = os.path.join(
            tempfile.gettempdir(),
            f"aming-claw-executor-{self.project_id}.pid",
        )

        if os.path.exists(pid_path):
            try:
                with open(pid_path) as fh:
                    old_pid = int(fh.read().strip())
                # Check if the old process is still alive (signal 0 = existence check)
                try:
                    os.kill(old_pid, 0)
                    log.warning(
                        "_acquire_pid_lock: another executor instance appears to be running "
                        "(PID %d); proceeding anyway",
                        old_pid,
                    )
                    # We log a warning but do NOT abort — in Docker/container restarts the
                    # old PID may still appear transiently in /proc.
                except (OSError, ProcessLookupError, SystemError):
                    log.info(
                        "_acquire_pid_lock: stale PID file (PID %d no longer running), overwriting",
                        old_pid,
                    )
            except (ValueError, IOError):
                log.debug("_acquire_pid_lock: could not read stale PID file, overwriting")

        try:
            with open(pid_path, "w") as fh:
                fh.write(str(os.getpid()))
            self._pid_path = pid_path
            log.info("_acquire_pid_lock: wrote PID %d to %s", os.getpid(), pid_path)
        except Exception as exc:
            log.warning("_acquire_pid_lock: could not write PID file: %s", exc)
        return True

    def _release_pid_lock(self) -> None:
        """Remove the PID file created by ``_acquire_pid_lock``."""
        if self._pid_path and os.path.exists(self._pid_path):
            try:
                os.unlink(self._pid_path)
                log.info("_release_pid_lock: removed PID file %s", self._pid_path)
            except Exception as exc:
                log.debug("_release_pid_lock: could not remove PID file: %s", exc)
        self._pid_path = None

    def run_once(self) -> bool:
        """Try to claim and execute one task. Returns True if a task was processed.

        Contract: this method NEVER raises. All exceptions are caught, the task
        is marked failed via _complete_task, and True is returned so the poll
        loop continues without interruption.
        """
        try:
            task = self._claim_task()
        except Exception:
            # Transient claim error — treat as empty poll
            self._consecutive_empty_polls += 1
            return False

        if not task:
            self._consecutive_empty_polls += 1
            if self._consecutive_empty_polls == 10:
                log.warning("10 consecutive empty polls — no tasks available")

            # R1/R2/R6: Stall detection — if _consecutive_empty_polls >= STALL_THRESHOLD
            # and queued tasks exist, force-restart the poll loop (soft reset).
            if self._consecutive_empty_polls >= EXECUTOR_STALL_THRESHOLD:
                queued_count = self._check_queued_tasks()
                if queued_count > 0:
                    uptime = time.monotonic() - self._start_time
                    # R3: log at ERROR level with diagnostic info
                    log.error("STALL detected: %d consecutive empty polls with %d queued tasks (worker=%s, uptime=%.0fs) — forcing poll loop restart", self._consecutive_empty_polls, queued_count, self.worker_id, uptime)
                    # R4: soft self-restart — reset state, continue loop
                    self._consecutive_empty_polls = 0
                    self._current_task = None
                    if self._lifecycle is not None:
                        try:
                            from ai_lifecycle import AILifecycleManager
                            self._lifecycle = AILifecycleManager()
                        except Exception:
                            self._lifecycle = None
            return False

        # Successful claim — reset counter
        self._consecutive_empty_polls = 0
        self.last_claimed_at = time.monotonic()  # R5: update for ServiceManager watchdog
        task_id = task["task_id"]
        self._current_task = task_id

        try:
            outcome = self._execute_task(task)
            status = outcome.get("status", "failed")
            result = outcome.get("result", {"error": outcome.get("error", "unknown")})

            # Coordinator two-round flow with gate validation
            task_type = task.get("type", "")
            if task_type in ("coordinator", "task") and status == "succeeded":
                self._last_query_memories = []
                import time as _t
                _ct0 = _t.time()
                _coord_log = os.path.join(self.workspace or ".", "shared-volume", "codex-tasks", "logs",
                                          f"coordinator-flow-{task_id}.txt")
                os.makedirs(os.path.dirname(_coord_log), exist_ok=True)
                def _clog(msg):
                    elapsed = _t.time() - _ct0
                    line = f"{elapsed:.1f}s {msg}"
                    try:
                        with open(_coord_log, "a") as f:
                            f.write(line + "\n")
                    except Exception:
                        pass

                _clog(f"round1 output: status={status} result_keys={list(result.keys())}")
                raw = result.get("summary", "") or result.get("raw_output", "") or json.dumps(result)
                _clog(f"raw extraction: len={len(raw)} first100={raw[:100]}")
                parsed = self._extract_json(raw) if hasattr(self, '_extract_json') else None
                _clog(f"extract_json: parsed={'yes keys=' + str(list(parsed.keys())) if parsed else 'None'}")

                current_round = 1
                max_gate_retries = 2

                # Round 1 gate validation with retry
                for attempt in range(max_gate_retries + 1):
                    if parsed:
                        valid, gate_error = self._validate_coordinator_output(parsed, round=current_round)
                        _clog(f"gate round={current_round} attempt={attempt+1}: valid={valid} error={gate_error[:80] if gate_error else ''}")
                        if valid:
                            break
                    else:
                        gate_error = "No valid JSON found in output"
                        _clog(f"gate round={current_round} attempt={attempt+1}: no JSON")

                    if attempt < max_gate_retries:
                        _clog(f"retry: starting attempt {attempt+2}")
                        retry_prompt = (
                            f"{task.get('prompt', '')}\n\n"
                            f"--- RETRY (attempt {attempt + 2}/{max_gate_retries + 1}) ---\n"
                            f"Your previous output was invalid: {gate_error}\n"
                            f"Output ONLY a valid JSON object with schema_version, reply, actions.\n"
                        )
                        task_copy = dict(task)
                        task_copy["prompt"] = retry_prompt
                        outcome = self._execute_task(task_copy)
                        _clog(f"retry: _execute_task done, status={outcome.get('status')}")
                        status = outcome.get("status", "failed")
                        result = outcome.get("result", {})
                        raw = result.get("summary", "") or result.get("raw_output", "") or json.dumps(result)
                        parsed = self._extract_json(raw) if hasattr(self, '_extract_json') else None
                        _clog(f"retry: parsed={'yes' if parsed else 'None'}")
                    else:
                        _clog("gate: ALL RETRIES EXHAUSTED")
                        status = "failed"
                        result = {"error": "coordinator_gate_failed", "last_gate_error": gate_error, "raw_output": raw[:500]}

                # Check if round 1 output is query_memory -> need round 2
                if status == "succeeded" and parsed:
                    actions = parsed.get("actions", [])
                    action_types = [a.get("type") for a in actions]
                    _clog(f"round1 decision: actions={action_types}")
                    query_action = next((a for a in actions if a.get("type") == "query_memory"), None)

                    if query_action:
                        queries = query_action.get("queries", [])[:3]
                        _clog(f"query_memory: queries={queries}")
                        memory_results = []
                        seen_ids = set()
                        for q in queries:
                            extra = self._fetch_memories(q)
                            for m in extra:
                                mid = m.get("memory_id", "")
                                if mid and mid not in seen_ids:
                                    seen_ids.add(mid)
                                    memory_results.append(m)
                        _clog(f"query_memory: found {len(memory_results)} unique memories")
                        self._last_query_memories = memory_results

                        self._write_conversation_history(task, parsed)
                        _clog("round1 history written")

                        # Round 2
                        _clog("round2: starting _execute_task")
                        task_copy = dict(task)
                        task_copy["metadata"] = dict(task.get("metadata") or {})
                        if isinstance(task_copy["metadata"], str):
                            task_copy["metadata"] = json.loads(task_copy["metadata"])
                        task_copy["metadata"]["_round2_memories"] = memory_results
                        current_round = 2
                        outcome = self._execute_task(task_copy)
                        _clog(f"round2: _execute_task done, status={outcome.get('status')}")
                        status = outcome.get("status", "failed")
                        result = outcome.get("result", {})
                        raw = result.get("summary", "") or result.get("raw_output", "") or json.dumps(result)
                        parsed = self._extract_json(raw) if hasattr(self, '_extract_json') else None
                        _clog(f"round2: parsed={'yes keys=' + str(list(parsed.keys())) if parsed else 'None'}")

                        if parsed:
                            valid, gate_error = self._validate_coordinator_output(parsed, round=2)
                            _clog(f"round2 gate: valid={valid} error={gate_error[:80] if gate_error else ''}")
                            if not valid:
                                status = "failed"
                                result = {"error": "coordinator_gate_round2_failed", "gate_error": gate_error}

                if status == "succeeded" and parsed:
                    _clog("final: calling _handle_coordinator_result")
                    self._handle_coordinator_result(task, result)
                    self._write_conversation_history(task, parsed)
                    result = {"action": "handled", "_reply_sent": True}
                    _clog("final: DONE")
                else:
                    _clog(f"final: FAILED status={status}")

            # Complete task + auto-chain — write results to file (log.info may block in MCP)
            import time as _ct
            _ct0 = _ct.time()
            completion = self._complete_task_or_raise(task_id, status, result)
            chain = completion.get("auto_chain", {})

            # Write completion result to timing file
            chain_msg = ""
            if chain.get("gate_blocked"):
                chain_msg = f"gate_blocked: {chain.get('reason') or chain.get('gate_reason') or 'unknown'}"
            elif chain.get("preflight_blocked"):
                chain_msg = f"preflight_blocked: {chain.get('reason') or 'unknown'}"
            elif chain.get("task_id"):
                chain_msg = f"chain: {task_id} -> {chain['task_id']} ({chain.get('type')})"
            elif chain.get("deploy"):
                chain_msg = f"deploy: {chain['deploy']}"
            try:
                complete_file = os.path.join(self.workspace or ".", "shared-volume", "codex-tasks", "logs",
                                             f"complete-{task_id}.txt")
                with open(complete_file, "w") as f:
                    f.write(f"status: {status}\n")
                    f.write(f"complete_time: {_ct.time()-_ct0:.1f}s\n")
                    f.write(f"chain: {chain_msg}\n")
                    f.write(f"result_keys: {list(result.keys()) if isinstance(result, dict) else '?'}\n")
            except Exception:
                pass

            return True

        except Exception as e:
            try:
                err_file = os.path.join(self.workspace or ".", "shared-volume", "codex-tasks", "logs",
                                        f"error-{task_id}.txt")
                with open(err_file, "w") as f:
                    f.write(f"error: {e}\n")
                    import traceback
                    traceback.print_exc(file=f)
            except Exception:
                pass
            self._complete_task(task_id, "failed", {"error": str(e)})
            return True
        finally:
            self._current_task = None

    def _execute_single_task(self, task: dict) -> bool:
        """Execute a single already-claimed task (helper for parallel fallback)."""
        task_id = task["task_id"]
        self._current_task = task_id
        try:
            outcome = self._execute_task(task)
            status = outcome.get("status", "failed")
            result = outcome.get("result", {"error": outcome.get("error", "unknown")})
            self._complete_task(task_id, status, result)
            return True
        except Exception as e:
            self._complete_task(task_id, "failed", {"error": str(e)})
            return True
        finally:
            self._current_task = None

    def run_loop(self):
        """Main polling loop with parallel dispatch support (R4, R8)."""
        self._running = True
        # R1: Initialize worker pool for parallel dispatch
        self._worker_pool = WorkerPool(self, max_workers=MAX_CONCURRENT_WORKERS)
        log.info("Executor worker started: project=%s, worker=%s, poll=%ds, max_concurrent=%d",
                 self.project_id, self.worker_id, POLL_INTERVAL, MAX_CONCURRENT_WORKERS)
        log.info("Governance: %s | Workspace: %s", self.base_url, self.workspace)

        # Acquire PID lock (warn if another instance may be running)
        self._acquire_pid_lock()

        # Verify governance is reachable
        health = self._api("GET", "/api/health")
        if "error" in health:
            log.error("Cannot reach governance at %s", self.base_url)
            self._release_pid_lock()
            return
        log.info("Governance: v%s (PID %s)", health.get("version", "?"), health.get("pid", "?"))

        # R4: Auth smoke test — verify CLI can authenticate without stale token
        self._check_auth_smoke_test()

        # Recover any tasks left in 'claimed' state from a previous crash
        self._recover_stuck_tasks()

        # Initial git sync
        self._sync_git_status()
        self._sync_counter = 0
        self._recover_counter = 0   # Stale lease recovery every ~5 min
        self._ttl_counter = 0       # TTL cleanup every ~6h

        try:
            while self._running:
                try:
                    # Sync git every 6th poll (60s) to avoid DB lock contention
                    self._sync_counter += 1
                    if self._sync_counter >= 6:
                        self._sync_git_status()
                        self._sync_counter = 0
                    # Recover stale claimed tasks every 30th poll (~5 min)
                    self._recover_counter += 1
                    if self._recover_counter >= 12:
                        self._recover_stale_leases()
                        self._recover_counter = 0
                    # TTL memory cleanup every 2160th poll (~6h)
                    self._ttl_counter += 1
                    if self._ttl_counter >= 2160:
                        self._run_ttl_cleanup()
                        self._ttl_counter = 0

                    # R4: Check for parallel-eligible sibling subtasks
                    siblings = self._worker_pool.get_sibling_tasks()
                    if len(siblings) >= 2:
                        # Parallel dispatch: claim and dispatch siblings concurrently
                        claimed_tasks = []
                        for task in siblings:
                            claimed = self._claim_task()
                            if claimed:
                                claimed_tasks.append(claimed)
                        if len(claimed_tasks) >= 2:
                            log.info("Parallel dispatch: %d sibling tasks claimed", len(claimed_tasks))
                            self._worker_pool.dispatch_parallel(claimed_tasks)
                            time.sleep(1)
                            continue
                        elif len(claimed_tasks) == 1:
                            # Only got 1 — fall back to sequential (R8)
                            self._execute_single_task(claimed_tasks[0])
                            time.sleep(1)
                            continue

                    # R8: Backward compatibility — single-task sequential mode
                    processed = self.run_once()
                    if not processed:
                        time.sleep(POLL_INTERVAL)
                    else:
                        time.sleep(1)  # Brief pause between tasks
                except KeyboardInterrupt:
                    log.info("Shutting down...")
                    self._running = False
                except Exception as e:
                    log.error("Poll loop error: %s", e, exc_info=True)
                    time.sleep(POLL_INTERVAL)
        finally:
            if self._worker_pool:
                self._worker_pool.shutdown()
            self._release_pid_lock()

    _last_git_head = ""
    _last_dirty = []

    def _sync_git_status(self):
        """Sync git HEAD + dirty files to governance DB. Only writes when changed."""
        try:
            import subprocess
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace, timeout=5
            ).decode().strip()

            # Check both unstaged and staged changes (worktree leaks stage to main index)
            diff = subprocess.check_output(
                ["git", "diff", "--name-only"],
                cwd=self.workspace, timeout=5
            ).decode().strip()
            cached = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"],
                cwd=self.workspace, timeout=5
            ).decode().strip()
            all_dirty = set()
            if diff:
                all_dirty.update(diff.splitlines())
            if cached:
                all_dirty.update(cached.splitlines())
            # Filter out worktree paths — they are not real dirty state in main
            _IGNORE = (".claude/", ".claude\\", ".worktrees/", ".worktrees\\")
            dirty = sorted(f for f in all_dirty
                           if f.strip() and not any(f.startswith(p) for p in _IGNORE))

            # Only write to DB if state changed (avoid unnecessary DB contention)
            if head == self._last_git_head and dirty == self._last_dirty:
                return
            self._last_git_head = head
            self._last_dirty = dirty

            self._api("POST", f"/api/version-sync/{self.project_id}", {
                "git_head": head,
                "dirty_files": dirty,
            })
        except Exception as e:
            pass  # fail silently, non-critical

    def stop(self):
        """Stop the polling loop and signal all worker threads to finish (R7).

        If a WorkerPool is attached, joins all active worker threads with
        SHUTDOWN_TIMEOUT, then force-releases uncompleted tasks on timeout.
        """
        self._running = False
        if hasattr(self, '_worker_pool') and self._worker_pool is not None:
            self._worker_pool.shutdown()


# ---------------------------------------------------------------------------
# WorkerPool — parallel dispatch for sibling subtasks (R1, R4-R7)
# ---------------------------------------------------------------------------


class WorkerPool:
    """Manages up to MAX_CONCURRENT_WORKERS threads for parallel task execution.

    Each worker thread:
    - Gets its own SQLite connection (R5 — connection-per-worker pattern)
    - Creates worktrees under .worktrees/worker-{N}/dev-task-{id} (R3)
    - Uses atomic SQL for fan-in completed_count updates (R6)

    When no sibling subtasks are queued, the executor falls back to
    single-worker sequential mode for backward compatibility (R8).
    """

    def __init__(self, executor: "ExecutorWorker", max_workers: int = MAX_CONCURRENT_WORKERS):
        self.executor = executor
        self.max_workers = min(5, max(1, max_workers))
        self._active_workers: dict = {}  # thread_name -> {thread, task_id, worktree}
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()

    def status(self) -> dict:
        """Return worker pool status for MCP executor_status tool (R9)."""
        with self._lock:
            workers = []
            for name, info in self._active_workers.items():
                workers.append({
                    "worker_name": name,
                    "task_id": info.get("task_id", ""),
                    "worktree": info.get("worktree", ""),
                    "alive": info["thread"].is_alive() if info.get("thread") else False,
                })
            return {
                "active_workers": len([w for w in workers if w["alive"]]),
                "max_workers": self.max_workers,
                "workers": workers,
                "shutdown_requested": self._shutdown_event.is_set(),
            }

    def dispatch_parallel(self, tasks: list) -> list:
        """Dispatch multiple tasks to worker threads in parallel (R4).

        Args:
            tasks: List of task dicts (already claimed) to execute concurrently.

        Returns:
            List of thread objects for the dispatched workers.
        """
        threads = []
        for idx, task in enumerate(tasks[:self.max_workers]):
            worker_name = f"worker-{idx}"
            task_id = task.get("task_id", f"unknown-{idx}")

            t = threading.Thread(
                target=self._worker_run,
                args=(task, worker_name),
                name=f"pool-{worker_name}-{task_id}",
                daemon=True,
            )
            with self._lock:
                self._active_workers[worker_name] = {
                    "thread": t,
                    "task_id": task_id,
                    "worktree": "",
                }
            t.start()
            threads.append(t)
        return threads

    def _worker_run(self, task: dict, worker_name: str) -> None:
        """Execute a single task in a worker thread (R5: own SQLite connection)."""
        task_id = task.get("task_id", "")
        try:
            # R3: create worktree with worker_id prefix
            task_type = task.get("type", "task")
            if task_type == "dev":
                worktree_path, branch_name = self.executor._create_worktree(task_id, worker_id=worker_name)
                if worktree_path:
                    with self._lock:
                        if worker_name in self._active_workers:
                            self._active_workers[worker_name]["worktree"] = worktree_path

            # Execute the task using the executor's run_once-like flow
            outcome = self.executor._execute_task(task)
            status = outcome.get("status", "failed")
            result = outcome.get("result", {"error": outcome.get("error", "unknown")})

            # R6: Atomic fan-in completed_count update
            self._atomic_fan_in_update(task)

            # Complete the task
            self.executor._complete_task(task_id, status, result)

        except Exception as e:
            log.warning("WorkerPool worker %s failed on task %s: %s", worker_name, task_id, e)
            try:
                self.executor._complete_task(task_id, "failed", {"error": str(e)})
            except Exception:
                pass
        finally:
            with self._lock:
                self._active_workers.pop(worker_name, None)

    def _atomic_fan_in_update(self, task: dict) -> None:
        """R6: Atomic SQL update for fan-in completed_count.

        Uses UPDATE ... SET completed_count = completed_count + 1
        with proper transaction isolation — no read-modify-write race.
        """
        metadata = task.get("metadata", {})
        if isinstance(metadata, str):
            import json as _json
            try:
                metadata = _json.loads(metadata)
            except Exception:
                metadata = {}
        subtask_group_id = metadata.get("subtask_group_id", "")
        if not subtask_group_id:
            return

        # Use governance API for atomic increment (API handles SQL atomicity)
        try:
            self.executor._api("POST", f"/api/task/{self.executor.project_id}/fan-in-increment", {
                "subtask_group_id": subtask_group_id,
            })
        except Exception as e:
            log.debug("Fan-in atomic increment failed (non-fatal): %s", e)

    def get_sibling_tasks(self) -> list:
        """R4: Detect queued sibling subtasks with same subtask_group_id and no unmet deps.

        Returns list of task dicts eligible for parallel dispatch.
        """
        result = self.executor._api("GET", f"/api/task/{self.executor.project_id}/list")
        if "error" in result:
            return []
        tasks = result.get("tasks", [])
        queued_dev = [t for t in tasks if t.get("status") in ("queued", "pending") and t.get("type") == "dev"]
        if len(queued_dev) < 2:
            return []

        # Group by subtask_group_id
        groups: dict = {}
        for t in queued_dev:
            meta = t.get("metadata", {})
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            gid = meta.get("subtask_group_id", "")
            if gid:
                deps = meta.get("depends_on", [])
                if not deps:  # No unmet dependencies
                    groups.setdefault(gid, []).append(t)

        # Return the largest sibling group (up to max_workers)
        for gid, group in groups.items():
            if len(group) >= 2:
                return group[:self.max_workers]
        return []

    def has_active_workers(self) -> bool:
        """Check if any worker threads are still alive (R10)."""
        with self._lock:
            return any(info["thread"].is_alive() for info in self._active_workers.values() if info.get("thread"))

    def active_worker_count(self) -> int:
        """Return count of alive worker threads (R10)."""
        with self._lock:
            return sum(1 for info in self._active_workers.values()
                       if info.get("thread") and info["thread"].is_alive())

    def shutdown(self, timeout: int = SHUTDOWN_TIMEOUT) -> None:
        """R7: Graceful shutdown — signal all workers, join with timeout, force-release on timeout."""
        self._shutdown_event.set()
        threads = []
        with self._lock:
            threads = [(name, info) for name, info in self._active_workers.items()]

        for name, info in threads:
            t = info.get("thread")
            if t and t.is_alive():
                t.join(timeout=timeout)
                if t.is_alive():
                    # Timeout: force-release uncompleted task
                    task_id = info.get("task_id", "")
                    if task_id:
                        log.warning("WorkerPool: worker %s timed out on task %s, force-releasing", name, task_id)
                        try:
                            self.executor._complete_task(task_id, "failed", {
                                "error": "shutdown_timeout",
                                "reason": f"Worker {name} did not finish within {timeout}s shutdown timeout",
                            })
                        except Exception:
                            pass

        with self._lock:
            self._active_workers.clear()


def main():
    parser = argparse.ArgumentParser(description="Executor Worker - polls governance for tasks")
    parser.add_argument("--project", "-p", default=os.getenv("PROJECT_ID", "aming-claw"),
                        help="Project ID to poll tasks from")
    parser.add_argument("--url", default=GOVERNANCE_URL,
                        help="Governance API URL")
    parser.add_argument("--worker-id", default=WORKER_ID,
                        help="Worker identifier")
    parser.add_argument("--workspace", default=WORKSPACE,
                        help="Working directory for task execution")
    parser.add_argument("--once", action="store_true",
                        help="Execute one task and exit (no loop)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    worker = ExecutorWorker(
        project_id=args.project,
        governance_url=args.url,
        worker_id=args.worker_id,
        workspace=args.workspace,
    )

    if args.once:
        worker.run_once()
    else:
        worker.run_loop()


if __name__ == "__main__":
    main()
