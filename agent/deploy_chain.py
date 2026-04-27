"""deploy_chain.py – Orchestrate service restarts after a code push.
# Multi-project deploy orchestration - supports any project via .aming-claw.yaml config

Functions
---------
detect_affected_services  Map changed file paths → services to restart.
restart_executor          Write manager signal file → executor restart.
rebuild_governance        Run deploy-governance.sh, health-check afterwards.
restart_gateway           docker compose restart telegram-gateway.
smoke_test                Quick health check of all three services.
run_deploy                Full orchestration: detect → restart → smoke → notify.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path bootstrap so we can import utils regardless of CWD
# ---------------------------------------------------------------------------
_agent_dir = Path(__file__).resolve().parent
if str(_agent_dir) not in sys.path:
    sys.path.insert(0, str(_agent_dir))

from utils import save_json, tasks_root, utc_iso  # noqa: E402

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    """Return the state directory (tasks_root / 'state'), creating it if needed."""
    d = tasks_root() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _matches_any(path: str, patterns: list[str]) -> bool:
    """Return True if *path* matches at least one glob pattern."""
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, p) for p in patterns)


def _executor_health_from_state() -> bool:
    """Fallback executor health check when the HTTP status port is unavailable."""
    try:
        status_path = _state_dir() / "manager_status.json"
        if not status_path.exists():
            return False
        data = json.loads(status_path.read_text(encoding="utf-8"))
        services = data.get("services", {}) or {}
        return services.get("executor") == "running" and services.get("manager") == "running"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 1. detect_affected_services
# ---------------------------------------------------------------------------

_SERVICE_RULES: list[tuple[list[str], list[str]]] = [
    # patterns                                         services
    (["docs/**", "tests/**", "*.md"],                 []),          # no restart
    (["scripts/**"],                                  ["all"]),
    (["agent/utils.py", "agent/i18n.py",
      "agent/workspace*.py"],                         ["executor", "governance"]),
    (["agent/governance/**"],                         ["governance"]),
    (["agent/telegram_gateway/**"],                   ["gateway"]),
    (["agent/executor.py", "agent/task_*.py",
      "agent/ai_lifecycle.py",
      "agent/parallel_dispatcher.py"],               ["executor"]),
]


def detect_affected_services(changed_files: list[str], project_id: str = "") -> list[str]:
    """Map *changed_files* to a deduplicated list of services that need restarting.

    Returns a list such as ``['executor', 'governance']`` or ``['all']``.
    If project_id is provided, reads service_rules from project config.
    Files that match no rule at all are treated as requiring
    'executor' (safest default).
    """
    # Try project-specific rules first
    if project_id:
        try:
            from project_config import get_service_rules
            rules = get_service_rules(project_id)
            if rules:
                services: set[str] = set()
                for f in changed_files:
                    normalized = f.replace("\\", "/")
                    matched = False
                    for rule in rules:
                        patterns = rule.patterns if hasattr(rule, 'patterns') else rule.get("patterns", [])
                        svcs = rule.services if hasattr(rule, 'services') else rule.get("services", [])
                        if any(_matches_any(normalized, [p]) for p in patterns):
                            services.update(svcs)
                            matched = True
                    if not matched:
                        services.add("executor")
                return sorted(services - {""})
        except (ImportError, Exception):
            pass  # Fall through to default rules

    services: set[str] = set()
    for f in changed_files:
        matched = False
        for patterns, svcs in _SERVICE_RULES:
            if _matches_any(f, patterns):
                services.update(svcs)
                matched = True
                break
        if not matched:
            # Unknown file → restart executor as safest default
            services.add("executor")

    # Expand 'all' early so callers see concrete names
    if "all" in services:
        return ["executor", "governance", "gateway"]

    return sorted(services)


# ---------------------------------------------------------------------------
# 2. restart_executor
# ---------------------------------------------------------------------------

def restart_executor() -> bool:
    """Write state/manager_signal.json with action='restart'.

    Returns True on success, False if an exception occurred.
    """
    try:
        signal_path = _state_dir() / "manager_signal.json"
        payload: dict[str, Any] = {
            "action": "restart",
            "requested_at": utc_iso(),
        }
        save_json(signal_path, payload)
        log.info("restart_executor: wrote restart signal → %s", signal_path)
        return True
    except Exception as exc:  # noqa: BLE001
        _log_error("restart_executor", exc)
        return False


# ---------------------------------------------------------------------------
# 3. rebuild_governance
# ---------------------------------------------------------------------------

def _is_host_runtime_mode() -> bool:
    """Detect whether governance runs on the host (not Docker).

    Returns True if GOVERNANCE_RUNTIME=host env var is set,
    or docker-compose.governance.yml does not exist.
    """
    if os.environ.get("GOVERNANCE_RUNTIME", "").lower() == "host":
        return True
    repo_root = Path(__file__).resolve().parent.parent
    compose_file = repo_root / "docker-compose.governance.yml"
    return not compose_file.exists()


def rebuild_governance() -> tuple[bool, str]:
    """Rebuild + restart governance Docker container, then health-check.

    Uses docker compose build + up directly (Windows-compatible).
    In host-runtime mode (no Docker), falls directly to restart_local_governance.
    Returns (success, output_summary).
    """
    # R4: detect host-runtime mode and skip Docker
    if _is_host_runtime_mode():
        return restart_local_governance(port=40000)

    repo_root = Path(__file__).resolve().parent.parent
    compose_file = repo_root / "docker-compose.governance.yml"
    output_lines: list[str] = []
    try:
        # Step 1: docker compose build governance
        build = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "build", "governance"],
            capture_output=True, text=True, timeout=300, cwd=str(repo_root),
        )
        if build.returncode != 0:
            return False, f"build failed: {build.stderr[:300]}"
        output_lines.append("build OK")

        # Step 2: docker compose up -d governance
        up = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d", "governance"],
            capture_output=True, text=True, timeout=60, cwd=str(repo_root),
        )
        if up.returncode != 0:
            return False, f"up failed: {up.stderr[:300]}"
        output_lines.append("container restarted")

        # Capture combined output for diagnostics
        combined = (build.stdout + "\n" + up.stdout).strip()
        if combined:
            output_lines.append(combined[-200:])
    except FileNotFoundError:
        msg = "docker compose not found — is Docker installed?"
        _log_error("rebuild_governance", msg)
        return False, msg
    except subprocess.TimeoutExpired:
        msg = "governance rebuild timed out after 300s"
        _log_error("rebuild_governance", msg)
        return False, msg
    except Exception as exc:  # noqa: BLE001
        _log_error("rebuild_governance", exc)
        return False, str(exc)

    # Health check with retry (container needs a few seconds after restart)
    import time as _time
    try:
        import requests  # local import to avoid hard dep at module level

        for attempt in range(4):  # 0, 1, 2, 3 — up to ~15s total wait
            if attempt > 0:
                _time.sleep(5)
            try:
                resp = requests.get("http://localhost:40000/api/health", timeout=10)
                if resp.status_code == 200:
                    output_lines.append("[health] governance OK")
                    return True, "\n".join(output_lines)
                elif attempt < 3:
                    continue  # Retry on non-200 (e.g., 502 from nginx)
                else:
                    output_lines.append(f"[health] governance returned HTTP {resp.status_code} after {attempt+1} attempts")
                    return False, "\n".join(output_lines)
            except Exception:
                if attempt < 3:
                    continue
                raise
    except Exception as exc:  # noqa: BLE001
        output_lines.append(f"[health] governance unreachable after retries: {exc}")
        return False, "\n".join(output_lines)


# ---------------------------------------------------------------------------
# 3b. restart_local_governance (fallback for non-Docker environments)
# ---------------------------------------------------------------------------

def _is_port_free(port: int) -> bool:
    """Check whether *port* is available for binding (R4: port release verification)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _read_stderr_log(stderr_path: str | Path, max_bytes: int = 2000) -> str:
    """Read and return the tail of the stderr log file (R5: include stderr on failure)."""
    try:
        p = Path(stderr_path)
        if p.exists():
            content = p.read_text(encoding="utf-8", errors="replace")
            if len(content) > max_bytes:
                return f"...truncated...\n{content[-max_bytes:]}"
            return content
    except Exception:
        pass
    return ""


