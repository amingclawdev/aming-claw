"""CLI entry point for aming-claw.

Usage:
    aming-claw init            - create .aming-claw.yaml in current directory
    aming-claw bootstrap       - bootstrap an external project
    aming-claw status          - show governance status
    aming-claw plugin install  - install/update plugin assets from Git
    aming-claw start           - start governance in the foreground
    aming-claw open            - open the dashboard URL
    aming-claw launcher        - write a local launcher HTML artifact
    aming-claw run-executor    - start executor worker
"""

import os
import sys
import logging
import json
import webbrowser
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import click
except ImportError:
    # Provide a helpful error when click isn't installed
    print("Error: 'click' package is required. Install with: pip install click", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger(__name__)

DEFAULT_GOVERNANCE_URL = "http://localhost:40000"

_YAML_TEMPLATE = """\
# aming-claw project configuration
project_id: ""
workspace_path: "."
governance_port: 40000
notification_backend: "telegram"
redis_url: "redis://localhost:6379/0"
max_workers: 4
db_path: ""
"""


@click.group()
@click.version_option(package_name="aming-claw")
def main():
    """aming-claw - governance-driven workflow platform."""
    pass


@main.command()
def init():
    """Initialize project: create .aming-claw.yaml in the current directory."""
    target = os.path.join(os.getcwd(), ".aming-claw.yaml")
    if os.path.exists(target):
        click.echo(f".aming-claw.yaml already exists at {target}")
        return
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(_YAML_TEMPLATE)
    click.echo(f"Created {target}")


@main.command()
@click.option("--path", default=".", help="Workspace path to bootstrap")
@click.option("--name", default="", help="Project name")
def bootstrap(path, name):
    """Bootstrap an external project into aming-claw governance."""
    from agent.governance.project_service import bootstrap_project
    result = bootstrap_project(workspace_path=path, project_name=name)
    click.echo(f"Bootstrap result: {result}")


@main.command("scan")
@click.option("--path", default=".", help="External project path to scan")
@click.option("--project-id", default="", help="Governance project id")
@click.option("--session-id", default="", help="Optional deterministic scan session id")
def scan(path, project_id, session_id):
    """Scan an external project into a local .aming-claw candidate workspace."""
    from agent.governance.external_project_governance import scan_external_project

    result = scan_external_project(
        path,
        project_id=project_id or None,
        session_id=session_id or None,
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@main.command()
def status():
    """Show governance service status."""
    from agent.config import AmingConfig
    import requests as _requests
    cfg = AmingConfig.load()
    url = f"http://localhost:{cfg.governance_port}/api/health"
    try:
        resp = _requests.get(url, timeout=5)
        click.echo(resp.json())
    except Exception as exc:
        click.echo(f"Governance unreachable: {exc}", err=True)
        sys.exit(1)


def _dashboard_url(governance_url: str) -> str:
    return governance_url.rstrip("/") + "/dashboard"


def _probe_governance(port: int, *, timeout: float = 2.0) -> Optional[dict]:
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost probe
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _port_is_open(port: int, *, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_owner_hint(port: int) -> str:
    if sys.platform.startswith("win"):
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            return ""
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].endswith(f":{port}"):
                    return f" PID={parts[-1]}"
        return ""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return ""
    pid = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    return f" PID={pid}" if pid else ""


def _launcher_html(governance_url: str) -> str:
    dashboard_url = _dashboard_url(governance_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aming Claw Launcher</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #172033; }}
    main {{ max-width: 760px; }}
    a.button {{ display: inline-block; padding: 10px 14px; border: 1px solid #b6c7e6; border-radius: 6px; text-decoration: none; color: #0f3d7a; background: #f6f9ff; }}
    code {{ background: #f2f5fa; padding: 2px 5px; border-radius: 4px; }}
    pre {{ background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 6px; overflow: auto; }}
  </style>
</head>
<body>
  <main>
    <h1>Aming Claw Launcher</h1>
    <p>This local launcher never starts governance automatically. Start services explicitly, then open the dashboard.</p>
    <p><a class="button" href="{dashboard_url}">Open dashboard</a></p>
    <h2>Start locally</h2>
    <pre>aming-claw start</pre>
    <p>If the console script is not on PATH yet, use:</p>
    <pre>python -m agent.cli start</pre>
    <h2>Install/update plugin from Git</h2>
    <pre>aming-claw plugin install https://github.com/amingclawdev/aming-claw</pre>
    <h2>Check status</h2>
    <pre>aming-claw status</pre>
    <p>Codex and Claude Code should connect through the project <code>.mcp.json</code> after governance is available at <code>{governance_url}</code>.</p>
  </main>
</body>
</html>
"""


@main.command()
@click.option("--workspace", default=".", help="Workspace root used for shared-volume and project state.")
@click.option("--port", default=40000, type=int, help="Governance HTTP port.")
def start(workspace, port):
    """Start governance in the foreground without spawning plugin-owned workers."""
    health = _probe_governance(port)
    if health and health.get("status") == "ok" and health.get("service") == "governance":
        dashboard = _dashboard_url(f"http://localhost:{port}")
        version = health.get("version") or health.get("runtime_version") or "unknown"
        click.echo(f"Governance already running on port {port} (version {version}).")
        click.echo(f"Dashboard: {dashboard}")
        return
    if _port_is_open(port):
        owner = _port_owner_hint(port)
        raise click.ClickException(
            f"Port {port} is already in use{owner}, but /api/health is not Aming Claw governance. "
            "Stop that process or choose a different --port."
        )
    os.environ["GOVERNANCE_PORT"] = str(port)
    os.environ.setdefault("AMING_CLAW_HOME", str(Path(workspace).resolve()))
    import start_governance

    start_governance.main(workspace_root=Path(workspace).resolve())


@main.command("open")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
def open_dashboard(governance_url):
    """Open the dashboard in the default browser."""
    url = _dashboard_url(governance_url)
    webbrowser.open(url)
    click.echo(url)


@main.command()
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--output", default="", help="Output HTML path. Defaults to .aming-claw/aming-claw-launcher.html.")
@click.option("--open-browser", is_flag=True, help="Open the generated launcher in the default browser.")
def launcher(governance_url, output, open_browser):
    """Write a local launcher HTML artifact with dashboard links and start commands."""
    target = Path(output) if output else Path.cwd() / ".aming-claw" / "aming-claw-launcher.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_launcher_html(governance_url), encoding="utf-8")
    if open_browser:
        webbrowser.open(target.resolve().as_uri())
    click.echo(str(target))


@main.command("run-executor")
def run_executor():
    """Start the executor worker."""
    from agent.executor_worker import main as worker_main
    worker_main()


@main.group()
def plugin():
    """Install and validate local Aming Claw plugin assets."""
    pass


@plugin.command("install")
@click.argument("repo_url", required=False)
@click.option("--install-root", default="", help="User-local plugin cache root.")
@click.option("--ref", default="", help="Optional branch, tag, or commit to checkout.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable for pip/start commands.")
@click.option("--no-pip", is_flag=True, help="Clone and validate only; do not pip install.")
@click.option("--start", is_flag=True, help="Run the start command after install.")
@click.option("--dry-run", is_flag=True, help="Print planned commands without changing state.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
@click.option("--validate-only", is_flag=True, help="Validate the computed checkout path without cloning or fetching.")
def plugin_install(repo_url, install_root, ref, python_executable, no_pip, start, dry_run, json_output, validate_only):
    """Clone/update the plugin from a Git URL and print next steps."""
    from agent.plugin_installer import (
        DEFAULT_REPO_URL,
        PluginInstallError,
        format_result,
        install_from_git,
    )

    try:
        result = install_from_git(
            repo_url or DEFAULT_REPO_URL,
            install_root=install_root or None,
            ref=ref,
            python_executable=python_executable,
            install_package=not no_pip,
            start=start,
            dry_run=dry_run,
            validate_only=validate_only,
        )
    except PluginInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_result(result))


@plugin.command("doctor")
@click.option("--plugin-root", default="", help="Local Aming Claw plugin checkout root.")
@click.option("--governance-url", default="http://localhost:40000", help="Governance service URL.")
@click.option("--codex-config", default="", help="Optional Codex config.toml path.")
@click.option("--skip-governance", is_flag=True, help="Skip governance health probe.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def plugin_doctor(plugin_root, governance_url, codex_config, skip_governance, json_output):
    """Run read-only aftercare checks for a local plugin install."""
    from agent.plugin_installer import doctor_plugin, format_doctor_result

    result = doctor_plugin(
        plugin_root=plugin_root or None,
        governance_url=governance_url,
        codex_config=codex_config or None,
        check_governance=not skip_governance,
    )
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_doctor_result(result))
    if not result.ok:
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    main()
