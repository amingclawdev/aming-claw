"""Managed CLI profile login preparation, status, and activation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .adapters.claude_cli import (
    CLAUDE_SUBSCRIPTION_ISOLATION_SUPPORTED,
    ClaudeAdapterError,
    ClaudeCliAdapter,
)
from .adapters.codex_cli import CodexAdapterError, CodexCliAdapter


PROFILE_AUTH_SCHEMA_VERSION = "cli_agent_service.profile_auth.v1"
PROFILE_LOGIN_ACTION_SCHEMA_VERSION = "cli_agent_service.profile_login_action.v1"
PROFILE_AUTH_EVIDENCE_SCHEMA_VERSION = "cli_agent_service.profile_auth_evidence.v1"

AUTH_STATES = (
    "discovered",
    "login_required",
    "login_in_progress",
    "ready",
    "expired",
    "revoked",
    "blocked",
    "error",
)
DISCOVERED = "discovered"
LOGIN_REQUIRED = "login_required"
LOGIN_IN_PROGRESS = "login_in_progress"
READY = "ready"
EXPIRED = "expired"
REVOKED = "revoked"
BLOCKED = "blocked"
ERROR = "error"

_PROFILE_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,127}")
_PROVIDER_ALIASES = {
    "codex": "codex",
    "openai": "codex",
    "claude": "claude",
    "anthropic": "claude",
}
_DIRECT_AUTH_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_ACCESS_TOKEN",
    "CODEX_API_KEY",
}
_PROFILE_ENV_KEYS = {"CODEX_HOME", "CLAUDE_CONFIG_DIR"}
_READY_MARKERS = (
    "authenticated",
    "logged in",
    "logged_in",
    "login successful",
)
_LOGIN_REQUIRED_MARKERS = (
    "authentication required",
    "login required",
    "not authenticated",
    "not logged in",
    "logged out",
    "logged_out",
    "please log in",
    "unauthenticated",
)
_EXPIRED_MARKERS = (
    "authentication expired",
    "credentials expired",
    "session expired",
    "token expired",
)
_REVOKED_MARKERS = (
    "access revoked",
    "credentials revoked",
    "invalid grant",
    "token revoked",
)
_BLOCKED_MARKERS = (
    "account blocked",
    "account disabled",
    "access denied",
    "authorization prompt",
    "errsecinteractionnotallowed",
    "keychain access denied",
    "keychain is locked",
    "user interaction is not allowed",
)
_READY_JSON_VALUES = {"authenticated", "logged_in", "ready", "ok"}
_LOGIN_REQUIRED_JSON_VALUES = {
    "logged_out",
    "login_required",
    "not_authenticated",
    "unauthenticated",
}


class ProfileAuthError(ValueError):
    pass


@dataclass(frozen=True)
class ProfileAuthResult(Mapping[str, Any]):
    profile_id: str
    provider: str
    state: str
    reason_code: str
    profile_home: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    login_session_id: str = ""
    copy_command: str = ""
    actions: tuple[Mapping[str, Any], ...] = ()
    activated: bool = False
    environment: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = field(default=PROFILE_AUTH_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        if self.state not in AUTH_STATES:
            raise ValueError("invalid profile auth state")
        object.__setattr__(self, "evidence", dict(self.evidence))
        object.__setattr__(self, "actions", tuple(dict(item) for item in self.actions))
        object.__setattr__(self, "environment", dict(self.environment))

    @property
    def ok(self) -> bool:
        return self.state not in {BLOCKED, ERROR}

    def to_public_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "profile_id": self.profile_id,
            "provider": self.provider,
            "state": self.state,
            "reason_code": self.reason_code,
            "profile_home": self.profile_home,
            "login_session_id": self.login_session_id,
            "copy_command": self.copy_command,
            "login_command": self.copy_command,
            "actions": [dict(item) for item in self.actions],
            "activated": self.activated,
            "profile_environment": dict(self.environment),
            "evidence": dict(self.evidence),
            "raw_credential_material_exposed": False,
            "raw_provider_output_exposed": False,
            "raw_provider_output_persisted": False,
        }
        return payload

    def __getitem__(self, key: str) -> Any:
        return self.to_public_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_public_dict())

    def __len__(self) -> int:
        return len(self.to_public_dict())


def _normalize_provider(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    try:
        return _PROVIDER_ALIASES[normalized]
    except KeyError as exc:
        raise ProfileAuthError("provider must be codex or claude") from exc


def _validate_profile_id(profile_id: str) -> str:
    normalized = str(profile_id or "").strip().lower()
    if not _PROFILE_ID_PATTERN.fullmatch(normalized):
        raise ProfileAuthError("profile_id is not a safe managed profile id")
    return normalized


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _output_hash(stdout: Any, stderr: Any) -> str:
    value = (_text(stdout) + "\0" + _text(stderr)).encode(
        "utf-8", errors="replace"
    )
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json_auth_state(output: str) -> str:
    try:
        decoded = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return ""
    pending = [decoded]
    visited = 0
    while pending and visited < 100:
        value = pending.pop()
        visited += 1
        if isinstance(value, Mapping):
            for key in ("loggedIn", "logged_in", "authenticated"):
                state = value.get(key)
                if isinstance(state, bool):
                    return READY if state else LOGIN_REQUIRED
            for key in ("status", "authStatus", "auth_status"):
                state = str(value.get(key) or "").strip().lower()
                if state in _READY_JSON_VALUES:
                    return READY
                if state in _LOGIN_REQUIRED_JSON_VALUES:
                    return LOGIN_REQUIRED
                if state in {EXPIRED, REVOKED, BLOCKED, ERROR}:
                    return state
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return ""


def classify_auth_state(
    provider: str,
    returncode: int | None,
    stdout: Any = "",
    stderr: Any = "",
    *,
    timed_out: bool = False,
    launch_error: bool = False,
) -> tuple[str, str]:
    """Classify provider status output without returning or persisting it."""

    normalized_provider = _normalize_provider(provider)
    combined = (_text(stdout) + "\n" + _text(stderr)).casefold()
    if timed_out:
        return BLOCKED, "{}_status_probe_timed_out".format(normalized_provider)
    if launch_error:
        return ERROR, "{}_status_launch_failed".format(normalized_provider)
    if any(marker in combined for marker in _REVOKED_MARKERS):
        return REVOKED, "{}_authentication_revoked".format(normalized_provider)
    if any(marker in combined for marker in _EXPIRED_MARKERS):
        return EXPIRED, "{}_authentication_expired".format(normalized_provider)
    if any(marker in combined for marker in _BLOCKED_MARKERS):
        return BLOCKED, "{}_authentication_blocked".format(normalized_provider)
    json_state = _json_auth_state(_text(stdout).strip())
    if json_state:
        return json_state, "{}_status_{}".format(normalized_provider, json_state)
    if any(marker in combined for marker in _LOGIN_REQUIRED_MARKERS):
        return LOGIN_REQUIRED, "{}_login_required".format(normalized_provider)
    if returncode == 0 and any(marker in combined for marker in _READY_MARKERS):
        return READY, "{}_status_ready".format(normalized_provider)
    if returncode == 0:
        return ERROR, "{}_status_response_unrecognized".format(normalized_provider)
    return ERROR, "{}_status_command_failed".format(normalized_provider)


class ProfileAuthController:
    """Own clean profile homes and fixed provider authentication operations."""

    def __init__(
        self,
        profiles_root: str | os.PathLike[str],
        *,
        codex_executable: str = "",
        claude_executable: str = "",
        runner: Callable[..., Any] | None = None,
        timeout_seconds: float = 5.0,
        claude_spike_decision: str | Mapping[str, str] = "",
    ) -> None:
        self.profiles_root = Path(profiles_root).expanduser()
        self.codex = CodexCliAdapter(executable=codex_executable)
        self.claude = ClaudeCliAdapter(executable=claude_executable)
        self.runner = runner
        self.timeout_seconds = max(float(timeout_seconds), 0.05)
        self.claude_spike_decision = claude_spike_decision

    def _profile_paths(self, profile_id: str, provider: str) -> tuple[Path, Path]:
        safe_profile_id = _validate_profile_id(profile_id)
        safe_provider = _normalize_provider(provider)
        profile_dir = self.profiles_root / safe_provider / safe_profile_id
        return profile_dir, profile_dir / "home"

    def _assert_managed_path(self, path: Path) -> Path:
        root = self.profiles_root.absolute()
        candidate = path.absolute()
        resolved_root = root.resolve(strict=False)
        if resolved_root != root:
            raise ProfileAuthError(
                "managed profiles root cannot contain symlinked ancestors"
            )
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ProfileAuthError(
                "managed profile path escapes profiles root"
            ) from exc

        current = root
        if current.is_symlink():
            raise ProfileAuthError("managed profile path contains a symlink")
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ProfileAuthError("managed profile path contains a symlink")

        resolved_candidate = candidate.resolve(strict=False)
        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ProfileAuthError(
                "managed profile path escapes profiles root"
            ) from exc
        return resolved_candidate

    def _prepare_private_directory(self, path: Path) -> bool:
        self._assert_managed_path(path)
        if path.is_symlink():
            raise ProfileAuthError("managed profile directories cannot be symlinks")
        created = not path.exists()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._assert_managed_path(path)
        if not path.is_dir() or path.is_symlink():
            raise ProfileAuthError("managed profile directory is invalid")
        os.chmod(path, 0o700)
        return created

    def ensure_profile_home(self, profile_id: str, provider: str) -> tuple[Path, bool]:
        profile_dir, home = self._profile_paths(profile_id, provider)
        self._prepare_private_directory(self.profiles_root)
        self._prepare_private_directory(profile_dir.parent)
        self._prepare_private_directory(profile_dir)
        created = self._prepare_private_directory(home)
        return home.resolve(), created

    def managed_profile_home(self, profile_id: str, provider: str) -> Path:
        """Return the canonical server-owned home without accepting a path."""

        profile_dir, home = self._profile_paths(profile_id, provider)
        self._assert_managed_path(profile_dir)
        return self._assert_managed_path(home)

    @staticmethod
    def _state_path(profile_dir: Path) -> Path:
        return profile_dir / "auth-state.json"

    def _read_state(self, profile_dir: Path) -> dict[str, Any]:
        state_path = self._state_path(profile_dir)
        self._assert_managed_path(state_path)
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, profile_dir: Path, result: ProfileAuthResult) -> None:
        payload = {
            "schema_version": PROFILE_AUTH_SCHEMA_VERSION,
            "profile_id": result.profile_id,
            "provider": result.provider,
            "state": result.state,
            "reason_code": result.reason_code,
            "login_session_id": result.login_session_id,
            "activated": result.activated,
            "evidence": dict(result.evidence),
            "raw_credential_material_persisted": False,
            "raw_provider_output_persisted": False,
        }
        state_path = self._state_path(profile_dir)
        self._assert_managed_path(state_path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}.".format(state_path.name),
            suffix=".tmp",
            dir=profile_dir,
        )
        temporary = Path(temporary_name)
        try:
            self._assert_managed_path(temporary)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = -1
            with handle:
                handle.write(
                    json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _adapter(self, provider: str) -> CodexCliAdapter | ClaudeCliAdapter:
        return self.codex if provider == "codex" else self.claude

    @staticmethod
    def _operation_evidence(
        *,
        profile_id: str,
        provider: str,
        operation: str,
        environment_key: str,
        reason_code: str,
        **values: Any,
    ) -> dict[str, Any]:
        evidence = {
            "schema_version": PROFILE_AUTH_EVIDENCE_SCHEMA_VERSION,
            "profile_id": profile_id,
            "provider": provider,
            "operation": operation,
            "environment_keys": [environment_key],
            "reason_code": reason_code,
            "provider_specific": True,
            "raw_credential_material_exposed": False,
            "raw_provider_output_exposed": False,
            "raw_provider_output_persisted": False,
        }
        evidence.update(values)
        if provider == "claude":
            evidence.update(ClaudeCliAdapter.subscription_isolation_evidence())
        return evidence

    def _command_contract(
        self,
        profile_id: str,
        provider: str,
        home: Path,
        *,
        status: bool,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        adapter = self._adapter(provider)
        environment = adapter.profile_environment(home)
        command = (
            adapter.build_auth_status_command(profile_home=home)
            if status
            else adapter.build_login_command(profile_home=home)
        )
        return command, environment

    def discover(self, profile_id: str, provider: str) -> ProfileAuthResult:
        safe_profile_id = _validate_profile_id(profile_id)
        safe_provider = _normalize_provider(provider)
        profile_dir, home = self._profile_paths(safe_profile_id, safe_provider)
        resolved_home = self._assert_managed_path(home)
        if not home.is_dir():
            return ProfileAuthResult(
                profile_id=safe_profile_id,
                provider=safe_provider,
                state=DISCOVERED,
                reason_code="managed_profile_not_prepared",
                evidence=self._operation_evidence(
                    profile_id=safe_profile_id,
                    provider=safe_provider,
                    operation="{}_profile_discovery".format(safe_provider),
                    environment_key=(
                        "CODEX_HOME" if safe_provider == "codex" else "CLAUDE_CONFIG_DIR"
                    ),
                    reason_code="managed_profile_not_prepared",
                    profile_home_exists=False,
                ),
            )
        previous = self._read_state(profile_dir)
        state = str(previous.get("state") or DISCOVERED)
        if state not in AUTH_STATES:
            state = ERROR
        return ProfileAuthResult(
            profile_id=safe_profile_id,
            provider=safe_provider,
            state=state,
            reason_code=str(previous.get("reason_code") or "profile_discovered"),
            profile_home=str(resolved_home),
            login_session_id=str(previous.get("login_session_id") or ""),
            activated=bool(previous.get("activated")),
            evidence=(
                previous.get("evidence")
                if isinstance(previous.get("evidence"), Mapping)
                else {}
            ),
        )

    def prepare_login(self, profile_id: str, provider: str) -> ProfileAuthResult:
        safe_profile_id = _validate_profile_id(profile_id)
        safe_provider = _normalize_provider(provider)
        home, created = self.ensure_profile_home(safe_profile_id, safe_provider)
        profile_dir = home.parent
        home_was_empty = not any(home.iterdir())
        environment_key = "CODEX_HOME" if safe_provider == "codex" else "CLAUDE_CONFIG_DIR"
        try:
            command, environment = self._command_contract(
                safe_profile_id, safe_provider, home, status=False
            )
        except (CodexAdapterError, ClaudeAdapterError) as exc:
            reason_code = "{}_login_cli_unavailable".format(safe_provider)
            result = ProfileAuthResult(
                profile_id=safe_profile_id,
                provider=safe_provider,
                state=ERROR,
                reason_code=reason_code,
                profile_home=str(home),
                evidence=self._operation_evidence(
                    profile_id=safe_profile_id,
                    provider=safe_provider,
                    operation="{}_login_prepare".format(safe_provider),
                    environment_key=environment_key,
                    reason_code=reason_code,
                    detail=str(exc),
                    profile_home_created=created,
                    profile_home_was_empty=home_was_empty,
                ),
            )
            self._write_state(profile_dir, result)
            return result

        copy_command = shlex.join(
            ("env", "{}={}".format(environment_key, home), *command)
        )
        login_session_id = "login-{}".format(uuid.uuid4().hex)
        reason_code = "{}_user_login_action_required".format(safe_provider)
        actions = (
            {
                "schema_version": PROFILE_LOGIN_ACTION_SCHEMA_VERSION,
                "action": "open_terminal",
                "user_triggered": True,
                "auto_execute": False,
                "copy_safe": True,
                "command": copy_command,
            },
            {
                "schema_version": PROFILE_LOGIN_ACTION_SCHEMA_VERSION,
                "action": "copy_command",
                "user_triggered": True,
                "auto_execute": False,
                "copy_safe": True,
                "command": copy_command,
            },
        )
        result = ProfileAuthResult(
            profile_id=safe_profile_id,
            provider=safe_provider,
            state=LOGIN_IN_PROGRESS,
            reason_code=reason_code,
            profile_home=str(home),
            login_session_id=login_session_id,
            copy_command=copy_command,
            actions=actions,
            evidence=self._operation_evidence(
                profile_id=safe_profile_id,
                provider=safe_provider,
                operation="{}_login_prepare".format(safe_provider),
                environment_key=environment_key,
                reason_code=reason_code,
                login_args=list(command[1:]),
                profile_home_created=created,
                profile_home_was_empty=home_was_empty,
                login_session_id=login_session_id,
                user_triggered=True,
                command_copy_safe=True,
            ),
            environment=environment,
        )
        self._write_state(profile_dir, result)
        return result

    def _probe_environment(self, profile_environment: Mapping[str, str]) -> dict[str, str]:
        environment = dict(os.environ)
        for key in _DIRECT_AUTH_ENV_KEYS | _PROFILE_ENV_KEYS:
            environment.pop(key, None)
        environment.update(profile_environment)
        environment.update({"CI": "1", "NO_COLOR": "1", "TERM": "dumb"})
        return environment

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> None:
        if not hasattr(os, "killpg"):
            process.kill()
            process.wait(timeout=1.0)
            return

        process_group_id = process.pid
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass

        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        os.killpg(process_group_id, signal.SIGKILL)
        process.wait(timeout=1.0)

    def _run_status_process(
        self,
        command: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> subprocess.CompletedProcess:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(environment),
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_group(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                exc.cmd,
                exc.timeout,
                output=stdout,
                stderr=stderr,
            ) from None
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )

    def auth_status(self, profile_id: str, provider: str) -> ProfileAuthResult:
        safe_profile_id = _validate_profile_id(profile_id)
        safe_provider = _normalize_provider(provider)
        profile_dir, home = self._profile_paths(safe_profile_id, safe_provider)
        resolved_home = self._assert_managed_path(home)
        if not home.is_dir():
            return self.discover(safe_profile_id, safe_provider)
        home = resolved_home
        environment_key = "CODEX_HOME" if safe_provider == "codex" else "CLAUDE_CONFIG_DIR"
        try:
            command, profile_environment = self._command_contract(
                safe_profile_id, safe_provider, home, status=True
            )
        except (CodexAdapterError, ClaudeAdapterError) as exc:
            state, reason_code = classify_auth_state(
                safe_provider, None, launch_error=True
            )
            result = ProfileAuthResult(
                profile_id=safe_profile_id,
                provider=safe_provider,
                state=state,
                reason_code=reason_code,
                profile_home=str(home),
                evidence=self._operation_evidence(
                    profile_id=safe_profile_id,
                    provider=safe_provider,
                    operation="{}_auth_status".format(safe_provider),
                    environment_key=environment_key,
                    reason_code=reason_code,
                    detail=str(exc),
                    status_args=[],
                    exit_code=None,
                    timed_out=False,
                    output_hash="",
                ),
            )
            self._write_state(profile_dir, result)
            return result

        timed_out = False
        try:
            probe_environment = self._probe_environment(profile_environment)
            if self.runner is None:
                completed = self._run_status_process(command, probe_environment)
            else:
                completed = self.runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    stdin=subprocess.DEVNULL,
                    env=probe_environment,
                    start_new_session=True,
                )
            returncode = int(completed.returncode)
            stdout = completed.stdout
            stderr = completed.stderr
            state, reason_code = classify_auth_state(
                safe_provider, returncode, stdout, stderr
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = None
            stdout = exc.stdout
            stderr = exc.stderr
            state, reason_code = classify_auth_state(
                safe_provider,
                None,
                stdout,
                stderr,
                timed_out=True,
            )
        except OSError:
            returncode = None
            stdout = stderr = ""
            state, reason_code = classify_auth_state(
                safe_provider, None, launch_error=True
            )

        previous = self._read_state(profile_dir)
        result = ProfileAuthResult(
            profile_id=safe_profile_id,
            provider=safe_provider,
            state=state,
            reason_code=reason_code,
            profile_home=str(home),
            login_session_id=str(previous.get("login_session_id") or ""),
            activated=(
                state == READY and bool(previous.get("activated"))
            ),
            evidence=self._operation_evidence(
                profile_id=safe_profile_id,
                provider=safe_provider,
                operation="{}_auth_status".format(safe_provider),
                environment_key=environment_key,
                reason_code=reason_code,
                status_args=list(command[1:]),
                exit_code=returncode,
                timed_out=timed_out,
                output_hash=_output_hash(stdout, stderr),
                noninteractive=True,
            ),
            environment=profile_environment,
        )
        self._write_state(profile_dir, result)
        return result

    def _claude_spike_decision(self, profile_id: str) -> str:
        value = self.claude_spike_decision
        if isinstance(value, Mapping):
            return str(value.get(profile_id) or value.get("*") or "").strip()
        return str(value or "").strip()

    def activate(self, profile_id: str, provider: str) -> ProfileAuthResult:
        status = self.auth_status(profile_id, provider)
        if status.state != READY:
            return status
        evidence = dict(status.evidence)
        if status.provider == "claude":
            spike_decision = self._claude_spike_decision(status.profile_id)
            evidence.update(
                {
                    "claude_spike_decision": spike_decision or "missing",
                    "subscription_isolation_supported": (
                        CLAUDE_SUBSCRIPTION_ISOLATION_SUPPORTED
                    ),
                    "subscription_isolation_claimed": False,
                }
            )
            if spike_decision != "unattended-safe":
                reason_code = (
                    "claude_spike_{}_blocks_activation".format(spike_decision)
                    if spike_decision
                    else "claude_verified_spike_required"
                )
                evidence["reason_code"] = reason_code
                result = ProfileAuthResult(
                    profile_id=status.profile_id,
                    provider=status.provider,
                    state=BLOCKED,
                    reason_code=reason_code,
                    profile_home=status.profile_home,
                    login_session_id=status.login_session_id,
                    evidence=evidence,
                    environment=status.environment,
                )
                self._write_state(Path(status.profile_home).parent, result)
                return result

        reason_code = "{}_profile_activated".format(status.provider)
        evidence.update(
            {
                "operation": "{}_profile_activate".format(status.provider),
                "reason_code": reason_code,
                "activation_profile_scoped": True,
                "environment_keys": sorted(status.environment),
            }
        )
        result = ProfileAuthResult(
            profile_id=status.profile_id,
            provider=status.provider,
            state=READY,
            reason_code=reason_code,
            profile_home=status.profile_home,
            login_session_id=status.login_session_id,
            evidence=evidence,
            activated=True,
            environment=status.environment,
        )
        self._write_state(Path(status.profile_home).parent, result)
        return result

    status = auth_status
    verify = auth_status
    activate_profile = activate
