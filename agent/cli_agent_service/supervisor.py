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
from .evidence import (
    ExecutionReceipt,
    RunReceiptEmitter,
    RunReceiptJournal,
    hash_command,
    hash_file,
    hash_text,
)
from .launchers import (
    HostEnvelopeStore,
    child_process_environment,
    clear_worker_auth_environment,
    default_host_envelope_store,
)
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
    started_monotonic: float
    process_start_identity: str
    receipt_emitter: RunReceiptEmitter | None
    handle: RunHandle
    cancel_requested: threading.Event
    output_suppressed: bool


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
        run_receipt_sink: Callable[[dict[str, Any]], Any] | None = None,
        host_envelope_store: HostEnvelopeStore | None = None,
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
        self.host_envelope_store = (
            host_envelope_store
            if host_envelope_store is not None
            else default_host_envelope_store()
        )
        self.receipt_journal = RunReceiptJournal(self.state_dir / "run-receipts")
        self.run_receipt_sink = run_receipt_sink
        self.owner_id = "cli-agent-host-{}".format(os.getpid())
        self._active: dict[str, _ActiveRun] = {}
        self._results: dict[str, ExecutionReceipt] = {}
        self._receipt_sink_errors: list[str] = []
        self._lock = threading.RLock()

    def _emit_run_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        persisted = self.receipt_journal.append(receipt)
        if self.run_receipt_sink is not None:
            try:
                self.run_receipt_sink(dict(persisted))
            except Exception as exc:
                with self._lock:
                    self._receipt_sink_errors.append(type(exc).__name__)
        return persisted

    def _run_receipt_emitter(
        self,
        run: AgentRun,
        execution_ticket: Mapping[str, Any] | None,
        command_hash: str,
    ) -> RunReceiptEmitter | None:
        if execution_ticket is None:
            return None
        if not isinstance(execution_ticket, Mapping):
            raise SupervisorError("execution_ticket must be an issued ticket object")
        if (
            execution_ticket.get("status") != "issued"
            or execution_ticket.get("issue_allowed") is not True
        ):
            raise SupervisorError("execution_ticket is not issued")
        profile = execution_ticket.get("profile_requirements")
        dispatch = execution_ticket.get("dispatch_identity")
        if not isinstance(profile, Mapping) or not isinstance(dispatch, Mapping):
            raise SupervisorError("execution_ticket is missing run identity")
        profile_id = str(profile.get("profile_id") or "").strip()
        if profile_id != run.config.profile_id:
            raise SupervisorError("execution_ticket profile does not match the run")
        return RunReceiptEmitter(
            run_id=run.run_id,
            ticket_id=execution_ticket.get("ticket_id", ""),
            ticket_hash=execution_ticket.get("ticket_hash", ""),
            profile_id=profile_id,
            runtime_context_id=dispatch.get("runtime_context_id", ""),
            command_hash=command_hash,
            sink=self._emit_run_receipt,
        )

    @staticmethod
    def _receipt_process_identity(
        *,
        pid: int,
        process_group_id: int,
        process_start_identity_value: str,
    ) -> dict[str, Any]:
        return {
            "pid": int(pid),
            "process_group_id": int(process_group_id),
            "process_start_identity_hash": hash_text(process_start_identity_value),
        }

    def _process_identity(self, pid: int) -> str:
        for _ in range(20):
            identity = self.process_identity_reader(pid)
            if identity:
                return identity
            time.sleep(0.01)
        raise SupervisorError("spawned process identity is not observable")

    def _revoke_host_envelope(self, run_id: str, owner_id: str = "") -> None:
        try:
            self.host_envelope_store.revoke(
                run_id,
                lease_owner_id=owner_id or self.owner_id,
            )
        except Exception:
            pass

    def start_run(
        self,
        run: AgentRun,
        *,
        prompt: str,
        worktree: str | os.PathLike[str],
        evidence_refs: Mapping[str, str] | None = None,
        execution_ticket: Mapping[str, Any] | None = None,
        require_host_envelope: bool = False,
    ) -> RunHandle:
        with self._lock:
            if run.run_id in self._active:
                raise SupervisorError("run is already active")
        run_dir = Path(self.state_dir) / "run-{}".format(uuid.uuid4().hex)
        output_path = run_dir / "last-message.txt"
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        try:
            launch = self.adapter.build_launch_spec(
                run,
                worktree=worktree,
                output_path=output_path,
            )
            command_hash = hash_command(launch.command)
            prompt_hash = hash_text(prompt)
            receipt_emitter = self._run_receipt_emitter(
                run,
                execution_ticket,
                command_hash,
            )
            self.registry.register_run(run, evidence_refs=evidence_refs)
            lease = self.registry.acquire_lease(
                run.run_id,
                self.owner_id,
                ttl_seconds=self.lease_ttl_seconds,
            )
        except BaseException:
            self._revoke_host_envelope(run.run_id)
            raise
        handle = RunHandle(run.run_id)
        process: subprocess.Popen[str] | None = None
        stdout_handle: Any = None
        stderr_handle: Any = None
        host_envelope = None
        output_suppressed = False
        accepted_monotonic = time.monotonic()
        try:
            if receipt_emitter is not None:
                receipt_emitter.emit("accepted", observed_at=_timestamp())
            if (
                lease.run_id != run.run_id
                or lease.owner_id != self.owner_id
                or lease.status != "active"
            ):
                raise SupervisorError("acquired lease identity does not match the run")
            host_envelope = self.host_envelope_store.consume(
                run.run_id,
                lease_owner_id=self.owner_id,
                lease_id=lease.lease_id,
            )
            if require_host_envelope and host_envelope is None:
                raise SupervisorError("required host envelope is unavailable")
            output_suppressed = host_envelope is not None
            run_dir.mkdir(mode=0o700)
            if output_suppressed:
                stdout_target: Any = subprocess.DEVNULL
                stderr_target: Any = subprocess.DEVNULL
            else:
                stdout_handle = stdout_path.open("w+", encoding="utf-8")
                stderr_handle = stderr_path.open("w+", encoding="utf-8")
                os.chmod(stdout_path, 0o600)
                os.chmod(stderr_path, 0o600)
                stdout_target = stdout_handle
                stderr_target = stderr_handle
            spawn_environment = child_process_environment(launch.environment)
            try:
                if host_envelope is not None:
                    host_envelope.apply_to(spawn_environment)
                process = self.process_factory(
                    list(launch.command),
                    stdin=subprocess.PIPE,
                    stdout=stdout_target,
                    stderr=stderr_target,
                    text=True,
                    cwd=launch.cwd,
                    env=spawn_environment,
                    start_new_session=True,
                )
            finally:
                clear_worker_auth_environment(spawn_environment)
                if host_envelope is not None:
                    host_envelope.discard()
            process_group_id = os.getpgid(process.pid) if os.name != "nt" else process.pid
            identity = self._process_identity(process.pid)
            self.registry.record_process_start(
                run.run_id,
                pid=process.pid,
                process_start_identity=identity,
                process_group_id=process_group_id,
                argv_hash=command_hash,
            )
            receipt_process_identity = self._receipt_process_identity(
                pid=process.pid,
                process_group_id=process_group_id,
                process_start_identity_value=identity,
            )
            receipt_emitter and receipt_emitter.emit(
                "started",
                observed_at=_timestamp(),
                process_identity=receipt_process_identity,
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
                started_monotonic=accepted_monotonic,
                process_start_identity=identity,
                receipt_emitter=receipt_emitter,
                handle=handle,
                cancel_requested=threading.Event(),
                output_suppressed=output_suppressed,
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
            if host_envelope is not None:
                host_envelope.discard()
            self._revoke_host_envelope(run.run_id)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()
            shutil.rmtree(run_dir, ignore_errors=True)
            self.registry.record_exit(run.run_id, 127, failure_category="spawn_error")
            if receipt_emitter is not None:
                process_identity_payload: dict[str, Any] = {}
                if process is not None:
                    try:
                        process_group_id = (
                            os.getpgid(process.pid)
                            if os.name != "nt"
                            else process.pid
                        )
                        process_start_identity = self.process_identity_reader(
                            process.pid
                        )
                        if process_start_identity:
                            process_identity_payload = self._receipt_process_identity(
                                pid=process.pid,
                                process_group_id=process_group_id,
                                process_start_identity_value=process_start_identity,
                            )
                    except OSError:
                        pass
                try:
                    receipt_emitter.emit(
                        "failed",
                        observed_at=_timestamp(),
                        process_identity=process_identity_payload,
                        output_hash=hash_text(""),
                        duration_ms=max(
                            0,
                            int((time.monotonic() - accepted_monotonic) * 1000),
                        ),
                        exit_code=127,
                        failure_category="spawn_error",
                    )
                except Exception:
                    pass
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
                try:
                    if active.receipt_emitter is not None:
                        active.receipt_emitter.emit(
                            "heartbeat",
                            observed_at=_timestamp(),
                            process_identity=self._receipt_process_identity(
                                pid=active.process.pid,
                                process_group_id=active.process_group_id,
                                process_start_identity_value=(
                                    active.process_start_identity
                                ),
                            ),
                        )
                except Exception:
                    failure_category = "receipt_heartbeat_failed"
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
        effective_exit_code = 130 if failure_category == "cancelled" else returncode
        status = "cancelled" if failure_category == "cancelled" else "completed" if effective_exit_code == 0 else "failed"
        self._revoke_host_envelope(active.run.run_id, active.owner_id)
        self.registry.record_exit(
            active.run.run_id,
            effective_exit_code,
            failure_category=failure_category,
        )
        if active.stdout_handle is not None:
            active.stdout_handle.flush()
            active.stdout_handle.close()
        if active.stderr_handle is not None:
            active.stderr_handle.flush()
            active.stderr_handle.close()
        if active.output_suppressed:
            output_hash = hash_text("")
            stdout_hash = hash_text("")
            stderr_hash = hash_text("")
        else:
            output_hash = hash_file(active.output_path)
            stdout_hash = hash_file(active.stdout_path)
            stderr_hash = hash_file(active.stderr_path)
        if active.receipt_emitter is not None:
            terminal_failure_category = failure_category
            if status == "failed" and not terminal_failure_category:
                terminal_failure_category = "process_error"
            active.receipt_emitter.emit(
                status,
                observed_at=_timestamp(),
                process_identity=self._receipt_process_identity(
                    pid=active.process.pid,
                    process_group_id=active.process_group_id,
                    process_start_identity_value=active.process_start_identity,
                ),
                output_hash=output_hash,
                duration_ms=max(
                    0,
                    int((time.monotonic() - active.started_monotonic) * 1000),
                ),
                exit_code=effective_exit_code,
                failure_category=terminal_failure_category,
            )
        receipt = ExecutionReceipt(
            run_id=active.run.run_id,
            status=status,
            exit_code=effective_exit_code,
            pid=active.process.pid,
            process_group_id=active.process_group_id,
            command_hash=active.command_hash,
            prompt_hash=active.prompt_hash,
            output_hash=output_hash,
            stdout_hash=stdout_hash,
            stderr_hash=stderr_hash,
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
            self._revoke_host_envelope(run_id)
            return False
        self._revoke_host_envelope(run_id, active.owner_id)
        active.cancel_requested.set()
        self._terminate(active)
        return True

    def cancel_all(self) -> None:
        for run_id in self.active_run_ids():
            self.cancel_run(run_id)
        self.host_envelope_store.revoke_all()

    def result(self, run_id: str) -> ExecutionReceipt | None:
        with self._lock:
            return self._results.get(run_id)

    def active_run_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active))

    def reconcile_restart(self) -> tuple[ReconciliationResult, ...]:
        results = self.registry.reconcile_runs()
        for result in results:
            if result.classification != "lost":
                continue
            previous = self.receipt_journal.latest(result.run_id)
            if not previous or previous["state"] in {
                "completed",
                "failed",
                "cancelled",
                "lost",
            }:
                continue
            emitter = RunReceiptEmitter(
                run_id=previous["run_id"],
                ticket_id=previous["ticket_id"],
                ticket_hash=previous["ticket_hash"],
                profile_id=previous["profile_id"],
                runtime_context_id=previous["runtime_context_id"],
                command_hash=previous["command_hash"],
                sink=self._emit_run_receipt,
                previous_receipt=previous,
            )
            record = self.registry.get_run(result.run_id)
            process_identity_payload = dict(previous.get("process_identity") or {})
            if record and record.pid and record.process_start_identity:
                process_identity_payload = self._receipt_process_identity(
                    pid=record.pid,
                    process_group_id=record.process_group_id or record.pid,
                    process_start_identity_value=record.process_start_identity,
                )
            emitter.emit(
                "lost",
                observed_at=_timestamp(),
                process_identity=process_identity_payload,
                output_hash=hash_text(""),
                duration_ms=0,
                failure_category="lost",
            )
        return results

    def run_receipts(self, run_id: str) -> tuple[dict[str, Any], ...]:
        return self.receipt_journal.receipts(run_id)

    def receipt_sink_errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._receipt_sink_errors)
