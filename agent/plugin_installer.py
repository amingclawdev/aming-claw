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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union


DEFAULT_REPO_URL = "https://github.com/amingclawdev/aming-claw"
MIN_PYTHON_VERSION = (3, 9)
CODEX_MARKETPLACE_NAME = "aming-claw-local"
CODEX_PLUGIN_NAME = "aming-claw"
CODEX_PLUGIN_ID = f"{CODEX_PLUGIN_NAME}@{CODEX_MARKETPLACE_NAME}"
REQUIRED_PLUGIN_FILES = (
    ".codex-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "skills/aming-claw/SKILL.md",
    "skills/aming-claw-launcher/SKILL.md",
    ".mcp.json",
)
CODEX_PLUGIN_PAYLOAD = (
    ".codex-plugin",
    "skills",
    ".mcp.json",
    "README.md",
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
    installed_codex_plugin: bool
    started: bool
    codex_home: str = ""
    codex_cache_path: str = ""
    codex_marketplace_root: str = ""
    codex_config_path: str = ""
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
            "installed_codex_plugin": self.installed_codex_plugin,
            "codex_home": self.codex_home,
            "codex_cache_path": self.codex_cache_path,
            "codex_marketplace_root": self.codex_marketplace_root,
            "codex_config_path": self.codex_config_path,
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


def default_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def default_codex_marketplace_root() -> Path:
    return Path.home() / ".aming-claw" / "codex-marketplaces" / CODEX_MARKETPLACE_NAME


def _read_codex_manifest_version(plugin_root: Path) -> str:
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    if not manifest_path.is_file():
        return "0.1.0"
    manifest = _read_json_file(manifest_path)
    version = str(manifest.get("version") or "").strip()
    return version or "0.1.0"


def codex_cache_plugin_root(
    plugin_root: Path,
    *,
    codex_home: Optional[Union[Path, str]] = None,
) -> Path:
    home = Path(codex_home).expanduser() if codex_home else default_codex_home()
    version = _read_codex_manifest_version(plugin_root)
    return home / "plugins" / "cache" / CODEX_MARKETPLACE_NAME / CODEX_PLUGIN_NAME / version


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _copy_plugin_payload(plugin_root: Path, target_root: Path, *, dry_run: bool = False) -> None:
    source_root = plugin_root.expanduser().resolve()
    target = target_root.expanduser().resolve()
    if dry_run:
        return
    target.mkdir(parents=True, exist_ok=True)
    for rel in CODEX_PLUGIN_PAYLOAD:
        source = source_root / rel
        if not source.exists():
            continue
        destination = target / rel
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def install_codex_plugin_cache(
    plugin_root: Path,
    *,
    codex_home: Optional[Union[Path, str]] = None,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> Path:
    target = codex_cache_plugin_root(plugin_root, codex_home=codex_home)
    home = Path(codex_home).expanduser().resolve() if codex_home else default_codex_home().resolve()
    if not _is_relative_to(target, home / "plugins" / "cache"):
        raise PluginInstallError(f"refusing to write Codex plugin cache outside {home / 'plugins' / 'cache'}: {target}")
    if commands is not None:
        commands.append(CommandRecord(args=["install-codex-cache", str(target)], skipped=dry_run))
    _copy_plugin_payload(plugin_root, target, dry_run=dry_run)
    return target


def _codex_marketplace_payload() -> dict:
    return {
        "name": CODEX_MARKETPLACE_NAME,
        "interface": {"displayName": "Aming Claw Local"},
        "plugins": [
            {
                "name": CODEX_PLUGIN_NAME,
                "source": {"source": "local", "path": f"./{CODEX_PLUGIN_NAME}"},
                "policy": {
                    "installation": "INSTALLED_BY_DEFAULT",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }


def install_codex_marketplace(
    plugin_root: Path,
    *,
    marketplace_root: Optional[Union[Path, str]] = None,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> Path:
    root = Path(marketplace_root).expanduser() if marketplace_root else default_codex_marketplace_root()
    root = root.resolve()
    agents_root = root / ".agents" / "plugins"
    plugin_target = agents_root / CODEX_PLUGIN_NAME
    if commands is not None:
        commands.append(CommandRecord(args=["install-codex-marketplace", str(root)], skipped=dry_run))
    if dry_run:
        return root
    agents_root.mkdir(parents=True, exist_ok=True)
    (agents_root / "marketplace.json").write_text(
        json.dumps(_codex_marketplace_payload(), indent=2),
        encoding="utf-8",
    )
    _copy_plugin_payload(plugin_root, plugin_target, dry_run=False)
    return root


def _toml_quote(value: Union[str, Path]) -> str:
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_table_pattern(table_name: str) -> re.Pattern[str]:
    escaped = re.escape(table_name)
    return re.compile(rf"(?ms)^\[{escaped}\]\s*\r?\n.*?(?=^\[|\Z)")


def _upsert_toml_table(text: str, table_name: str, block_body: str) -> str:
    block = f"[{table_name}]\n{block_body.rstrip()}\n"
    pattern = _toml_table_pattern(table_name)
    if pattern.search(text):
        return pattern.sub(block, text).rstrip() + "\n"
    prefix = text.rstrip()
    return (prefix + "\n\n" if prefix else "") + block


def configure_codex_plugin(
    *,
    codex_config: Optional[Union[Path, str]] = None,
    marketplace_root: Optional[Union[Path, str]] = None,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> Path:
    config_path = Path(codex_config).expanduser() if codex_config else default_codex_config_path()
    market_root = Path(marketplace_root).expanduser() if marketplace_root else default_codex_marketplace_root()
    if commands is not None:
        commands.append(CommandRecord(args=["configure-codex-plugin", str(config_path)], skipped=dry_run))
    if dry_run:
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = config_path.read_text(encoding="utf-8", errors="replace") if config_path.is_file() else ""
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = _upsert_toml_table(
        text,
        f"marketplaces.{CODEX_MARKETPLACE_NAME}",
        "\n".join(
            [
                'source_type = "local"',
                f"source = {_toml_quote(market_root.resolve())}",
                f"last_updated = {_toml_quote(timestamp)}",
            ]
        ),
    )
    text = _upsert_toml_table(
        text,
        f'plugins."{CODEX_PLUGIN_ID}"',
        "enabled = true",
    )
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _default_doctor_root() -> Path:
    cwd = Path.cwd()
    if (cwd / ".codex-plugin" / "plugin.json").is_file():
        return cwd.resolve()
    return plugin_root_for(DEFAULT_REPO_URL, default_install_root())


def _doctor_check(name: str, status: str, detail: str = "") -> DoctorCheck:
    return DoctorCheck(name=name, status=status, detail=detail)


def _read_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_python_version(text: str) -> Optional[tuple[int, int, int]]:
    match = re.search(r"Python\s+(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )


def _python_version_check(python_executable: str) -> DoctorCheck:
    try:
        proc = subprocess.run(
            [python_executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return _doctor_check(
            "python_runtime",
            "fail",
            f"{python_executable}: cannot run --version ({exc})",
        )
    version_text = (proc.stdout or proc.stderr or "").strip()
    parsed = _parse_python_version(version_text)
    if proc.returncode != 0 or parsed is None:
        return _doctor_check(
            "python_runtime",
            "fail",
            f"{python_executable}: cannot determine Python version ({version_text or proc.returncode})",
        )
    required = ".".join(str(part) for part in MIN_PYTHON_VERSION)
    found = ".".join(str(part) for part in parsed)
    if parsed < (*MIN_PYTHON_VERSION, 0):
        return _doctor_check(
            "python_runtime",
            "fail",
            f"{python_executable}: Python {found} detected; Aming Claw requires Python {required}+; pass --python <path-to-python-{required}-or-newer>",
        )
    return _doctor_check("python_runtime", "ok", f"{python_executable}: Python {found}")


def _ensure_supported_python(python_executable: str) -> None:
    check = _python_version_check(python_executable)
    if check.status != "ok":
        raise PluginInstallError(check.detail)


def _check_codex_manifest(plugin_root: Path) -> DoctorCheck:
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        manifest = _read_json_file(manifest_path)
    except Exception as exc:
        return _doctor_check("codex_manifest", "fail", f"{manifest_path}: {exc}")
    interface = manifest.get("interface") if isinstance(manifest, dict) else {}
    prompts = interface.get("defaultPrompt") if isinstance(interface, dict) else []
    if prompts is None:
        prompts = []
    if not isinstance(prompts, list):
        return _doctor_check("codex_manifest", "fail", "interface.defaultPrompt must be a list")
    if len(prompts) > 3:
        return _doctor_check("codex_manifest", "fail", "interface.defaultPrompt must contain at most 3 prompts")
    too_long = [str(prompt) for prompt in prompts if len(str(prompt)) > 128]
    if too_long:
        return _doctor_check("codex_manifest", "fail", "interface.defaultPrompt entries must be <=128 chars")
    return _doctor_check("codex_manifest", "ok", f"{manifest_path} defaultPrompt count={len(prompts)}")


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
    if raw_path in {".", "./"}:
        return _doctor_check(
            "codex_marketplace",
            "fail",
            f"source.path={raw_path!r} normalizes to an empty local plugin path for Codex CLI",
        )

    marketplace_root = marketplace_path.parent.resolve()
    resolved = (marketplace_root / raw_path).resolve()
    if not _is_relative_to(resolved, marketplace_root):
        return _doctor_check(
            "codex_marketplace",
            "warn",
            f"{marketplace_path} is a repo-local compatibility manifest; source.path={raw_path!r} escapes the Codex marketplace root. The installer writes a generated marketplace/cache for real Codex CLI loading.",
        )
    if not (resolved / ".codex-plugin" / "plugin.json").is_file():
        return _doctor_check(
            "codex_marketplace",
            "warn",
            f"source.path={raw_path!r} resolves to {resolved}, but no .codex-plugin/plugin.json was found",
        )
    if not raw_path.startswith("./"):
        return _doctor_check(
            "codex_marketplace",
            "warn",
            f"source.path={raw_path!r} resolves to {resolved}; prefer './{CODEX_PLUGIN_NAME}' inside the marketplace root",
        )
    return _doctor_check(
        "codex_marketplace",
        "ok",
        f"{marketplace_path} -> source.path {raw_path!r} resolves to {resolved}",
    )


def _check_claude_marketplace(plugin_root: Path) -> DoctorCheck:
    marketplace_path = plugin_root / ".claude-plugin" / "marketplace.json"
    try:
        marketplace = _read_json_file(marketplace_path)
    except Exception as exc:
        return _doctor_check("claude_marketplace", "fail", f"{marketplace_path}: {exc}")

    if not isinstance(marketplace, dict):
        return _doctor_check("claude_marketplace", "fail", f"{marketplace_path}: top-level must be an object")
    if not str(marketplace.get("name") or "").strip():
        return _doctor_check("claude_marketplace", "fail", "missing top-level `name`")

    owner = marketplace.get("owner")
    if not isinstance(owner, dict) or not str(owner.get("name") or "").strip():
        return _doctor_check("claude_marketplace", "fail", "missing or empty `owner.name`")

    plugins = marketplace.get("plugins")
    match = next(
        (
            item
            for item in plugins or []
            if isinstance(item, dict) and item.get("name") == "aming-claw"
        ),
        None,
    )
    if not match:
        return _doctor_check("claude_marketplace", "fail", "missing plugin entry `aming-claw`")

    source = str(match.get("source") or "").strip()
    if not source:
        return _doctor_check("claude_marketplace", "fail", "missing plugins[].source")
    if not source.startswith("./"):
        # Claude Code 2.1.140 rejects bare "." as Invalid input; "./" is the
        # canonical relative form (see MF-2026-05-15-CLAUDE-MARKETPLACE-SOURCE-SCHEMA).
        return _doctor_check(
            "claude_marketplace",
            "fail",
            f"plugins[].source={source!r} must start with './' (Claude Code rejects bare '.' as Invalid input)",
        )

    metadata = marketplace.get("metadata")
    if not isinstance(metadata, dict) or not str(metadata.get("description") or "").strip():
        # claude plugin validate warns when metadata.description is missing.
        return _doctor_check(
            "claude_marketplace",
            "warn",
            f"{marketplace_path}: missing metadata.description (claude plugin validate warns)",
        )

    return _doctor_check(
        "claude_marketplace",
        "ok",
        f"{marketplace_path} -> name={marketplace.get('name')!r} source={source!r} metadata.description set",
    )


def _check_claude_manifest(plugin_root: Path) -> DoctorCheck:
    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    try:
        manifest = _read_json_file(manifest_path)
    except Exception as exc:
        return _doctor_check("claude_manifest", "fail", f"{manifest_path}: {exc}")

    if not isinstance(manifest, dict):
        return _doctor_check("claude_manifest", "fail", f"{manifest_path}: top-level must be an object")
    for field in ("name", "version", "description"):
        if not str(manifest.get(field) or "").strip():
            return _doctor_check("claude_manifest", "fail", f"missing or empty `{field}`")

    mcp_servers = manifest.get("mcpServers")
    if mcp_servers is None:
        # mcpServers is optional in the Claude Code schema; without it the plugin
        # install will not expose any MCP server (see MF #2a manifest fix).
        return _doctor_check(
            "claude_manifest",
            "warn",
            f"{manifest_path}: no `mcpServers` declared; plugin install will not expose an MCP server",
        )

    if isinstance(mcp_servers, str):
        if not mcp_servers.strip():
            return _doctor_check("claude_manifest", "fail", "`mcpServers` path is empty")
    elif isinstance(mcp_servers, dict):
        if not mcp_servers:
            return _doctor_check("claude_manifest", "fail", "`mcpServers` object is empty")
        for server_name, spec in mcp_servers.items():
            if not isinstance(spec, dict):
                return _doctor_check(
                    "claude_manifest",
                    "fail",
                    f"mcpServers[{server_name!r}] must be an object",
                )
            command = str(spec.get("command") or "").strip()
            if not command:
                return _doctor_check(
                    "claude_manifest",
                    "fail",
                    f"mcpServers[{server_name!r}].command must be non-empty",
                )
            args = spec.get("args")
            if not isinstance(args, list):
                return _doctor_check(
                    "claude_manifest",
                    "fail",
                    f"mcpServers[{server_name!r}].args must be a list",
                )
    else:
        return _doctor_check(
            "claude_manifest",
            "fail",
            "`mcpServers` must be a path string or an object map of server specs",
        )

    return _doctor_check(
        "claude_manifest",
        "ok",
        f"{manifest_path} -> name={manifest.get('name')!r} mcpServers declared",
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


def _extract_toml_table(text: str, table_name: str) -> str:
    text = text.lstrip("\ufeff")
    match = _toml_table_pattern(table_name).search(text)
    return match.group(0) if match else ""


def _extract_toml_string(block: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(['\"])(.*?)\1\s*$", block)
    if not match:
        return ""
    value = match.group(2)
    if match.group(1) == '"':
        value = value.replace("\\\\", "\\").replace('\\"', '"')
    return value


def _validate_codex_marketplace_root(root: Path) -> tuple[bool, str]:
    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    try:
        marketplace = _read_json_file(marketplace_path)
    except Exception as exc:
        return False, f"{marketplace_path}: {exc}"
    plugins = marketplace.get("plugins") if isinstance(marketplace, dict) else []
    match = next(
        (
            item
            for item in plugins or []
            if isinstance(item, dict) and item.get("name") == CODEX_PLUGIN_NAME
        ),
        None,
    )
    if not match:
        return False, f"{marketplace_path}: missing plugin entry `{CODEX_PLUGIN_NAME}`"
    source = match.get("source") if isinstance(match.get("source"), dict) else {}
    raw_path = str(source.get("path") or "").strip()
    if not raw_path:
        return False, f"{marketplace_path}: missing source.path"
    marketplace_root = marketplace_path.parent.resolve()
    resolved = (marketplace_root / raw_path).resolve()
    if not _is_relative_to(resolved, marketplace_root):
        return False, f"{marketplace_path}: source.path {raw_path!r} escapes marketplace root"
    if not (resolved / ".codex-plugin" / "plugin.json").is_file():
        return False, f"{marketplace_path}: source.path {raw_path!r} has no .codex-plugin/plugin.json"
    return True, f"{marketplace_path} -> {resolved}"


def _codex_plugin_enabled(text: str) -> bool:
    block = _extract_toml_table(text, f'plugins."{CODEX_PLUGIN_ID}"')
    return bool(re.search(r"(?m)^\s*enabled\s*=\s*true\s*$", block, flags=re.IGNORECASE))


def _check_codex_config(path: Path) -> DoctorCheck:
    if not path.is_file():
        return _doctor_check(
            "codex_config",
            "warn",
            f"{path} not found; run `aming-claw plugin install` to enable {CODEX_PLUGIN_ID}",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    plugin_enabled = _codex_plugin_enabled(text)
    marketplace_block = _extract_toml_table(text, f"marketplaces.{CODEX_MARKETPLACE_NAME}")
    marketplace_source = _extract_toml_string(marketplace_block, "source")
    if plugin_enabled and marketplace_source:
        ok, detail = _validate_codex_marketplace_root(Path(marketplace_source).expanduser())
        if not ok:
            return _doctor_check("codex_config", "fail", f"{path}: {detail}")
        return _doctor_check("codex_config", "ok", f"{path} enables {CODEX_PLUGIN_ID}; {detail}")
    if plugin_enabled:
        return _doctor_check(
            "codex_config",
            "ok",
            f"{path} enables {CODEX_PLUGIN_ID}; no marketplace source configured, so Codex relies on installed cache",
        )
    return _doctor_check(
        "codex_config",
        "warn",
        f"{path} exists but {CODEX_PLUGIN_ID} is not enabled",
    )


def _check_codex_cache(plugin_root: Path, *, codex_home: Optional[Union[Path, str]] = None, codex_config: Optional[Path] = None) -> DoctorCheck:
    home = Path(codex_home).expanduser() if codex_home else (
        codex_config.parent if codex_config else default_codex_home()
    )
    try:
        cache_root = codex_cache_plugin_root(plugin_root, codex_home=home)
    except Exception as exc:
        return _doctor_check("codex_plugin_cache", "fail", f"cannot compute cache path: {exc}")
    manifest = cache_root / ".codex-plugin" / "plugin.json"
    if manifest.is_file():
        return _doctor_check("codex_plugin_cache", "ok", f"{manifest}")
    return _doctor_check(
        "codex_plugin_cache",
        "fail",
        f"missing installed plugin cache at {cache_root}; run `aming-claw plugin install`",
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
        "missing dashboard index; installed plugins should include agent/governance/dashboard_dist/index.html. In a raw checkout, run `cd frontend/dashboard && npm install && npm run build`.",
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
    codex_home: Optional[Union[Path, str]] = None,
    check_governance: bool = True,
    python_executable: Optional[str] = None,
) -> DoctorResult:
    """Run read-only aftercare checks for a local plugin install."""

    root = Path(plugin_root).expanduser().resolve() if plugin_root else _default_doctor_root()
    result = DoctorResult(plugin_root=str(root), governance_url=governance_url)

    try:
        validated = validate_plugin_root(root)
        result.checks.append(_doctor_check("plugin_assets", "ok", ", ".join(validated)))
    except PluginInstallError as exc:
        result.checks.append(_doctor_check("plugin_assets", "fail", str(exc)))

    result.checks.append(_python_version_check(python_executable or sys.executable))
    result.checks.append(_check_codex_manifest(root))
    result.checks.append(_check_marketplace(root))
    result.checks.append(_check_claude_manifest(root))
    result.checks.append(_check_claude_marketplace(root))
    result.checks.append(_check_mcp_config(root))
    codex_config_path = Path(codex_config).expanduser() if codex_config else default_codex_config_path()
    result.checks.append(_check_codex_config(codex_config_path))
    result.checks.append(_check_codex_cache(root, codex_home=codex_home, codex_config=codex_config_path))
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
        f"Codex: plugin cache and config are installed for `{CODEX_PLUGIN_ID}`; reload Codex or open a new session, then confirm the Aming Claw skill/MCP tools are visible.",
        f"Claude Code: /plugin marketplace add {root}",
        "Claude Code: /plugin install aming-claw@aming-claw-local",
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
    install_codex_plugin: bool = True,
    codex_home: Optional[Union[Path, str]] = None,
    codex_config: Optional[Union[Path, str]] = None,
    codex_marketplace_root: Optional[Union[Path, str]] = None,
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
        if not dry_run:
            _ensure_supported_python(python)
        _run(
            [python, "-m", "pip", "install", "-e", str(plugin_root)],
            dry_run=dry_run,
            commands=commands,
        )
        installed_package = not dry_run

    codex_cache_path = ""
    codex_marketplace_path = ""
    codex_config_path = ""
    installed_codex_plugin = False
    if install_codex_plugin and not validate_only:
        cache_target = install_codex_plugin_cache(
            plugin_root,
            codex_home=codex_home,
            dry_run=dry_run,
            commands=commands,
        )
        marketplace_target = install_codex_marketplace(
            plugin_root,
            marketplace_root=codex_marketplace_root,
            dry_run=dry_run,
            commands=commands,
        )
        config_target = configure_codex_plugin(
            codex_config=codex_config,
            marketplace_root=marketplace_target,
            dry_run=dry_run,
            commands=commands,
        )
        codex_cache_path = str(cache_target)
        codex_marketplace_path = str(marketplace_target)
        codex_config_path = str(config_target)
        installed_codex_plugin = not dry_run

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
        installed_codex_plugin=installed_codex_plugin,
        codex_home=str(Path(codex_home).expanduser() if codex_home else default_codex_home()),
        codex_cache_path=codex_cache_path,
        codex_marketplace_root=codex_marketplace_path,
        codex_config_path=codex_config_path,
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
    if result.codex_cache_path or result.codex_config_path:
        lines.append("")
        lines.append("Codex plugin install:")
        if result.codex_cache_path:
            lines.append(f"  cache:       {result.codex_cache_path}")
        if result.codex_marketplace_root:
            lines.append(f"  marketplace: {result.codex_marketplace_root}")
        if result.codex_config_path:
            lines.append(f"  config:      {result.codex_config_path}")
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
    parser.add_argument("--no-codex-install", action="store_true", help="Do not install Codex plugin cache/config.")
    parser.add_argument("--codex-home", default="", help="Override Codex home for plugin cache/config.")
    parser.add_argument("--codex-config", default="", help="Override Codex config.toml path.")
    parser.add_argument("--codex-marketplace-root", default="", help="Override generated Codex marketplace root.")
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
            install_codex_plugin=not args.no_codex_install,
            codex_home=args.codex_home or None,
            codex_config=args.codex_config or None,
            codex_marketplace_root=args.codex_marketplace_root or None,
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
