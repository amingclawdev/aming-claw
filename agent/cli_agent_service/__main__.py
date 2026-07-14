"""Command-line entry point for the separately supervised CLI Agent Service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .service import (
    ServiceAlreadyRunningError,
    ServicePaths,
    ServiceUnavailableError,
    current_status,
    request_service,
    run_foreground,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cli_agent_service")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "health", "status", "stop"):
        command = subparsers.add_parser(name)
        command.add_argument("--state-dir", type=Path, default=None)

    profile = subparsers.add_parser("profile")
    profile_actions = profile.add_subparsers(
        dest="profile_action",
        required=True,
    )
    profile_list = profile_actions.add_parser("list")
    profile_list.add_argument("--state-dir", type=Path, default=None)

    login = profile_actions.add_parser("login")
    login_actions = login.add_subparsers(dest="login_action", required=True)
    login_prepare = login_actions.add_parser("prepare")
    _add_profile_selector_arguments(login_prepare)

    auth = profile_actions.add_parser("auth")
    auth_actions = auth.add_subparsers(dest="auth_action", required=True)
    auth_status = auth_actions.add_parser("status")
    _add_profile_selector_arguments(auth_status)

    activate = profile_actions.add_parser("activate")
    _add_profile_selector_arguments(activate)
    return parser


def _add_profile_selector_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("profile_id")
    parser.add_argument(
        "--provider",
        choices=("codex", "openai"),
        default="codex",
    )
    parser.add_argument("--state-dir", type=Path, default=None)


def _print(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = ServicePaths.from_state_dir(args.state_dir)
    if args.command == "start":
        try:
            run_foreground(paths)
        except ServiceAlreadyRunningError as exc:
            _print({"ok": False, "status": "already_running", "error": str(exc)})
            return 2
        return 0
    if args.command == "status":
        payload = current_status(paths)
        _print(payload)
        return 0 if payload.get("status") == "running" else 1
    if args.command == "profile":
        operations = {
            "list": "profile_list",
            "login": "profile_login_prepare",
            "auth": "profile_auth_status",
            "activate": "profile_activate",
        }
        operation = operations[args.profile_action]
        request_payload = None
        if args.profile_action != "list":
            request_payload = {
                "profile_id": args.profile_id,
                "provider": args.provider,
            }
        try:
            payload = request_service(
                paths,
                operation,
                payload=request_payload,
            )
        except ServiceUnavailableError as exc:
            payload = {**current_status(paths), "error": str(exc)}
            _print(payload)
            return 1
        _print(payload)
        return 0 if payload.get("ok") is True else 1
    try:
        payload = request_service(paths, args.command)
    except ServiceUnavailableError as exc:
        payload = {**current_status(paths), "error": str(exc)}
        _print(payload)
        return 1
    _print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
