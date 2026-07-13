#!/usr/bin/env python3
"""Probe whether Claude CLI authentication is safe for unattended service use."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


PROBE_SCHEMA_VERSION = "claude_service_auth_spike.v1"
AUTH_STATUS_ARGS = ("auth", "status", "--json")
PROFILE_IDS = ("inherited", "clean-1", "clean-2")
DIRECT_AUTH_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

AUTHENTICATED = "authenticated"
CONFIGURATION_MISMATCH = "configuration_mismatch"
KEYCHAIN_OR_GUI = "keychain_acl_or_gui_prompt"
UNAUTHENTICATED = "unauthenticated_profile"
PROVIDER_FAILURE = "provider_cli_failure"

CONFIGURATION_MARKERS = (
    "configuration error",
    "failed to load config",
    "invalid config",
    "unknown command",
    "unknown option",
    "unrecognized option",
    "unsupported option",
)
INTERACTION_MARKERS = (
    "authorization prompt",
    "errsecinteractionnotallowed",
    "keychain access denied",
    "keychain is locked",
    "securityagent",
    "user interaction is not allowed",
)
UNAUTHENTICATED_MARKERS = (
    "authentication required",
    "login required",
    "not authenticated",
    "not logged in",
    "please log in",
    "unauthenticated",
)
AUTHENTICATED_STATUS_VALUES = {"authenticated", "logged_in", "ok"}
UNAUTHENTICATED_STATUS_VALUES = {
    "logged_out",
    "not_authenticated",
    "unauthenticated",
}


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _output_hash(stdout: Any, stderr: Any) -> str:
    output = (_as_text(stdout) + "\0" + _as_text(stderr)).encode(
        "utf-8", errors="replace"
    )
    return "sha256:" + hashlib.sha256(output).hexdigest()


def _json_auth_state(output: str) -> bool | None:
    try:
        decoded = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None

    pending = [decoded]
    visited = 0
    while pending and visited < 100:
        value = pending.pop()
        visited += 1
        if isinstance(value, Mapping):
            for key in ("loggedIn", "logged_in", "authenticated"):
                state = value.get(key)
                if isinstance(state, bool):
                    return state
            for key in ("status", "authStatus", "auth_status"):
                status = str(value.get(key) or "").strip().lower()
                if status in AUTHENTICATED_STATUS_VALUES:
                    return True
                if status in UNAUTHENTICATED_STATUS_VALUES:
                    return False
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return None


def classify_auth_status(
    returncode: int | None,
    stdout: Any = "",
    stderr: Any = "",
    *,
    timed_out: bool = False,
    launch_error: bool = False,
) -> tuple[str, str]:
    """Classify one status probe without returning provider output."""

    combined = (_as_text(stdout) + "\n" + _as_text(stderr)).casefold()
    if launch_error:
        return PROVIDER_FAILURE, "cli_launch_failed"
    if any(marker in combined for marker in CONFIGURATION_MARKERS):
        return CONFIGURATION_MISMATCH, "status_command_or_config_unsupported"
    if any(marker in combined for marker in INTERACTION_MARKERS):
        return KEYCHAIN_OR_GUI, "keychain_or_gui_interaction_reported"

    auth_state = _json_auth_state(_as_text(stdout).strip())
    if auth_state is False or any(
        marker in combined for marker in UNAUTHENTICATED_MARKERS
    ):
        return UNAUTHENTICATED, "profile_not_authenticated"
    if timed_out:
        return KEYCHAIN_OR_GUI, "noninteractive_status_timed_out"
    if returncode == 0 and auth_state is True:
        return AUTHENTICATED, "status_authenticated"
    if returncode == 0:
        return PROVIDER_FAILURE, "status_response_unrecognized"
    return PROVIDER_FAILURE, "status_command_failed"


def _public_result(
    *,
    profile_id: str,
    profile_kind: str,
    classification: str,
    reason: str,
    exit_code: int | None,
    timed_out: bool,
    output_hash: str,
    clean_config_dir_was_empty: bool | None,
) -> dict[str, Any]:
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "profile_kind": profile_kind,
        "classification": classification,
        "reason": reason,
        "authenticated": classification == AUTHENTICATED,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output_hash": output_hash,
        "noninteractive": True,
        "clean_config_dir_was_empty": clean_config_dir_was_empty,
        "authentication_material_copied": False,
        "direct_auth_environment_forwarded": False,
        "raw_prompt_used": False,
        "raw_output_persisted": False,
    }


def probe_profile(
    *,
    profile_id: str,
    profile_kind: str,
    executable: str,
    environment: Mapping[str, str],
    config_dir: Path | None = None,
    timeout_seconds: float = 5.0,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run one prompt-free, noninteractive authentication status probe."""

    child_env = dict(environment)
    for key in DIRECT_AUTH_ENV_KEYS:
        child_env.pop(key, None)
    child_env.update({"CI": "1", "NO_COLOR": "1", "TERM": "dumb"})

    clean_config_dir_was_empty: bool | None = None
    if config_dir is not None:
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        clean_config_dir_was_empty = not any(config_dir.iterdir())
        child_env["CLAUDE_CONFIG_DIR"] = str(config_dir)

    command = (executable, *AUTH_STATUS_ARGS)
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(float(timeout_seconds), 0.01),
            stdin=subprocess.DEVNULL,
            env=child_env,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        classification, reason = classify_auth_status(
            None,
            exc.stdout,
            exc.stderr,
            timed_out=True,
        )
        return _public_result(
            profile_id=profile_id,
            profile_kind=profile_kind,
            classification=classification,
            reason=reason,
            exit_code=None,
            timed_out=True,
            output_hash=_output_hash(exc.stdout, exc.stderr),
            clean_config_dir_was_empty=clean_config_dir_was_empty,
        )
    except OSError:
        classification, reason = classify_auth_status(None, launch_error=True)
        return _public_result(
            profile_id=profile_id,
            profile_kind=profile_kind,
            classification=classification,
            reason=reason,
            exit_code=None,
            timed_out=False,
            output_hash="",
            clean_config_dir_was_empty=clean_config_dir_was_empty,
        )

    stdout = _as_text(completed.stdout)
    stderr = _as_text(completed.stderr)
    classification, reason = classify_auth_status(
        int(completed.returncode), stdout, stderr
    )
    return _public_result(
        profile_id=profile_id,
        profile_kind=profile_kind,
        classification=classification,
        reason=reason,
        exit_code=int(completed.returncode),
        timed_out=False,
        output_hash=_output_hash(stdout, stderr),
        clean_config_dir_was_empty=clean_config_dir_was_empty,
    )


