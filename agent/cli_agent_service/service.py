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
from .auth import ProfileAuthController
from .health import health_payload, stopped_payload
from .launchers import (
    HostEnvelopeError,
    HostEnvelopeStore,
    scrub_host_envelope_payload,
)
from .models import RunState
from .registry import AgentRegistry, RegistryError
from .scheduler import AgentScheduler, RunIdentityConflictError, SchedulerError
from .profile_control import (
    ManagedProfileControl,
    PROFILE_OPERATIONS,
    ProfileControlError,
)
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

_GUIDED_RUNTIME_RESOLVER_FIELDS = frozenset(
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
_GUIDED_RUNTIME_ROUTE_FIELDS = (
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_token_ref",
    "visible_injection_manifest_hash",
)
_GUIDED_RUNTIME_PROFILE_FIELDS = (
    "profile_id",
    "harness",
    "provider",
    "model",
    "runtime_id",
    "endpoint_id",
    "launcher_id",
)
_GUIDED_RUNTIME_AUTHORITY_FIELDS = frozenset(
    {
        *_GUIDED_RUNTIME_RESOLVER_FIELDS,
        *_GUIDED_RUNTIME_ROUTE_FIELDS,
        *_GUIDED_RUNTIME_PROFILE_FIELDS,
        "backend_mode",
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
    "expected_execution_state_hash",
    "expected_dispatch_identity_hash",
    *_GUIDED_RUNTIME_ROUTE_FIELDS,
    "backend_mode",
)


def _stable_json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
    role_token: str = "",
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
    headers = {"Content-Type": "application/json"}
    if role_token:
        headers["X-Gov-Token"] = role_token
    request = urllib.request.Request(
        base_url + path,
        data=request_bytes,
        headers=headers,
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
    qa_session_token: str = "",
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
        role_token=qa_session_token,
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
        profile_auth_controller: ProfileAuthController | None = None,
        profile_control: ManagedProfileControl | None = None,
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
        self.scheduler = AgentScheduler(self.registry)
        if profile_control is not None:
            if profile_control.registry is not self.registry:
                raise ServiceError("profile control registry does not match service registry")
            if (
                profile_auth_controller is not None
                and profile_control.auth_controller is not profile_auth_controller
            ):
                raise ServiceError("profile control auth controller does not match")
            self.profile_control = profile_control
            self.profile_auth_controller = profile_control.auth_controller
        else:
            self.profile_auth_controller = (
                profile_auth_controller
                or ProfileAuthController(self.paths.state_dir / "profiles")
            )
            self.profile_control = ManagedProfileControl(
                self.registry,
                self.profile_auth_controller,
            )
        self.supervisor.managed_profile_home_resolver = (
            self.profile_control.resolve_profile_home
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
        except Exception as exc:
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
        *,
        qa_mode: bool = False,
    ) -> dict[str, Any]:
        allowed_payload_fields = (
            {"authority_selectors", "qa_session_token"}
            if qa_mode
            else {"authority_selectors", "host_envelope"}
        )
        if set(payload) - allowed_payload_fields:
            raise ServiceError("governed run request contains unsupported fields")
        selectors = payload.get("authority_selectors")
        if not isinstance(selectors, Mapping):
            raise ServiceError("governed run requires ContractRuntime selectors")
        unsupported = sorted(set(selectors) - _GUIDED_RUNTIME_AUTHORITY_FIELDS)
        if unsupported:
            raise ServiceError("governed run contains caller-owned authority fields")
        required_selectors = (
            tuple(
                field
                for field in _GUIDED_RUNTIME_REQUIRED_SELECTORS
                if field != "profile_id"
            )
            if qa_mode
            else _GUIDED_RUNTIME_REQUIRED_SELECTORS
        )
        if any(
            not str(selectors.get(field) or "").strip()
            for field in required_selectors
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

        authority_request = {
            field: selectors.get(field)
            for field in _GUIDED_RUNTIME_RESOLVER_FIELDS
            if selectors.get(field) not in (None, "")
        }
        authority_request["expected_execution_state_revision"] = expected_revision
        qa_session_token = str(payload.get("qa_session_token") or "")
        if qa_mode and not qa_session_token:
            raise ServiceError("QA run requires a transient QA session token")
        if qa_mode:
            ticket = resolve_governance_execution_ticket(
                authority_request,
                qa_session_token=qa_session_token,
            )
        else:
            ticket = self._contract_runtime_authority_resolver(authority_request)
        qa_session_token = ""
        if not isinstance(ticket, Mapping):
            raise ServiceError("ContractRuntime resolver returned an invalid ticket")
        dispatch = ticket.get("dispatch_identity")
        profile_requirements = ticket.get("profile_requirements")
        retry_policy = ticket.get("retry_policy")
        next_legal_action = ticket.get("next_legal_action")
        if (
            ticket.get("status") != "issued"
            or ticket.get("issue_allowed") is not True
            or ticket.get("source_of_authority") != "ContractRuntime"
            or ticket.get("authority_decision_source")
            not in (
                {"contract_runtime_qa_execution_ticket"}
                if qa_mode
                else {"contract_runtime_completed_dispatch_line"}
            )
            or ticket.get("immutable") is not True
            or ticket.get("consumed") is not False
            or not isinstance(dispatch, Mapping)
            or not isinstance(profile_requirements, Mapping)
            or not isinstance(retry_policy, Mapping)
            or not isinstance(next_legal_action, Mapping)
        ):
            raise ServiceError("ContractRuntime resolver did not issue a canonical ticket")

        ticket_id = str(ticket.get("ticket_id") or "").strip()
        ticket_hash = str(ticket.get("ticket_hash") or "").strip()
        ticket_material = dict(ticket)
        ticket_material.pop("ticket_hash", None)
        integrity_mismatches: list[str] = []
        if not ticket_id:
            integrity_mismatches.append("ticket_id")
        if not ticket_hash or _stable_json_hash(ticket_material) != ticket_hash:
            integrity_mismatches.append("ticket_hash")
        if _stable_json_hash(dict(dispatch)) != str(
            ticket.get("dispatch_identity_hash") or ""
        ).strip():
            integrity_mismatches.append("dispatch_identity_hash")
        if _stable_json_hash(dict(profile_requirements)) != str(
            ticket.get("profile_requirements_hash") or ""
        ).strip():
            integrity_mismatches.append("profile_requirements_hash")
        if _stable_json_hash(dict(retry_policy)) != str(
            ticket.get("retry_policy_hash") or ""
        ).strip():
            integrity_mismatches.append("retry_policy_hash")
        if _stable_json_hash(dict(next_legal_action)) != str(
            ticket.get("next_legal_action_hash") or ""
        ).strip():
            integrity_mismatches.append("next_legal_action_hash")
        if integrity_mismatches:
            raise ServiceError(
                "ContractRuntime ticket integrity is invalid: "
                + ", ".join(sorted(set(integrity_mismatches)))
            )

        selector_comparisons = {
            "project_id": dispatch.get("project_id"),
            "backlog_id": dispatch.get("backlog_id"),
            "contract_execution_id": ticket.get("contract_execution_id"),
            "runtime_context_id": dispatch.get("runtime_context_id"),
            "task_id": dispatch.get("task_id"),
            "worker_id": dispatch.get("worker_id"),
            "worker_slot_id": dispatch.get("worker_slot_id"),
            "observer_command_id": dispatch.get("observer_command_id"),
            "principal_id": dispatch.get("worker_id"),
            "role": dispatch.get("worker_role"),
            **{
                field: dispatch.get(field)
                for field in _GUIDED_RUNTIME_ROUTE_FIELDS
            },
        }
        if not qa_mode:
            selector_comparisons["profile_id"] = profile_requirements.get("profile_id")
        mismatches = [
            field
            for field, canonical in selector_comparisons.items()
            if str(canonical or "").strip()
            != str(selectors.get(field) or "").strip()
        ]
        for field in _GUIDED_RUNTIME_PROFILE_FIELDS:
            expected = str(selectors.get(field) or "").strip()
            if expected and expected != str(
                profile_requirements.get(field) or ""
            ).strip():
                mismatches.append(field)
        role = str(dispatch.get("worker_role") or "").strip().lower()
        if qa_mode and role != "qa":
            mismatches.append("qa_role")
        profile_role = str(profile_requirements.get("role") or "").strip().lower()
        if profile_role and profile_role != role:
            mismatches.append("profile_requirements.role")
        worktree = str(dispatch.get("worktree_path") or "").strip()
        if not worktree:
            mismatches.append("worktree")
        try:
            ticket_revision = int(ticket.get("execution_state_revision") or 0)
        except (TypeError, ValueError):
            ticket_revision = -1
        if ticket_revision != expected_revision:
            mismatches.append("execution_state_revision")
        for field in (
            "expected_execution_state_hash",
            "expected_dispatch_identity_hash",
        ):
            expected = str(selectors.get(field) or "").strip()
            ticket_field = field.removeprefix("expected_")
            if expected and str(ticket.get(ticket_field) or "").strip() != expected:
                mismatches.append(ticket_field)
        for field in _GUIDED_RUNTIME_ROUTE_FIELDS:
            if str(next_legal_action.get(field) or "").strip() != str(
                dispatch.get(field) or ""
            ).strip():
                mismatches.append("next_legal_action.{}".format(field))
        for field in (
            "runtime_context_id",
            "task_id",
            "worker_id",
            "worker_slot_id",
            "observer_command_id",
        ):
            if str(next_legal_action.get(field) or "").strip() != str(
                dispatch.get(field) or ""
            ).strip():
                mismatches.append("next_legal_action.{}".format(field))
        if mismatches:
            raise ServiceError(
                "ContractRuntime ticket does not match run admission: "
                + ", ".join(sorted(set(mismatches)))
            )

        project_id = str(dispatch.get("project_id") or "").strip()
        evidence_refs = {
            "project_id": str(dispatch.get("project_id") or ""),
            "backlog_id": str(dispatch.get("backlog_id") or ""),
            "contract_execution_id": str(ticket.get("contract_execution_id") or ""),
            "runtime_context_id": str(dispatch.get("runtime_context_id") or ""),
            "task_id": str(dispatch.get("task_id") or ""),
        }
        principal_id = str(dispatch.get("worker_id") or "")
        canonical_run_id = "run-{}".format(ticket_id)
        run_id = canonical_run_id
        parent_run_id = ""
        successor_of_run_id = ""
        expected_backend = str(selectors.get("backend_mode") or "").strip()
        scheduler_requirements = dict(profile_requirements)
        scheduler_requirements["role"] = role
        if expected_backend:
            scheduler_requirements["backend_mode"] = expected_backend

        try:
            ticket_attempt = int(retry_policy.get("attempt") or 0)
            max_attempts = int(retry_policy.get("max_attempts") or 0)
        except (TypeError, ValueError) as exc:
            raise ServiceError("canonical execution ticket retry policy is invalid") from exc
        # ContractRuntime numbers the initial dispatch at attempt zero.  The
        # operational spawn loop below starts at the following one-based
        # attempt, so zero is a valid canonical ticket value here.
        if ticket_attempt < 0 or max_attempts < ticket_attempt:
            raise ServiceError("canonical execution ticket retry policy is invalid")

        terminal_failure_states = {
            RunState.FAILED.value,
            RunState.LOST.value,
        }
        canonical_run = self.registry.get_run(canonical_run_id)
        expected_profile_id = str(
            profile_requirements.get("profile_id") or ""
        ).strip()
        if canonical_run is not None:
            canonical_refs = {
                ref.name: ref.value for ref in canonical_run.evidence_refs
            }
            if (
                (expected_profile_id and canonical_run.run.config.profile_id != expected_profile_id)
                or canonical_run.run.config.role != role
                or canonical_run.run.config.project_id != project_id
                or any(
                    canonical_refs.get(name) != value
                    for name, value in evidence_refs.items()
                )
            ):
                raise ServiceError(
                    "canonical execution run identity does not match the ticket"
                )
            if canonical_run.state == RunState.COMPLETED.value:
                raise ServiceError("canonical execution run is already complete")
            if canonical_run.state not in terminal_failure_states:
                raise ServiceError("canonical execution run is already admitted")
        if canonical_run is not None and canonical_run.state in terminal_failure_states:
            retry_allowed = (
                str(retry_policy.get("on_crash") or "").strip()
                == "retry_same_profile"
                and retry_policy.get("successor_required") is True
            )
            if not retry_allowed:
                raise ServiceError(
                    "canonical execution ticket does not permit a same-profile retry"
                )

            predecessor_run_id = canonical_run_id
            for operational_attempt in range(ticket_attempt + 1, max_attempts + 1):
                candidate_run_id = "{}-attempt-{}".format(
                    canonical_run_id,
                    operational_attempt,
                )
                candidate = self.registry.get_run(candidate_run_id)
                if candidate is not None:
                    candidate_refs = {
                        ref.name: ref.value for ref in candidate.evidence_refs
                    }
                    if (
                        candidate.run.config.profile_id != expected_profile_id
                        or candidate.run.config.role != role
                        or candidate.run.config.project_id != project_id
                        or candidate.run.parent_run_id != canonical_run_id
                        or candidate.run.successor_of_run_id != predecessor_run_id
                        or any(
                            candidate_refs.get(name) != value
                            for name, value in evidence_refs.items()
                        )
                    ):
                        raise ServiceError(
                            "canonical execution retry lineage is invalid"
                        )
                    if candidate.state == RunState.COMPLETED.value:
                        raise ServiceError("canonical execution run is already complete")
                    if candidate.state in terminal_failure_states:
                        predecessor_run_id = candidate_run_id
                        continue
                    raise ServiceError(
                        "canonical execution retry attempt is already admitted"
                    )
                run_id = candidate_run_id
                parent_run_id = canonical_run_id
                successor_of_run_id = predecessor_run_id
                break
            else:
                raise ServiceError("canonical execution ticket retry attempts exhausted")

        schedule_kwargs: dict[str, Any] = {}
        if successor_of_run_id:
            schedule_kwargs = {
                "parent_run_id": parent_run_id,
                "successor_of_run_id": successor_of_run_id,
            }
        try:
            scheduled = self.scheduler.schedule_run(
                run_id=run_id,
                owner_id=self.supervisor.owner_id,
                role=role,
                project_id=project_id,
                profile_requirements=scheduler_requirements,
                governance_refs=evidence_refs,
                evidence_refs=evidence_refs,
                **schedule_kwargs,
                require_new_run=True,
                ttl_seconds=max(
                    1,
                    int(getattr(self.supervisor, "lease_ttl_seconds", 60) or 60),
                ),
            )
        except RunIdentityConflictError as exc:
            raise ServiceError(
                "canonical execution run is already admitted"
            ) from exc
        except (RegistryError, SchedulerError, TypeError, ValueError) as exc:
            raise ServiceError(
                "canonical execution ticket has no eligible registered profile"
            ) from exc
        run = scheduled.run
        profile = run.profile
        lease = scheduled.lease
        if (
            profile is None
            or (
                expected_profile_id
                and profile.profile_id != expected_profile_id
            )
            or profile.inference_endpoint.backend_mode != expected_backend
            or lease.run_id != run.run_id
            or lease.profile_id != profile.profile_id
            or lease.owner_id != self.supervisor.owner_id
            or lease.status != "active"
        ):
            self.registry.record_exit(
                run.run_id,
                127,
                failure_category="scheduled_identity_mismatch",
            )
            raise ServiceError("scheduler result does not match the canonical ticket")
        prompt = (
            "Proceed as the allocated Aming Claw {role} worker.\n"
            "Runtime context: {runtime_context_id}\n"
            "Task: {task_id}\n"
            "Contract execution: {contract_execution_id}\n"
            "Read the current worker guide and follow ContractRuntime's current "
            "next legal action. After each accepted ContractRuntime line, re-read "
            "the current guide and continue through worker startup, graph context, "
            "bounded implementation, worker commit, finish-time worker attestation, "
            "and finish gate. Stop only at "
            "terminal completion or a real blocker reported by the current guide. "
            "ContractRuntime, not this operational prompt, is the authority; remain "
            "within the allocated worker scope."
        ).format(
            role=role,
            runtime_context_id=evidence_refs["runtime_context_id"],
            task_id=evidence_refs["task_id"],
            contract_execution_id=evidence_refs["contract_execution_id"],
        )
        if qa_mode:
            prompt = (
                "Proceed as the ContractRuntime-authorized independent QA verifier.\n"
                "Runtime context: {runtime_context_id}\n"
                "Task: {task_id}\n"
                "Contract execution: {contract_execution_id}\n"
                "Read the current QA guide, run the bounded graph context and focused "
                "verification, then record exactly one audited QA verdict."
            ).format(**evidence_refs)
        supplied_host_envelope = payload.get("host_envelope")
        canonical_host_envelope: dict[str, Any] = {
            "project_id": evidence_refs["project_id"],
            "backlog_id": evidence_refs["backlog_id"],
            "runtime_context_id": evidence_refs["runtime_context_id"],
            "task_id": evidence_refs["task_id"],
            "parent_task_id": str(dispatch.get("parent_task_id") or ""),
            "worker_role": role,
            "worker_id": principal_id,
            "worker_slot_id": str(dispatch.get("worker_slot_id") or ""),
        }
        try:
            if not qa_mode and (not isinstance(supplied_host_envelope, Mapping) or set(
                supplied_host_envelope
            ) != {"env"}):
                raise HostEnvelopeError(
                    "governed run requires one transient worker host envelope"
                )
            if not qa_mode:
                canonical_host_envelope["env"] = supplied_host_envelope.get("env")
            public_text = json.dumps(
                {
                    "run": run.to_public_dict(),
                    "worktree": worktree,
                    "execution_ticket_id": ticket_id,
                    "execution_ticket_hash": ticket_hash,
                    "evidence_refs": evidence_refs,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            if not qa_mode:
                self.host_envelope_store.stage(
                    run.run_id,
                    canonical_host_envelope,
                    lease_owner_id=lease.owner_id,
                    public_text=public_text,
                )
            del public_text
            self.supervisor.start_run(
                run,
                prompt=prompt,
                worktree=worktree,
                evidence_refs=evidence_refs,
                execution_ticket=ticket,
                require_host_envelope=not qa_mode,
            )
        except BaseException as exc:
            try:
                self.host_envelope_store.revoke(
                    run.run_id,
                    lease_owner_id=lease.owner_id,
                )
            except Exception:
                pass
            try:
                self.supervisor.cancel_run(run.run_id)
            except Exception:
                pass
            record = self.registry.get_run(run.run_id)
            if record is not None and record.lease is not None:
                try:
                    self.registry.record_exit(
                        run.run_id,
                        127,
                        failure_category="governed_admission_failed",
                    )
                except Exception:
                    pass
            raise ServiceError("governed run could not be started") from exc
        return {
            "ok": True,
            "status": "started",
            "run_id": run.run_id,
            "profile_id": profile.profile_id,
            "role": role,
            "principal_id": principal_id,
            "runtime_context_id": evidence_refs["runtime_context_id"],
            "task_id": evidence_refs["task_id"],
            "contract_execution_id": evidence_refs["contract_execution_id"],
            "execution_ticket_id": ticket_id,
            "execution_ticket_hash": ticket_hash,
            "selection_reason": scheduled.selection.selection_reason,
            "direct_invocation_fallback": False,
            "caller_run_accepted": False,
            "caller_prompt_accepted": False,
            "caller_environment_accepted": False,
            "transient_host_envelope_required": not qa_mode,
            "transient_host_envelope_accepted": not qa_mode,
            "transient_host_envelope_consumed": not qa_mode,
            "transient_host_envelope_persisted": False,
            "host_envelope_run_authority": False,
            "raw_argv_persisted": False,
            "raw_environment_persisted": False,
            "provider_home_path_persisted": False,
            "raw_credentials_persisted": False,
            "raw_route_token_persisted": False,
            "raw_session_token_persisted": False,
            "raw_fence_token_persisted": False,
            "transient_qa_session_token_accepted": qa_mode,
            "transient_qa_session_token_persisted": False,
            "raw_prompt_persisted": False,
            "raw_provider_output_persisted": False,
            "provider_output_suppressed": True,
            "governance_authority": False,
            "operational_dispatch_only": True,
        }

    def _dispatch(self, request: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        operation = str(request.get("operation") or "").strip().lower()
        if operation == "health":
            return self._snapshot(), False
        if operation == "status":
            return self._snapshot(accepting_agent_runs=False), False
        if operation == "stop":
            return self._snapshot(stopping=True), True
        if operation in PROFILE_OPERATIONS:
            payload = request.get("payload")
            if payload is None:
                payload = {}
            if not isinstance(payload, Mapping):
                raise ServiceError("profile operation payload must be an object")
            try:
                return self.profile_control.dispatch(operation, payload), False
            except ProfileControlError as exc:
                raise ServiceError(str(exc)) from exc
        if operation == "start_host_envelope_run":
            payload = request.get("payload")
            if not isinstance(payload, Mapping):
                raise ServiceError("host envelope run payload must be an object")
            return self._admit_governed_host_envelope_run(payload), False
        if operation == "start_qa_execution_ticket_run":
            payload = request.get("payload")
            if not isinstance(payload, Mapping):
                raise ServiceError("QA execution ticket payload must be an object")
            return self._admit_governed_host_envelope_run(
                payload,
                qa_mode=True,
            ), False
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
                "start_qa_execution_ticket_run",
                *sorted(PROFILE_OPERATIONS),
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
