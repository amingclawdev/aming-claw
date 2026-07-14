"""Small, separately supervised host daemon for CLI Agent Service foundation."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .adapters.codex_desktop import (
    CodexDesktopAdapter,
    DesktopHostAdapterError,
)
from .health import health_payload, stopped_payload
from .launchers import (
    HostEnvelopeError,
    HostEnvelopeStore,
    scrub_host_envelope_payload,
)
from .registry import AgentRegistry, _profile_from_dict, _run_from_dict
from .supervisor import CodexC0Supervisor


MAX_REQUEST_BYTES = 64 * 1024
MAX_LOCAL_SERVICE_MESSAGE_BYTES = 1024 * 1024
DEFAULT_SOCKET_TIMEOUT_SECONDS = 3.0
DEFAULT_GOVERNANCE_URL = "http://localhost:40000"

_DESKTOP_ADMISSION_PUBLIC_FIELDS = frozenset(
    {
        "host_kind",
        "project_id",
        "backlog_id",
        "contract_execution_id",
        "runtime_context_id",
        "task_id",
        "worker_id",
        "worker_slot_id",
        "observer_command_id",
        "expected_execution_state_revision",
        "expected_execution_state_hash",
        "expected_dispatch_identity_hash",
        "now_iso",
    }
)
_DESKTOP_ADMISSION_IDENTITY_FIELDS = (
    "project_id",
    "backlog_id",
    "runtime_context_id",
    "task_id",
    "worker_id",
    "worker_slot_id",
    "observer_command_id",
)

_GUIDED_RUNTIME_AUTHORITY_FIELDS = frozenset(
    {
        "project_id",
        "backlog_id",
        "contract_execution_id",
        "runtime_context_id",
        "task_id",
        "worker_id",
        "worker_slot_id",
        "observer_command_id",
        "role",
        "profile_id",
        "principal_id",
        "expected_execution_state_revision",
        "expected_execution_state_hash",
        "expected_dispatch_identity_hash",
    }
)
_GUIDED_RUNTIME_REQUIRED_SELECTORS = (
    "project_id",
    "backlog_id",
    "contract_execution_id",
    "runtime_context_id",
    "task_id",
    "worker_id",
    "worker_slot_id",
    "observer_command_id",
    "role",
    "profile_id",
    "principal_id",
)


class ServiceError(RuntimeError):
    pass


class ServiceAlreadyRunningError(ServiceError):
    pass


class ServiceUnavailableError(ServiceError):
    pass


def default_state_dir() -> Path:
    configured = os.environ.get("AMING_CLAW_CLI_AGENT_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / "AmingClaw" / "cli-agent-service"


@dataclass(frozen=True)
class ServicePaths:
    state_dir: Path
    socket_path: Path
    status_path: Path

    @classmethod
    def from_state_dir(cls, state_dir: str | os.PathLike[str] | None = None) -> "ServicePaths":
        root = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        socket_path = root / "service.sock"
        if len(os.fsencode(socket_path)) >= 100:
            digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
            socket_path = Path("/tmp") / "amingclaw-cli-agent-{}.sock".format(digest)
        return cls(root, socket_path, root / "status.json")

    def prepare(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
    temporary.write_text(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _wipe_buffer(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0
    value.clear()


def read_status(paths: ServicePaths) -> dict[str, Any]:
    try:
        value = json.loads(paths.status_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return stopped_payload()
    return value if isinstance(value, dict) else stopped_payload()


def request_service(
    paths: ServicePaths,
    operation: str,
    *,
    payload: Mapping[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request_value: dict[str, Any] = {"operation": str(operation)}
    if payload is not None:
        request_value["payload"] = payload
    serialized = json.dumps(request_value, separators=(",", ":")) + "\n"
    request = bytearray(serialized.encode("utf-8"))
    del serialized
    response = bytearray()
    try:
        if len(request) > MAX_LOCAL_SERVICE_MESSAGE_BYTES:
            raise ServiceError("CLI Agent Service request exceeded the size limit")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(max(float(timeout_seconds), 0.05))
            client.connect(str(paths.socket_path))
            client.sendall(request)
            while len(response) <= MAX_LOCAL_SERVICE_MESSAGE_BYTES:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if b"\n" in chunk:
                    break
    except (FileNotFoundError, ConnectionError, OSError, socket.timeout) as exc:
        raise ServiceUnavailableError("CLI Agent Service is not reachable") from exc
    finally:
        _wipe_buffer(request)
        scrub_host_envelope_payload(request_value)
    try:
        if len(response) > MAX_LOCAL_SERVICE_MESSAGE_BYTES:
            raise ServiceError("CLI Agent Service response exceeded the size limit")
        result = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError("CLI Agent Service returned an invalid response") from exc
    finally:
        _wipe_buffer(response)
    if not isinstance(result, dict):
        raise ServiceError("CLI Agent Service returned an invalid response object")
    return result


def current_status(paths: ServicePaths) -> dict[str, Any]:
    try:
        return request_service(paths, "status")
    except ServiceUnavailableError:
        payload = read_status(paths)
        if payload.get("status") not in {"stopped", "failed"}:
            payload = {**payload, "ok": False, "status": "unreachable", "socket_ready": False}
        return payload


def _governance_json_post(
    path: str,
    payload: Mapping[str, Any],
    *,
    governance_url: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    base_url = str(
        governance_url
        or os.environ.get("AMING_CLAW_GOVERNANCE_URL")
        or DEFAULT_GOVERNANCE_URL
    ).rstrip("/")
    request_bytes = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request_bytes) > MAX_REQUEST_BYTES:
        raise ServiceError("governance request exceeded the size limit")
    request = urllib.request.Request(
        base_url + path,
        data=request_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(float(timeout_seconds), 0.05),
        ) as response:
            response_bytes = response.read(MAX_REQUEST_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ServiceUnavailableError("governance service is unavailable") from exc
    if len(response_bytes) > MAX_REQUEST_BYTES:
        raise ServiceError("governance response exceeded the size limit")
    try:
        resolved = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError("governance service returned an invalid response") from exc
    if not isinstance(resolved, Mapping):
        raise ServiceError("governance service returned an invalid response")
    return dict(resolved)


def resolve_governance_execution_ticket(
    authority_request: Mapping[str, Any],
    *,
    governance_url: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Resolve one role-generic ticket from governance-owned ContractRuntime state."""

    project_id = str(authority_request.get("project_id") or "").strip()
    if not project_id:
        raise ServiceError("authority request requires project_id")
    path = "/api/projects/{}/cli-agent/execution-ticket/resolve".format(
        urllib.parse.quote(project_id, safe=""),
    )
    resolved = _governance_json_post(
        path,
        authority_request,
        governance_url=governance_url,
        timeout_seconds=timeout_seconds,
    )
    ticket = resolved.get("execution_ticket")
    if resolved.get("ok") is not True or not isinstance(ticket, Mapping):
        raise ServiceError("ContractRuntime authority rejected run admission")
    return dict(ticket)


