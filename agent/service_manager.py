"""Service Manager — Lifecycle management for the Executor subprocess.

Provides start/stop/reload and status inspection for the governance Executor process.
Intended for use by the bot layer (e.g. Telegram bot) for operational control.

Public interfaces
-----------------
start()                     — spawn the executor process
stop()                      — terminate the executor process
reload(callback=None)       — graceful restart: waits for active tasks to finish
                              (timeout 120 s), then stop→start; fires *callback* when done
status() -> dict            — structured snapshot: PID, uptime_s, active_tasks, queued_tasks

Design notes
------------
* The executor is launched as a child subprocess via ``subprocess.Popen``.
* Active / queued task counts are obtained by querying the Governance API
  (same endpoint the executor worker itself uses).
* ``reload()`` blocks the *calling* thread but does not hold the GIL; it polls
  the API on a configurable interval and respects the 120 s timeout.
* A reload callback is called in the *same* thread after the new process has
  been confirmed running, so it can safely call ``send_text`` / Telegram helpers.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import signal
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# B48 FIX B (observer-hotfix 2026-04-23): Ensure the project root is on
# sys.path so `from agent.manager_http_server import run_server` works when
# this file is invoked as `python agent/service_manager.py`. Without this,
# the sidecar thread raises ImportError: No module named 'agent', sets
# _sidecar_crashed=True and _running=False, which silently kills the monitor
# loop. Workers then die with no respawn (root cause of B48). See
# docs/dev/b48-investigation-and-fix-proposal.md §1.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    import requests  # noqa: F401 — imported here so tests can patch service_manager.requests.get
except ImportError:  # pragma: no cover — requests may be absent in minimal test envs
    requests = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults (all overridable via environment variables)
# ---------------------------------------------------------------------------

_RELOAD_TIMEOUT: int = int(os.getenv("SERVICE_RELOAD_TIMEOUT", "120"))
_POLL_INTERVAL: float = float(os.getenv("SERVICE_POLL_INTERVAL", "2"))

_agent_dir = str(Path(__file__).resolve().parent)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_env_file(env_path: Optional[Path] = None) -> None:
    """Load a simple KEY=VALUE .env file into the process environment."""
    target = env_path or (_repo_root() / ".env")
    if not target.exists():
        return
    try:
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
    except Exception as exc:
        log.debug("ServiceManager: failed to load %s: %s", target, exc)


def _default_governance_url() -> str:
    return os.getenv("GOVERNANCE_URL", "http://localhost:40000")


def _default_project_id() -> str:
    return os.getenv("EXECUTOR_PROJECT_ID", os.getenv("PROJECT_ID", "aming-claw"))


def _default_workspace() -> str:
    return os.getenv("CODEX_WORKSPACE", str(_repo_root()))


def _default_executor_cmd(project_id: str, governance_url: str, workspace: str) -> list[str]:
    # NOTE: Uses script-path form (NOT -m module form) because embedded Python
    # runtime has restrictive python312._pth that doesn't include project root.
    # Module form fails with "No module named 'agent'" before executor_worker.py
    # has a chance to bootstrap its sys.path. Script-path works because
    # executor_worker.py's top-level _proj_root sys.path.insert handles the
    # `from agent.governance.X import Y` imports it needs internally.
    return [
        sys.executable,
        str(Path(__file__).resolve().parent / "executor_worker.py"),
        "--project",
        project_id,
        "--url",
        governance_url,
        "--workspace",
        workspace,
    ]


def _shared_log_dir() -> Path:
    return Path(os.getenv("SHARED_VOLUME_PATH", str(_repo_root() / "shared-volume"))) / "codex-tasks" / "logs"


def _signal_file_path() -> Path:
    """Path to the manager restart signal file (manager_signal.json)."""
    return Path(os.getenv("SHARED_VOLUME_PATH", str(_repo_root() / "shared-volume"))) / "codex-tasks" / "state" / "manager_signal.json"


# ---------------------------------------------------------------------------
# ServiceManager
# ---------------------------------------------------------------------------


class ServiceManager:
    """Manages the lifecycle of the Executor subprocess.

    Args:
        project_id: Governance project identifier used when querying task counts.
        governance_url: Base URL of the Governance HTTP API.
        executor_cmd: Command list passed to ``subprocess.Popen``. Defaults to
            ``[sys.executable, "-m", "agent.executor_worker", "--project", <project_id>]``.
        reload_timeout: Seconds to wait for active tasks to drain before a reload
            forcefully proceeds. Default: 120.
        poll_interval: Seconds between active-task polls during reload. Default: 2.
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        governance_url: Optional[str] = None,
        executor_cmd: Optional[list] = None,
        reload_timeout: int = _RELOAD_TIMEOUT,
        poll_interval: float = _POLL_INTERVAL,
        workspace: Optional[str] = None,
    ) -> None:
        self.project_id = project_id or _default_project_id()
        self.governance_url = (governance_url or _default_governance_url()).rstrip("/")
        self.reload_timeout = reload_timeout
        self.poll_interval = poll_interval
        self.workspace = workspace or _default_workspace()

        self._executor_cmd: list = executor_cmd or _default_executor_cmd(
            self.project_id,
            self.governance_url,
            self.workspace,
        )

        self._process: Optional[subprocess.Popen] = None
        self._start_time: Optional[float] = None
        self._lock = threading.Lock()

        # Monitor-thread state
        self._running: bool = False
        self._monitor_thread: Optional[threading.Thread] = None
        self.restart_count: int = 0
        self.last_crash_at: Optional[float] = None   # wall-clock (time.time())
        self._restart_times: list = []               # monotonic timestamps for circuit-breaker window
        self._circuit_breaker_tripped: bool = False

        # R10: Worker pool awareness — track external worker threads
        self._worker_pool = None  # Set by executor when WorkerPool is initialized

        # Sidecar HTTP server (manager_http_server) state
        self._sidecar_thread: Optional[threading.Thread] = None
        self._sidecar_crashed: bool = False

    # ------------------------------------------------------------------
    # start / stop
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Spawn the executor subprocess if it is not already running.

        Returns:
            ``True`` if a new process was started, ``False`` if one was already
            alive.
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                log.info("ServiceManager.start: executor already running (PID %d)", self._process.pid)
                # Ensure monitor thread is running even if process was already up
                self._ensure_monitor_running()
                return False

            log.info("ServiceManager.start: launching executor %s", self._executor_cmd)
            self._process = self._spawn_executor_process()
            self._start_time = time.monotonic()
            log.info("ServiceManager.start: executor started (PID %d)", self._process.pid)

        # Start monitor thread outside the lock to avoid re-entrant lock issues
        self._ensure_monitor_running()
        return True

    def stop(self) -> bool:
        """Terminate the executor subprocess gracefully (SIGTERM, then SIGKILL after 5 s).

        Returns:
            ``True`` if a running process was stopped, ``False`` if none was
            running.
        """
        self._running = False  # Signal monitor loop to exit
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> bool:
        """Internal stop — caller must hold ``self._lock``."""
        proc = self._process
        if proc is None or proc.poll() is not None:
            log.info("ServiceManager.stop: no running executor to stop")
            self._process = None
            self._start_time = None
            return False

        log.info("ServiceManager.stop: terminating executor (PID %d)", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("ServiceManager.stop: executor did not exit after SIGTERM; sending SIGKILL")
            proc.kill()
            proc.wait(timeout=5)

        self._process = None
        self._start_time = None
        log.info("ServiceManager.stop: executor stopped")
        return True

    # ------------------------------------------------------------------
    # reload
    # ------------------------------------------------------------------

    def reload(self, callback: Optional[Callable[[dict], None]] = None) -> dict:
        """Gracefully restart the executor.

        Workflow:
        1. Poll Governance API until ``active_tasks == 0`` *or* *reload_timeout*
           seconds elapse (whichever comes first).
        2. Stop the current executor process.
        3. Start a new executor process.
        4. Call *callback(status_dict)* if provided.

        Args:
            callback: Optional callable invoked after the new process is running.
                Receives the result of :meth:`status` as its sole argument.  Use
                this hook to send a Telegram notification, for example.

        Returns:
            A dict describing the outcome::

                {
                    "success": True,
                    "waited_s": 12.4,
                    "timed_out": False,
                    "pid": 12345,
                }
        """
        log.info("ServiceManager.reload: initiating graceful reload (timeout=%ds)", self.reload_timeout)

        waited = 0.0
        timed_out = False

        # --- Phase 1: drain active tasks ---
        deadline = time.monotonic() + self.reload_timeout
        while True:
            active = self._get_active_task_count()
            if active == 0:
                log.info("ServiceManager.reload: active_tasks=0, proceeding immediately")
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "ServiceManager.reload: timeout reached (%ds); proceeding despite active_tasks=%d",
                    self.reload_timeout, active,
                )
                timed_out = True
                break
            sleep_for = min(self.poll_interval, remaining)
            log.debug("ServiceManager.reload: active_tasks=%d, waiting %.1fs …", active, sleep_for)
            time.sleep(sleep_for)
            waited = time.monotonic() - (deadline - self.reload_timeout)

        if not timed_out:
            waited = time.monotonic() - (deadline - self.reload_timeout)

        # --- Phase 2: stop then start ---
        with self._lock:
            self._stop_locked()
            self._process = self._spawn_executor_process()
            self._start_time = time.monotonic()
            new_pid = self._process.pid
            log.info("ServiceManager.reload: executor restarted (PID %d)", new_pid)

        result = {
            "success": True,
            "waited_s": round(waited, 2),
            "timed_out": timed_out,
            "pid": new_pid,
        }

        # --- Phase 3: fire callback ---
        if callback is not None:
            try:
                callback(self.status())
            except Exception as exc:
                log.error("ServiceManager.reload: callback raised %s", exc)

        return result

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a structured snapshot of the service state.

        Returns:
            A dict with the following keys:

            * ``pid``          — int or ``None`` if not running
            * ``running``      — bool
            * ``uptime_s``     — float seconds since last start, or ``None``
            * ``active_tasks`` — int queried from Governance API
            * ``queued_tasks`` — int queried from Governance API
        """
        with self._lock:
            proc = self._process
            if proc is not None and proc.poll() is not None:
                # Process has exited since we last checked — clean up references.
                self._process = None
                self._start_time = None
                proc = None

            pid = proc.pid if proc is not None else None
            running = proc is not None
            uptime_s = (
                round(time.monotonic() - self._start_time, 2)
                if self._start_time is not None and running
                else None
            )

        active, queued = self._get_task_counts()

        # Determine health state
        if self._circuit_breaker_tripped:
            health = "crash_loop"
        elif self.restart_count > 0:
            health = "degraded"
        else:
            health = "healthy"

        result = {
            "pid": pid,
            "running": running,
            "uptime_s": uptime_s,
            "active_tasks": active,
            "queued_tasks": queued,
            "restart_count": self.restart_count,
            "last_crash_at": self.last_crash_at,
            "health": health,
            "circuit_breaker": self._circuit_breaker_tripped,
        }
        # R10: Include worker pool status if available
        pool_status = self._worker_pool_status()
        if pool_status:
            result.update(pool_status)
        return result

    def set_worker_pool(self, pool) -> None:
        """R10: Register a WorkerPool instance for lifecycle monitoring."""
        self._worker_pool = pool

    def _worker_pool_status(self) -> dict:
        """R10: Get worker pool status if pool is registered."""
        if self._worker_pool and hasattr(self._worker_pool, 'status'):
            return self._worker_pool.status()
        return {}

    # ------------------------------------------------------------------
    # Sidecar HTTP server lifecycle (R4)
    # ------------------------------------------------------------------

    def _start_sidecar(self) -> None:
        """Start the manager_http_server sidecar in a background thread.

        R4: The sidecar runs in its own thread with its own asyncio event loop.
        If the sidecar crashes, it sets _sidecar_crashed=True which causes
        the main ServiceManager monitor loop to stop (crash-together semantics).
        """
        if self._sidecar_thread is not None and self._sidecar_thread.is_alive():
            log.info("ServiceManager: sidecar already running")
            return

        self._sidecar_crashed = False

        def _sidecar_runner():
            """Thread target: run the manager_http_server; on crash, log and degrade.

            B48 FIX B (observer-hotfix 2026-04-23): Previously a sidecar crash
            set ``self._running = False`` which killed the monitor loop and
            caused workers to never be respawned. Now a sidecar crash is
            treated as a non-fatal degradation: executor supervision
            continues uninterrupted. The ``_sidecar_crashed`` flag is still
            set for external inspection (e.g. health endpoints), but the
            monitor loop no longer self-terminates.
            """
            try:
                from agent.manager_http_server import run_server
                log.info("ServiceManager: sidecar thread starting manager_http_server")
                run_server()
            except Exception as exc:
                log.error(
                    "ServiceManager: sidecar crashed (non-fatal, monitor loop continues): %s",
                    exc,
                    exc_info=True,
                )
                self._sidecar_crashed = True
                # B48 FIX B: do NOT set self._running = False here. Keep monitor
                # alive so workers continue to be supervised / respawned.
                # Sidecar is best-effort HTTP control-plane; executor lifecycle
                # is the primary responsibility.

        self._sidecar_thread = threading.Thread(
            target=_sidecar_runner,
            name="manager-http-sidecar",
            daemon=True,
        )
        self._sidecar_thread.start()
        log.info("ServiceManager: sidecar thread started (manager_http_server on port 40101)")

    # ------------------------------------------------------------------
    # Monitor loop
    # ------------------------------------------------------------------

    _CIRCUIT_BREAKER_MAX = 5
    _CIRCUIT_BREAKER_WINDOW = 300  # seconds

    def _ensure_monitor_running(self) -> None:
        """Start monitor thread if it is not already alive."""
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="executor-monitor",
                daemon=True,
            )
            self._monitor_thread.start()
            log.info("ServiceManager: monitor thread started")

    def _monitor_loop(self) -> None:
        """Background thread: checks every 10 s if executor is alive; restarts if dead.

        Circuit breaker: if the executor has been restarted
        ``_CIRCUIT_BREAKER_MAX`` or more times within the last
        ``_CIRCUIT_BREAKER_WINDOW`` seconds, stop trying and log an error.
        """
        while self._running:
            time.sleep(10)
            if not self._running:
                break

            # B48 FIX B (observer-hotfix 2026-04-23): Previously crash-together
            # semantics — any sidecar exception would stop monitor loop, leaving
            # workers un-supervised. Now sidecar crash is non-fatal; log once
            # then continue supervising the executor. Sidecar is best-effort
            # HTTP control-plane; executor lifecycle is the primary job.
            if self._sidecar_crashed and not getattr(self, "_sidecar_warning_logged", False):
                log.warning(
                    "ServiceManager._monitor_loop: sidecar is down (non-fatal); "
                    "continuing executor supervision"
                )
                self._sidecar_warning_logged = True

            # R1: Check for restart signal on each cycle
            self._check_restart_signal()

            with self._lock:
                proc = self._process
                if proc is None:
                    # Nothing to monitor
                    if self._circuit_breaker_tripped:
                        log.error(
                            "ServiceManager._monitor_loop: circuit breaker is tripped; "
                            "executor will not be restarted automatically"
                        )
                    continue

                if proc.poll() is None:
                    # Still alive — also check worker threads (R10)
                    if self._worker_pool and hasattr(self._worker_pool, 'active_worker_count'):
                        try:
                            worker_count = self._worker_pool.active_worker_count()
                            if worker_count > 0:
                                log.debug(
                                    "ServiceManager._monitor_loop: %d active worker thread(s)",
                                    worker_count,
                                )
                        except Exception:
                            pass
                    continue

                # ---- Process has died ----
                if self._circuit_breaker_tripped:
                    log.error(
                        "ServiceManager._monitor_loop: executor died but circuit breaker is tripped; "
                        "will not restart"
                    )
                    self._process = None
                    self._start_time = None
                    continue

                dead_pid = proc.pid
                log.warning(
                    "ServiceManager._monitor_loop: executor process (PID %d) died; "
                    "preparing restart …",
                    dead_pid,
                )
                self.last_crash_at = time.time()
                self._process = None
                self._start_time = None

            # Clear pycache outside the lock (I/O operation)
            self._clear_pycache()

            with self._lock:
                # Update circuit-breaker state
                now = time.monotonic()
                self._restart_times.append(now)
                self._restart_times = [
                    t for t in self._restart_times
                    if now - t < self._CIRCUIT_BREAKER_WINDOW
                ]
                self.restart_count = len(self._restart_times)

                if self.restart_count >= self._CIRCUIT_BREAKER_MAX:
                    log.error(
                        "ServiceManager._monitor_loop: circuit breaker tripped — %d restarts "
                        "within %ds; stopping automatic restarts",
                        self._CIRCUIT_BREAKER_MAX,
                        self._CIRCUIT_BREAKER_WINDOW,
                    )
                    self._circuit_breaker_tripped = True
                    continue

                # Restart the executor
                log.info(
                    "ServiceManager._monitor_loop: restarting executor (restart #%d of %d)",
                    self.restart_count,
                    self._CIRCUIT_BREAKER_MAX - 1,
                )
                try:
                    self._process = self._spawn_executor_process()
                    self._start_time = time.monotonic()
                    log.info(
                        "ServiceManager._monitor_loop: executor restarted (PID %d)",
                        self._process.pid,
                    )
                except Exception as exc:
                    log.error(
                        "ServiceManager._monitor_loop: failed to restart executor: %s", exc
                    )

    def _check_restart_signal(self) -> None:
        """Read manager_signal.json; if action==restart, stop+start executor (R1-R5).

        * Missing file → no-op (R4/AC5).
        * Malformed JSON → log warning, delete corrupt file (R4/AC6).
        * Valid restart signal → stop current executor, start fresh one, delete
          signal file.  Does NOT increment circuit breaker (R5).
        """
        signal_path = _signal_file_path()
        if not signal_path.exists():
            return

        # Read and parse the signal file
        try:
            raw = signal_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "ServiceManager._check_restart_signal: malformed signal file (%s); deleting",
                exc,
            )
            try:
                signal_path.unlink()
            except OSError:
                pass
            return
        except OSError as exc:
            log.debug("ServiceManager._check_restart_signal: cannot read signal file: %s", exc)
            return

        action = data.get("action") if isinstance(data, dict) else None
        if action != "restart":
            log.debug(
                "ServiceManager._check_restart_signal: unknown action %r; ignoring", action
            )
            return

        active_tasks = self._get_active_task_count()
        if active_tasks > 0:
            log.info(
                "ServiceManager._check_restart_signal: deferring restart; %d active task(s)",
                active_tasks,
            )
            return

        # R2: Perform intentional restart (R5: do NOT count toward circuit breaker)
        log.info("ServiceManager._check_restart_signal: restart signal received; restarting executor")
        with self._lock:
            self._stop_locked()
            try:
                self._process = self._spawn_executor_process()
                self._start_time = time.monotonic()
                log.info(
                    "ServiceManager._check_restart_signal: executor restarted (PID %d)",
                    self._process.pid,
                )
            except Exception as exc:
                log.error(
                    "ServiceManager._check_restart_signal: failed to restart executor: %s", exc
                )

        # R3: Delete signal file after consumption
        try:
            signal_path.unlink()
        except OSError:
            pass

    def _clear_pycache(self) -> None:
        """Walk the workspace and remove every ``__pycache__`` directory found."""
        workspace = str(Path(__file__).resolve().parent.parent)
        removed = 0
        for dirpath, dirnames, _ in os.walk(workspace):
            if "__pycache__" in dirnames:
                cache_path = os.path.join(dirpath, "__pycache__")
                try:
                    shutil.rmtree(cache_path)
                    removed += 1
                    # Prevent os.walk from descending into the (now-deleted) dir
                    dirnames.remove("__pycache__")
                except Exception as exc:
                    log.debug(
                        "ServiceManager._clear_pycache: could not remove %s: %s",
                        cache_path, exc,
                    )
        if removed:
            log.info("ServiceManager._clear_pycache: removed %d __pycache__ dir(s)", removed)

    def _spawn_executor_process(self) -> subprocess.Popen:
        """Spawn the executor and redirect output to a persistent host log file."""
        log_dir = _shared_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / f"service-manager-executor-{self.project_id}.log"
        stderr_path = log_dir / f"service-manager-executor-{self.project_id}.err.log"
        stdout_handle = open(stdout_path, "ab")
        stderr_handle = open(stderr_path, "ab")
        try:
            return subprocess.Popen(
                self._executor_cmd,
                stdout=stdout_handle,
                stderr=stderr_handle,
                cwd=str(_repo_root()),
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_active_task_count(self) -> int:
        """Return the number of currently active (claimed/processing) tasks."""
        active, _ = self._get_task_counts()
        return active

    def _get_task_counts(self) -> tuple[int, int]:
        """Query the Governance API and return (active_count, queued_count).

        Returns ``(0, 0)`` on any network or parse error so that callers degrade
        gracefully.
        """
        try:
            url = f"{self.governance_url}/api/task/{self.project_id}/list"
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            tasks: list = data.get("tasks", [])

            active = sum(1 for t in tasks if t.get("status") in ("claimed", "processing", "running"))
            queued = sum(1 for t in tasks if t.get("status") in ("queued", "pending"))
            return active, queued

        except Exception as exc:
            log.debug("ServiceManager._get_task_counts: API error — %s", exc)
            return 0, 0


# ---------------------------------------------------------------------------
# Module-level singleton convenience
# ---------------------------------------------------------------------------

_default_manager: Optional[ServiceManager] = None
_default_lock = threading.Lock()


def get_manager(
    project_id: Optional[str] = None,
    governance_url: Optional[str] = None,
    workspace: Optional[str] = None,
) -> ServiceManager:
    """Return the module-level singleton :class:`ServiceManager`.

    Creates it on first call with the supplied parameters.  Subsequent calls
    return the same instance regardless of parameters.
    """
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = ServiceManager(
                project_id=project_id,
                governance_url=governance_url,
                workspace=workspace,
            )
    return _default_manager


def _install_signal_handlers(stop_fn: Callable[[], None]) -> None:
    """Stop gracefully when the host process receives termination signals."""
    def _handler(signum, frame):  # pragma: no cover - exercised by manual host runs
        log.info("ServiceManager host process received signal %s, stopping", signum)
        stop_fn()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            signal.signal(sig, _handler)


def main() -> None:
    import argparse

    _load_env_file()

    parser = argparse.ArgumentParser(
        description="Host-side ServiceManager that supervises agent.executor_worker",
    )
    parser.add_argument("--project", default=_default_project_id(), help="Project ID")
    parser.add_argument(
        "--governance-url",
        default=_default_governance_url(),
        help="Governance base URL (use nginx entrypoint, e.g. http://localhost:40000)",
    )
    parser.add_argument(
        "--workspace",
        default=_default_workspace(),
        help="Host workspace passed to executor_worker",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Print current manager status and exit without starting a process",
    )
    args = parser.parse_args()

    # B48 FIX A (observer-hotfix 2026-04-23): Add RotatingFileHandler so SM logs
    # are captured to disk. Previously -WindowStyle Hidden + basicConfig with no
    # FileHandler silently discarded every SM log message. See
    # docs/dev/b48-investigation-and-fix-proposal.md §2.
    from logging.handlers import RotatingFileHandler

    _log_dir = _shared_log_dir()
    _log_dir.mkdir(parents=True, exist_ok=True)
    _sm_log_path = _log_dir / f"service-manager-{args.project}.log"

    _formatter = logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _file_handler = RotatingFileHandler(
        str(_sm_log_path),
        maxBytes=50_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setFormatter(_formatter)
    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(_formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[_file_handler, _stream_handler],
    )
    log.info("ServiceManager logging initialized: file=%s (B48 Fix A)", _sm_log_path)

    manager = ServiceManager(
        project_id=args.project,
        governance_url=args.governance_url,
        workspace=args.workspace,
    )

    if args.status_only:
        print(manager.status())
        return

    os.environ.setdefault("GOVERNANCE_URL", args.governance_url)
    os.environ.setdefault("CODEX_WORKSPACE", args.workspace)
    _install_signal_handlers(manager.stop)

    # R4: Start sidecar HTTP server before executor
    manager._start_sidecar()

    manager.start()
    log.info(
        "ServiceManager host loop started (project=%s, governance=%s, workspace=%s)",
        args.project,
        args.governance_url,
        args.workspace,
    )

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("ServiceManager host loop interrupted, stopping")
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
