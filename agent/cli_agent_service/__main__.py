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
    return parser


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
