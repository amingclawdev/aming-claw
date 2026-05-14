"""Git URL bootstrap helpers for local Aming Claw plugin installs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
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
AI_CLI_REQUIREMENTS = {
    "openai": {
        "runtime": "Codex CLI",
        "command": "codex",
        "env_var": "CODEX_BIN",
    },
    "anthropic": {
        "runtime": "Claude Code CLI",
        "command": "claude",
        "env_var": "CLAUDE_BIN",
    },
}


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


@dataclass
class DoctorCheck:
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class DoctorResult:
    plugin_root: str
    governance_url: str
    checks: list[DoctorCheck] = field(default_factory=list)
    manual_steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "plugin_root": self.plugin_root,
            "governance_url": self.governance_url,
            "checks": [check.to_dict() for check in self.checks],
            "manual_steps": list(self.manual_steps),
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


def _spawn_long_running(
    args: Sequence[str],
    *,
    cwd: Path,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> None:
    record = CommandRecord(args=[str(part) for part in args], cwd=str(cwd), skipped=dry_run)
    if commands is not None:
        commands.append(record)
    if dry_run:
        return

    log_path = cwd / ".aming-claw-start.log"
    try:
        log_handle = log_path.open("ab")
    except OSError as exc:
        raise PluginInstallError(f"cannot open start log {log_path}: {exc}") from exc

    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [str(part) for part in args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
    except FileNotFoundError as exc:
        raise PluginInstallError(f"command not found: {args[0]}") from exc
    except OSError as exc:
        raise PluginInstallError(f"failed to start long-running service: {exc}") from exc
    finally:
        log_handle.close()


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


def default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _default_doctor_root() -> Path:
    cwd = Path.cwd()
    if (cwd / ".codex-plugin" / "plugin.json").is_file():
        return cwd.resolve()
    return plugin_root_for(DEFAULT_REPO_URL, default_install_root())


def _doctor_check(name: str, status: str, detail: str = "") -> DoctorCheck:
    return DoctorCheck(name=name, status=status, detail=detail)


def _read_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_marketplace(plugin_root: Path) -> DoctorCheck:
    marketplace_path = plugin_root / ".agents" / "plugins" / "marketplace.json"
    try:
        marketplace = _read_json_file(marketplace_path)
    except Exception as exc:
        return _doctor_check("codex_marketplace", "fail", f"{marketplace_path}: {exc}")

    plugins = marketplace.get("plugins") if isinstance(marketplace, dict) else []
    match = next(
        (
            item
            for item in plugins or []
            if isinstance(item, dict) and item.get("name") == "aming-claw"
        ),
        None,
    )
    if not match:
        return _doctor_check("codex_marketplace", "fail", "missing plugin entry `aming-claw`")

    source = match.get("source") if isinstance(match.get("source"), dict) else {}
    raw_path = str(source.get("path") or "").strip()
    if not raw_path:
        return _doctor_check("codex_marketplace", "fail", "missing source.path")

    # Codex marketplace paths are resolved relative to the marketplace root,
    # not relative to `.agents/plugins/marketplace.json`.
    resolved = (plugin_root / raw_path).resolve()
    if not (resolved / ".codex-plugin" / "plugin.json").is_file():
        return _doctor_check(
            "codex_marketplace",
            "fail",
            f"source.path={raw_path!r} resolves to {resolved}, but no .codex-plugin/plugin.json was found",
        )
    if not (raw_path.startswith("./") or raw_path.startswith("../") or os.path.isabs(raw_path)):
        return _doctor_check(
            "codex_marketplace",
            "warn",
            f"source.path={raw_path!r} resolves to {resolved}; prefer './' for Codex CLI local plugin paths",
        )
    return _doctor_check(
        "codex_marketplace",
        "ok",
        f"{marketplace_path} -> source.path {raw_path!r} resolves to {resolved}",
    )


def _check_mcp_config(plugin_root: Path) -> DoctorCheck:
    path = plugin_root / ".mcp.json"
    try:
        payload = _read_json_file(path)
    except Exception as exc:
        return _doctor_check("mcp_config", "fail", f"{path}: {exc}")
    servers = payload.get("mcpServers") if isinstance(payload, dict) else {}
    if not isinstance(servers, dict) or "aming-claw" not in servers:
        return _doctor_check("mcp_config", "fail", "missing mcpServers.aming-claw")
    return _doctor_check("mcp_config", "ok", str(path))


def _check_codex_config(path: Path) -> DoctorCheck:
    if not path.is_file():
        return _doctor_check(
            "codex_config",
            "warn",
            f"{path} not found; install may still work through a repo-local marketplace, but restart Codex/new session is required",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    has_marketplace = "marketplace" in text.lower()
    has_plugin = "aming-claw" in text and ("aming-claw-local" in text or ".agents" in text)
    if has_marketplace and has_plugin:
        return _doctor_check("codex_config", "ok", f"{path} references aming-claw marketplace/plugin")
    return _doctor_check(
        "codex_config",
        "warn",
        f"{path} exists but no obvious aming-claw marketplace/plugin entry was found",
    )


def _check_governance(governance_url: str) -> DoctorCheck:
    url = governance_url.rstrip("/") + "/api/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return _doctor_check("governance_health", "warn", f"{url}: {exc}")
    if payload.get("status") == "ok" or payload.get("ok") is True:
        return _doctor_check("governance_health", "ok", f"{url} status ok")
    return _doctor_check("governance_health", "warn", f"{url} returned {payload}")


def _check_dashboard_assets(plugin_root: Path) -> DoctorCheck:
    candidates = [
        plugin_root / "agent" / "governance" / "dashboard_dist" / "index.html",
        plugin_root / "frontend" / "dashboard" / "dist" / "index.html",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return _doctor_check("dashboard_static_assets", "ok", str(candidate))
    return _doctor_check(
        "dashboard_static_assets",
        "warn",
        "missing dashboard index; run `cd frontend/dashboard && npm install && npm run build` in a raw checkout",
    )


def _check_dashboard_route(governance_url: str) -> DoctorCheck:
    url = governance_url.rstrip("/") + "/dashboard"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            status = getattr(response, "status", response.getcode())
    except urllib.error.HTTPError as exc:
        return _doctor_check(
            "dashboard_http_route",
            "warn",
            f"{url} returned HTTP {exc.code}; root `/` is not the dashboard",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _doctor_check("dashboard_http_route", "warn", f"{url}: {exc}")
    if status == 200:
        return _doctor_check("dashboard_http_route", "ok", f"{url} returned 200")
    return _doctor_check("dashboard_http_route", "warn", f"{url} returned HTTP {status}")


def _check_manager_health(manager_url: str = "http://127.0.0.1:40101") -> DoctorCheck:
    url = manager_url.rstrip("/") + "/api/manager/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return _doctor_check(
            "service_manager_health",
            "warn",
            f"{url}: {exc}; governance/dashboard can still be usable, chain/executor is degraded",
        )
    if payload.get("ok") is True:
        version = payload.get("runtime_version") or ""
        detail = f"{url} ok"
        if version:
            detail += f", runtime_version={version}"
        return _doctor_check("service_manager_health", "ok", detail)
    return _doctor_check("service_manager_health", "warn", f"{url} returned {payload}")


def _check_ai_cli(provider: str, requirement: dict[str, str]) -> DoctorCheck:
    env_var = requirement["env_var"]
    configured = os.environ.get(env_var, "").strip()
    command = configured or requirement["command"]
    resolved = command if os.path.isabs(command) else shutil.which(command)
    label = requirement["runtime"]
    if not resolved:
        return _doctor_check(
            f"ai_cli_{provider}",
            "warn",
            f"{label} missing; expected `{requirement['command']}` or {env_var}",
        )
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return _doctor_check(
            f"ai_cli_{provider}",
            "warn",
            f"{label} found at {resolved}, but version probe failed: {exc}",
        )
    version = (proc.stdout or proc.stderr or "").strip().splitlines()[0:1]
    if proc.returncode != 0:
        return _doctor_check(
            f"ai_cli_{provider}",
            "warn",
            f"{label} found at {resolved}, but version probe exited {proc.returncode}",
        )
    suffix = f", version {version[0]}" if version else ""
    return _doctor_check(
        f"ai_cli_{provider}",
        "ok",
        f"{label}: detected at {resolved}{suffix}, auth unknown",
    )


def doctor_plugin(
    *,
    plugin_root: Optional[Union[Path, str]] = None,
    governance_url: str = "http://localhost:40000",
    codex_config: Optional[Union[Path, str]] = None,
    check_governance: bool = True,
) -> DoctorResult:
    """Run read-only aftercare checks for a local plugin install."""

    root = Path(plugin_root).expanduser().resolve() if plugin_root else _default_doctor_root()
    result = DoctorResult(plugin_root=str(root), governance_url=governance_url)

    try:
        validated = validate_plugin_root(root)
        result.checks.append(_doctor_check("plugin_assets", "ok", ", ".join(validated)))
    except PluginInstallError as exc:
        result.checks.append(_doctor_check("plugin_assets", "fail", str(exc)))

    result.checks.append(_check_marketplace(root))
    result.checks.append(_check_mcp_config(root))
    result.checks.append(_check_codex_config(Path(codex_config).expanduser() if codex_config else default_codex_config_path()))
    result.checks.append(_check_dashboard_assets(root))
    for provider, requirement in AI_CLI_REQUIREMENTS.items():
        result.checks.append(_check_ai_cli(provider, requirement))
    if check_governance:
        result.checks.append(_check_governance(governance_url))
        result.checks.append(_check_dashboard_route(governance_url))
        result.checks.append(_check_manager_health(os.environ.get("MANAGER_URL", "http://127.0.0.1:40101")))

    result.manual_steps.extend(
        [
            "Restart/reload Codex or open a new session after installing the plugin; existing threads may not hot-load new skills/MCP tools.",
            "In the new session, confirm the Aming Claw skill is visible and mcp__aming_claw tools are available.",
            "Remember: `aming-claw start` only starts governance; it does not prove plugin loading, dashboard assets, ServiceManager, executor, or AI auth.",
        ]
    )
    return result


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
        "Reload Codex or open a new session after plugin install; current threads may not hot-load new skills/MCP tools.",
        f"Verify install: {python_executable} -m agent.cli plugin doctor --plugin-root {root}",
        "Start services in a separate terminal/window; this is a long-running command:",
        f"  cd {root}",
        f"  {python_executable} -m agent.cli start --workspace {root}",
        "Do not wait for the start command to exit; verify with `aming-claw status` or the dashboard.",
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
        _spawn_long_running(
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


def format_doctor_result(result: DoctorResult) -> str:
    lines = [
        "Aming Claw plugin doctor",
        f"  plugin root:    {result.plugin_root}",
        f"  governance url: {result.governance_url}",
        f"  overall:        {'ok' if result.ok else 'needs attention'}",
        "",
        "Checks:",
    ]
    for check in result.checks:
        detail = f" - {check.detail}" if check.detail else ""
        lines.append(f"  [{check.status}] {check.name}{detail}")
    if result.manual_steps:
        lines.append("")
        lines.append("Manual aftercare:")
        lines.extend(f"  - {step}" for step in result.manual_steps)
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
