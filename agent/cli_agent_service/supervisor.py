"""C0 process-group, lease, heartbeat, cancellation, and restart supervision."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters.codex_cli import CodexCliAdapter
from .evidence import ExecutionReceipt, hash_command, hash_file, hash_text
from .models import AgentRun, ReconciliationResult
from .registry import AgentRegistry, process_start_identity


class SupervisorError(RuntimeError):
    pass


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    normalized = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


class RunHandle:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._done = threading.Event()
        self._receipt: ExecutionReceipt | None = None

    def _finish(self, receipt: ExecutionReceipt) -> None:
        self._receipt = receipt
        self._done.set()

    def wait(self, timeout: float | None = None) -> ExecutionReceipt:
        if not self._done.wait(timeout):
            raise TimeoutError("supervised run did not finish before timeout")
        assert self._receipt is not None
        return self._receipt

    @property
    def done(self) -> bool:
        return self._done.is_set()


@dataclass
class _ActiveRun:
    run: AgentRun
    owner_id: str
    process: subprocess.Popen[str]
    process_group_id: int
    command_hash: str
    prompt_hash: str
    output_path: Path
    stdout_path: Path
    stderr_path: Path
    stdout_handle: Any
    stderr_handle: Any
    run_dir: Path
    started_at: str
    handle: RunHandle
    cancel_requested: threading.Event


class CodexC0Supervisor:
    """Own one inherited-profile Codex process per immutable registry run."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        state_dir: str | os.PathLike[str],
        adapter: CodexCliAdapter | None = None,
        heartbeat_interval_seconds: float = 5.0,
        lease_ttl_seconds: int = 30,
        cancellation_grace_seconds: float = 3.0,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        process_identity_reader: Callable[[int], str | None] = process_start_identity,
    ) -> None:
        self.registry = registry
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.adapter = adapter or CodexCliAdapter()
        self.heartbeat_interval_seconds = max(float(heartbeat_interval_seconds), 0.01)
        self.lease_ttl_seconds = max(int(lease_ttl_seconds), 1)
        self.cancellation_grace_seconds = max(float(cancellation_grace_seconds), 0.05)
        self.process_factory = process_factory
        self.process_identity_reader = process_identity_reader
        self.owner_id = "cli-agent-host-{}".format(os.getpid())
        self._active: dict[str, _ActiveRun] = {}
        self._results: dict[str, ExecutionReceipt] = {}
        self._lock = threading.RLock()

    def _process_identity(self, pid: int) -> str:
        for _ in range(20):
            identity = self.process_identity_reader(pid)
            if identity:
                return identity
            time.sleep(0.01)
        raise SupervisorError("spawned process identity is not observable")

    def start_run(
        self,
        run: AgentRun,
        *,
        prompt: str,
        worktree: str | os.PathLike[str],
        evidence_refs: Mapping[str, str] | None = None,
    ) -> RunHandle:
        with self._lock:
            if run.run_id in self._active:
                raise SupervisorError("run is already active")
        run_dir = Path(self.state_dir) / "run-{}".format(uuid.uuid4().hex)
        output_path = run_dir / "last-message.txt"
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        launch = self.adapter.build_launch_spec(run, worktree=worktree, output_path=output_path)
        command_hash = hash_command(launch.command)
        prompt_hash = hash_text(prompt)
        self.registry.register_run(run, evidence_refs=evidence_refs)
        self.registry.acquire_lease(
            run.run_id,
            self.owner_id,
            ttl_seconds=self.lease_ttl_seconds,
        )
        handle = RunHandle(run.run_id)
        process: subprocess.Popen[str] | None = None
        stdout_handle: Any = None
        stderr_handle: Any = None
        try:
            run_dir.mkdir(mode=0o700)
            stdout_handle = stdout_path.open("w+", encoding="utf-8")
            stderr_handle = stderr_path.open("w+", encoding="utf-8")
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            process = self.process_factory(
                list(launch.command),
                stdin=subprocess.PIPE,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                cwd=launch.cwd,
                env=launch.environment,
                start_new_session=True,
            )
            process_group_id = os.getpgid(process.pid) if os.name != "nt" else process.pid
            identity = self._process_identity(process.pid)
            self.registry.record_process_start(
                run.run_id,
                pid=process.pid,
                process_start_identity=identity,
                process_group_id=process_group_id,
                argv_hash=command_hash,
            )
            if process.stdin is not None:
                try:
                    process.stdin.write(prompt)
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            active = _ActiveRun(
                run=run,
                owner_id=self.owner_id,
                process=process,
                process_group_id=process_group_id,
                command_hash=command_hash,
                prompt_hash=prompt_hash,
                output_path=output_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
                run_dir=run_dir,
                started_at=_timestamp(),
                handle=handle,
                cancel_requested=threading.Event(),
            )
            with self._lock:
                self._active[run.run_id] = active
            threading.Thread(
                target=self._monitor,
                args=(active,),
                name="cli-agent-run-{}".format(run.run_id),
                daemon=True,
            ).start()
            return handle
        except BaseException as exc:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()
            shutil.rmtree(run_dir, ignore_errors=True)
            self.registry.record_exit(run.run_id, 127, failure_category="spawn_error")
            if isinstance(exc, SupervisorError):
                raise
            raise SupervisorError("Codex process could not be started") from exc

    def _terminate(self, active: _ActiveRun) -> None:
        process = active.process
        if process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(active.process_group_id, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=self.cancellation_grace_seconds)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if os.name != "nt":
                os.killpg(active.process_group_id, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _monitor(self, active: _ActiveRun) -> None:
        failure_category = ""
        next_heartbeat = time.monotonic()
        while active.process.poll() is None:
            if active.cancel_requested.is_set():
                failure_category = "cancelled"
                self._terminate(active)
                break
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    self.registry.heartbeat(
                        active.run.run_id,
                        active.owner_id,
                        ttl_seconds=self.lease_ttl_seconds,
                    )
                except Exception:
                    failure_category = "lease_heartbeat_failed"
                    self._terminate(active)
                    break
                next_heartbeat = now + self.heartbeat_interval_seconds
            active.cancel_requested.wait(
                min(self.heartbeat_interval_seconds, max(0.01, next_heartbeat - now))
            )
        returncode = int(active.process.wait())
        if active.cancel_requested.is_set():
            failure_category = "cancelled"
        elif not failure_category and returncode != 0:
            failure_category = "process_error"
        effective_exit_code = 130 if failure_category == "cancelled" and returncode == 0 else returncode
        status = "cancelled" if failure_category == "cancelled" else "completed" if effective_exit_code == 0 else "failed"
        self.registry.record_exit(
            active.run.run_id,
            effective_exit_code,
            failure_category=failure_category,
        )
        active.stdout_handle.flush()
        active.stderr_handle.flush()
        active.stdout_handle.close()
        active.stderr_handle.close()
        receipt = ExecutionReceipt(
            run_id=active.run.run_id,
            status=status,
            exit_code=effective_exit_code,
            pid=active.process.pid,
            process_group_id=active.process_group_id,
            command_hash=active.command_hash,
            prompt_hash=active.prompt_hash,
            output_hash=hash_file(active.output_path),
            stdout_hash=hash_file(active.stdout_path),
            stderr_hash=hash_file(active.stderr_path),
            started_at=active.started_at,
            finished_at=_timestamp(),
            failure_category=failure_category,
        )
        shutil.rmtree(active.run_dir, ignore_errors=True)
        with self._lock:
            self._active.pop(active.run.run_id, None)
            self._results[active.run.run_id] = receipt
        active.handle._finish(receipt)

    def cancel_run(self, run_id: str) -> bool:
        with self._lock:
            active = self._active.get(run_id)
        if active is None:
            return False
        active.cancel_requested.set()
        self._terminate(active)
        return True

    def result(self, run_id: str) -> ExecutionReceipt | None:
        with self._lock:
            return self._results.get(run_id)

    def active_run_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active))

    def reconcile_restart(self) -> tuple[ReconciliationResult, ...]:
        return self.registry.reconcile_runs()
