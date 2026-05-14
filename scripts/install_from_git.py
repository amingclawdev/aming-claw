"""Bootstrap Aming Claw from a Git URL without a preinstalled console script.

This file is intentionally usable in two modes:

- From a cloned checkout, it imports ``agent.plugin_installer`` directly.
- From a raw downloaded script, it uses only the standard library plus ``git``
  to clone/update the repo, then delegates to the real installer inside that
  checkout.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional


DEFAULT_REPO_URL = "https://github.com/amingclawdev/aming-claw"


def _repo_root() -> Optional[Path]:
    try:
        return Path(__file__).resolve().parents[1]
    except NameError:
        return None


def _command_text(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _slug_from_repo_url(repo_url: str) -> str:
    cleaned = repo_url.rstrip("/").rstrip()
    tail = cleaned.rsplit("/", 1)[-1] or "aming-claw"
    tail = tail[:-4] if tail.endswith(".git") else tail
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", tail).strip(".-")
    return slug or "aming-claw"


def _default_install_root() -> Path:
    raw = os.environ.get("AMING_CLAW_PLUGIN_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".aming-claw" / "plugins"


def _parse_fallback_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("repo_url", nargs="?", default=DEFAULT_REPO_URL)
    parser.add_argument("--install-root", default="")
    parser.add_argument("--ref", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    return args


def _run(args: list[str], *, cwd: Optional[Path] = None, dry_run: bool = False) -> None:
    if dry_run:
        cwd_text = f" (cwd={cwd})" if cwd else ""
        print(f"would run: {_command_text(args)}{cwd_text}")
        return
    subprocess.run(args, cwd=str(cwd) if cwd else None, check=True)


def _fallback_clone_then_delegate(argv: list[str]) -> int:
    args = _parse_fallback_args(argv)
    install_root = Path(args.install_root).expanduser() if args.install_root else _default_install_root()
    plugin_root = install_root.resolve() / _slug_from_repo_url(args.repo_url)
    git_dir = plugin_root / ".git"

    if args.validate_only:
        raise SystemExit("error: --validate-only requires a cloned checkout with agent.plugin_installer")

    if git_dir.is_dir():
        _run(["git", "fetch", "--all", "--prune"], cwd=plugin_root, dry_run=args.dry_run)
        if args.ref:
            _run(["git", "checkout", args.ref], cwd=plugin_root, dry_run=args.dry_run)
        _run(["git", "pull", "--ff-only"], cwd=plugin_root, dry_run=args.dry_run)
    else:
        if plugin_root.exists() and any(plugin_root.iterdir()):
            raise SystemExit(f"error: install target exists and is not a git checkout: {plugin_root}")
        if not args.dry_run:
            plugin_root.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", args.repo_url, str(plugin_root)], dry_run=args.dry_run)
        if args.ref:
            _run(["git", "checkout", args.ref], cwd=plugin_root, dry_run=args.dry_run)

    if args.dry_run:
        print(f"plugin root: {plugin_root}")
        return 0

    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    from agent.plugin_installer import main as real_main

    delegated = list(argv)
    if "--validate-only" not in delegated:
        delegated.append("--validate-only")
    return real_main(delegated)


repo_root = _repo_root()
if repo_root and str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

try:
    from agent.plugin_installer import main
except ModuleNotFoundError:
    main = _fallback_clone_then_delegate


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
