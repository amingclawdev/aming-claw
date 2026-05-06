"""Manager HTTP Server — Sidecar HTTP endpoint for ServiceManager.

Exposes POST /api/manager/redeploy/{target} to allow external callers
(e.g. auto_chain deploy finalization) to trigger a governance redeploy
via HTTP instead of inline subprocess manipulation.

Binding: 127.0.0.1:40101 (localhost only — not exposed externally).

PR-1 refactor: Uses stdlib ThreadingHTTPServer + BaseHTTPRequestHandler
(dropped aiohttp dependency) for consistency with agent/governance/server.py.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

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
    try:
        parsed = urlparse(url)
        return parsed.port or 40000
    except Exception:
        return 40000


def _governance_log_paths(chain_version: str) -> tuple[Path, Path]:
    """Return durable stdout/stderr log paths for a spawned governance process."""
    project_root = _project_root()
    shared_root = Path(os.getenv("SHARED_VOLUME_PATH", str(project_root / "shared-volume")))
    log_dir = shared_root / "codex-tasks" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    safe_version = "".join(
        ch for ch in (chain_version or "unknown") if ch.isalnum() or ch in ("-", "_")
    )[:32] or "unknown"
    prefix = f"governance-redeploy-{_governance_port()}-{safe_version}-{stamp}"
    return log_dir / f"{prefix}.out.log", log_dir / f"{prefix}.err.log"


# ---------------------------------------------------------------------------
# Governance redeploy logic
# ---------------------------------------------------------------------------


def _find_governance_process() -> Optional[int]:
    """Find the PID of the currently running governance process, if any."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            return None
        else:
            return None
    except Exception:
        return None


def _stop_governance_process() -> bool:
    """Attempt to stop the currently running governance process.

    Probes existing governance, sends SIGTERM with 5s timeout, then SIGKILL
    if still alive.

    Returns True if a process was stopped or none was running.
    """
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
                    log.info("manager_http_server: sending SIGTERM to governance PID %d", pid)
                    # SIGTERM equivalent on Windows
                    subprocess.run(
                        ["taskkill", "/PID", str(pid)],
                        capture_output=True, timeout=5
                    )
                    # Wait up to 5s for graceful exit
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        check = subprocess.run(
                            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                            capture_output=True, text=True, timeout=3
                        )
                        if str(pid) not in check.stdout:
                            log.info("manager_http_server: governance PID %d exited", pid)
                            return True
                        time.sleep(0.5)
                    # Still alive → SIGKILL
                    log.warning("manager_http_server: governance PID %d still alive after 5s, force-killing", pid)
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
                os.kill(pid, signal.SIGTERM)
                # Wait up to 5s
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)  # probe: still alive?
                    except OSError:
                        log.info("manager_http_server: governance PID %d exited after SIGTERM", pid)
                        return True
                    time.sleep(0.5)
                # Still alive → SIGKILL
                log.warning("manager_http_server: governance PID %d still alive after 5s, sending SIGKILL", pid)
                os.kill(pid, signal.SIGKILL)
                time.sleep(1)
                return True
    except Exception as exc:
        log.warning("manager_http_server: failed to stop governance: %s", exc)

    return True  # Proceed even if we couldn't confirm stop


