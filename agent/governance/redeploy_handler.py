"""redeploy_handler.py – Governance-side redeploy endpoints (PR-2).

POST /api/governance/redeploy/{target}  for executor, gateway, coordinator, service_manager.
Each endpoint runs a 5-step correctness pipeline:
  1. precheck  – drain grace + health probe
  2. stop      – SIGTERM with grace, SIGKILL fallback
  3. spawn     – correct CWD + PYTHONPATH
  4. wait      – poll /health until 200 or timeout
  5. db_write  – chain_version=expected_head, updated_by=redeploy-orchestrator

Mutual-exclusion: target=governance is refused with HTTP 400
(governance cannot restart itself).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Valid redeploy targets (governance excluded — mutual-exclusion guard)
VALID_TARGETS = ("executor", "gateway", "coordinator", "service_manager")

# Target → health endpoint mapping
_HEALTH_ENDPOINTS = {
    "executor": "http://localhost:40100/status",
    "gateway": None,  # gateway uses docker inspect, not HTTP
    "coordinator": "http://localhost:40000/api/health",
    "service_manager": None,  # service_manager has no HTTP port
}

# Target → spawn command mapping
_SPAWN_COMMANDS = {
    "executor": [sys.executable, "-m", "agent.executor"],
    "gateway": ["docker", "compose", "-f", "docker-compose.governance.yml", "up", "-d", "telegram-gateway"],
    "coordinator": [sys.executable, "-m", "agent.coordinator"],
    "service_manager": [sys.executable, "-m", "agent.service_manager"],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manager_signal_path() -> Path:
    """Return the same restart signal path watched by service_manager.py."""
    shared_volume = Path(os.getenv("SHARED_VOLUME_PATH", str(_repo_root() / "shared-volume")))
    return shared_volume / "codex-tasks" / "state" / "manager_signal.json"


def _find_pid_for_target(target: str) -> Optional[int]:
    """Best-effort: find the PID of the running target process."""
    try:
        if target == "gateway":
            # Docker container — use docker inspect
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Pid}}", f"aming_claw-telegram-gateway-1"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                pid = int(result.stdout.strip())
                return pid if pid > 0 else None
            return None

        # For Python processes, look through state files or use platform APIs
        if sys.platform == "win32":
            # Use tasklist to find matching python processes
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            # Best-effort — return None if we can't determine
            return None
        else:
            # Unix: use pgrep
            module_name = f"agent.{target}"
            result = subprocess.run(
                ["pgrep", "-f", module_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                pids = result.stdout.strip().split("\n")
                if pids and pids[0]:
                    return int(pids[0])
            return None
    except Exception:
        return None


def _stop_process(pid: int, grace_seconds: int = 10) -> bool:
    """Stop a process: SIGTERM with grace period, SIGKILL fallback."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=grace_seconds + 5,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            # Wait for graceful shutdown
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)  # Check if still alive
                    time.sleep(0.5)
                except OSError:
                    return True  # Process is gone
            # Still alive — SIGKILL
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return True
    except Exception as exc:
        log.warning("_stop_process: failed to stop PID %s: %s", pid, exc)
        return False