def resolve_governance_desktop_execution_ticket(
    authority_request: Mapping[str, Any],
    *,
    governance_url: str = "",
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Compatibility alias for the role-generic ContractRuntime resolver."""

    return resolve_governance_execution_ticket(
        authority_request,
        governance_url=governance_url,
        timeout_seconds=timeout_seconds,
    )


class CliAgentService:
    """Foreground Unix-socket service intended to be supervised by launchd."""

    def __init__(
        self,
        paths: ServicePaths,
        *,
        host_envelope_store: HostEnvelopeStore | None = None,
        registry: AgentRegistry | None = None,
        supervisor: CodexC0Supervisor | None = None,
    ) -> None:
        self.paths = paths
        self.paths.prepare()
        self.started_at = datetime.now(timezone.utc)
        self.pid = os.getpid()
        self._stop_event = threading.Event()
        self._server: socket.socket | None = None
        if supervisor is not None:
            if registry is not None and supervisor.registry is not registry:
                raise ServiceError("supervisor registry does not match service registry")
            if (
                host_envelope_store is not None
                and supervisor.host_envelope_store is not host_envelope_store
            ):
                raise ServiceError("supervisor host envelope store does not match")
            self.registry = supervisor.registry
            self.host_envelope_store = supervisor.host_envelope_store
            self.supervisor = supervisor
        else:
            self.host_envelope_store = host_envelope_store or HostEnvelopeStore()
            self.registry = registry or AgentRegistry(
                self.paths.state_dir / "registry" / "runs.db"
            )
            self.supervisor = CodexC0Supervisor(
                self.registry,
                state_dir=self.paths.state_dir / "supervisor",
                host_envelope_store=self.host_envelope_store,
            )
        self._receipt_sink_delegate = self.supervisor.run_receipt_sink
        self.supervisor.run_receipt_sink = self._project_run_receipt
        self._restart_reconciled = False
        self._desktop_adapters: dict[str, CodexDesktopAdapter] = {
            "codex_desktop": CodexDesktopAdapter(),
        }
        self._contract_runtime_authority_resolver = resolve_governance_execution_ticket

    def _project_run_receipt(self, receipt: dict[str, Any]) -> None:
        """Project one daemon fact without granting it contract authority."""

        failure: BaseException | None = None
        try:
            record = self.registry.get_run(str(receipt.get("run_id") or ""))
            if record is None:
                raise ServiceError("run receipt has no registry identity")
            refs = {ref.name: ref.value for ref in record.evidence_refs}
            project_id = str(
                refs.get("project_id") or record.run.config.project_id or ""
            ).strip()
            backlog_id = str(refs.get("backlog_id") or "").strip()
            task_id = str(refs.get("task_id") or "").strip()
            if not project_id or not backlog_id or not task_id:
                raise ServiceError("run receipt lacks governance projection selectors")
            projected = _governance_json_post(
                "/api/graph-governance/{}/cli-agent/run-receipts".format(
                    urllib.parse.quote(project_id, safe="")
                ),
                {
                    "backlog_id": backlog_id,
                    "task_id": task_id,
                    "receipt": receipt,
                },
            )
            ingestion = projected.get("receipt_ingestion")
            if not isinstance(ingestion, Mapping) or ingestion.get(
                "governance_authority"
            ) is not False:
                raise ServiceError("run receipt projection was not non-authoritative")
        except BaseException as exc:
            failure = exc
        if self._receipt_sink_delegate is not None:
            self._receipt_sink_delegate(dict(receipt))
        if failure is not None:
            raise failure

    def _desktop_adapter(self, payload: Mapping[str, Any]) -> CodexDesktopAdapter:
        host_kind = str(payload.get("host_kind") or "").strip().lower()
        adapter = self._desktop_adapters.get(host_kind)
        if adapter is None:
            raise ServiceError("Desktop host kind is unsupported")
        return adapter

    def _admit_desktop_execution_ticket(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        unsupported = sorted(set(payload) - _DESKTOP_ADMISSION_PUBLIC_FIELDS)
        if unsupported:
            raise ServiceError(
                "Desktop ticket admission contains unsupported authority fields"
            )
        missing = [
            field
            for field in (
                "host_kind",
                "project_id",
                "backlog_id",
                "contract_execution_id",
                "runtime_context_id",
                "task_id",
                "worker_id",
                "worker_slot_id",
                "observer_command_id",
            )
            if not str(payload.get(field) or "").strip()
        ]
        if missing:
            raise ServiceError(
                "Desktop ticket admission is missing canonical authority selectors"
            )
        try:
            expected_revision = int(
                payload.get("expected_execution_state_revision") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ServiceError(
                "Desktop ticket admission state revision is invalid"
            ) from exc
        if expected_revision <= 0:
            raise ServiceError(
                "Desktop ticket admission requires current authority coordinates"
            )
        authority_request = {
            field: payload.get(field)
            for field in _DESKTOP_ADMISSION_PUBLIC_FIELDS
            if field not in {"host_kind", "now_iso"}
            and payload.get(field) not in (None, "")
        }
        authority_request["expected_execution_state_revision"] = expected_revision
        canonical_ticket = self._contract_runtime_authority_resolver(
            authority_request
        )
        if not isinstance(canonical_ticket, Mapping):
            raise ServiceError(
                "ContractRuntime authority resolver returned an invalid ticket"
            )
        dispatch = canonical_ticket.get("dispatch_identity")
        if not isinstance(dispatch, Mapping):
            raise ServiceError(
                "ContractRuntime authority resolver returned an invalid dispatch"
            )
        mismatches = [
            field
            for field in _DESKTOP_ADMISSION_IDENTITY_FIELDS
            if str(dispatch.get(field) or "").strip()
            != str(payload.get(field) or "").strip()
        ]
        if str(canonical_ticket.get("contract_execution_id") or "").strip() != str(
            payload.get("contract_execution_id") or ""
        ).strip():
            mismatches.append("contract_execution_id")
        try:
            canonical_revision = int(
                canonical_ticket.get("execution_state_revision") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ServiceError(
                "ContractRuntime authority resolver returned an invalid state revision"
            ) from exc
        if canonical_revision != expected_revision:
            mismatches.append("execution_state_revision")
        expected_state_hash = str(
            payload.get("expected_execution_state_hash") or ""
        ).strip()
        if expected_state_hash and str(
            canonical_ticket.get("execution_state_hash") or ""
        ).strip() != expected_state_hash:
            mismatches.append("execution_state_hash")
        expected_dispatch_hash = str(
            payload.get("expected_dispatch_identity_hash") or ""
        ).strip()
        if expected_dispatch_hash and str(
            canonical_ticket.get("dispatch_identity_hash") or ""
        ).strip() != expected_dispatch_hash:
            mismatches.append("dispatch_identity_hash")
        if mismatches:
            raise ServiceError(
                "ContractRuntime authority resolver returned stale or mismatched authority"
            )
        adapter = self._desktop_adapter(payload)
        admission = adapter._admit_service_execution_ticket(
            canonical_execution_ticket=canonical_ticket,
            now_iso=str(payload.get("now_iso") or ""),
        )
        return {
            "ok": True,
            "host_kind": adapter.host_kind,
            "execution_ticket": dict(canonical_ticket),
            **admission,
        }

    def _dispatch_desktop_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if operation == "desktop_execution_ticket_admit":
            return self._admit_desktop_execution_ticket(payload)
        adapter = self._desktop_adapter(payload)
        if operation == "desktop_host_register":
            result = adapter.register_host(
                host_id=str(payload.get("host_id") or ""),
                capabilities=list(payload.get("capabilities") or []),
                automation_mode=str(
                    payload.get("automation_mode") or "service_callable"
                ),
                auth_mode=str(payload.get("auth_mode") or "host_owned"),
                heartbeat_ttl_seconds=payload.get("heartbeat_ttl_seconds") or 30,
                host_session_id=str(payload.get("host_session_id") or ""),
                now_iso=str(payload.get("now_iso") or ""),
            )
        elif operation == "desktop_host_heartbeat":
            result = adapter.heartbeat(
                host_id=str(payload.get("host_id") or ""),
                heartbeat_id=str(payload.get("heartbeat_id") or ""),
                capabilities=list(payload.get("capabilities") or []),
                now_iso=str(payload.get("now_iso") or ""),
            )
        elif operation == "desktop_execution_ticket_ack":
            ticket = payload.get("execution_ticket")
            if not isinstance(ticket, Mapping):
                raise ServiceError("Desktop execution ticket is required")
            result = adapter.acknowledge_execution_ticket(
                host_id=str(payload.get("host_id") or ""),
                execution_ticket=ticket,
                run_id=str(payload.get("run_id") or ""),
                now_iso=str(payload.get("now_iso") or ""),
            )
        elif operation == "desktop_runtime_join":
            ack = payload.get("ticket_ack")
            if not isinstance(ack, Mapping):
                raise ServiceError("Desktop ticket acknowledgement is required")
            canonical_ack = dict(ack)
            canonical_ack.pop("ok", None)
            result = adapter.join_runtime_context(
                ticket_ack=canonical_ack,
                actual_host_worker_id=str(
                    payload.get("actual_host_worker_id") or ""
                ),
                worker_session_id=str(payload.get("worker_session_id") or ""),
                worker_transcript_ref=str(
                    payload.get("worker_transcript_ref") or ""
                ),
                session_token_ref=str(payload.get("session_token_ref") or ""),
                observer_command_id=str(
                    payload.get("observer_command_id") or ""
                ),
                launch_text_hash=str(payload.get("launch_text_hash") or ""),
                worker_slot_id=str(payload.get("worker_slot_id") or ""),
                host_startup_id=str(payload.get("host_startup_id") or ""),
                now_iso=str(payload.get("now_iso") or ""),
            )
        elif operation == "desktop_run_cleanup":
            result = adapter.cleanup_run(
                str(payload.get("run_id") or ""),
                reason=str(payload.get("reason") or "run_cleanup"),
            )
        else:  # pragma: no cover - guarded by _dispatch
            raise ServiceError("unsupported Desktop host operation")
        return {"ok": True, "host_kind": adapter.host_kind, **result}

    def _snapshot(
        self,
        *,
        stopping: bool = False,
        accepting_agent_runs: bool = True,
    ) -> dict[str, Any]:
        payload = health_payload(
            pid=self.pid,
            started_at=self.started_at,
            socket_ready=self._server is not None,
            stopping=stopping,
        )
        return {
            **payload,
            "accepting_agent_runs": bool(
                accepting_agent_runs and not stopping and self.supervisor is not None
            ),
        }

    def _remove_stale_socket(self) -> None:
        if not self.paths.socket_path.exists():
            return
        try:
            request_service(self.paths, "health", timeout_seconds=0.2)
        except ServiceUnavailableError:
            self.paths.socket_path.unlink(missing_ok=True)
            return
        raise ServiceAlreadyRunningError("CLI Agent Service is already running")

    def _read_request(self, connection: socket.socket) -> dict[str, Any]:
        data = bytearray()
        try:
            while len(data) <= MAX_LOCAL_SERVICE_MESSAGE_BYTES:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
            if len(data) > MAX_LOCAL_SERVICE_MESSAGE_BYTES:
                raise ServiceError("request exceeded the size limit")
            value = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
        finally:
            _wipe_buffer(data)
        if not isinstance(value, dict):
            raise ServiceError("request must be a JSON object")
        return value

    def _admit_governed_host_envelope_run(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        caller_fields = {
            "run",
            "worktree",
            "prompt",
            "authority_selectors",
            "host_envelope",
            "ttl_seconds",
            "expires_at",
        }
        if set(payload) - caller_fields:
            raise ServiceError("governed run request contains unsupported fields")
        selectors = payload.get("authority_selectors")
        if not isinstance(selectors, Mapping):
            raise ServiceError("governed run requires ContractRuntime selectors")
        unsupported = sorted(set(selectors) - _GUIDED_RUNTIME_AUTHORITY_FIELDS)
        if unsupported:
            raise ServiceError("governed run contains caller-owned authority fields")
        if any(
            not str(selectors.get(field) or "").strip()
            for field in _GUIDED_RUNTIME_REQUIRED_SELECTORS
        ):
            raise ServiceError("governed run is missing canonical authority selectors")
        try:
            expected_revision = int(
                selectors.get("expected_execution_state_revision") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ServiceError("governed run state revision is invalid") from exc
        if expected_revision <= 0:
            raise ServiceError("governed run requires current authority coordinates")

        run_value = payload.get("run")
        if not isinstance(run_value, Mapping):
            raise ServiceError("host envelope run must be a public run object")
        profile_value = run_value.get("profile")
        if not isinstance(profile_value, Mapping):
            raise ServiceError("host envelope run requires an immutable profile")
        try:
            profile = _profile_from_dict(profile_value)
            run = _run_from_dict(run_value, profile)
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError("host envelope run identity is invalid") from exc
        worktree = str(payload.get("worktree") or "").strip()
        if not worktree:
            raise ServiceError("host envelope run worktree is required")

        authority_request = {
            field: selectors.get(field)
            for field in _GUIDED_RUNTIME_AUTHORITY_FIELDS
            if selectors.get(field) not in (None, "")
        }
        authority_request["expected_execution_state_revision"] = expected_revision
        ticket = self._contract_runtime_authority_resolver(authority_request)
        if not isinstance(ticket, Mapping):
            raise ServiceError("ContractRuntime resolver returned an invalid ticket")
        dispatch = ticket.get("dispatch_identity")
        profile_requirements = ticket.get("profile_requirements")
        if (
            ticket.get("status") != "issued"
            or ticket.get("issue_allowed") is not True
            or not isinstance(dispatch, Mapping)
            or not isinstance(profile_requirements, Mapping)
        ):
            raise ServiceError("ContractRuntime resolver did not issue a canonical ticket")

        selector_comparisons = {
            "project_id": dispatch.get("project_id"),
            "backlog_id": dispatch.get("backlog_id"),
            "runtime_context_id": dispatch.get("runtime_context_id"),
            "task_id": dispatch.get("task_id"),
            "worker_id": dispatch.get("worker_id"),
            "worker_slot_id": dispatch.get("worker_slot_id"),
            "observer_command_id": dispatch.get("observer_command_id"),
            "principal_id": dispatch.get("worker_id"),
            "role": dispatch.get("worker_role"),
            "profile_id": profile_requirements.get("profile_id"),
        }
        mismatches = [
            field
            for field, canonical in selector_comparisons.items()
            if str(canonical or "").strip()
            != str(selectors.get(field) or "").strip()
        ]
        run_role = str(getattr(run.config.role, "value", run.config.role)).strip()
        if str(dispatch.get("worktree_path") or "").strip() != worktree:
            mismatches.append("worktree")
        if str(run.config.project_id or "").strip() != str(
            dispatch.get("project_id") or ""
        ).strip():
            mismatches.append("run.project_id")
        if run.config.profile_id != str(
            profile_requirements.get("profile_id") or ""
        ).strip():
            mismatches.append("run.profile_id")
        if run_role != str(profile_requirements.get("role") or "").strip():
            mismatches.append("run.role")
        if str(ticket.get("contract_execution_id") or "").strip() != str(
            selectors.get("contract_execution_id") or ""
        ).strip():
            mismatches.append("contract_execution_id")
        if int(ticket.get("execution_state_revision") or 0) != expected_revision:
            mismatches.append("execution_state_revision")
        for field in (
            "expected_execution_state_hash",
            "expected_dispatch_identity_hash",
        ):
            expected = str(selectors.get(field) or "").strip()
            ticket_field = field.removeprefix("expected_")
            if expected and str(ticket.get(ticket_field) or "").strip() != expected:
                mismatches.append(ticket_field)
        if mismatches:
            raise ServiceError(
                "ContractRuntime ticket does not match run admission: "
                + ", ".join(sorted(set(mismatches)))
            )

        evidence_refs = {
            "project_id": str(dispatch.get("project_id") or ""),
            "backlog_id": str(dispatch.get("backlog_id") or ""),
            "contract_execution_id": str(ticket.get("contract_execution_id") or ""),
            "runtime_context_id": str(dispatch.get("runtime_context_id") or ""),
            "task_id": str(dispatch.get("task_id") or ""),
        }
        return self._start_host_envelope_run(
            {
                **{
                    field: value
                    for field, value in payload.items()
                    if field != "authority_selectors"
                },
                "execution_ticket": dict(ticket),
                "evidence_refs": evidence_refs,
            }
        )

    def _start_host_envelope_run(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        allowed_fields = {
            "run",
            "worktree",
            "prompt",
            "execution_ticket",
            "evidence_refs",
            "host_envelope",
            "ttl_seconds",
            "expires_at",
        }
        if set(payload) - allowed_fields:
            raise ServiceError("host envelope run request contains unsupported fields")
        run_value = payload.get("run")
        if not isinstance(run_value, Mapping):
            raise ServiceError("host envelope run must be a public run object")
        profile_value = run_value.get("profile")
        if not isinstance(profile_value, Mapping):
            raise ServiceError("host envelope run requires an immutable profile")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise ServiceError("host envelope run prompt must be text")
        worktree = str(payload.get("worktree") or "").strip()
        if not worktree:
            raise ServiceError("host envelope run worktree is required")
        execution_ticket = payload.get("execution_ticket")
        if not isinstance(execution_ticket, Mapping):
            raise ServiceError("host envelope run requires an execution ticket")
        evidence_refs = payload.get("evidence_refs")
        if evidence_refs is not None and not isinstance(evidence_refs, Mapping):
            raise ServiceError("host envelope evidence_refs must be an object")
        try:
            profile = _profile_from_dict(profile_value)
            run = _run_from_dict(run_value, profile)
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError("host envelope run identity is invalid") from exc

        public_text = json.dumps(
            {
                "run": run_value,
                "worktree": worktree,
                "prompt": prompt,
                "execution_ticket": execution_ticket,
                "evidence_refs": evidence_refs or {},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self.host_envelope_store.stage(
            run.run_id,
            payload.get("host_envelope"),
            lease_owner_id=self.supervisor.owner_id,
            ttl_seconds=payload.get("ttl_seconds"),
            expires_at=payload.get("expires_at"),
            public_text=public_text,
        )
        del public_text
        try:
            self.supervisor.start_run(
                run,
                prompt=prompt,
                worktree=worktree,
                evidence_refs=evidence_refs,
                execution_ticket=execution_ticket,
                require_host_envelope=True,
            )
        except BaseException as exc:
            self.supervisor.cancel_run(run.run_id)
            raise ServiceError("host envelope run could not be started") from exc
        return {"ok": True, "status": "started", "run_id": run.run_id}

    def _dispatch(self, request: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        operation = str(request.get("operation") or "").strip().lower()
        if operation == "health":
            return self._snapshot(), False
        if operation == "status":
            return self._snapshot(accepting_agent_runs=False), False
        if operation == "stop":
            return self._snapshot(stopping=True), True
        if operation == "start_host_envelope_run":
            payload = request.get("payload")
            if not isinstance(payload, Mapping):
                raise ServiceError("host envelope run payload must be an object")
            return self._admit_governed_host_envelope_run(payload), False
        if operation in {
            "desktop_host_register",
            "desktop_host_heartbeat",
            "desktop_execution_ticket_admit",
            "desktop_execution_ticket_ack",
            "desktop_runtime_join",
            "desktop_run_cleanup",
        }:
            payload = request.get("payload")
            if not isinstance(payload, Mapping):
                raise ServiceError("Desktop host payload must be an object")
            return self._dispatch_desktop_operation(operation, payload), False
        if operation == "host_envelope":
            payload = request.get("payload")
            if not isinstance(payload, Mapping):
                raise ServiceError("host envelope payload must be an object")
            action = str(payload.get("action") or "stage").strip().lower()
            run_id = str(payload.get("run_id") or "").strip()
            if action == "stage":
                summary = self.host_envelope_store.stage(
                    run_id,
                    payload.get("host_envelope"),
                    lease_owner_id=self.supervisor.owner_id,
                    ttl_seconds=payload.get("ttl_seconds"),
                    expires_at=payload.get("expires_at"),
                )
                return {"ok": True, **summary}, False
            if action == "revoke":
                summary = self.host_envelope_store.revoke(
                    run_id,
                    envelope_ref=str(payload.get("envelope_ref") or ""),
                    lease_owner_id=self.supervisor.owner_id,
                )
                return {"ok": True, **summary}, False
            raise ServiceError("unsupported host envelope action")
        return {
            "ok": False,
            "status": "invalid_request",
            "error": "unsupported operation",
            "allowed_operations": [
                "health",
                "status",
                "stop",
                "host_envelope",
                "start_host_envelope_run",
                "desktop_host_register",
                "desktop_host_heartbeat",
                "desktop_execution_ticket_admit",
                "desktop_execution_ticket_ack",
                "desktop_runtime_join",
                "desktop_run_cleanup",
            ],
        }, False

    def _handle_connection(self, connection: socket.socket) -> None:
        should_stop = False
        request: dict[str, Any] = {}
        try:
            request = self._read_request(connection)
            response, should_stop = self._dispatch(request)
        except (
            DesktopHostAdapterError,
            HostEnvelopeError,
            ServiceError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            response = {"ok": False, "status": "invalid_request", "error": str(exc)}
        finally:
            scrub_host_envelope_payload(request)
        serialized = json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        connection.sendall(serialized + b"\n")
        if should_stop:
            self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self.supervisor.cancel_all()

    def serve_forever(self) -> None:
        self.paths.prepare()
        self._remove_stale_socket()
        if not self._restart_reconciled:
            self.supervisor.reconcile_restart()
            self._restart_reconciled = True
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.paths.socket_path))
            os.chmod(self.paths.socket_path, 0o600)
            server.listen(16)
            server.settimeout(0.25)
            self._server = server
            _write_json(self.paths.status_path, self._snapshot())
            while not self._stop_event.is_set():
                self.host_envelope_store.purge_expired()
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    self._handle_connection(connection)
        finally:
            self.supervisor.cancel_all()
            self._server = None
            server.close()
            self.paths.socket_path.unlink(missing_ok=True)
            _write_json(self.paths.status_path, stopped_payload(pid=self.pid))


def run_foreground(paths: ServicePaths) -> None:
    service = CliAgentService(paths)

    def stop_service(_signum: int, _frame: Any) -> None:
        service.stop()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop_service)
        signal.signal(signal.SIGINT, stop_service)
    service.serve_forever()