def _spawn_governance_process(chain_version: str) -> subprocess.Popen:
    """Spawn a new governance process with correct CWD and PYTHONPATH."""
    project_root = _project_root()
    stdout_log, stderr_log = _governance_log_paths(chain_version)

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    project_root_str = str(project_root)
    if project_root_str not in existing_pythonpath:
        env["PYTHONPATH"] = (
            f"{project_root_str}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else project_root_str
        )
    env["GOVERNANCE_STDOUT_LOG"] = str(stdout_log)
    env["GOVERNANCE_STDERR_LOG"] = str(stderr_log)

    # The bundled Windows Python uses python312._pth, where "." resolves to the
    # runtime directory, not the process cwd.  Running with "-m agent..." can
    # therefore fail to import the repo package even when PYTHONPATH is set.
    # start_governance.py inserts the project root into sys.path before importing
    # the server and is the same host-first entrypoint used by the ops script.
    governance_cmd = [
        sys.executable, str(project_root / "start_governance.py"),
    ]

    log.info(
        "manager_http_server: spawning governance with cwd=%s, PYTHONPATH=%s, stdout=%s, stderr=%s",
        project_root_str,
        env.get("PYTHONPATH", ""),
        stdout_log,
        stderr_log,
    )

    stdout_handle = stdout_log.open("ab", buffering=0)
    stderr_handle = stderr_log.open("ab", buffering=0)
    try:
        proc = subprocess.Popen(
            governance_cmd,
            cwd=project_root_str,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()

    log.info("manager_http_server: governance spawned (PID %d)", proc.pid)
    return proc


def _wait_for_health(timeout: float = _HEALTH_CHECK_TIMEOUT) -> bool:
    """Poll governance health endpoint until it responds 200 or timeout."""
    import urllib.request
    import urllib.error

    gov_url = _governance_url()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"{gov_url}/api/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    log.info("manager_http_server: governance health check passed")
                    return True
        except Exception:
            pass
        time.sleep(_HEALTH_CHECK_INTERVAL)

    log.error("manager_http_server: governance health check timed out after %ds", timeout)
    return False


def _write_chain_version(chain_version: str) -> bool:
    """Write chain_version to governance DB via /api/version-update.

    Only called after successful spawn + health check.
    Writes exactly once with updated_by='manager-redeploy'.
    """
    import urllib.request
    import urllib.error

    gov_url = _governance_url()
    project_id = os.getenv("EXECUTOR_PROJECT_ID", os.getenv("PROJECT_ID", "aming-claw"))
    url = f"{gov_url}/api/version-update/{project_id}"
    data = json.dumps({
        "chain_version": chain_version,
        "updated_by": "manager-redeploy",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status < 300:
                log.info(
                    "manager_http_server: chain_version %s written to DB",
                    chain_version,
                )
                return True
            log.error(
                "manager_http_server: version-update returned %d",
                resp.status,
            )
            return False
    except Exception as exc:
        log.error("manager_http_server: failed to write chain_version: %s", exc)
        return False


# ---------------------------------------------------------------------------
# HTTP handler (stdlib BaseHTTPRequestHandler)
# ---------------------------------------------------------------------------


class ManagerHTTPHandler(BaseHTTPRequestHandler):
    """Handler for /api/manager/redeploy/{target} endpoint."""

    def log_message(self, format, *args):
        """Route http.server logs through our logger instead of stderr."""
        log.debug("manager_http_server: %s", format % args)

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_POST(self):
        """Route POST requests."""
        path = self.path.rstrip("/")

        # Match /api/manager/respawn-executor
        if path == "/api/manager/respawn-executor":
            self._handle_respawn_executor()
            return

        # Match /api/manager/redeploy/{target}
        prefix = "/api/manager/redeploy/"
        if path.startswith(prefix):
            target = path[len(prefix):]
            self._handle_redeploy(target)
            return

        self._send_json({"ok": False, "detail": "Not found"}, 404)

    def do_GET(self):
        """Health endpoint for the manager HTTP server itself."""
        if self.path.rstrip("/") == "/api/manager/health":
            runtime_version = ""
            try:
                from agent.governance.chain_trailer import get_runtime_version
                runtime_version = get_runtime_version()
            except Exception:
                pass
            self._send_json({"ok": True, "service": "manager_http_server",
                             "runtime_version": runtime_version})
            return
        self._send_json({"ok": False, "detail": "Not found"}, 404)

    def _handle_respawn_executor(self) -> None:
        """POST /api/manager/respawn-executor — write manager_signal.json."""
        body = self._read_json_body()
        try:
            state_dir = _project_root() / "shared-volume" / "codex-tasks" / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            sig = {
                "action": "restart",
                "requested_action": "respawn_executor",
                "chain_version": body.get("chain_version", ""),
                "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            (state_dir / "manager_signal.json").write_text(json.dumps(sig), encoding="utf-8")
            self._send_json({"ok": True, "detail": "signal written"})
        except Exception as exc:
            self._send_json({"ok": False, "detail": str(exc)}, 500)

    def _handle_redeploy(self, target: str) -> None:
        """POST /api/manager/redeploy/{target}

        Mutual-exclusion guard:
          - target=service_manager → 400 (cannot redeploy self)
          - target=governance → perform redeploy
          - any other target → 404

        Request body (JSON):
          {"chain_version": "<short git hash>"}

        Response (JSON):
          {"ok": true/false, "detail": "...", "pid": <int or null>}
        """
        # Mutual-exclusion guard — refuse to redeploy service_manager
        if target in _FORBIDDEN_TARGETS:
            self._send_json(
                {
                    "ok": False,
                    "detail": f"cannot redeploy self",
                    "error_code": "SELF_REDEPLOY_FORBIDDEN",
                },
                400,
            )
            return

        # Unknown target → 404
        if target not in _VALID_TARGETS:
            self._send_json(
                {
                    "ok": False,
                    "detail": f"Unknown redeploy target: '{target}'",
                    "error_code": "UNKNOWN_TARGET",
                },
                404,
            )
            return

        # --- target == "governance" ---
        body = self._read_json_body()
        chain_version = body.get("chain_version", "")
        if not chain_version:
            self._send_json(
                {"ok": False, "detail": "Missing required field: chain_version"},
                400,
            )
            return

        log.info(
            "manager_http_server: redeploy governance requested (chain_version=%s)",
            chain_version,
        )

        # Step 1: Stop old governance (probe → SIGTERM 5s → SIGKILL)
        try:
            stopped = _stop_governance_process()
            if not stopped:
                log.warning("manager_http_server: could not confirm old governance stopped")
        except Exception as exc:
            log.error("manager_http_server: error stopping governance: %s", exc)

        # Step 2: Spawn new governance with correct CWD/PYTHONPATH
        try:
            proc = _spawn_governance_process(chain_version)
        except Exception as exc:
            log.error("manager_http_server: failed to spawn governance: %s", exc)
            self._send_json(
                {"ok": False, "detail": f"Failed to spawn governance: {exc}", "pid": None},
                500,
            )
            return

        # Step 3: Health-poll /api/health up to 30s
        healthy = _wait_for_health()

        if not healthy:
            self._send_json(
                {
                    "ok": False,
                    "detail": "Governance spawned but health check failed",
                    "pid": proc.pid,
                },
                500,
            )
            return

        # Step 4: Write chain_version to DB (only on success)
        version_written = _write_chain_version(chain_version)

        if not version_written:
            self._send_json(
                {
                    "ok": False,
                    "detail": "Governance running but failed to write chain_version",
                    "pid": proc.pid,
                },
                500,
            )
            return

        self._send_json(
            {
                "ok": True,
                "detail": "Governance redeployed successfully",
                "pid": proc.pid,
                "chain_version": chain_version,
            },
            200,
        )


# ---------------------------------------------------------------------------
# Server factory + runner
# ---------------------------------------------------------------------------


def create_server(
    host: str = MANAGER_HTTP_HOST,
    port: int = MANAGER_HTTP_PORT,
) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer bound to host:port."""
    server = ThreadingHTTPServer((host, port), ManagerHTTPHandler)
    return server


def run_server(host: str = MANAGER_HTTP_HOST, port: int = MANAGER_HTTP_PORT) -> None:
    """Run the HTTP server (blocking). Intended to be called from a thread.

    Contract: service_manager.py imports and calls this function from its
    sidecar thread. Signature must remain (host, port) -> None.
    """
    server = create_server(host, port)
    log.info("manager_http_server: starting on %s:%d", host, port)
    try:
        server.serve_forever()
    except Exception as exc:
        log.error("manager_http_server: server loop crashed: %s", exc)
        raise
    finally:
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    )
    run_server()
