"""Fixed local control surface for server-owned managed CLI profiles."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

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


def _bounded_codex_profile(profile_id: str) -> AgentProfile:
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
            model="gpt-5.4-codex",
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
            launcher_id="launcher-codex-managed",
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


class ManagedProfileControl:
    """Connect profile auth to the existing immutable operational registry."""

    def __init__(
        self,
        registry: AgentRegistry,
        auth_controller: ProfileAuthController,
    ) -> None:
        self.registry = registry
        self.auth_controller = auth_controller

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
        expected = _bounded_codex_profile(profile.profile_id)
        if registered != expected:
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
    "PROFILE_OPERATIONS",
    "PROFILE_LIST_OPERATION",
    "PROFILE_LOGIN_PREPARE_OPERATION",
    "PROFILE_AUTH_STATUS_OPERATION",
    "PROFILE_ACTIVATE_OPERATION",
]
