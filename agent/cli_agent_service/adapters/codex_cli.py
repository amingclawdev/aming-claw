"""Bounded Codex CLI discovery and launch adapter."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ..models import AgentRun


CODEX_PROFILE_ENV_KEY = "CODEX_HOME"
CODEX_LOGIN_ARGS = ("login", "--device-auth")
CODEX_AUTH_STATUS_ARGS = ("login", "status")
CODEX_CLI_DEFAULT_MODEL = "codex-cli-default"
CODEX_MANAGED_LAUNCHER_ID = "launcher-codex-managed"
CODEX_LEGACY_MANAGED_DEFAULT_MODELS = frozenset({"gpt-5.4-codex"})
MACOS_CODEX_APP_BUNDLE_EXECUTABLES = (
    "/Applications/ChatGPT.app/Contents/Resources/codex",
)


class CodexAdapterError(ValueError):
    pass


def _resolve_configured_executable(configured: str) -> str:
    if os.path.isabs(configured) or os.sep in configured:
        path = Path(configured).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise CodexAdapterError("configured Codex executable is unavailable")
        return str(path)
    resolved = shutil.which(configured)
    if not resolved:
        raise CodexAdapterError("configured Codex executable is unavailable")
    return resolved


def _resolve_supported_app_bundle_executable() -> str:
    if sys.platform != "darwin":
        return ""
    for candidate in MACOS_CODEX_APP_BUNDLE_EXECUTABLES:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return ""


def resolve_codex_executable(
    *,
    explicit: str = "",
    executable_ref: str = "",
) -> str:
    """Apply the server-owned Codex executable discovery policy."""

    configured = str(explicit or "").strip()
    if configured:
        return _resolve_configured_executable(configured)

    configured = os.environ.get("CODEX_BIN", "").strip()
    if configured:
        return _resolve_configured_executable(configured)

    executable_ref = str(executable_ref or "").strip()
    if executable_ref.startswith("path:"):
        return _resolve_configured_executable(
            executable_ref.removeprefix("path:")
        )

    resolved = shutil.which("codex")
    if resolved:
        return resolved

    resolved = _resolve_supported_app_bundle_executable()
    if resolved:
        return resolved
    raise CodexAdapterError("Codex executable is unavailable")


@dataclass(frozen=True)
class CodexLaunchSpec:
    command: tuple[str, ...]
    cwd: str
    environment: dict[str, str] | None = None


class CodexCliAdapter:
    """Build bounded Codex commands for one explicitly selected profile."""

    def __init__(
        self,
        *,
        executable: str = "",
        dangerous: bool = True,
        sandbox: str = "workspace-write",
        stream_json: bool = True,
    ) -> None:
        self.executable = str(executable or "").strip()
        self.dangerous = bool(dangerous)
        self.sandbox = str(sandbox or "workspace-write").strip()
        self.stream_json = bool(stream_json)

    def resolve_executable(self, executable_ref: str = "") -> str:
        return resolve_codex_executable(
            explicit=self.executable,
            executable_ref=executable_ref,
        )

    def _resolve_executable(self, run: AgentRun) -> str:
        executable_ref = str(run.profile.harness_runtime.executable_ref or "").strip()
        return self.resolve_executable(executable_ref)

    @staticmethod
    def profile_environment(
        profile_home: str | os.PathLike[str],
    ) -> dict[str, str]:
        home = Path(profile_home).expanduser().resolve()
        if not home.is_dir():
            raise CodexAdapterError("managed Codex profile home is unavailable")
        return {CODEX_PROFILE_ENV_KEY: str(home)}

    def build_login_command(
        self,
        *,
        profile_home: str | os.PathLike[str],
    ) -> tuple[str, ...]:
        self.profile_environment(profile_home)
        return (self.resolve_executable(), *CODEX_LOGIN_ARGS)

    def build_auth_status_command(
        self,
        *,
        profile_home: str | os.PathLike[str],
    ) -> tuple[str, ...]:
        self.profile_environment(profile_home)
        return (self.resolve_executable(), *CODEX_AUTH_STATUS_ARGS)

    def validate_run(self, run: AgentRun) -> None:
        if run.profile is None:
            raise CodexAdapterError("Codex C0 requires an explicitly imported profile")
        profile = run.profile
        if profile.harness_runtime.kind != "codex_cli":
            raise CodexAdapterError("profile harness runtime is not codex_cli")
        if run.config.backend_mode != "codex_cli" or run.config.provider != "openai":
            raise CodexAdapterError("resolved run is not routed to Codex CLI")
        if run.config.auth_mode != "cli_auth":
            raise CodexAdapterError("Codex C0 requires inherited CLI authentication")
        if profile.credential_ref.provider not in {"", "openai"}:
            raise CodexAdapterError("credential reference provider is not OpenAI")

    @staticmethod
    def _explicit_model(run: AgentRun) -> str:
        model = str(run.config.model or "").strip()
        if not model or model == CODEX_CLI_DEFAULT_MODEL:
            return ""
        resolution = run.config.resolution_for("model")
        if (
            run.profile.launcher_adapter.launcher_id == CODEX_MANAGED_LAUNCHER_ID
            and resolution.source == "agent_profile"
            and model in CODEX_LEGACY_MANAGED_DEFAULT_MODELS
        ):
            return ""
        return model

    def build_launch_spec(
        self,
        run: AgentRun,
        *,
        worktree: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        profile_home: str | os.PathLike[str] | None = None,
    ) -> CodexLaunchSpec:
        self.validate_run(run)
        cwd = Path(worktree).expanduser().resolve()
        if not cwd.is_dir():
            raise CodexAdapterError("assigned worktree is unavailable")
        command = [self._resolve_executable(run), "exec"]
        explicit_model = self._explicit_model(run)
        if explicit_model:
            command.extend(["--model", explicit_model])
        if self.dangerous:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", self.sandbox])
        command.append("--skip-git-repo-check")
        if self.stream_json:
            command.append("--json")
        command.extend(["-C", str(cwd), "-o", str(Path(output_path))])
        environment = (
            self.profile_environment(profile_home)
            if profile_home is not None
            else None
        )
        return CodexLaunchSpec(
            command=tuple(command),
            cwd=str(cwd),
            environment=environment,
        )
