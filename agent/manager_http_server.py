"""Manager HTTP Server — Sidecar HTTP endpoint for ServiceManager.

Exposes POST /api/manager/redeploy/{target} to allow external callers
(e.g. deploy_chain in PR-2) to trigger a governance redeploy via HTTP
instead of inline subprocess manipulation.

PR-1 scope: observable-only. This endpoint exists and is tested, but
nothing in run_deploy or deploy_chain calls it yet.

Binding: 127.0.0.1:40101 (localhost only — not exposed externally).
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MANAGER_HTTP_HOST = "127.0.0.1"
MANAGER_HTTP_PORT = 40101

_HEALTH_CHECK_TIMEOUT = 30  # seconds to wait for governance health endpoint
_HEALTH_CHECK_INTERVAL = 1  # seconds between health polls

# Targets that are explicitly forbidden (mutual-exclusion guard)
_FORBIDDEN_TARGETS = {"service_manager"}

# Targets that are implemented
_VALID_TARGETS = {"governance"}


def _project_root() -> Path:
    """Return the project root directory (parent of agent/)."""
    return Path(__file__).resolve().parent.parent


def _governance_url() -> str:
    return os.getenv("GOVERNANCE_URL", "http://localhost:40000")


def _governance_port() -> int:
    url = _governance_url()
    # Extract port from URL
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.port or 40000
    except Exception:
        return 40000


# ---------------------------------------------------------------------------
# Governance redeploy logic
# ---------------------------------------------------------------------------


def _find_governance_process() -> Optional[int]:
    """Find the PID of the currently running governance process, if any."""
    try:
        if sys.platform == "win32":
            # On Windows, look for python processes listening on governance port
            import ctypes
            # Use tasklist to find governance processes
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            # Simple heuristic: can't reliably find the exact process here,
            # so we rely on the health check approach instead
            return None
        else:
            # On Unix, use lsof or similar
            return None
    except Exception:
        return None


def _stop_governance_process() -> bool:
    """Attempt to stop the currently running governance process.

    Returns True if a process was stopped or none was running.
    """
    gov_url = _governance_url()
    try:
        import requests
        # Try graceful shutdown via API if available
        resp = requests.post(f"{gov_url}/api/shutdown", timeout=5)
        if resp.status_code == 200:
            log.info("manager_http_server: governance shut down via API")
            time.sleep(1)
            return True
    except Exception:
        pass

    # Try to find and kill the process by port
    port = _governance_port()
    try:
        if sys.platform == "win32":
            # Find PID using netstat
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = int(parts[-1])
                    log.info("manager_http_server: killing governance PID %d", pid)
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True, timeout=5
                    )
                    time.sleep(1)
                    return True
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                pid = int(result.stdout.strip().split()[0])
                os.kill(pid, 15)  # SIGTERM
                time.sleep(1)
                return True
    except Exception as exc:
        log.warning("manager_http_server: failed to stop governance: %s", exc)

    return True  # Proceed even if we couldn't confirm stop


def _spawn_governance_process(chain_version: str) -> subprocess.Popen:
    """Spawn a new governance process with correct CWD and PYTHONPATH.

    R3: Sets PYTHONPATH to include the project root directory and
    cwd to the project root, fixing ModuleNotFoundError for 'agent'.
    """
    project_root = _project_root()

    # Build environment with PYTHONPATH fix (R3)
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    project_root_str = str(project_root)
    if project_root_str not in existing_pythonpath:
        env["PYTHONPATH"] = (
            f"{project_root_str}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else project_root_str
        )

    # Governance start command
    governance_cmd = [
        sys.executable, "-m", "agent.governance.server",
    ]

    log.info(
        "manager_http_server: spawning governance with cwd=%s, PYTHONPATH=%s",
        project_root_str,
        env.get("PYTHONPATH", ""),
    )

    proc = subprocess.Popen(
        governance_cmd,
        cwd=project_root_str,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    log.info("manager_http_server: governance spawned (PID %d)", proc.pid)
    return proc


def _wait_for_health(timeout: float = _HEALTH_CHECK_TIMEOUT) -> bool:
    """Poll governance health endpoint until it responds 200 or timeout."""
    gov_url = _governance_url()
    deadline = time.monotonic() + timeout
    import requests as req_lib

    while time.monotonic() < deadline:
        try:
            resp = req_lib.get(f"{gov_url}/api/health", timeout=3)
            if resp.status_code == 200:
                log.info("manager_http_server: governance health check passed")
                return True
        except Exception:
            pass
        time.sleep(_HEALTH_CHECK_INTERVAL)

    log.error("manager_http_server: governance health check timed out after %ds", timeout)
    return False


def _write_chain_version(chain_version: str) -> bool:
    """Write chain_version to governance DB via /api/version-update.

    R5: Only called after successful spawn + health check.
    Writes exactly once; on failure returns False.
    """
    gov_url = _governance_url()
    project_id = os.getenv("EXECUTOR_PROJECT_ID", os.getenv("PROJECT_ID", "aming-claw"))

    try:
        import requests as req_lib
        resp = req_lib.post(
            f"{gov_url}/api/version-update/{project_id}",
            json={
                "chain_version": chain_version,
                "updated_by": "manager-redeploy",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            log.info(
                "manager_http_server: chain_version %s written to DB",
                chain_version,
            )
            return True
        else:
            log.error(
                "manager_http_server: version-update returned %d: %s",
                resp.status_code,
                resp.text,
            )
            return False
    except Exception as exc:
        log.error("manager_http_server: failed to write chain_version: %s", exc)
        return False


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


async def handle_redeploy(request: web.Request) -> web.Response:
    """POST /api/manager/redeploy/{target}

    R2: Mutual-exclusion guard:
      - target=service_manager → 400 (cannot redeploy self)
      - target=governance → perform redeploy
      - any other target → 404

    Request body (JSON):
      {"chain_version": "<short git hash>"}

    Response (JSON):
      {"ok": true/false, "detail": "...", "pid": <int or null>}
    """
    target = request.match_info.get("target", "")

    # R2: Mutual-exclusion guard — refuse to redeploy service_manager
    if target in _FORBIDDEN_TARGETS:
        return web.json_response(
            {
                "ok": False,
                "detail": f"Cannot redeploy target '{target}': mutual-exclusion guard. "
                          "ServiceManager cannot redeploy itself.",
                "error_code": "SELF_REDEPLOY_FORBIDDEN",
            },
            status=400,
        )

    # R2: Unknown target → 404
    if target not in _VALID_TARGETS:
        return web.json_response(
            {
                "ok": False,
                "detail": f"Unknown redeploy target: '{target}'",
                "error_code": "UNKNOWN_TARGET",
            },
            status=404,
        )

    # --- target == "governance" ---
    try:
        body = await request.json()
    except Exception:
        body = {}

    chain_version = body.get("chain_version", "")
    if not chain_version:
        return web.json_response(
            {"ok": False, "detail": "Missing required field: chain_version"},
            status=400,
        )

    log.info(
        "manager_http_server: redeploy governance requested (chain_version=%s)",
        chain_version,
    )

    # Step 1: Stop old governance
    try:
        stopped = await asyncio.get_event_loop().run_in_executor(
            None, _stop_governance_process
        )
        if not stopped:
            log.warning("manager_http_server: could not confirm old governance stopped")
    except Exception as exc:
        log.error("manager_http_server: error stopping governance: %s", exc)

    # Step 2: Spawn new governance with correct CWD/PYTHONPATH (R3)
    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None, _spawn_governance_process, chain_version
        )
    except Exception as exc:
        log.error("manager_http_server: failed to spawn governance: %s", exc)
        # R5: Do NOT write chain_version on failed spawn
        return web.json_response(
            {"ok": False, "detail": f"Failed to spawn governance: {exc}", "pid": None},
            status=500,
        )

    # Step 3: Health-wait
    healthy = await asyncio.get_event_loop().run_in_executor(
        None, _wait_for_health
    )

    if not healthy:
        # R5: Do NOT write chain_version on failed health check
        return web.json_response(
            {
                "ok": False,
                "detail": "Governance spawned but health check failed",
                "pid": proc.pid,
            },
            status=500,
        )

    # Step 4: Write chain_version to DB (R5 — exactly once, only on success)
    version_written = await asyncio.get_event_loop().run_in_executor(
        None, _write_chain_version, chain_version
    )

    if not version_written:
        return web.json_response(
            {
                "ok": False,
                "detail": "Governance running but failed to write chain_version",
                "pid": proc.pid,
            },
            status=500,
        )

    return web.json_response(
        {
            "ok": True,
            "detail": "Governance redeployed successfully",
            "pid": proc.pid,
            "chain_version": chain_version,
        },
        status=200,
    )


# ---------------------------------------------------------------------------
# Application factory + server runner
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    """Create the aiohttp web.Application with the redeploy route."""
    app = web.Application()
    app.router.add_post("/api/manager/redeploy/{target}", handle_redeploy)
    return app


def run_server(host: str = MANAGER_HTTP_HOST, port: int = MANAGER_HTTP_PORT) -> None:
    """Run the HTTP server (blocking). Intended to be called from a thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = create_app()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())

    site = web.TCPSite(runner, host, port)
    log.info("manager_http_server: starting on %s:%d", host, port)
    loop.run_until_complete(site.start())

    try:
        loop.run_forever()
    except Exception as exc:
        log.error("manager_http_server: server loop crashed: %s", exc)
        raise
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    )
    run_server()