def decide(results: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """Return the bounded service-auth decision for exactly three profiles."""

    by_profile = {str(item.get("profile_id") or ""): item for item in results}
    if len(results) != len(PROFILE_IDS) or set(by_profile) != set(PROFILE_IDS):
        return "reject", "invalid_probe_set"

    classifications = {
        profile_id: str(by_profile[profile_id].get("classification") or "")
        for profile_id in PROFILE_IDS
    }
    if CONFIGURATION_MISMATCH in classifications.values():
        return "reject", "configuration_mismatch"
    if PROVIDER_FAILURE in classifications.values():
        return "reject", "provider_cli_failure"
    if classifications["inherited"] == UNAUTHENTICATED:
        return "reject", "inherited_profile_unauthenticated"
    if KEYCHAIN_OR_GUI in classifications.values():
        return "interactive-only", "keychain_acl_or_gui_prompt"
    if any(
        classifications[profile_id] == UNAUTHENTICATED
        for profile_id in ("clean-1", "clean-2")
    ):
        return "interactive-only", "clean_profile_authentication_required"
    if all(value == AUTHENTICATED for value in classifications.values()):
        return "unattended-safe", "all_profiles_authenticated_noninteractively"
    return "reject", "unclassified_probe_state"


def build_report(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    decision, reason = decide(results)
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "decision": decision,
        "decision_reason": reason,
        "profiles": [dict(result) for result in results],
        "probe_contract": {
            "profile_count": 3,
            "clean_profile_count": 2,
            "authentication_material_copied": False,
            "direct_auth_environment_forwarded": False,
            "raw_credentials_exposed": False,
            "raw_prompt_used": False,
            "raw_output_persisted": False,
        },
    }


def run_spike(
    *,
    executable: str = "",
    timeout_seconds: float = 5.0,
    environment: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Probe inherited auth and two temporary, initially empty config dirs."""

    base_env = dict(os.environ if environment is None else environment)
    configured = str(executable or "").strip() or str(
        base_env.get("CLAUDE_BIN") or ""
    ).strip()
    resolved = configured or str(shutil.which("claude") or "")
    if not resolved:
        results = [
            _public_result(
                profile_id=profile_id,
                profile_kind="inherited" if profile_id == "inherited" else "clean",
                classification=PROVIDER_FAILURE,
                reason="cli_unavailable",
                exit_code=None,
                timed_out=False,
                output_hash="",
                clean_config_dir_was_empty=None if profile_id == "inherited" else True,
            )
            for profile_id in PROFILE_IDS
        ]
        return build_report(results)

    results = [
        probe_profile(
            profile_id="inherited",
            profile_kind="inherited",
            executable=resolved,
            environment=base_env,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
    ]
    with tempfile.TemporaryDirectory(prefix="aming-claude-auth-spike-") as root:
        root_path = Path(root)
        for profile_id in ("clean-1", "clean-2"):
            results.append(
                probe_profile(
                    profile_id=profile_id,
                    profile_kind="clean",
                    executable=resolved,
                    environment=base_env,
                    config_dir=root_path / profile_id,
                    timeout_seconds=timeout_seconds,
                    runner=runner,
                )
            )
    return build_report(results)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", default="", help="Claude CLI executable")
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_spike(executable=args.claude, timeout_seconds=args.timeout)
    print(json.dumps(report, sort_keys=True))
    return {"unattended-safe": 0, "interactive-only": 2}.get(
        str(report["decision"]), 3
    )


if __name__ == "__main__":
    raise SystemExit(main())