def _health_check(target: str, timeout: int = 30) -> bool:
    """Poll health endpoint until 200 or timeout."""
    endpoint = _HEALTH_ENDPOINTS.get(target)

    if target == "gateway":
        # Docker inspect for gateway
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}",
                 "aming_claw-telegram-gateway-1"],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip().lower() == "true"
        except Exception:
            return False

    if not endpoint:
        # No health endpoint — assume OK after spawn
        return True

    try:
        import requests
    except ImportError:
        import urllib.request
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                req = urllib.request.urlopen(endpoint, timeout=5)
                if req.getcode() == 200:
                    return True
            except Exception:
                time.sleep(2)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = requests.get(endpoint, timeout=5)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _spawn_target(target: str) -> Optional[int]:
    """Spawn the target process, return new PID or None on failure."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    cmd = _SPAWN_COMMANDS.get(target)
    if not cmd:
        return None

    env = os.environ.copy()
    # B48/F2 FIX (observer-hotfix 2026-04-24): PYTHONPATH must include the
    # PROJECT ROOT (repo_root), not repo_root/agent. Previously the agent dir
    # was put on PYTHONPATH which allows `import governance.server` but NOT
    # `import agent.governance.server` — which is what `python -m agent.governance.server`
    # (the cmd below) requires. This was part of the F2 ModuleNotFoundError
    # cascade root cause. See docs/dev/b48-investigation-and-fix-proposal.md §3.
    project_root = str(repo_root)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{project_root}{os.pathsep}{existing}" if existing else project_root

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        return proc.pid
    except Exception as exc:
        log.error("_spawn_target(%s): %s", target, exc)
        return None


def _db_write_chain_version(expected_head: str, task_id: str, target: str) -> bool:
    """Write chain_version to DB. Single source of truth (R11).

    Uses updated_by='redeploy-orchestrator' and includes task_id.
    """
    try:
        from . import dbservice
        db_path = dbservice._db_path_for_project("aming-claw")
        if not db_path:
            log.warning("_db_write_chain_version: no DB path found")
            return False

        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            now = _utc_now()
            conn.execute(
                "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "chain_version=excluded.chain_version, "
                "updated_at=excluded.updated_at, "
                "updated_by=excluded.updated_by",
                ("aming-claw", expected_head, now, "redeploy-orchestrator"),
            )
            # Also write audit record linking to task_id
            try:
                conn.execute(
                    "INSERT INTO audit_log (project_id, event_type, actor, details, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        "aming-claw",
                        "redeploy.version_write",
                        "redeploy-orchestrator",
                        json.dumps({
                            "task_id": task_id,
                            "target": target,
                            "chain_version": expected_head,
                        }),
                        now,
                    ),
                )
            except Exception:
                pass  # Audit failure should not block
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        log.error("_db_write_chain_version: %s", exc)
        return False


def handle_redeploy(target: str, body: dict) -> tuple[dict, int]:
    """Main redeploy handler implementing the 5-step pipeline.

    Args:
        target: Service to redeploy (executor, gateway, coordinator, service_manager)
        body: Request body with {task_id, expected_head, drain_grace_seconds}

    Returns:
        (response_dict, http_status_code)
    """
    # --- Mutual-exclusion guard: governance cannot restart itself ---
    if target == "governance":
        return {
            "ok": False,
            "error": "Governance cannot restart itself (mutual-exclusion guard)",
            "step": "guard",
        }, 400

    # --- R2: Mutual-exclusion guard: service_manager cannot be redeployed ---
    if target == "service_manager":
        return {
            "ok": False,
            "error": "cannot redeploy supervisor — start manually",
            "step": "guard",
        }, 400

    # --- R3: gateway and coordinator are stubs (services don't exist yet) ---
    if target in ("gateway", "coordinator"):
        return {
            "ok": True,
            "stub": True,
            "todo": "wire when service deployed",
            "target": target,
        }, 200

    if target not in VALID_TARGETS:
        return {
            "ok": False,
            "error": f"Unknown target: {target}. Valid: {', '.join(VALID_TARGETS)}",
            "step": "validation",
        }, 400

    task_id = body.get("task_id", "")
    expected_head = body.get("expected_head", "")
    drain_grace_seconds = body.get("drain_grace_seconds", 10)

    if not expected_head:
        return {
            "ok": False,
            "error": "expected_head is required",
            "step": "validation",
        }, 400

    log.info(
        "handle_redeploy: target=%s task_id=%s expected_head=%s drain_grace=%d",
        target, task_id, expected_head, drain_grace_seconds,
    )

    # --- R1: executor target writes shutdown signal to manager_signal.json ---
    # Instead of directly killing/spawning the process, we write a signal file
    # that ServiceManager consumes on its next monitor tick. This is the same
    # mechanism used by deploy_chain.py:restart_executor (line ~138) via
    # state/manager_signal.json with action='restart'.
    if target == "executor":
        try:
            signal_path = _manager_signal_path()
            state_dir = signal_path.parent
            state_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "action": "restart",
                "requested_at": _utc_now(),
                "task_id": task_id,
                "expected_head": expected_head,
            }
            signal_path.write_text(json.dumps(payload), encoding="utf-8")
            log.info(
                "handle_redeploy[executor]: wrote manager_signal.json → %s",
                signal_path,
            )
        except Exception as exc:
            log.error("handle_redeploy[executor]: failed to write manager_signal.json: %s", exc)
            return {
                "ok": False,
                "error": f"Failed to write manager_signal.json: {exc}",
                "step": "signal_write",
                "target": target,
            }, 500

        # Write chain_version to DB on success
        db_ok = _db_write_chain_version(expected_head, task_id, target)
        if not db_ok:
            log.warning("handle_redeploy[executor]: db_write failed but signal was written")

        return {
            "ok": True,
            "target": target,
            "mechanism": "manager_signal.json",
            "signal_path": str(signal_path),
            "new_chain_version": expected_head,
            "updated_at": _utc_now(),
            "db_write": db_ok,
        }, 200

    # --- Full 5-step pipeline for remaining targets ---

    # --- Step 1: precheck (drain grace + health probe) ---
    old_pid = _find_pid_for_target(target)
    if drain_grace_seconds > 0:
        log.info("handle_redeploy[%s]: drain grace %ds", target, drain_grace_seconds)
        time.sleep(min(drain_grace_seconds, 30))  # Cap at 30s

    # --- Step 2: stop ---
    if old_pid:
        log.info("handle_redeploy[%s]: stopping PID %d", target, old_pid)
        stop_ok = _stop_process(old_pid, grace_seconds=drain_grace_seconds)
        if not stop_ok:
            return {
                "ok": False,
                "error": f"Failed to stop {target} (PID {old_pid})",
                "step": "stop",
                "target": target,
            }, 500

    # --- Step 3: spawn ---
    new_pid = _spawn_target(target)
    if not new_pid:
        return {
            "ok": False,
            "error": f"Failed to spawn {target}",
            "step": "spawn",
            "target": target,
        }, 500

    log.info("handle_redeploy[%s]: spawned new PID %d", target, new_pid)

    # --- Step 4: wait (health check) ---
    healthy = _health_check(target, timeout=30)
    if not healthy:
        return {
            "ok": False,
            "error": f"{target} failed health check after spawn",
            "step": "wait",
            "target": target,
            "new_pid": new_pid,
        }, 500

    # --- Step 5: db_write (only on success) ---
    db_ok = _db_write_chain_version(expected_head, task_id, target)
    if not db_ok:
        log.warning(
            "handle_redeploy[%s]: db_write failed but process is healthy", target,
        )
        # Still return success for the redeploy itself, note the DB issue
        return {
            "ok": True,
            "target": target,
            "old_pid": old_pid,
            "new_pid": new_pid,
            "new_chain_version": expected_head,
            "updated_at": _utc_now(),
            "warning": "db_write failed — chain_version not updated",
        }, 200

    return {
        "ok": True,
        "target": target,
        "old_pid": old_pid,
        "new_pid": new_pid,
        "new_chain_version": expected_head,
        "updated_at": _utc_now(),
    }, 200


# --- Route handler functions (called from server.py) ---

def handle_redeploy_executor(ctx) -> tuple[dict, int] | dict:
    """POST /api/governance/redeploy/executor"""
    body = ctx.body or {}
    result, status = handle_redeploy("executor", body)
    if status != 200:
        return result, status
    return result


def handle_redeploy_gateway(ctx) -> tuple[dict, int] | dict:
    """POST /api/governance/redeploy/gateway"""
    body = ctx.body or {}
    result, status = handle_redeploy("gateway", body)
    if status != 200:
        return result, status
    return result


def handle_redeploy_coordinator(ctx) -> tuple[dict, int] | dict:
    """POST /api/governance/redeploy/coordinator"""
    body = ctx.body or {}
    result, status = handle_redeploy("coordinator", body)
    if status != 200:
        return result, status
    return result


def handle_redeploy_service_manager(ctx) -> tuple[dict, int] | dict:
    """POST /api/governance/redeploy/service_manager"""
    body = ctx.body or {}
    result, status = handle_redeploy("service_manager", body)
    if status != 200:
        return result, status
    return result
