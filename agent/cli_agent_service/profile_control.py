"""Fixed local control surface for server-owned managed CLI profiles."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from governance.contract_state_runtime import (
    cli_agent_managed_profile_tooling_binding,
    cli_agent_managed_profile_tooling_contract,
)

try:
    from agent.plugin_installer import (
        CODEX_MARKETPLACE_NAME,
        configure_codex_plugin,
        install_codex_marketplace,
        install_codex_plugin_cache,
    )
except ModuleNotFoundError:  # pragma: no cover - direct agent/ PYTHONPATH
    from plugin_installer import (  # type: ignore
        CODEX_MARKETPLACE_NAME,
        configure_codex_plugin,
        install_codex_marketplace,
        install_codex_plugin_cache,
    )

from .adapters.codex_cli import (
    CODEX_CLI_DEFAULT_MODEL,
    CODEX_LEGACY_MANAGED_DEFAULT_MODELS,
    CODEX_MANAGED_LAUNCHER_ID,
)
from .auth import ProfileAuthController, ProfileAuthError, ProfileAuthResult, READY
from .models import (
    AgentProfile,
    CredentialRef,
    HarnessRuntime,
    InferenceEndpoint,
    LauncherAdapter,
    RolePolicy,
)
from .registry import AgentRegistry, RegistryError


PROFILE_CONTROL_SCHEMA_VERSION = "cli_agent_service.profile_control.v1"
PROFILE_LIST_OPERATION = "profile_list"
PROFILE_LOGIN_PREPARE_OPERATION = "profile_login_prepare"
PROFILE_AUTH_STATUS_OPERATION = "profile_auth_status"
PROFILE_ACTIVATE_OPERATION = "profile_activate"
PROFILE_OPERATIONS = frozenset(
    {
        PROFILE_LIST_OPERATION,
        PROFILE_LOGIN_PREPARE_OPERATION,
        PROFILE_AUTH_STATUS_OPERATION,
        PROFILE_ACTIVATE_OPERATION,
    }
)

_PROFILE_INPUT_FIELDS = frozenset({"profile_id", "provider"})
_MANAGED_PROVIDER_ALIASES = {"codex": "codex", "openai": "codex"}


class ProfileControlError(ValueError):
    """A fixed profile-control request was invalid or not ready."""


class ProfileToolingError(ProfileControlError):
    """A managed profile could not satisfy its plugin/MCP contract."""


def _managed_provider(value: Any) -> str:
    provider = str(value or "codex").strip().lower()
    try:
        return _MANAGED_PROVIDER_ALIASES[provider]
    except KeyError as exc:
        raise ProfileControlError(
            "managed profile operations currently support codex only"
        ) from exc


def _credential_ref(profile_id: str) -> str:
    suffix = profile_id
    if len(suffix) > 63:
        suffix = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()[:32]
    return "credential:codex-home:{}".format(suffix)


def _bounded_codex_profile(
    profile_id: str,
    *,
    model: str = CODEX_CLI_DEFAULT_MODEL,
) -> AgentProfile:
    """Materialize the only profile shape this control surface can register."""

    return AgentProfile(
        profile_id=profile_id,
        version="1",
        harness_runtime=HarnessRuntime(
            runtime_id="runtime-codex-managed",
            version="1",
            kind="codex_cli",
            executable_ref="managed:codex",
            capabilities=("stdio", "worktree"),
        ),
        inference_endpoint=InferenceEndpoint(
            endpoint_id="endpoint-openai-managed-codex",
            version="1",
            provider="openai",
            model=model,
            backend_mode="codex_cli",
            auth_mode="cli_auth",
        ),
        credential_ref=CredentialRef(
            ref_id=_credential_ref(profile_id),
            version="1",
            provider="openai",
            ref_kind="provider_home",
        ),
        launcher_adapter=LauncherAdapter(
            launcher_id=CODEX_MANAGED_LAUNCHER_ID,
            version="1",
            environment_keys=("CODEX_HOME",),
            supports_host_handoff=True,
        ),
        role_policy=RolePolicy(
            policy_id="policy-codex-managed",
            version="1",
            roles=("observer", "dev", "test", "qa", "merge", "mf_sub"),
            max_concurrency=1,
            timeout_sec=300,
        ),
    )


def _is_bounded_codex_profile(profile: AgentProfile) -> bool:
    models = (CODEX_CLI_DEFAULT_MODEL, *CODEX_LEGACY_MANAGED_DEFAULT_MODELS)
    return any(
        profile == _bounded_codex_profile(profile.profile_id, model=model)
        for model in models
    )


class ManagedProfileControl:
    """Connect profile auth to the existing immutable operational registry."""

    def __init__(
        self,
        registry: AgentRegistry,
        auth_controller: ProfileAuthController,
        *,
        plugin_source_root: str | os.PathLike[str] | None = None,
        tooling_runner: Callable[..., Any] | None = None,
        tooling_timeout_seconds: float = 10.0,
    ) -> None:
        self.registry = registry
        self.auth_controller = auth_controller
        self.plugin_source_root = (
            Path(plugin_source_root).expanduser().resolve()
            if plugin_source_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.tooling_runner = tooling_runner or subprocess.run
        self.tooling_timeout_seconds = max(float(tooling_timeout_seconds), 0.05)
        self._tooling_lock = threading.RLock()

    @staticmethod
    def _auth_file_identity(profile_home: Path) -> tuple[int, ...] | None:
        auth_path = profile_home / "auth.json"
        try:
            stat = auth_path.stat()
        except FileNotFoundError:
            return None
        return (
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_mode),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
        )

    @staticmethod
    def _tooling_marker_path(profile_home: Path) -> Path:
        return profile_home / "managed-tooling" / "readiness.json"

    @staticmethod
    def _read_tooling_marker(profile_home: Path) -> dict[str, Any]:
        try:
            value = json.loads(
                ManagedProfileControl._tooling_marker_path(profile_home).read_text(
                    encoding="utf-8"
                )
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_tooling_marker(
        profile_home: Path,
        *,
        contract: Mapping[str, Any],
    ) -> None:
        marker_path = ManagedProfileControl._tooling_marker_path(profile_home)
        marker_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "schema_version": "cli_agent_service.managed_profile_tooling_readiness.v1",
            "ready": True,
            "tooling_contract": cli_agent_managed_profile_tooling_binding(),
            "plugin_id": str(contract["plugin_id"]),
            "plugin_version": str(contract["plugin_version"]),
            "mcp_server_name": str(contract["mcp_server_name"]),
            "repository_source_snapshot": True,
            "desktop_plugin_cache_copied": False,
            "raw_credentials_copied": False,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}.".format(marker_path.name),
            suffix=".tmp",
            dir=marker_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = -1
            with handle:
                handle.write(
                    json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, marker_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _source_tooling_contract(self) -> dict[str, Any]:
        contract = cli_agent_managed_profile_tooling_contract()
        try:
            manifest = json.loads(
                (self.plugin_source_root / ".codex-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
            )
            mcp = json.loads(
                (self.plugin_source_root / ".mcp.json").read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            raise ProfileToolingError(
                "managed Codex profile tooling source is invalid"
            ) from exc
        servers = mcp.get("mcpServers") if isinstance(mcp, Mapping) else None
        if (
            not isinstance(manifest, Mapping)
            or str(manifest.get("name") or "")
            != str(contract["plugin_id"]).split("@", 1)[0]
            or str(manifest.get("version") or "") != str(contract["plugin_version"])
            or not isinstance(servers, Mapping)
            or str(contract["mcp_server_name"]) not in servers
        ):
            raise ProfileToolingError(
                "managed Codex profile tooling source does not match its contract"
            )
        return contract

    @staticmethod
    def _tooling_environment(profile_home: Path) -> dict[str, str]:
        environment = dict(os.environ)
        for key in (
            "AMING_WORKER_SESSION_TOKEN",
            "AMING_WORKER_FENCE_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "OPENAI_ACCESS_TOKEN",
            "CODEX_API_KEY",
            "CLAUDE_CONFIG_DIR",
        ):
            environment.pop(key, None)
        environment.update(
            {
                "CODEX_HOME": str(profile_home),
                "CI": "1",
                "NO_COLOR": "1",
                "TERM": "dumb",
            }
        )
        return environment

    def _run_tooling_probe(
        self,
        profile_home: Path,
        *args: str,
    ) -> Any:
        try:
            executable = self.auth_controller.codex.resolve_executable()
            return self.tooling_runner(
                (executable, *args),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.tooling_timeout_seconds,
                stdin=subprocess.DEVNULL,
                env=self._tooling_environment(profile_home),
                cwd=str(self.plugin_source_root),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProfileToolingError(
                "managed Codex profile tooling visibility probe failed"
            ) from exc

    def _tooling_is_visible(
        self,
        profile_home: Path,
        *,
        contract: Mapping[str, Any],
    ) -> bool:
        plugin_result = self._run_tooling_probe(
            profile_home,
            "plugin",
            "list",
            "--json",
        )
        mcp_result = self._run_tooling_probe(profile_home, "mcp", "list", "--json")
        if int(plugin_result.returncode) != 0 or int(mcp_result.returncode) != 0:
            return False
        try:
            plugins = json.loads(str(plugin_result.stdout or ""))
            mcp_servers = json.loads(str(mcp_result.stdout or ""))
        except (json.JSONDecodeError, TypeError):
            return False
        installed = plugins.get("installed") if isinstance(plugins, Mapping) else None
        if not isinstance(installed, list) or not isinstance(mcp_servers, list):
            return False
        plugin_ready = any(
            isinstance(item, Mapping)
            and str(item.get("pluginId") or "") == str(contract["plugin_id"])
            and str(item.get("version") or "") == str(contract["plugin_version"])
            and item.get("installed") is True
            and item.get("enabled") is True
            for item in installed
        )
        mcp_ready = any(
            isinstance(item, Mapping)
            and str(item.get("name") or "") == str(contract["mcp_server_name"])
            and item.get("enabled") is True
            for item in mcp_servers
        )
        return plugin_ready and mcp_ready

    def ensure_profile_tooling(self, profile_home: Path) -> dict[str, Any]:
        """Idempotently make repository-source plugin/MCP tooling visible."""

        with self._tooling_lock:
            contract = self._source_tooling_contract()
            binding = cli_agent_managed_profile_tooling_binding()
            marker = self._read_tooling_marker(profile_home)
            auth_before = self._auth_file_identity(profile_home)
            try:
                if (
                    marker.get("ready") is True
                    and marker.get("tooling_contract") == binding
                    and self._tooling_is_visible(profile_home, contract=contract)
                ):
                    return dict(marker)
                marketplace_root = (
                    profile_home / "managed-tooling" / CODEX_MARKETPLACE_NAME
                )
                install_codex_plugin_cache(
                    self.plugin_source_root,
                    codex_home=profile_home,
                    python_executable=sys.executable,
                )
                install_codex_marketplace(
                    self.plugin_source_root,
                    marketplace_root=marketplace_root,
                    python_executable=sys.executable,
                )
                configure_codex_plugin(
                    codex_config=profile_home / "config.toml",
                    marketplace_root=marketplace_root,
                )
                if not self._tooling_is_visible(profile_home, contract=contract):
                    raise ProfileToolingError(
                        "managed Codex profile tooling is not visible"
                    )
                self._write_tooling_marker(profile_home, contract=contract)
            except ProfileToolingError:
                raise
            except BaseException as exc:
                raise ProfileToolingError(
                    "managed Codex profile tooling bootstrap failed"
                ) from exc
            finally:
                if self._auth_file_identity(profile_home) != auth_before:
                    raise ProfileToolingError(
                        "managed Codex profile auth changed during tooling bootstrap"
                    )
            return self._read_tooling_marker(profile_home)

    @staticmethod
    def _validate_payload(
        operation: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, str]:
        unsupported = sorted(set(payload) - _PROFILE_INPUT_FIELDS)
        if unsupported:
            raise ProfileControlError(
                "profile operation contains unsupported fields: {}".format(
                    ", ".join(unsupported)
                )
            )
        if operation == PROFILE_LIST_OPERATION:
            if payload:
                raise ProfileControlError("profile list does not accept selectors")
            return "", ""
        profile_id = str(payload.get("profile_id") or "").strip().lower()
        if not profile_id:
            raise ProfileControlError("profile_id is required")
        return profile_id, _managed_provider(payload.get("provider"))

    @staticmethod
    def _auth_payload(
        operation: str,
        result: ProfileAuthResult,
        *,
        profile: AgentProfile | None = None,
    ) -> dict[str, Any]:
        auth = result.to_public_dict()
        registered = profile is not None
        return {
            **auth,
            "schema_version": PROFILE_CONTROL_SCHEMA_VERSION,
            "ok": (
                registered
                if operation == PROFILE_ACTIVATE_OPERATION
                else result.ok
            ),
            "operation": operation,
            "auth": auth,
            "profile_registered": registered,
            "profile": profile.to_public_dict() if profile is not None else None,
            "raw_argv_accepted": False,
            "raw_environment_accepted": False,
            "provider_home_input_accepted": False,
            "credential_input_accepted": False,
        }

    def list_profiles(self) -> dict[str, Any]:
        profiles = [profile.to_public_dict() for profile in self.registry.list_profiles()]
        return {
            "schema_version": PROFILE_CONTROL_SCHEMA_VERSION,
            "ok": True,
            "status": "ok",
            "operation": PROFILE_LIST_OPERATION,
            "profiles": profiles,
            "profile_count": len(profiles),
            "raw_argv_accepted": False,
            "raw_environment_accepted": False,
            "provider_home_input_accepted": False,
            "credential_input_accepted": False,
        }

    def prepare_login(self, profile_id: str, provider: str = "codex") -> dict[str, Any]:
        result = self.auth_controller.prepare_login(
            profile_id,
            _managed_provider(provider),
        )
        return self._auth_payload(PROFILE_LOGIN_PREPARE_OPERATION, result)

    def auth_status(self, profile_id: str, provider: str = "codex") -> dict[str, Any]:
        result = self.auth_controller.auth_status(
            profile_id,
            _managed_provider(provider),
        )
        return self._auth_payload(PROFILE_AUTH_STATUS_OPERATION, result)

    def activate(self, profile_id: str, provider: str = "codex") -> dict[str, Any]:
        result = self.auth_controller.activate(
            profile_id,
            _managed_provider(provider),
        )
        if result.state != READY or not result.activated:
            return self._auth_payload(PROFILE_ACTIVATE_OPERATION, result)
        profile = _bounded_codex_profile(result.profile_id)
        registered = self.registry.get_profile(result.profile_id)
        if registered is not None:
            if not _is_bounded_codex_profile(registered):
                raise ProfileControlError(
                    "managed profile conflicts with an existing immutable profile"
                )
        else:
            try:
                registered = self.registry.register_profile(profile)
            except RegistryError as exc:
                raise ProfileControlError(
                    "managed profile conflicts with an existing immutable profile"
                ) from exc
        return self._auth_payload(
            PROFILE_ACTIVATE_OPERATION,
            result,
            profile=registered,
        )

    def resolve_profile_home(self, profile: AgentProfile) -> Path:
        """Resolve an activated home from server-owned profile identity only."""

        if not isinstance(profile, AgentProfile):
            raise ProfileControlError("managed profile identity is invalid")
        registered = self.registry.get_profile(profile.profile_id)
        if registered is None or registered != profile:
            raise ProfileControlError(
                "managed profile is not the registered immutable profile"
            )
        if not _is_bounded_codex_profile(registered):
            raise ProfileControlError("registered profile is not server managed")
        status = self.auth_controller.discover(profile.profile_id, "codex")
        if status.state != READY or not status.activated:
            raise ProfileControlError("managed profile is not activated")
        expected_home = self.auth_controller.managed_profile_home(
            profile.profile_id,
            "codex",
        )
        supplied_home = Path(status.profile_home).resolve()
        if supplied_home != expected_home or not expected_home.is_dir():
            raise ProfileControlError("managed profile home identity is invalid")
        self.ensure_profile_tooling(expected_home)
        return expected_home

    def dispatch(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = str(operation or "").strip().lower()
        if normalized not in PROFILE_OPERATIONS:
            raise ProfileControlError("unsupported profile operation")
        request = {} if payload is None else payload
        if not isinstance(request, Mapping):
            raise ProfileControlError("profile operation payload must be an object")
        profile_id, provider = self._validate_payload(normalized, request)
        try:
            if normalized == PROFILE_LIST_OPERATION:
                return self.list_profiles()
            if normalized == PROFILE_LOGIN_PREPARE_OPERATION:
                return self.prepare_login(profile_id, provider)
            if normalized == PROFILE_AUTH_STATUS_OPERATION:
                return self.auth_status(profile_id, provider)
            return self.activate(profile_id, provider)
        except ProfileAuthError as exc:
            raise ProfileControlError(str(exc)) from exc


ProfileControl = ManagedProfileControl


__all__ = [
    "ManagedProfileControl",
    "ProfileControl",
    "ProfileControlError",
    "ProfileToolingError",
    "PROFILE_OPERATIONS",
    "PROFILE_LIST_OPERATION",
    "PROFILE_LOGIN_PREPARE_OPERATION",
    "PROFILE_AUTH_STATUS_OPERATION",
    "PROFILE_ACTIVATE_OPERATION",
]
