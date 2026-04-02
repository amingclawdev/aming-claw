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
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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

def restart_local_governance(port: int = 40000) -> tuple[bool, str]:
    """Kill and restart governance as a local Python process.

    Fallback when Docker is not available or Docker rebuild fails.
    Returns (success, output_summary).
    """
    import time as _time
    output_lines: list[str] = []

    # Step 1: Find PID listening on the port
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
                _time.sleep(1)
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
            if pids:
                _time.sleep(1)
    except Exception as exc:
        output_lines.append(f"kill step failed: {exc}")

    # Step 2: Restart the governance server
    try:
        repo_root = Path(__file__).resolve().parent.parent
        python_exe = sys.executable or "python"
        proc = subprocess.Popen(
            [python_exe, "-m", "agent.governance.server"],
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        output_lines.append(f"started PID {proc.pid}")
    except Exception as exc:
        output_lines.append(f"start failed: {exc}")
        return False, "\n".join(output_lines)

    # Step 3: Health check
    _time.sleep(3)
    try:
        import requests
        resp = requests.get(f"http://localhost:{port}/api/health", timeout=5)
        if resp.status_code == 200:
            output_lines.append("[health] governance OK")
            return True, "\n".join(output_lines)
        else:
            output_lines.append(f"[health] HTTP {resp.status_code}")
            return False, "\n".join(output_lines)
    except Exception as exc:
        output_lines.append(f"[health] unreachable: {exc}")
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
        Services not in the list are marked ``'skipped'`` and excluded
        from the ``all_pass`` computation.

    Returns::

        {
            'executor':   bool | 'skipped',
            'governance': bool | 'skipped',
            'gateway':    bool | 'skipped',
            'all_pass':   bool,
        }
    """
    all_services = ["executor", "governance", "gateway"]
    results: dict[str, Any] = {svc: False for svc in all_services}
    results["all_pass"] = False

    import time as _time
    _time.sleep(5)  # Brief pause to let services stabilize after restarts

    # Mark services not in affected_services as 'skipped'
    if affected_services is not None:
        for svc in all_services:
            if svc not in affected_services:
                results[svc] = "skipped"

    # --- executor ---
    if results["executor"] != "skipped":
        try:
            import requests
            resp = requests.get("http://localhost:40100/status", timeout=5)
            results["executor"] = resp.status_code == 200
        except Exception:  # noqa: BLE001
            results["executor"] = _executor_health_from_state()

    # --- governance ---
    if results["governance"] != "skipped":
        try:
            import requests
            resp = requests.get("http://localhost:40000/api/health", timeout=5)
            results["governance"] = resp.status_code == 200
        except Exception:  # noqa: BLE001
            results["governance"] = False

    # --- gateway (docker inspect) ---
    if results["gateway"] != "skipped":
        try:
            insp = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}",
                 "aming_claw-telegram-gateway-1"],
                capture_output=True, text=True, timeout=10,
            )
            results["gateway"] = insp.stdout.strip().lower() == "true"
        except Exception:  # noqa: BLE001
            results["gateway"] = False

    # all_pass only considers non-skipped services
    checked = [results[k] for k in all_services if results[k] != "skipped"]
    results["all_pass"] = all(checked) if checked else True
    return results


# ---------------------------------------------------------------------------
# 6. run_deploy
# ---------------------------------------------------------------------------

def run_deploy(changed_files: list[str], chat_id: int = 0, project_id: str = "",
               skip_services: list[str] = None) -> dict[str, Any]:
    """Full deploy orchestration.

    Steps:
    1. Detect affected services from *changed_files*.
    2. Restart each affected service.
    3. Run smoke test.
    4. Optionally notify via Telegram if *chat_id* is non-zero.
    5. Persist a report to state/deploy_report_<ts>.json.

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

        # 2. Restart each service
        steps: dict[str, Any] = {}

        if "executor" in affected:
            ok = restart_executor()
            steps["executor"] = {"success": ok}

        if "governance" in affected:
            ok, summary = rebuild_governance()
            if not ok:
                # Docker rebuild failed — try local process restart
                ok2, summary2 = restart_local_governance()
                if ok2:
                    ok, summary = ok2, f"docker failed ({summary}), local restart OK: {summary2}"
                else:
                    summary = f"docker: {summary} | local: {summary2}"
            steps["governance"] = {"success": ok, "summary": summary}

        if "gateway" in affected:
            ok, summary = restart_gateway()
            steps["gateway"] = {"success": ok, "summary": summary}

        report["steps"] = steps

        # 3. Smoke test — only check affected services (R5)
        smoke = smoke_test(affected_services=affected)
        report["smoke_test"] = smoke

        # R6: Overall success = all step-level successes AND smoke_test.all_pass
        all_steps_ok = all(
            step.get("success", False) for step in steps.values()
        )
        report["success"] = all_steps_ok and smoke.get("all_pass", False)

        # Post-condition coherence invariant (R1): force success=False if
        # smoke_test.all_pass is False, regardless of how success was computed.
        # This is a defense-in-depth assertion at the result-construction layer.
        if smoke.get("all_pass") is False and report["success"]:
            report["success"] = False  # coherence invariant: success must agree with all_pass
            report["coherence_override"] = True

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
