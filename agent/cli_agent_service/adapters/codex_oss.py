"""Endpoint adapter for Codex OSS mode and local model providers."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..certification import is_local_endpoint
from ..models import InferenceEndpoint


class CodexOssAdapterError(ValueError):
    pass


@dataclass(frozen=True)
class CodexOssLaunchSpec:
    """Public launch material. Prompts and credentials are intentionally absent."""

    command: tuple[str, ...]
    cwd: str
    environment: dict[str, str] | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "environment": dict(self.environment or {}),
            "endpoint_adapter": "codex_oss",
            "credential_ref_exposed": False,
            "raw_credentials_exposed": False,
            "prompt_exposed": False,
        }


class CodexOssAdapter:
    """Render one bounded Codex command for a local inference endpoint."""

    def __init__(
        self,
        *,
        executable: str = "",
        local_provider: str = "",
        dangerous: bool = False,
        sandbox: str = "workspace-write",
        stream_json: bool = True,
    ) -> None:
        self.executable = str(executable or "").strip()
        self.local_provider = str(local_provider or "").strip().lower()
        self.dangerous = bool(dangerous)
        self.sandbox = str(sandbox or "workspace-write").strip()
        self.stream_json = bool(stream_json)
        if self.local_provider and self.local_provider not in {"ollama", "lmstudio"}:
            raise CodexOssAdapterError("local provider is not supported")
        if self.sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise CodexOssAdapterError("Codex sandbox mode is not supported")

    def resolve_executable(self, executable_ref: str = "") -> str:
        configured = self.executable or os.environ.get("CODEX_BIN", "").strip()
        executable_ref = str(executable_ref or "").strip()
        if not configured and executable_ref.startswith("path:"):
            configured = executable_ref.removeprefix("path:")
        configured = configured or "codex"
        if os.path.isabs(configured) or os.sep in configured:
            path = Path(configured).expanduser()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise CodexOssAdapterError("configured Codex executable is unavailable")
            return str(path)
        resolved = shutil.which(configured)
        if not resolved:
            raise CodexOssAdapterError("Codex executable is unavailable")
        return resolved

    @staticmethod
    def validate_endpoint(endpoint: InferenceEndpoint) -> None:
        if not isinstance(endpoint, InferenceEndpoint):
            raise CodexOssAdapterError("Codex OSS requires an inference endpoint")
        provider = endpoint.provider.strip().lower().replace("-", "_")
        if not is_local_endpoint(endpoint) or provider in {
            "anthropic",
            "anthropic_compatible",
            "claude_gateway",
        }:
            raise CodexOssAdapterError("endpoint is not a Codex OSS/local provider")
        if endpoint.auth_mode.strip().lower() in {
            "account",
            "cli_auth",
            "subscription",
        }:
            raise CodexOssAdapterError(
                "Codex OSS endpoints cannot use subscription account authentication"
            )

    def _provider(self, endpoint: InferenceEndpoint) -> str:
        if self.local_provider:
            return self.local_provider
        provider = endpoint.provider.strip().lower().replace("-", "_")
        return provider if provider in {"ollama", "lmstudio"} else ""

    def build_launch_spec(
        self,
        endpoint: InferenceEndpoint,
        *,
        worktree: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        executable_ref: str = "",
    ) -> CodexOssLaunchSpec:
        self.validate_endpoint(endpoint)
        cwd = Path(worktree).expanduser().resolve()
        if not cwd.is_dir():
            raise CodexOssAdapterError("assigned worktree is unavailable")
        output = Path(output_path).expanduser()
        command = [self.resolve_executable(executable_ref), "exec", "--oss"]
        local_provider = self._provider(endpoint)
        if local_provider:
            command.extend(["--local-provider", local_provider])
        command.extend(["--model", endpoint.model])
        if self.dangerous:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", self.sandbox])
        command.append("--skip-git-repo-check")
        if self.stream_json:
            command.append("--json")
        command.extend(["-C", str(cwd), "-o", str(output)])
        return CodexOssLaunchSpec(command=tuple(command), cwd=str(cwd))


CodexOssEndpointAdapter = CodexOssAdapter