def restart_local_governance(port: int = 40000) -> tuple[bool, str]:
    """Kill and restart governance as a local Python process.

    Fallback when Docker is not available or Docker rebuild fails.
    Returns (success, output_summary).

    Fixes applied (B7):
    - R1: stderr redirected to temp log file for diagnosis
    - R2: proc.poll() detects immediate crash before health check
    - R3: 4-attempt health check retry loop (matching rebuild_governance)
    - R4: port-free verification between kill and start
    - R5: stderr log content included in failure summary
    - R6: log.warning on restart failure
    """
    import tempfile
    import time as _time
    output_lines: list[str] = []
    stderr_path = None

    # Step 1: Find and kill PID listening on the port
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
            )
            pid = None
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = int(parts[-1])
                    break
            if pid:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                )
                output_lines.append(f"killed PID {pid}")
            else:
                output_lines.append(f"no process found on port {port}")
        else:
            # Unix: use fuser or lsof
            result = subprocess.run(
                ["fuser", f"{port}/tcp"],
                capture_output=True, text=True, timeout=10,
            )
            pids = result.stdout.strip().split()
            for p in pids:
                try:
                    os.kill(int(p), 9)
                    output_lines.append(f"killed PID {p}")
                except Exception:
                    pass
    except Exception as exc:
        output_lines.append(f"kill step failed: {exc}")

    # R4: Wait for port release (Windows TIME_WAIT can hold for seconds)
    for _wait in range(10):  # up to 5s (10 x 0.5s)
        if _is_port_free(port):
            if _wait > 0:
                output_lines.append(f"port {port} released after {_wait * 0.5:.1f}s")
            break
        _time.sleep(0.5)
    else:
        output_lines.append(f"port {port} still held after 5s — proceeding anyway")

    # Step 2: Restart the governance server
    # R1: Redirect stderr to temp file for diagnosis (not DEVNULL)
    try:
        stderr_fd = tempfile.NamedTemporaryFile(
            mode="w", prefix="governance_stderr_", suffix=".log",
            delete=False,
        )
        stderr_path = stderr_fd.name
        repo_root = Path(__file__).resolve().parent.parent
        python_exe = sys.executable or "python"
        # B48/F2 FIX (observer-hotfix 2026-04-24): Explicitly propagate PYTHONPATH
        # so the spawned `python -m agent.governance.server` can find the `agent`
        # package. Historically failed 24+ times with
        #   ModuleNotFoundError: No module named 'agent'
        # because cwd= alone doesn't put the project root on sys.path for `-m`
        # when using the embedded python runtime. See
        # docs/dev/b48-investigation-and-fix-proposal.md §3.
        _env = {**os.environ, "PYTHONPATH": str(repo_root)}
        proc = subprocess.Popen(
            [python_exe, "-m", "agent.governance.server"],
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=stderr_fd,
            start_new_session=True,
            env=_env,
        )
        stderr_fd.close()  # Process owns the fd now via inheritance
        output_lines.append(f"started PID {proc.pid}")
        output_lines.append(f"stderr log: {stderr_path}")
    except Exception as exc:
        output_lines.append(f"start failed: {exc}")
        log.warning("restart_local_governance: start failed: %s", exc)
        return False, "\n".join(output_lines)

    # R2: Check for immediate crash before health check
    _time.sleep(1)
    exit_code = proc.poll()
    if exit_code is not None:
        stderr_content = _read_stderr_log(stderr_path)
        output_lines.append(f"process crashed immediately (exit code {exit_code})")
        if stderr_content:
            output_lines.append(f"stderr:\n{stderr_content}")
        log.warning(
            "restart_local_governance: process PID %d crashed immediately (exit=%d), stderr: %s",
            proc.pid, exit_code, stderr_content[:500],
        )
        return False, "\n".join(output_lines)

    # Step 3: R3: Health check with retry (4 attempts, 5s between, 10s timeout)
    try:
        import requests

        for attempt in range(4):  # 0, 1, 2, 3 — up to ~20s total wait
            if attempt > 0:
                _time.sleep(5)
            # R2: Also check process is still alive during retries
            if proc.poll() is not None:
                stderr_content = _read_stderr_log(stderr_path)
                output_lines.append(
                    f"process died during health check (exit code {proc.returncode})"
                )
                if stderr_content:
                    output_lines.append(f"stderr:\n{stderr_content}")
                log.warning(
                    "restart_local_governance: process died during health check (exit=%s), stderr: %s",
                    proc.returncode, stderr_content[:500],
                )
                return False, "\n".join(output_lines)
            try:
                resp = requests.get(f"http://localhost:{port}/api/health", timeout=10)
                if resp.status_code == 200:
                    output_lines.append(f"[health] governance OK (attempt {attempt + 1})")
                    return True, "\n".join(output_lines)
                elif attempt < 3:
                    continue
                else:
                    output_lines.append(
                        f"[health] HTTP {resp.status_code} after {attempt + 1} attempts"
                    )
            except Exception:
                if attempt < 3:
                    continue
                raise
    except Exception as exc:
        output_lines.append(f"[health] unreachable after 4 attempts: {exc}")

    # R5/R6: Failure path — include stderr log and log warning
    stderr_content = _read_stderr_log(stderr_path)
    if stderr_content:
        output_lines.append(f"stderr:\n{stderr_content}")
    log.warning(
        "restart_local_governance: health check failed after 4 attempts. PID=%s, stderr: %s",
        proc.pid, stderr_content[:500] if stderr_content else "(empty)",
    )
    return False, "\n".join(output_lines)


