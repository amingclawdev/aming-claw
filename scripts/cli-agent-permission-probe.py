#!/usr/bin/env python3
"""Record bounded Codex/Claude launch capability facts without changing auth state."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Sequence


PROBE_SCHEMA_VERSION = "cli_agent_service.permission_probe.v1"
DEFAULT_PROVIDERS = ("codex", "claude")
INTERACTION_MARKERS = (
    "browser",
    "device code",
    "interactive",
    "press enter",
    "requires a tty",
    "sign in to continue",
)
DENIED_MARKERS = (
    "authentication required",
    "login required",
    "not authorized",
    "permission denied",
    "unauthorized",
)


def classify_result(returncode: int, output: str) -> str:
    normalized = output.casefold()
    if returncode == 0:
        return "allowed"
    if any(marker in normalized for marker in INTERACTION_MARKERS):
        return "requires_interaction"
    if any(marker in normalized for marker in DENIED_MARKERS):
        return "denied"
    return "error"


def _output_hash(stdout: str, stderr: str) -> str:
    value = (stdout + "\n" + stderr).encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def probe_executable(
    provider: str,
    *,
    executable: str | None = None,
    probe_args: Sequence[str] = ("--version",),
    timeout_seconds: float = 5.0,
) -> dict:
    resolved = executable or shutil.which(provider)
    base = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "provider": provider,
        "probe_args": list(probe_args),
        "raw_output_stored": False,
        "auth_state_changed": False,
    }
    if not resolved:
        return {**base, "status": "unavailable", "executable_found": False, "exit_code": None, "output_hash": ""}
    command = [resolved, *probe_args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(float(timeout_seconds), 0.01),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return {
            **base,
            "status": "timeout",
            "executable_found": True,
            "executable_name": Path(resolved).name,
            "exit_code": None,
            "output_hash": _output_hash(stdout, stderr),
        }
    except OSError:
        return {
            **base,
            "status": "error",
            "executable_found": True,
            "executable_name": Path(resolved).name,
            "exit_code": None,
            "output_hash": "",
        }
    output = completed.stdout + "\n" + completed.stderr
    return {
        **base,
        "status": classify_result(completed.returncode, output),
        "executable_found": True,
        "executable_name": Path(resolved).name,
        "exit_code": completed.returncode,
        "output_hash": _output_hash(completed.stdout, completed.stderr),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", action="append", choices=DEFAULT_PROVIDERS)
    parser.add_argument("--codex-executable")
    parser.add_argument("--claude-executable")
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    providers = tuple(args.provider or DEFAULT_PROVIDERS)
    overrides = {"codex": args.codex_executable, "claude": args.claude_executable}
    results = [
        probe_executable(provider, executable=overrides[provider], timeout_seconds=args.timeout)
        for provider in providers
    ]
    print(json.dumps({"schema_version": PROBE_SCHEMA_VERSION, "results": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
