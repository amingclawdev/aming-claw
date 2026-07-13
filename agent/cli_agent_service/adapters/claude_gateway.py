"""Endpoint adapter for Claude-compatible gateways with host-resolved secrets."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, MutableMapping
from urllib.parse import urlsplit

from ..certification import is_local_endpoint
from ..models import CredentialRef, InferenceEndpoint, validate_credential_ref_id


CLAUDE_GATEWAY_URL_ENV_KEY = "ANTHROPIC_BASE_URL"
CLAUDE_GATEWAY_CREDENTIAL_ENV_KEY = "ANTHROPIC_AUTH_TOKEN"


class ClaudeGatewayAdapterError(ValueError):
    pass


def _gateway_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        raise ClaudeGatewayAdapterError("gateway endpoint URL is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ClaudeGatewayAdapterError(
            "gateway endpoint URL must be credential-free HTTP(S)"
        )
    return normalized


@dataclass(frozen=True)
class ClaudeGatewayLaunchSpec:
    """Public launch material; credentials are applied after this object is built."""

    command: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]
    secret_environment_keys: tuple[str, ...] = (CLAUDE_GATEWAY_CREDENTIAL_ENV_KEY,)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "environment",
            MappingProxyType(dict(self.environment)),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "environment": dict(self.environment),
            "secret_environment_keys": list(self.secret_environment_keys),
            "endpoint_adapter": "claude_gateway",
            "credential_ref_exposed": False,
            "raw_credentials_exposed": False,
            "prompt_exposed": False,
        }


class ClaudeGatewayAdapter:
    """Render a Claude CLI gateway launch without inventing an account profile."""

    __slots__ = (
        "executable",
        "base_url",
        "_credential_ref",
        "stream_json",
        "permission_mode",
    )

    def __init__(
        self,
        *,
        executable: str = "",
        base_url: str = "",
        endpoint_url: str = "",
        credential_ref: CredentialRef | str | None = None,
        stream_json: bool = True,
        permission_mode: str = "dontAsk",
    ) -> None:
        self.executable = str(executable or "").strip()
        configured_url = str(base_url or endpoint_url or "").strip()
        self.base_url = _gateway_url(configured_url) if configured_url else ""
        if isinstance(credential_ref, CredentialRef):
            if credential_ref.provider not in {
                "",
                "anthropic",
                "anthropic_compatible",
                "claude_gateway",
            }:
                raise ClaudeGatewayAdapterError(
                    "credential reference provider is not gateway-compatible"
                )
            credential_id = credential_ref.ref_id
        elif credential_ref is None:
            credential_id = ""
        else:
            try:
                credential_id = validate_credential_ref_id(str(credential_ref))
            except ValueError:
                raise ClaudeGatewayAdapterError(
                    "gateway credential must be an opaque credential reference"
                ) from None
        self._credential_ref = credential_id
        self.stream_json = bool(stream_json)
        self.permission_mode = str(permission_mode or "dontAsk").strip()
        if self.permission_mode not in {"acceptEdits", "dontAsk", "plan"}:
            raise ClaudeGatewayAdapterError("Claude permission mode is not supported")

    def __repr__(self) -> str:
        return (
            "ClaudeGatewayAdapter(base_url={!r}, credential_ref=<redacted>, "
            "stream_json={!r})"
        ).format(self.base_url, self.stream_json)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "endpoint_adapter": "claude_gateway",
            "base_url": self.base_url,
            "credential_ref_configured": bool(self._credential_ref),
            "credential_ref_exposed": False,
            "raw_credentials_exposed": False,
        }

    def resolve_executable(self, executable_ref: str = "") -> str:
        configured = self.executable or os.environ.get("CLAUDE_BIN", "").strip()
        executable_ref = str(executable_ref or "").strip()
        if not configured and executable_ref.startswith("path:"):
            configured = executable_ref.removeprefix("path:")
        configured = configured or "claude"
        if os.path.isabs(configured) or os.sep in configured:
            path = Path(configured).expanduser()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ClaudeGatewayAdapterError(
                    "configured Claude executable is unavailable"
                )
            return str(path)
        resolved = shutil.which(configured)
        if not resolved:
            raise ClaudeGatewayAdapterError("Claude executable is unavailable")
        return resolved

    @staticmethod
    def validate_endpoint(endpoint: InferenceEndpoint) -> None:
        if not isinstance(endpoint, InferenceEndpoint):
            raise ClaudeGatewayAdapterError(
                "Claude gateway requires an inference endpoint"
            )
        provider = endpoint.provider.strip().lower().replace("-", "_")
        backend = endpoint.backend_mode.strip().lower().replace("-", "_")
        compatible = provider in {
            "anthropic_compatible",
            "claude_gateway",
        } or backend in {
            "claude_compatible",
            "claude_gateway",
            "compatible_gateway",
        } or endpoint.endpoint_kind.strip().lower().replace("-", "_") in {
            "claude_compatible_gateway",
            "claude_gateway",
            "compatible_gateway",
        }
        if not is_local_endpoint(endpoint) or not compatible:
            raise ClaudeGatewayAdapterError(
                "endpoint is not a Claude-compatible gateway"
            )
        if endpoint.auth_mode.strip().lower() in {
            "account",
            "cli_auth",
            "subscription",
        }:
            raise ClaudeGatewayAdapterError(
                "Claude gateway endpoints cannot use subscription account authentication"
            )

    def build_launch_spec(
        self,
        endpoint: InferenceEndpoint,
        *,
        worktree: str | os.PathLike[str],
        base_url: str = "",
        endpoint_url: str = "",
        executable_ref: str = "",
    ) -> ClaudeGatewayLaunchSpec:
        self.validate_endpoint(endpoint)
        cwd = Path(worktree).expanduser().resolve()
        if not cwd.is_dir():
            raise ClaudeGatewayAdapterError("assigned worktree is unavailable")
        url = str(base_url or endpoint_url or self.base_url or "").strip()
        if not url:
            raise ClaudeGatewayAdapterError("gateway endpoint URL is required")
        url = _gateway_url(url)
        if endpoint.auth_mode.strip().lower() not in {"none", "no_auth"}:
            if not self._credential_ref:
                raise ClaudeGatewayAdapterError(
                    "gateway credential reference is required"
                )
        command = [
            self.resolve_executable(executable_ref),
            "--print",
            "--model",
            endpoint.model,
            "--permission-mode",
            self.permission_mode,
        ]
        if self.stream_json:
            command.extend(["--output-format", "stream-json"])
        return ClaudeGatewayLaunchSpec(
            command=tuple(command),
            cwd=str(cwd),
            environment={CLAUDE_GATEWAY_URL_ENV_KEY: url},
        )

    def apply_credential(
        self,
        environment: MutableMapping[str, str],
        resolver: Callable[[str], str],
    ) -> None:
        """Resolve directly into a private process environment after spec logging."""

        if not self._credential_ref:
            return
        try:
            value = resolver(self._credential_ref)
        except BaseException:
            raise ClaudeGatewayAdapterError("gateway credential resolution failed") from None
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ClaudeGatewayAdapterError("gateway credential resolution failed")
        environment[CLAUDE_GATEWAY_CREDENTIAL_ENV_KEY] = value

    apply_credentials = apply_credential


ClaudeCompatibleGatewayAdapter = ClaudeGatewayAdapter
ClaudeGatewayEndpointAdapter = ClaudeGatewayAdapter