# ---------------------------------------------------------------------------
# 4. restart_gateway
# ---------------------------------------------------------------------------

def restart_gateway() -> tuple[bool, str]:
    """Rebuild + restart telegram-gateway Docker container, then verify via logs.

    Uses build + up (not just restart) to ensure latest code is deployed.
    Returns (success, output_summary).
    """
    compose_file = (
        Path(__file__).resolve().parent.parent / "docker-compose.governance.yml"
    )
    repo_root = compose_file.parent
    output_lines: list[str] = []
    try:
        # Build first to pick up code changes
        build = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "build", "telegram-gateway"],
            capture_output=True, text=True, timeout=300, cwd=str(repo_root),
        )
        if build.returncode != 0:
            return False, f"gateway build failed: {build.stderr[:300]}"
        output_lines.append("gateway build OK")

        # Up -d to restart with new image
        result = subprocess.run(
            [
                "docker", "compose",
                "-f", str(compose_file),
                "up", "-d", "telegram-gateway",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(repo_root),
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stdout:
            output_lines.append(stdout)
        if stderr:
            output_lines.append(f"[stderr] {stderr}")
        if result.returncode != 0:
            output_lines.append(f"[exit {result.returncode}] restart failed")
            return False, "\n".join(output_lines)
    except FileNotFoundError:
        msg = "docker not found – is Docker installed?"
        _log_error("restart_gateway", msg)
        return False, msg
    except subprocess.TimeoutExpired:
        msg = "docker compose restart timed out after 120 s"
        _log_error("restart_gateway", msg)
        return False, msg
    except Exception as exc:  # noqa: BLE001
        _log_error("restart_gateway", exc)
        return False, str(exc)

    # Check logs for startup confirmation
    try:
        log_result = subprocess.run(
            [
                "docker", "compose",
                "-f", str(compose_file),
                "logs", "--tail", "30", "telegram-gateway",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        logs = log_result.stdout + log_result.stderr
        started = any(
            kw in logs.lower()
            for kw in ("started", "listening", "ready", "running", "online")
        )
        if started:
            output_lines.append("[logs] gateway startup confirmed")
            return True, "\n".join(output_lines)
        else:
            output_lines.append("[logs] no startup keyword found in recent logs")
            # Still return True because restart command succeeded
            return True, "\n".join(output_lines)
    except Exception as exc:  # noqa: BLE001
        output_lines.append(f"[logs] could not read gateway logs: {exc}")
        return True, "\n".join(output_lines)  # restart itself succeeded


# ---------------------------------------------------------------------------
# 5. smoke_test
# ---------------------------------------------------------------------------

def smoke_test(affected_services: list[str] | None = None) -> dict[str, Any]:
    """Quick health check for executor, governance, and gateway.

    Parameters
    ----------
    affected_services : list[str] | None
        If provided, only services in this list are actively checked.
        Services not in the list are marked ``'not_applicable'`` and excluded
        from the ``all_pass`` computation.

    Returns::

        {
            'executor':   bool | 'not_applicable',
            'governance': bool | 'not_applicable',
            'gateway':    bool | 'not_applicable',
            'all_pass':   bool,
        }
    """
    all_services = ["executor", "governance", "gateway"]
    results: dict[str, Any] = {svc: False for svc in all_services}
    results["all_pass"] = False

    import time as _time
    _time.sleep(5)  # Brief pause to let services stabilize after restarts

    # Mark services not in affected_services as 'not_applicable' (R1/R6)
    if affected_services is not None:
        for svc in all_services:
            if svc not in affected_services:
                results[svc] = "not_applicable"

    # --- executor ---
    if results["executor"] != "not_applicable":
        try:
            import requests
            resp = requests.get("http://localhost:40100/status", timeout=5)
            results["executor"] = resp.status_code == 200
        except Exception:  # noqa: BLE001
            results["executor"] = _executor_health_from_state()

    # --- governance ---
    if results["governance"] != "not_applicable":
        try:
            import requests
            resp = requests.get("http://localhost:40000/api/health", timeout=5)
            results["governance"] = resp.status_code == 200
        except Exception:  # noqa: BLE001
            results["governance"] = False

    # --- gateway (docker inspect) ---
    if results["gateway"] != "not_applicable":
        try:
            insp = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}",
                 "aming_claw-telegram-gateway-1"],
                capture_output=True, text=True, timeout=10,
            )
            results["gateway"] = insp.stdout.strip().lower() == "true"
        except Exception:  # noqa: BLE001
            results["gateway"] = False

    # all_pass only considers affected services (not 'not_applicable')
    checked = [results[k] for k in all_services if results[k] != "not_applicable"]
    results["all_pass"] = all(checked) if checked else True
    return results


# ---------------------------------------------------------------------------
# 6. run_deploy
# ---------------------------------------------------------------------------

def _post_redeploy(target: str, task_id: str = "", expected_head: str = "",
                   drain_grace_seconds: int = 5) -> dict[str, Any]:
    """POST to the governance redeploy endpoint for a target service.

    Returns the JSON response dict, or an error dict on failure.
    """
    import urllib.request
    import urllib.error

    url = f"http://localhost:40000/api/governance/redeploy/{target}"
    payload = json.dumps({
        "task_id": task_id,
        "expected_head": expected_head,
        "drain_grace_seconds": drain_grace_seconds,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"error": str(exc)}
        return {"ok": False, **body}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _post_manager_redeploy_governance(task_id: str = "", expected_head: str = "",
                                      drain_grace_seconds: int = 5) -> dict[str, Any]:
    """POST to /api/manager/redeploy/governance (PR-1 service_manager endpoint)."""
    import urllib.request
    import urllib.error

    url = "http://localhost:40101/api/manager/redeploy/governance"
    # observer-hotfix: manager_http_server reads body.get("chain_version") not "expected_head"
    # PR2 fixed the URL port (40200→40101) but missed the field-name mismatch; this flips
    # the deploy through the legacy ModuleNotFoundError path instead of the manager redeploy.
    # Send both names for compat; manager picks chain_version, future PR3 can rename.
    payload = json.dumps({
        "task_id": task_id,
        "expected_head": expected_head,
        "chain_version": expected_head,  # observer-hotfix alias for manager_http_server
        "drain_grace_seconds": drain_grace_seconds,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"error": str(exc)}
        return {"ok": False, **body}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_deploy(changed_files: list[str], chat_id: int = 0, project_id: str = "",
               skip_services: list[str] = None,
               task_id: str = "", expected_head: str = "") -> dict[str, Any]:
    """Full deploy orchestration with double-write (legacy + redeploy).

    Steps:
    1. Detect affected services from *changed_files*.
    2. Check for unsupported dual-restart case (governance + service_manager).
    3. For each affected service:
       a. POST to redeploy endpoint (new path)
       b. Call legacy restart function (old path)
       c. Log both outcomes side-by-side
    4. Run smoke test.
    5. Optionally notify via Telegram if *chat_id* is non-zero.
    6. Persist a report to state/deploy_report_<ts>.json.

    Returns a full report dict.
    """
    started_at = utc_iso()
    report: dict[str, Any] = {
        "started_at": started_at,
        "changed_files": changed_files,
        "affected_services": [],
        "steps": {},
        "smoke_test": {},
        "success": False,
        "finished_at": "",
    }

    try:
        # 1. Detect
        affected = detect_affected_services(changed_files, project_id=project_id)
        if skip_services:
            affected = [s for s in affected if s not in skip_services]
        report["affected_services"] = affected

        if not affected:
            report["success"] = True
            report["note"] = "No services needed restarting."
            report["finished_at"] = utc_iso()
            _save_report(report)
            return report

        # R10: Check for unsupported dual-restart case
        if "governance" in affected and "service_manager" in affected:
            report["success"] = False
            report["error"] = (
                "Cannot auto-redeploy governance + service_manager simultaneously. "
                "See docs/dev/dual-restart-runbook.md for the manual procedure."
            )
            report["dual_restart_required"] = True
            report["finished_at"] = utc_iso()
            _save_report(report)
            return report

        # 2. Restart each service (double-write: redeploy + legacy)
        steps: dict[str, Any] = {}

        # R4/PR2: When BOTH governance AND executor are affected, governance
        # MUST be redeployed FIRST (via manager_http_server on port 40101),
        # then executor. This ensures governance is healthy before executor
        # tries to register with it.

        # R7: Event-driven governance restart via HTTP (no direct sqlite3)
        if "governance" in affected:
            import urllib.request as _ur
            import urllib.error as _ue
            chain_version_short = expected_head or ""
            if not chain_version_short:
                try:
                    from agent.governance.chain_trailer import get_chain_version
                    chain_version_short = get_chain_version()
                except Exception:
                    chain_version_short = "unknown"
            ok = False
            summary = ""
            try:
                _pid = project_id or "aming-claw"
                _payload = json.dumps({"task_id": task_id, "chain_version": chain_version_short}).encode()
                _req = _ur.Request(
                    f"http://localhost:40000/api/governance/redeploy-after-merge/{_pid}",
                    data=_payload, headers={"Content-Type": "application/json"}, method="POST")
                with _ur.urlopen(_req, timeout=30) as _resp:
                    _body = json.loads(_resp.read())
                ok = _body.get("ok", False)
                summary = f"redeploy-after-merge ok={ok}"
            except Exception as exc:
                summary = f"redeploy-after-merge failed: {exc}"
                log.warning("deploy: governance HTTP restart failed: %s", exc)
            steps["governance"] = {"success": ok, "summary": summary}

        # R6: For executor, mark task SUCCEEDED with redeploy_pending BEFORE kill
        if "executor" in affected:
            # [redeploy] POST to redeploy endpoint
            redeploy_result = _post_redeploy(
                "executor", task_id=task_id,
                expected_head=expected_head,
            )
            log.info("[redeploy] executor: %s", redeploy_result)

            # R6: pre-SUCCESS write before executor kill
            if task_id:
                try:
                    _mark_task_succeeded_pre_kill(task_id, project_id)
                    log.info("[redeploy] executor: marked task %s SUCCEEDED with redeploy_pending", task_id)
                except Exception as exc:
                    log.warning("[redeploy] executor: pre-SUCCESS write failed: %s", exc)

            # B48-sequel FIX (observer-hotfix 2026-04-24): Skip legacy
            # restart_executor signal write to avoid SELFKILL loop.
            #
            # Problem: legacy restart_executor() writes manager_signal.json.
            # On next SM monitor tick (~10s), SM reads signal and taskkills
            # the worker — but that worker IS the one executing this deploy
            # task. Worker dies mid-deploy → _recover_stuck_tasks on new
            # worker marks the task failed → auto-chain retries → same SELFKILL
            # loops 3× → deploy task terminally failed.
            #
            # The [redeploy] path (_post_manager_redeploy_executor) is the
            # modern replacement; it handles executor reload without the
            # self-kill race. Rely on its result.
            ok = bool(redeploy_result.get("ok", True))
            log.info("[legacy] executor: SKIPPED (B48-sequel); using redeploy_result.ok=%s", ok)

            steps["executor"] = {
                "success": ok,
                "redeploy_result": redeploy_result,
                "legacy_skipped": True,
            }

        if "gateway" in affected:
            # [redeploy] POST to redeploy endpoint
            redeploy_result = _post_redeploy(
                "gateway", task_id=task_id,
                expected_head=expected_head,
            )
            log.info("[redeploy] gateway: %s", redeploy_result)

            # [legacy] existing restart path
            ok, summary = restart_gateway()
            log.info("[legacy] gateway: success=%s", ok)

            steps["gateway"] = {
                "success": ok,
                "summary": summary,
                "redeploy_result": redeploy_result,
                "legacy_success": ok,
            }

        # R9: For service_manager, governance performs restart via redeploy endpoint
        if "service_manager" in affected:
            redeploy_result = _post_redeploy(
                "service_manager", task_id=task_id,
                expected_head=expected_head,
            )
            log.info("[redeploy] service_manager: %s", redeploy_result)
            steps["service_manager"] = {
                "success": redeploy_result.get("ok", False),
                "redeploy_result": redeploy_result,
            }

        report["steps"] = steps

        # 3. Smoke test — only check affected services (R5)
        smoke = smoke_test(affected_services=affected)
        report["smoke_test"] = smoke

        # R2: Single derivation — success = all steps OK AND smoke_test.all_pass
        all_steps_ok = all(
            step.get("success", False) for step in steps.values()
        )
        report["success"] = all_steps_ok and smoke.get("all_pass", False)

    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        report["success"] = False

    report["finished_at"] = utc_iso()

    # 4. Notify via Telegram if chat_id provided
    if chat_id:
        _notify_telegram(chat_id, report)

    # 5. Persist report
    _save_report(report)

    return report


def _mark_task_succeeded_pre_kill(task_id: str, project_id: str) -> None:
    """Mark a task as SUCCEEDED with redeploy_pending note before executor kill (R6).

    Best-effort: failure here does not block the deploy.
    """
    import urllib.request
    import urllib.error

    url = f"http://localhost:40000/api/task/{project_id or 'aming-claw'}/complete"
    payload = json.dumps({
        "task_id": task_id,
        "status": "succeeded",
        "result": {"note": "redeploy_pending"},
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:
        log.warning("_mark_task_succeeded_pre_kill: %s", exc)


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _log_error(context: str, exc: Any) -> None:
    """Best-effort stderr logging."""
    try:
        print(f"[deploy_chain][{context}] ERROR: {exc}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _save_report(report: dict[str, Any]) -> None:
    """Persist report JSON to state directory."""
    try:
        ts = report.get("started_at", utc_iso()).replace(":", "-").replace(" ", "_")
        path = _state_dir() / f"deploy_report_{ts}.json"
        save_json(path, report)
    except Exception as exc:  # noqa: BLE001
        _log_error("_save_report", exc)


def _notify_telegram(chat_id: int, report: dict[str, Any]) -> None:
    """Send a brief deploy summary to Telegram (best-effort)."""
    try:
        # Import lazily to avoid hard dep when not using Telegram
        from telegram_gateway.bot import send_message  # type: ignore[import]

        status = "✅ Deploy succeeded" if report.get("success") else "❌ Deploy failed"
        services = ", ".join(report.get("affected_services", [])) or "none"
        text = (
            f"{status}\n"
            f"Services: {services}\n"
            f"Finished: {report.get('finished_at', '')}"
        )
        send_message(chat_id, text)
    except Exception as exc:  # noqa: BLE001
        _log_error("_notify_telegram", exc)
