"""Small, separately supervised host daemon for CLI Agent Service foundation."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .health import health_payload, stopped_payload


MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_SOCKET_TIMEOUT_SECONDS = 3.0


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
    timeout_seconds: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request = json.dumps({"operation": str(operation)}, separators=(",", ":")).encode("utf-8") + b"\n"
    response = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(max(float(timeout_seconds), 0.05))
            client.connect(str(paths.socket_path))
            client.sendall(request)
            while len(response) <= MAX_REQUEST_BYTES:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if b"\n" in chunk:
                    break
    except (FileNotFoundError, ConnectionError, OSError, socket.timeout) as exc:
        raise ServiceUnavailableError("CLI Agent Service is not reachable") from exc
    if len(response) > MAX_REQUEST_BYTES:
        raise ServiceError("CLI Agent Service response exceeded the size limit")
    try:
        payload = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError("CLI Agent Service returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise ServiceError("CLI Agent Service returned an invalid response object")
    return payload


def current_status(paths: ServicePaths) -> dict[str, Any]:
    try:
        return request_service(paths, "status")
    except ServiceUnavailableError:
        payload = read_status(paths)
        if payload.get("status") not in {"stopped", "failed"}:
            payload = {**payload, "ok": False, "status": "unreachable", "socket_ready": False}
        return payload


class CliAgentService:
    """Foreground Unix-socket service intended to be supervised by launchd."""

    def __init__(self, paths: ServicePaths) -> None:
        self.paths = paths
        self.started_at = datetime.now(timezone.utc)
        self.pid = os.getpid()
        self._stop_event = threading.Event()
        self._server: socket.socket | None = None

    def _snapshot(self, *, stopping: bool = False) -> dict[str, Any]:
        return health_payload(
            pid=self.pid,
            started_at=self.started_at,
            socket_ready=self._server is not None,
            stopping=stopping,
        )

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
        while len(data) <= MAX_REQUEST_BYTES:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in chunk:
                break
        if len(data) > MAX_REQUEST_BYTES:
            raise ServiceError("request exceeded the size limit")
        value = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
        if not isinstance(value, dict):
            raise ServiceError("request must be a JSON object")
        return value

    def _dispatch(self, request: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        operation = str(request.get("operation") or "").strip().lower()
        if operation in {"health", "status"}:
            return self._snapshot(), False
        if operation == "stop":
            return self._snapshot(stopping=True), True
        return {
            "ok": False,
            "status": "invalid_request",
            "error": "unsupported operation",
            "allowed_operations": ["health", "status", "stop"],
        }, False

    def _handle_connection(self, connection: socket.socket) -> None:
        should_stop = False
        try:
            request = self._read_request(connection)
            response, should_stop = self._dispatch(request)
        except (ServiceError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            response = {"ok": False, "status": "invalid_request", "error": str(exc)}
        connection.sendall(json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
        if should_stop:
            self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()

    def serve_forever(self) -> None:
        self.paths.prepare()
        self._remove_stale_socket()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.paths.socket_path))
            os.chmod(self.paths.socket_path, 0o600)
            server.listen(16)
            server.settimeout(0.25)
            self._server = server
            _write_json(self.paths.status_path, self._snapshot())
            while not self._stop_event.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    self._handle_connection(connection)
        finally:
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
