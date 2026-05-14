"""Git URL bootstrap helpers for local Aming Claw plugin installs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union


DEFAULT_REPO_URL = "https://github.com/amingclawdev/aming-claw"
REQUIRED_PLUGIN_FILES = (
    ".codex-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "skills/aming-claw/SKILL.md",
    "skills/aming-claw-launcher/SKILL.md",
    ".mcp.json",
)


@dataclass
class CommandRecord:
    args: list[str]
    cwd: str = ""
    skipped: bool = False


@dataclass
class InstallResult:
    repo_url: str
    install_root: str
    plugin_root: str
    dry_run: bool
    installed_package: bool
    started: bool
    validated_files: list[str] = field(default_factory=list)
    commands: list[CommandRecord] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "repo_url": self.repo_url,
            "install_root": self.install_root,
            "plugin_root": self.plugin_root,
            "dry_run": self.dry_run,
            "installed_package": self.installed_package,
            "started": self.started,
            "validated_files": list(self.validated_files),
            "commands": [
                {"args": list(cmd.args), "cwd": cmd.cwd, "skipped": cmd.skipped}
                for cmd in self.commands
            ],
            "next_steps": list(self.next_steps),
        }


class PluginInstallError(RuntimeError):
    """Raised when a Git URL plugin bootstrap step cannot complete."""


def default_install_root() -> Path:
    raw = os.environ.get("AMING_CLAW_PLUGIN_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".aming-claw" / "plugins"


def slug_from_repo_url(repo_url: str) -> str:
    cleaned = repo_url.rstrip("/").rstrip()
    tail = cleaned.rsplit("/", 1)[-1] or "aming-claw"
    tail = tail[:-4] if tail.endswith(".git") else tail
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", tail).strip(".-")
    return slug or "aming-claw"


def plugin_root_for(repo_url: str, install_root: Path) -> Path:
    return install_root.expanduser().resolve() / slug_from_repo_url(repo_url)


def _command_text(args: Sequence[str]) -> str:
    parts = [str(part) for part in args]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _run(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> None:
    record = CommandRecord(args=[str(part) for part in args], cwd=str(cwd or ""), skipped=dry_run)
    if commands is not None:
        commands.append(record)
    if dry_run:
        return
    try:
        subprocess.run([str(part) for part in args], cwd=str(cwd) if cwd else None, check=True)
    except FileNotFoundError as exc:
        raise PluginInstallError(f"command not found: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise PluginInstallError(
            f"command failed ({exc.returncode}): {_command_text(record.args)}"
        ) from exc


def validate_plugin_root(plugin_root: Path) -> list[str]:
    root = plugin_root.expanduser().resolve()
    missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (root / rel).is_file()]
    if missing:
        raise PluginInstallError(
            "plugin root is missing required files: " + ", ".join(missing)
        )

    for rel in (
        ".codex-plugin/plugin.json",
        ".agents/plugins/marketplace.json",
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".mcp.json",
    ):
        try:
            json.loads((root / rel).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PluginInstallError(f"invalid JSON in {rel}: {exc}") from exc

    return list(REQUIRED_PLUGIN_FILES)


def clone_or_update(
    repo_url: str,
    plugin_root: Path,
    *,
    ref: str = "",
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> None:
    root = plugin_root.expanduser().resolve()
    git_dir = root / ".git"
    if git_dir.is_dir():
        _run(["git", "fetch", "--all", "--prune"], cwd=root, dry_run=dry_run, commands=commands)
        if ref:
            _run(["git", "checkout", ref], cwd=root, dry_run=dry_run, commands=commands)
        _run(["git", "pull", "--ff-only"], cwd=root, dry_run=dry_run, commands=commands)
        return

    if root.exists() and any(root.iterdir()):
        raise PluginInstallError(f"install target exists and is not a git checkout: {root}")

    if not dry_run:
        root.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", repo_url, str(root)], dry_run=dry_run, commands=commands)
    if ref:
        _run(["git", "checkout", ref], cwd=root, dry_run=dry_run, commands=commands)


def build_next_steps(plugin_root: Path, python_executable: str) -> list[str]:
    root = str(plugin_root)
    return [
        f"Claude Code: /plugin marketplace add {root}",
        "Claude Code: /plugin install aming-claw@aming-claw-local",
        f"Codex: open {root} or add its local plugin marketplace, then use the Aming Claw skill.",
        f"Start services: {python_executable} -m agent.cli start --workspace {root}",
        "Dashboard: http://localhost:40000/dashboard",
    ]


def install_from_git(
    repo_url: str = DEFAULT_REPO_URL,
    *,
    install_root: Optional[Union[Path, str]] = None,
    ref: str = "",
    python_executable: Optional[str] = None,
    install_package: bool = True,
    start: bool = False,
    dry_run: bool = False,
    validate_only: bool = False,
) -> InstallResult:
    """Clone/update a plugin checkout, validate it, and optionally install runtime."""

    root = Path(install_root).expanduser() if install_root else default_install_root()
    plugin_root = plugin_root_for(repo_url, root)
    python = python_executable or sys.executable
    commands: list[CommandRecord] = []

    if not validate_only:
        clone_or_update(repo_url, plugin_root, ref=ref, dry_run=dry_run, commands=commands)

    validated: list[str] = []
    if dry_run and not plugin_root.exists():
        # Network-free dry-runs are allowed to plan a fresh clone without
        # validating files that do not exist yet.
        validated = []
    else:
        validated = validate_plugin_root(plugin_root)

    installed_package = False
    if install_package:
        _run(
            [python, "-m", "pip", "install", "-e", str(plugin_root)],
            dry_run=dry_run,
            commands=commands,
        )
        installed_package = not dry_run

    started = False
    if start:
        _run(
            [python, "-m", "agent.cli", "start"],
            cwd=plugin_root,
            dry_run=dry_run,
            commands=commands,
        )
        started = not dry_run

    return InstallResult(
        repo_url=repo_url,
        install_root=str(root.expanduser().resolve()),
        plugin_root=str(plugin_root),
        dry_run=dry_run,
        installed_package=installed_package,
        started=started,
        validated_files=validated,
        commands=commands,
        next_steps=build_next_steps(plugin_root, python),
    )


def format_result(result: InstallResult) -> str:
    lines = [
        "Aming Claw plugin bootstrap",
        f"  repo:        {result.repo_url}",
        f"  plugin root: {result.plugin_root}",
        f"  dry run:     {str(result.dry_run).lower()}",
    ]
    if result.commands:
        lines.append("")
        lines.append("Commands:")
        for command in result.commands:
            prefix = "  would run:" if command.skipped else "  ran:"
            cwd = f" (cwd={command.cwd})" if command.cwd else ""
            lines.append(f"{prefix} {_command_text(command.args)}{cwd}")
    if result.validated_files:
        lines.append("")
        lines.append("Validated plugin assets:")
        lines.extend(f"  - {rel}" for rel in result.validated_files)
    lines.append("")
    lines.append("Next steps:")
    lines.extend(f"  {step}" for step in result.next_steps)
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the Aming Claw local plugin/runtime from a Git URL."
    )
    parser.add_argument("repo_url", nargs="?", default=DEFAULT_REPO_URL)
    parser.add_argument("--install-root", default="", help="User-local plugin cache root.")
    parser.add_argument("--ref", default="", help="Optional branch, tag, or commit to checkout.")
    parser.add_argument("--python", default=sys.executable, help="Python executable for pip/start commands.")
    parser.add_argument("--no-pip", action="store_true", help="Clone and validate only; do not pip install.")
    parser.add_argument("--start", action="store_true", help="Run the start command after install.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without changing state.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the computed checkout path without cloning or fetching.",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = install_from_git(
            args.repo_url,
            install_root=args.install_root or None,
            ref=args.ref,
            python_executable=args.python,
            install_package=not args.no_pip,
            start=args.start,
            dry_run=args.dry_run,
            validate_only=args.validate_only,
        )
    except PluginInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
