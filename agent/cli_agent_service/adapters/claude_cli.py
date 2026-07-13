"""Claude CLI adapter helpers for bounded, user-triggered profile login."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


CLAUDE_PROFILE_ENV_KEY = "CLAUDE_CONFIG_DIR"
CLAUDE_LOGIN_ARGS = ("auth", "login", "--claudeai")
CLAUDE_AUTH_STATUS_ARGS = ("auth", "status", "--json")
CLAUDE_SUBSCRIPTION_ISOLATION_SUPPORTED = False


class ClaudeAdapterError(ValueError):
    pass


class ClaudeCliAdapter:
    """Build fixed Claude auth commands without claiming account isolation."""

    def __init__(self, *, executable: str = "") -> None:
        self.executable = str(executable or "").strip()

    def resolve_executable(self, executable_ref: str = "") -> str:
        configured = self.executable or os.environ.get("CLAUDE_BIN", "").strip()
        executable_ref = str(executable_ref or "").strip()
        if not configured and executable_ref.startswith("path:"):
            configured = executable_ref.removeprefix("path:")
        configured = configured or "claude"
        if os.path.isabs(configured) or os.sep in configured:
            path = Path(configured).expanduser()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ClaudeAdapterError("configured Claude executable is unavailable")
            return str(path)
        resolved = shutil.which(configured)
        if not resolved:
            raise ClaudeAdapterError("Claude executable is unavailable")
        return resolved

    @staticmethod
    def profile_environment(
        profile_home: str | os.PathLike[str],
    ) -> dict[str, str]:
        home = Path(profile_home).expanduser().resolve()
        if not home.is_dir():
            raise ClaudeAdapterError("managed Claude profile home is unavailable")
        return {CLAUDE_PROFILE_ENV_KEY: str(home)}

    def build_login_command(
        self,
        *,
        profile_home: str | os.PathLike[str],
    ) -> tuple[str, ...]:
        self.profile_environment(profile_home)
        return (self.resolve_executable(), *CLAUDE_LOGIN_ARGS)

    def build_auth_status_command(
        self,
        *,
        profile_home: str | os.PathLike[str],
    ) -> tuple[str, ...]:
        self.profile_environment(profile_home)
        return (self.resolve_executable(), *CLAUDE_AUTH_STATUS_ARGS)

    @staticmethod
    def subscription_isolation_evidence() -> dict[str, object]:
        return {
            "subscription_isolation_supported": (
                CLAUDE_SUBSCRIPTION_ISOLATION_SUPPORTED
            ),
            "subscription_isolation_claimed": False,
            "reason_code": "claude_subscription_isolation_not_proven",
        }
