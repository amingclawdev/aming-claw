"""Codex CLI adapter for one explicitly imported inherited profile."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..models import AgentRun


CODEX_PROFILE_ENV_KEY = "CODEX_HOME"
CODEX_LOGIN_ARGS = ("login", "--device-auth")
CODEX_AUTH_STATUS_ARGS = ("login", "status")


class CodexAdapterError(ValueError):
    pass


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
        configured = self.executable or os.environ.get("CODEX_BIN", "").strip()
        executable_ref = str(executable_ref or "").strip()
        if not configured and executable_ref.startswith("path:"):
            configured = executable_ref.removeprefix("path:")
        configured = configured or "codex"
        if os.path.isabs(configured) or os.sep in configured:
            path = Path(configured).expanduser()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise CodexAdapterError("configured Codex executable is unavailable")
            return str(path)
        resolved = shutil.which(configured)
        if not resolved:
            raise CodexAdapterError("Codex executable is unavailable")
        return resolved

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
        if run.config.model:
            command.extend(["--model", run.config.model])
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
