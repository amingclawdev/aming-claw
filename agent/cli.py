"""CLI entry point for aming-claw.

Usage:
    aming-claw init            - create .aming-claw.yaml in current directory
    aming-claw bootstrap       - bootstrap an external project
    aming-claw status          - show governance status
    aming-claw plugin install  - install/update plugin assets from Git
    aming-claw plugin update   - check/apply plugin updates from Git
    aming-claw backlog export  - export portable backlog JSON
    aming-claw backlog import  - import portable backlog JSON
    aming-claw start           - start governance in the foreground
    aming-claw open            - open the dashboard URL
    aming-claw launcher        - write a local launcher HTML artifact
    aming-claw run-executor    - start executor worker
    aming-claw observer run    - build or execute route-bound observer invocation
    aming-claw observer poll   - claim observer command and plan route-bound work
    aming-claw observer dogfood - plan controlled dogfood observer/subagent run
    aming-claw mf precommit-check - run MF pre-commit guards
    aming-claw mf dispatch-gate - validate MF subagent dispatch evidence
"""

import os
import sys
import logging
import json
import time
import webbrowser
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

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


def _default_runtime_workspace() -> Path:
    """Return the plugin/runtime root used for local governance state."""
    return Path(__file__).resolve().parents[1]


def _probe_governance(port: int, *, timeout: float = 2.0) -> Optional[dict]:
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost probe
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _http_json(method: str, url: str, payload: dict | None = None, *, timeout: float = 30.0) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local governance URL by default
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return resp.status, parsed if isinstance(parsed, dict) else {"ok": False, "error": "non_object_response"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"ok": False, "error": "http_error", "message": body}
        if not isinstance(parsed, dict):
            parsed = {"ok": False, "error": "http_error", "message": body}
        return exc.code, parsed


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
@click.option(
    "--workspace",
    default="",
    help="Runtime workspace root for shared-volume/project state. Defaults to the plugin runtime root, not the current project.",
)
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
    runtime_workspace = Path(workspace).resolve() if workspace else _default_runtime_workspace()
    os.environ["AMING_CLAW_HOME"] = str(runtime_workspace)
    os.environ.setdefault("SHARED_VOLUME_PATH", str(runtime_workspace / "shared-volume"))
    import start_governance

    start_governance.main(workspace_root=runtime_workspace)


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
def backlog():
    """Export and import portable backlog data."""
    pass


@backlog.command("export")
@click.option("--project-id", default="aming-claw", help="Governance project id.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--output", default="", help="Output JSON path. Prints JSON to stdout when omitted.")
@click.option("--status", default="", help="Optional backlog status filter, e.g. OPEN or FIXED.")
@click.option("--priority", default="", help="Optional priority filter, e.g. P1.")
@click.option("--bug-id", "bug_ids", multiple=True, help="Optional bug id to export. Can be repeated.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON even when --output is used.")
def backlog_export(project_id, governance_url, output, status, priority, bug_ids, json_output):
    """Export backlog rows as portable JSON."""
    query = {
        key: value
        for key, value in {
            "status": status,
            "priority": priority,
            "bug_id": ",".join(bug_ids),
        }.items()
        if value
    }
    qs = f"?{urllib.parse.urlencode(query)}" if query else ""
    url = f"{governance_url.rstrip('/')}/api/backlog/{urllib.parse.quote(project_id, safe='')}/portable/export{qs}"
    code, payload = _http_json("GET", url)
    if code >= 400 or payload.get("ok") is False:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise click.exceptions.Exit(1)

    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if json_output or not output:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(f"Exported {payload.get('row_count', 0)} backlog row(s) to {output}")


@backlog.command("import")
@click.option("--project-id", default="aming-claw", help="Governance project id.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--input", "input_path", required=True, help="Input JSON path, or '-' for stdin.")
@click.option("--on-conflict", default="skip", type=click.Choice(["skip", "overwrite", "fail"]), help="How to handle existing bug ids.")
@click.option("--dry-run", is_flag=True, help="Validate and report planned changes without writing rows.")
@click.option("--actor", default="cli", help="Actor recorded in the import result.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def backlog_import_cmd(project_id, governance_url, input_path, on_conflict, dry_run, actor, json_output):
    """Import portable backlog JSON into a governance project."""
    try:
        raw = sys.stdin.read() if input_path == "-" else Path(input_path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Cannot read backlog import JSON: {exc}") from exc

    url = f"{governance_url.rstrip('/')}/api/backlog/{urllib.parse.quote(project_id, safe='')}/portable/import"
    body = {
        "payload": payload,
        "on_conflict": on_conflict,
        "dry_run": dry_run,
        "actor": actor,
    }
    code, result = _http_json("POST", url, body)
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            "Backlog import "
            f"{'dry-run ' if dry_run else ''}"
            f"inserted={result.get('inserted_count', 0)} "
            f"updated={result.get('updated_count', 0)} "
            f"skipped={result.get('skipped_count', 0)} "
            f"errors={result.get('error_count', 0)}"
        )
    if code >= 400 or not result.get("ok", False):
        raise click.exceptions.Exit(1)


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
@click.option("--no-codex-install", is_flag=True, help="Do not install Codex plugin cache/config.")
@click.option("--codex-home", default="", help="Override Codex home for plugin cache/config.")
@click.option("--codex-config", default="", help="Override Codex config.toml path.")
@click.option("--codex-marketplace-root", default="", help="Override generated Codex marketplace root.")
@click.option("--start", is_flag=True, help="Run the start command after install.")
@click.option("--dry-run", is_flag=True, help="Print planned commands without changing state.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
@click.option("--validate-only", is_flag=True, help="Validate the computed checkout path without cloning or fetching.")
def plugin_install(repo_url, install_root, ref, python_executable, no_pip, no_codex_install, codex_home, codex_config, codex_marketplace_root, start, dry_run, json_output, validate_only):
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
            install_codex_plugin=not no_codex_install,
            codex_home=codex_home or None,
            codex_config=codex_config or None,
            codex_marketplace_root=codex_marketplace_root or None,
            start=start,
            dry_run=dry_run,
            validate_only=validate_only,
            suppress_command_output=json_output,
        )
    except PluginInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_result(result))


@plugin.command("update")
@click.argument("repo_url", required=False)
@click.option("--check", "check_only", is_flag=True, help="Check for updates and refresh local state without applying.")
@click.option("--apply", "apply_update", is_flag=True, help="Apply a fast-forward update to the local plugin checkout.")
@click.option("--install-root", default="", help="User-local plugin cache root.")
@click.option("--ref", default="", help="Optional branch, tag, or commit to compare/apply.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable for pip/cache commands.")
@click.option("--no-pip", is_flag=True, help="Do not pip install after applying.")
@click.option("--no-codex-install", is_flag=True, help="Do not refresh Codex plugin cache/config after applying.")
@click.option("--codex-home", default="", help="Override Codex home for plugin cache checks.")
@click.option("--codex-config", default="", help="Override Codex config.toml path.")
@click.option("--codex-marketplace-root", default="", help="Override generated Codex marketplace root.")
@click.option("--plugin-state", default="", help="Optional plugin update state JSON path.")
@click.option("--dry-run", is_flag=True, help="Print planned update commands without changing state.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def plugin_update(repo_url, check_only, apply_update, install_root, ref, python_executable, no_pip, no_codex_install, codex_home, codex_config, codex_marketplace_root, plugin_state, dry_run, json_output):
    """Check or apply updates for a Git-backed local plugin checkout."""
    if check_only and apply_update:
        raise click.ClickException("Use either --check or --apply, not both.")
    from agent.plugin_installer import (
        DEFAULT_REPO_URL,
        format_plugin_update_result,
        update_plugin_from_git,
    )

    result = update_plugin_from_git(
        repo_url or DEFAULT_REPO_URL,
        install_root=install_root or None,
        ref=ref,
        apply_update=apply_update,
        python_executable=python_executable,
        install_package=not no_pip,
        install_codex_plugin=not no_codex_install,
        codex_home=codex_home or None,
        codex_config=codex_config or None,
        codex_marketplace_root=codex_marketplace_root or None,
        state_path=plugin_state or None,
        suppress_command_output=json_output,
        dry_run=dry_run,
    )
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_plugin_update_result(result))
    if not result.ok:
        raise click.exceptions.Exit(1)


@plugin.command("doctor")
@click.option("--plugin-root", default="", help="Local Aming Claw plugin checkout root.")
@click.option("--governance-url", default="http://localhost:40000", help="Governance service URL.")
@click.option("--codex-config", default="", help="Optional Codex config.toml path.")
@click.option("--codex-home", default="", help="Optional Codex home for plugin cache checks.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable to validate for local runtime.")
@click.option("--skip-governance", is_flag=True, help="Skip governance health probe.")
@click.option("--check-service-manager", is_flag=True, help="Also check advanced chain/executor ServiceManager health.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def plugin_doctor(plugin_root, governance_url, codex_config, codex_home, python_executable, skip_governance, check_service_manager, json_output):
    """Run read-only aftercare checks for a local plugin install."""
    from agent.plugin_installer import doctor_plugin, format_doctor_result

    result = doctor_plugin(
        plugin_root=plugin_root or None,
        governance_url=governance_url,
        codex_config=codex_config or None,
        codex_home=codex_home or None,
        python_executable=python_executable,
        check_governance=not skip_governance,
        check_service_manager=check_service_manager,
    )
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_doctor_result(result))
    if not result.ok:
        raise click.exceptions.Exit(1)


@main.group()
def observer():
    """Observer runtime launcher."""
    pass


def _observer_poll_session_registration_payload(
    *,
    observer_kind: str,
    session_label: str,
    cwd: str,
) -> dict:
    return {
        "observer_kind": observer_kind or "codex",
        "session_label": session_label,
        "pid": os.getpid(),
        "cwd": cwd,
        "capabilities": {
            "actions": [
                "observer_session_heartbeat",
                "observer_session_close",
                "observer_command_claim",
                "observer_command_complete",
                "observer_command_fail",
            ],
            "command_types": ["execute_backlog_row"],
        },
    }


def _observer_poll_public_session(
    payload: dict,
    *,
    print_session_token: bool,
) -> dict:
    if not isinstance(payload, dict):
        return {}
    public = dict(payload)
    if not print_session_token:
        public.pop("session_token", None)
    return public


def _observer_poll_completion_result(plan: dict) -> dict:
    route_identity = plan.get("route_identity") if isinstance(plan.get("route_identity"), dict) else {}
    return {
        "ok": bool(plan.get("ok")),
        "status": str(plan.get("status") or ""),
        "schema_version": str(plan.get("schema_version") or ""),
        "observer_command_id": str(plan.get("observer_command_id") or ""),
        "backlog_id": str(plan.get("backlog_id") or ""),
        "route_id": str(route_identity.get("route_id") or ""),
        "route_context_hash": str(route_identity.get("route_context_hash") or ""),
        "prompt_contract_id": str(route_identity.get("prompt_contract_id") or ""),
        "visible_injection_manifest_hash": str(
            route_identity.get("visible_injection_manifest_hash") or ""
        ),
        "calls_models": bool(plan.get("calls_models")),
        "execute": bool(plan.get("execute")),
        "service_manager_required": False,
        "executor_worker_required": False,
        "uses_task_create": False,
        "payload_free_reminder": True,
        "reminder_payload_required": False,
    }


def _observer_poll_append_timeline(
    *,
    base_url: str,
    project_id: str,
    observer_command_id: str,
    event_type: str,
    phase: str,
    status: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not observer_command_id:
        return {
            "ok": False,
            "skipped": True,
            "event_type": event_type,
            "phase": phase,
            "error": "missing_observer_command_id",
        }
    encoded_project = urllib.parse.quote(project_id, safe="")
    body = {
        "task_id": observer_command_id,
        "backlog_id": str(payload.get("backlog_id") or ""),
        "event_type": event_type,
        "phase": phase,
        "event_kind": "observer_poll",
        "status": status,
        "actor": "observer_poll_cli",
        "payload": payload,
    }
    try:
        code, response = _http_json(
            "POST",
            f"{base_url}/api/task/{encoded_project}/timeline",
            body,
        )
    except Exception as exc:  # pragma: no cover - defensive fail-soft CLI guard
        return {
            "ok": False,
            "event_type": event_type,
            "phase": phase,
            "http_status": 0,
            "error": str(exc),
        }
    ok = code < 400 and response.get("ok", True) is not False
    result = {
        "ok": ok,
        "event_type": event_type,
        "phase": phase,
        "http_status": code,
    }
    if not ok:
        result["response"] = response
    return result


def _observer_poll_heartbeat(
    *,
    base_url: str,
    project_id: str,
    session_id: str,
    session_token: str,
) -> dict[str, Any]:
    encoded_project = urllib.parse.quote(project_id, safe="")
    encoded_session = urllib.parse.quote(session_id, safe="")
    try:
        code, response = _http_json(
            "POST",
            (
                f"{base_url}/api/projects/{encoded_project}/observer-sessions/"
                f"{encoded_session}/heartbeat"
            ),
            {"session_id": session_id, "session_token": session_token},
        )
    except Exception as exc:  # pragma: no cover - defensive fail-soft CLI guard
        return {"ok": False, "http_status": 0, "error": str(exc)}
    return {
        "ok": code < 400 and response.get("ok", True) is not False,
        "http_status": code,
        "observer_session_id": str(
            response.get("observer_session_id") or response.get("session_id") or session_id
        ),
        "heartbeat_interval_sec": response.get("heartbeat_interval_sec"),
        "response": response if code >= 400 or response.get("ok", True) is False else {},
    }


def _observer_poll_normalize_claim_response(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "error": "non_object_claim_response"}
    if isinstance(payload.get("command"), dict) or payload.get("empty") is True:
        return payload
    if payload.get("command_id") and payload.get("command_type"):
        return {
            "ok": True,
            "project_id": str(payload.get("project_id") or ""),
            "observer_session_id": str(payload.get("claimed_by_session_id") or ""),
            "command": payload,
            "empty": False,
            "normalized_from": "raw_command",
        }
    return payload


@observer.command("poll")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service URL.")
@click.option("--session-id", default="", help="Existing observer session id. Omit to register one.")
@click.option(
    "--session-token",
    default="",
    envvar="AMING_CLAW_OBSERVER_SESSION_TOKEN",
    help="Existing observer session token. Can also use AMING_CLAW_OBSERVER_SESSION_TOKEN.",
)
@click.option(
    "--command-id",
    default="",
    help="Specific observer command id to claim. Defaults to next command.",
)
@click.option("--observer-kind", default="codex", help="Observer kind used when registering a session.")
@click.option("--session-label", default="", help="Observer session label used when registering a session.")
@click.option(
    "--print-session-token",
    is_flag=True,
    help="Include a newly registered session token in JSON output.",
)
@click.option("--provider", default="openai", help="Provider name, e.g. openai or anthropic.")
@click.option("--model", default="", help="Optional provider model override.")
@click.option(
    "--backend-mode",
    default="codex_cli",
    help="Invocation backend, e.g. codex_cli, claude_cli, openai_api, anthropic_api.",
)
@click.option("--workspace", default="", help="Observer workspace. Defaults to current working directory.")
@click.option(
    "--prompt-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Optional observer prompt file.",
)
@click.option(
    "--dispatch-gate-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="MF subagent dispatch gate evidence JSON required for live code-mutating backends.",
)
@click.option(
    "--main-worktree",
    default="",
    help="Target/main worktree path blocked by one-hop dispatch policy.",
)
@click.option(
    "--timeout-sec",
    default=120,
    type=int,
    help="Observer invocation timeout if --execute is used.",
)
@click.option(
    "--execute",
    is_flag=True,
    help="Actually invoke the configured provider after one-hop gate validation.",
)
@click.option(
    "--watch/--once",
    default=False,
    help="Keep polling until --max-commands or --idle-timeout-sec is reached. Defaults to --once.",
)
@click.option(
    "--max-commands",
    default=0,
    type=int,
    help="Maximum commands to process in --watch mode. Use 0 for no command limit.",
)
@click.option(
    "--idle-timeout-sec",
    default=None,
    type=float,
    help="Exit --watch after this many idle seconds. Defaults to 60; use 0 to exit on first empty poll.",
)
@click.option(
    "--poll-interval-sec",
    default=5.0,
    type=float,
    help="Seconds between empty --watch polls.",
)
@click.option(
    "--complete-planned",
    is_flag=True,
    help="Complete the claimed command with the poll/plan result.",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_poll(
    project_id,
    governance_url,
    session_id,
    session_token,
    command_id,
    observer_kind,
    session_label,
    print_session_token,
    provider,
    model,
    backend_mode,
    workspace,
    prompt_file,
    dispatch_gate_file,
    main_worktree,
    timeout_sec,
    execute,
    watch,
    max_commands,
    idle_timeout_sec,
    poll_interval_sec,
    complete_planned,
    json_output,
):
    """Claim an observer command and build a standalone route-bound plan."""
    from agent.observer_runtime import (
        ObserverPollLoopConfig,
        ObserverPollRequest,
        build_observer_poll_loop_metadata,
        build_observer_poll_plan,
        observer_poll_timeline_payload,
    )

    base_url = governance_url.rstrip("/")
    encoded_project = urllib.parse.quote(project_id, safe="")
    cwd = workspace or os.getcwd()
    effective_idle_timeout_sec = 60.0 if idle_timeout_sec is None and watch else (idle_timeout_sec or 0.0)
    loop = build_observer_poll_loop_metadata(
        ObserverPollLoopConfig(
            watch=bool(watch),
            max_commands=max_commands,
            idle_timeout_sec=effective_idle_timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
    )
    result: dict = {
        "ok": False,
        "schema_version": "observer_poll_cli.v1",
        "project_id": project_id,
        "governance_url": base_url,
        "execute": execute,
        "watch": bool(watch),
        "complete_planned": complete_planned,
        "service_manager_required": False,
        "executor_worker_required": False,
        "uses_task_create": False,
        "payload_free_reminder": True,
        "reminder_payload_required": False,
        "loop": loop,
        "heartbeats": [],
        "observer_polls": [],
        "completions": [],
        "timeline": [],
    }
    active_session_id = session_id
    active_session_token = session_token
    registered_session: dict = {}

    if watch and command_id:
        result.update(
            {
                "status": "rejected",
                "error": "command-id cannot be combined with --watch",
            }
        )
        click.echo(json.dumps(result, indent=2, sort_keys=True) if json_output else result["error"])
        raise click.exceptions.Exit(1)

    if bool(active_session_id) != bool(active_session_token):
        result.update(
            {
                "status": "rejected",
                "error": "session-id and session-token must be supplied together",
            }
        )
        click.echo(json.dumps(result, indent=2, sort_keys=True) if json_output else result["error"])
        raise click.exceptions.Exit(1)

    if not active_session_id:
        register_payload = _observer_poll_session_registration_payload(
            observer_kind=observer_kind,
            session_label=session_label,
            cwd=cwd,
        )
        code, registered = _http_json(
            "POST",
            f"{base_url}/api/projects/{encoded_project}/observer-sessions/register",
            register_payload,
        )
        registered_session = _observer_poll_public_session(
            registered,
            print_session_token=print_session_token,
        )
        result["registered_session"] = registered_session
        if code >= 400 or not registered.get("ok"):
            result.update(
                {
                    "status": "rejected",
                    "error": "observer session registration failed",
                    "http_status": code,
                    "response": registered_session,
                }
            )
            click.echo(json.dumps(result, indent=2, sort_keys=True) if json_output else result["error"])
            raise click.exceptions.Exit(1)
        active_session_id = str(registered.get("observer_session_id") or registered.get("session_id") or "")
        active_session_token = str(registered.get("session_token") or "")

    result["observer_session_id"] = active_session_id
    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    dispatch_gate = {}
    if dispatch_gate_file:
        try:
            parsed_gate = json.loads(Path(dispatch_gate_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid dispatch gate file: {exc}") from exc
        if not isinstance(parsed_gate, dict):
            raise click.ClickException("dispatch gate file must contain a JSON object")
        dispatch_gate = parsed_gate

    last_activity = time.monotonic()
    next_command_id = command_id
    stop_reason = ""
    while True:
        heartbeat = _observer_poll_heartbeat(
            base_url=base_url,
            project_id=project_id,
            session_id=active_session_id,
            session_token=active_session_token,
        )
        result["heartbeats"].append(heartbeat)
        loop["heartbeat_count"] = len(result["heartbeats"])
        if not heartbeat.get("ok"):
            result.update(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "observer session heartbeat failed",
                    "heartbeat": heartbeat,
                }
            )
            break

        claim_payload = {
            "session_id": active_session_id,
            "session_token": active_session_token,
        }
        claim_endpoint = "claim" if next_command_id else "next"
        if next_command_id:
            claim_payload["command_id"] = next_command_id
        next_command_id = ""
        loop["claim_attempts"] += 1
        claim_code, raw_claim_response = _http_json(
            "POST",
            f"{base_url}/api/projects/{encoded_project}/observer-commands/{claim_endpoint}",
            claim_payload,
        )
        claim_response = _observer_poll_normalize_claim_response(raw_claim_response)
        if claim_code >= 400 or not claim_response.get("ok"):
            result.update(
                {
                    "ok": False,
                    "status": "rejected",
                    "error": "observer command claim failed",
                    "http_status": claim_code,
                    "response": raw_claim_response,
                }
            )
            stop_reason = "claim_failed"
            break

        command = (
            claim_response.get("command")
            if isinstance(claim_response.get("command"), dict)
            else None
        )
        if command:
            observer_command_id = str(command.get("command_id") or "")
            result["timeline"].append(
                _observer_poll_append_timeline(
                    base_url=base_url,
                    project_id=project_id,
                    observer_command_id=observer_command_id,
                    event_type="observer_poll_claimed",
                    phase="claim",
                    status="claimed",
                    payload=observer_poll_timeline_payload(
                        observer_command_id=observer_command_id,
                        command=command,
                        event="claim",
                    ),
                )
            )

        plan = build_observer_poll_plan(
            ObserverPollRequest(
                project_id=project_id,
                observer_session_id=active_session_id,
                command=command,
                provider=provider,
                model=model,
                backend_mode=backend_mode,
                workspace=cwd,
                prompt=prompt,
                timeout_sec=timeout_sec,
                dispatch_gate=dispatch_gate,
                main_worktree=main_worktree or cwd,
            ),
            execute=execute,
        )
        result["observer_polls"].append(plan)
        result.update(
            {
                "ok": bool(plan.get("ok")),
                "status": plan.get("status") or "planned",
                "empty": bool(plan.get("empty")),
                "claim": {
                    "http_status": claim_code,
                    "empty": bool(claim_response.get("empty")),
                    "observer_command_id": str((command or {}).get("command_id") or ""),
                },
                "observer_poll": plan,
            }
        )
        if command:
            observer_command_id = str(command.get("command_id") or "")
            result["timeline"].append(
                _observer_poll_append_timeline(
                    base_url=base_url,
                    project_id=project_id,
                    observer_command_id=observer_command_id,
                    event_type="observer_poll_planned",
                    phase="plan",
                    status=str(plan.get("status") or "planned"),
                    payload=observer_poll_timeline_payload(
                        observer_command_id=observer_command_id,
                        command=command,
                        plan=plan,
                        event="plan",
                    ),
                )
            )

        if not plan.get("ok"):
            stop_reason = "plan_rejected"
            break

        if command:
            loop["processed_count"] += 1
            last_activity = time.monotonic()
            if complete_planned:
                completion_result = _observer_poll_completion_result(plan)
                complete_payload = {
                    "session_id": active_session_id,
                    "session_token": active_session_token,
                    "result": completion_result,
                }
                complete_code, complete_response = _http_json(
                    "POST",
                    (
                        f"{base_url}/api/projects/{encoded_project}/observer-commands/"
                        f"{urllib.parse.quote(str(command.get('command_id') or ''), safe='')}/complete"
                    ),
                    complete_payload,
                )
                completion = {
                    "http_status": complete_code,
                    "ok": bool(complete_response.get("ok")),
                    "observer_command_id": str(
                        (complete_response.get("command") or {}).get("command_id") or ""
                    ),
                }
                result["completion"] = completion
                result["completions"].append(completion)
                if complete_code >= 400 or not complete_response.get("ok"):
                    result["ok"] = False
                    result["status"] = "rejected"
                    result["error"] = "observer command completion failed"
                    completion["response"] = complete_response
                    stop_reason = "completion_failed"
                observer_command_id = str(command.get("command_id") or "")
                result["timeline"].append(
                    _observer_poll_append_timeline(
                        base_url=base_url,
                        project_id=project_id,
                        observer_command_id=observer_command_id,
                        event_type="observer_poll_completed",
                        phase="complete",
                        status=(
                            "completed"
                            if complete_code < 400 and complete_response.get("ok")
                            else "completion_failed"
                        ),
                        payload=observer_poll_timeline_payload(
                            observer_command_id=observer_command_id,
                            command=command,
                            plan=plan,
                            result=completion_result,
                            event="complete",
                        ),
                    )
                )
                if stop_reason:
                    break
            elif watch:
                stop_reason = "claimed_command_left_open"
                break

            if not watch:
                stop_reason = "once"
                break
            if loop["effective_max_commands"] and loop["processed_count"] >= loop["effective_max_commands"]:
                stop_reason = "max_commands"
                break
            continue

        loop["empty_polls"] += 1
        if not watch:
            stop_reason = "empty"
            break
        idle_elapsed_sec = max(0.0, time.monotonic() - last_activity)
        loop["idle_elapsed_sec"] = idle_elapsed_sec
        if loop["idle_timeout_sec"] <= 0 or idle_elapsed_sec >= loop["idle_timeout_sec"]:
            stop_reason = "idle_timeout"
            break
        sleep_for = min(loop["poll_interval_sec"], loop["idle_timeout_sec"] - idle_elapsed_sec)
        if sleep_for > 0:
            time.sleep(sleep_for)

    loop["stop_reason"] = stop_reason or result.get("status") or ""

    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            f"observer poll: {result.get('status')} project={project_id} "
            f"session={active_session_id}"
        )
        poll = result.get("observer_poll") or {}
        if poll.get("observer_command_id"):
            click.echo(f"command: {poll.get('observer_command_id')} backlog={poll.get('backlog_id')}")
        click.echo(f"execute={execute} calls_models={poll.get('calls_models', False)}")
        if not result.get("ok"):
            click.echo(
                f"error: {result.get('error') or poll.get('error') or 'observer poll rejected'}",
                err=True,
            )
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@observer.command("run")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--backlog-id", required=True, help="Backlog id the observer will supervise.")
@click.option("--route-context-hash", required=True, help="Route context hash for this observer run.")
@click.option("--prompt-contract-id", required=True, help="Prompt contract id for this observer run.")
@click.option("--prompt-contract-hash", default="", help="Optional prompt contract hash.")
@click.option("--route-token-ref", default="", help="Optional route token id/ref.")
@click.option("--provider", default="openai", help="Provider name, e.g. openai or anthropic.")
@click.option("--model", default="", help="Optional provider model override.")
@click.option("--backend-mode", default="codex_cli", help="Invocation backend, e.g. codex_cli, claude_cli, openai_api, anthropic_api.")
@click.option("--workspace", default="", help="Observer workspace. Defaults to current working directory.")
@click.option("--prompt-file", default=None, type=click.Path(exists=True, dir_okay=False, readable=True), help="Optional observer prompt file.")
@click.option(
    "--dispatch-gate-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="MF subagent dispatch gate evidence JSON required for live code-mutating backends.",
)
@click.option("--main-worktree", default="", help="Target/main worktree path blocked by one-hop dispatch policy.")
@click.option("--execute", is_flag=True, help="Actually invoke the configured provider. Default is dry-run evidence only.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_run(
    project_id,
    backlog_id,
    route_context_hash,
    prompt_contract_id,
    prompt_contract_hash,
    route_token_ref,
    provider,
    model,
    backend_mode,
    workspace,
    prompt_file,
    dispatch_gate_file,
    main_worktree,
    execute,
    json_output,
):
    """Build or execute a route-bound observer invocation."""
    from agent.observer_runtime import ObserverRunRequest, run_observer
    from agent.ai_invocation import RoutePromptContract

    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    dispatch_gate = {}
    if dispatch_gate_file:
        try:
            parsed_gate = json.loads(Path(dispatch_gate_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid dispatch gate file: {exc}") from exc
        if not isinstance(parsed_gate, dict):
            raise click.ClickException("dispatch gate file must contain a JSON object")
        dispatch_gate = parsed_gate
    request = ObserverRunRequest(
        project_id=project_id,
        backlog_id=backlog_id,
        route=RoutePromptContract(
            route_context_hash=route_context_hash,
            prompt_contract_id=prompt_contract_id,
            prompt_contract_hash=prompt_contract_hash,
            route_token_ref=route_token_ref,
        ),
        provider=provider,
        model=model,
        backend_mode=backend_mode,
        workspace=workspace or os.getcwd(),
        prompt=prompt,
        dispatch_gate=dispatch_gate,
        main_worktree=main_worktree or os.getcwd(),
    )
    result = run_observer(request, execute=execute)
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(f"observer run: {result.get('status')} project={project_id} backlog={backlog_id}")
        invocation = result.get("invocation") or result.get("invocation_request") or {}
        click.echo(f"backend: {invocation.get('backend_mode', backend_mode)} execute={execute}")
        click.echo(f"route: {route_context_hash}")
        if not result.get("ok"):
            click.echo("missing: " + ", ".join(result.get("missing") or []), err=True)
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@observer.command("dogfood")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--backlog-id", required=True, help="Backlog id the observer will supervise.")
@click.option("--route-context-hash", required=True, help="Route context hash for this observer run.")
@click.option("--prompt-contract-id", required=True, help="Prompt contract id for this observer run.")
@click.option("--prompt-contract-hash", default="", help="Optional prompt contract hash.")
@click.option("--route-token-ref", default="", help="Optional route token id/ref.")
@click.option("--route-id", default="", help="Route id for route-owned dogfood evidence.")
@click.option("--precheck-run-id", default="", help="Optional judgment topology precheck id for evidence.")
@click.option("--visible-injection-manifest-hash", default="", help="Visible injection manifest hash for route-owned dogfood evidence.")
@click.option("--provider", default="openai", help="Provider name, e.g. openai or anthropic.")
@click.option("--model", default="", help="Optional provider model override.")
@click.option("--backend-mode", default="codex_cli", help="Invocation backend, e.g. codex_cli, claude_cli, openai_api, anthropic_api.")
@click.option("--main-worktree", default="", help="Target/main worktree path blocked by dispatch policy. Defaults to cwd.")
@click.option("--workspace-root", default="", help="Parent workspace root for generated worker worktrees. Defaults to main worktree parent.")
@click.option("--owned-file", "owned_files", multiple=True, required=True, help="Owned file fence for the worker. Repeatable.")
@click.option("--task-id", default="", help="Worker task id. Defaults to backlog id.")
@click.option("--worker-id", default="", help="Worker id used in deterministic worktree planning.")
@click.option("--attempt", default=1, type=int, help="Worker attempt number.")
@click.option("--worktree-root", default=".worktrees", help="Worktree root under workspace-root.")
@click.option("--branch-prefix", default="dogfood", help="Generated branch prefix.")
@click.option("--merge-queue-id", default="", help="Merge queue id. Defaults to a deterministic dogfood id.")
@click.option("--fence-token", default="", help="Fence token. Defaults to a deterministic dogfood token.")
@click.option(
    "--branch-runtime-registration-ref",
    default="",
    help="Allocation source/API/CLI reference; not the worker runtime_context_id.",
)
@click.option(
    "--runtime-context-id",
    default="",
    help="Worker runtime_context_id returned by branch allocation.",
)
@click.option(
    "--branch-runtime-evidence-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Optional JSON allocation evidence object, including source_ref, "
        "runtime_context_id, and persisted branch context."
    ),
)
@click.option("--graph-trace-id", "graph_trace_ids", multiple=True, required=True, help="Graph query trace id proving graph-first evidence. Repeatable.")
@click.option("--base-commit", default="", help="Optional base commit. Defaults to main worktree HEAD.")
@click.option("--target-head-commit", default="", help="Optional target HEAD commit. Defaults to base commit.")
@click.option("--timeout-sec", default=120, type=int, help="Observer invocation timeout if --execute is used.")
@click.option("--gate-output", "--gate-output-path", "gate_output", default="", type=click.Path(dir_okay=False), help="Optional path to write generated dispatch gate JSON.")
@click.option("--materialize-worktree", is_flag=True, help="Create the gated worker worktree before planning/execution.")
@click.option("--execute", is_flag=True, help="Invoke the configured provider after gate and worktree preflight. Default is dry-run evidence only.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_dogfood(
    project_id,
    backlog_id,
    route_context_hash,
    prompt_contract_id,
    prompt_contract_hash,
    route_token_ref,
    route_id,
    precheck_run_id,
    visible_injection_manifest_hash,
    provider,
    model,
    backend_mode,
    main_worktree,
    workspace_root,
    owned_files,
    task_id,
    worker_id,
    attempt,
    worktree_root,
    branch_prefix,
    merge_queue_id,
    fence_token,
    branch_runtime_registration_ref,
    runtime_context_id,
    branch_runtime_evidence_file,
    graph_trace_ids,
    base_commit,
    target_head_commit,
    timeout_sec,
    gate_output,
    materialize_worktree,
    execute,
    json_output,
):
    """Plan or execute a controlled source-backed dogfood observer run."""
    from agent.ai_invocation import RoutePromptContract
    from agent.observer_runtime import (
        DogfoodObserverPlanRequest,
        build_dogfood_observer_run_plan,
    )

    branch_runtime_evidence: dict[str, Any] = {}
    if branch_runtime_evidence_file:
        try:
            parsed_evidence = json.loads(Path(branch_runtime_evidence_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid branch runtime evidence file: {exc}") from exc
        if not isinstance(parsed_evidence, dict):
            raise click.ClickException("branch runtime evidence file must contain a JSON object")
        branch_runtime_evidence = parsed_evidence
    request = DogfoodObserverPlanRequest(
        project_id=project_id,
        backlog_id=backlog_id,
        route=RoutePromptContract(
            route_context_hash=route_context_hash,
            prompt_contract_id=prompt_contract_id,
            prompt_contract_hash=prompt_contract_hash,
            route_token_ref=route_token_ref,
        ),
        provider=provider,
        model=model,
        backend_mode=backend_mode,
        main_worktree=main_worktree or os.getcwd(),
        workspace_root=workspace_root,
        owned_files=tuple(owned_files),
        task_id=task_id,
        worker_id=worker_id,
        attempt=attempt,
        worktree_root=worktree_root,
        branch_prefix=branch_prefix,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
        graph_trace_ids=tuple(graph_trace_ids),
        branch_runtime_registration_ref=branch_runtime_registration_ref,
        branch_runtime_evidence=branch_runtime_evidence,
        runtime_context_id=runtime_context_id,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        timeout_sec=timeout_sec,
        route_id=route_id,
        precheck_run_id=precheck_run_id,
        visible_injection_manifest_hash=visible_injection_manifest_hash,
    )
    result = build_dogfood_observer_run_plan(
        request,
        execute=execute,
        materialize_worktree=materialize_worktree,
    )
    if gate_output:
        route_allowed = bool((result.get("route_identity_validation") or {}).get("allowed", True))
        gate_allowed = bool((result.get("dispatch_gate_validation") or {}).get("allowed", False))
        if route_allowed and gate_allowed:
            gate_path = Path(gate_output)
            gate_path.parent.mkdir(parents=True, exist_ok=True)
            gate_path.write_text(
                json.dumps(result.get("dispatch_gate") or {}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            result["gate_output"] = str(gate_path)
        else:
            result["gate_output_skipped"] = {
                "path": gate_output,
                "reason": "route_identity_or_dispatch_gate_validation_failed",
                "route_identity_allowed": route_allowed,
                "dispatch_gate_allowed": gate_allowed,
            }
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(f"observer dogfood: {result.get('status')} project={project_id} backlog={backlog_id}")
        click.echo(f"execute={execute} calls_models={result.get('calls_models')}")
        runtime_context = result.get("runtime_context") or {}
        if runtime_context.get("worktree_path"):
            click.echo(f"worktree: {runtime_context.get('worktree_path')}")
        if gate_output:
            click.echo(f"gate: {gate_output}")
        if not result.get("ok"):
            validation = (
                result.get("route_identity_validation")
                or result.get("dispatch_gate_validation")
                or result.get("materialization_preflight")
                or result.get("execute_preflight")
                or {}
            )
            click.echo(f"error: {validation.get('error', 'observer dogfood rejected')}", err=True)
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@observer.group("runtime-text")
def observer_runtime_text():
    """Observer runtime text preparation."""
    pass


@observer_runtime_text.command("prepare")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--backlog-id", required=True, help="Backlog id for the bounded worker.")
@click.option("--route-context-hash", required=True, help="Route context hash for this worker launch.")
@click.option("--prompt-contract-id", required=True, help="Prompt contract id for this worker launch.")
@click.option("--prompt-contract-hash", default="", help="Optional prompt contract hash.")
@click.option("--route-token-ref", default="", help="Optional route token id/ref.")
@click.option("--route-id", default="", help="Parent route id for route-owned evidence.")
@click.option("--precheck-run-id", default="", help="Optional route/topology precheck id.")
@click.option("--visible-injection-manifest-hash", default="", help="Public-safe visible injection manifest hash.")
@click.option("--main-worktree", default="", help="Target/main worktree path blocked by dispatch policy. Defaults to cwd.")
@click.option("--workspace-root", default="", help="Parent workspace root for generated worker worktrees. Defaults to main worktree parent.")
@click.option("--owned-file", "owned_files", multiple=True, help="Owned file fence for the worker. Repeatable.")
@click.option("--task-id", default="", help="Worker task id. Defaults to backlog id.")
@click.option("--parent-task-id", default="", help="Parent observer/MF task id. Defaults to backlog id.")
@click.option("--worker-id", default="", help="Worker id used in deterministic worktree planning.")
@click.option("--attempt", default=1, type=int, help="Worker attempt number.")
@click.option("--worktree-root", default=".worktrees", help="Worktree root under workspace-root.")
@click.option("--branch-prefix", default="runtime-text", help="Generated branch prefix.")
@click.option("--merge-queue-id", default="", help="Merge queue id. Defaults to a deterministic runtime-text id.")
@click.option("--fence-token", default="", help="Fence token. Defaults to a deterministic runtime-text token.")
@click.option(
    "--branch-runtime-registration-ref",
    default="",
    help="Allocation source/API/CLI reference; not the worker runtime_context_id.",
)
@click.option(
    "--runtime-context-id",
    default="",
    help="Worker contract runtime_context_id from persisted allocation evidence.",
)
@click.option(
    "--branch-runtime-evidence-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Optional JSON allocation evidence object, including source_ref, "
        "runtime_context_id, and persisted branch context."
    ),
)
@click.option("--graph-trace-id", "graph_trace_ids", multiple=True, help="Graph query trace id proving graph-first evidence. Repeatable.")
@click.option("--base-commit", default="", help="Optional base commit. Defaults to main worktree HEAD.")
@click.option("--target-head-commit", default="", help="Optional target HEAD commit. Defaults to base commit.")
@click.option("--acceptance-criterion", "acceptance_criteria", multiple=True, help="Acceptance criterion for the worker contract. Repeatable.")
@click.option("--test-command", "test_commands", multiple=True, help="Focused test command for the worker contract. Repeatable.")
@click.option(
    "--prompt-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Optional worker prompt file.",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_runtime_text_prepare(
    project_id,
    backlog_id,
    route_context_hash,
    prompt_contract_id,
    prompt_contract_hash,
    route_token_ref,
    route_id,
    precheck_run_id,
    visible_injection_manifest_hash,
    main_worktree,
    workspace_root,
    owned_files,
    task_id,
    parent_task_id,
    worker_id,
    attempt,
    worktree_root,
    branch_prefix,
    merge_queue_id,
    fence_token,
    branch_runtime_registration_ref,
    runtime_context_id,
    branch_runtime_evidence_file,
    graph_trace_ids,
    base_commit,
    target_head_commit,
    acceptance_criteria,
    test_commands,
    prompt_file,
    json_output,
):
    """Prepare runtime launch text for a host-created mf_sub worker."""
    from agent.ai_invocation import RoutePromptContract
    from agent.observer_runtime import (
        ObserverRuntimeTextPrepareRequest,
        build_observer_runtime_text_context,
    )

    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    branch_runtime_evidence: dict[str, Any] = {}
    if branch_runtime_evidence_file:
        try:
            parsed_evidence = json.loads(Path(branch_runtime_evidence_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid branch runtime evidence file: {exc}") from exc
        if not isinstance(parsed_evidence, dict):
            raise click.ClickException("branch runtime evidence file must contain a JSON object")
        branch_runtime_evidence = parsed_evidence
    request = ObserverRuntimeTextPrepareRequest(
        project_id=project_id,
        backlog_id=backlog_id,
        route=RoutePromptContract(
            route_context_hash=route_context_hash,
            prompt_contract_id=prompt_contract_id,
            prompt_contract_hash=prompt_contract_hash,
            route_token_ref=route_token_ref,
        ),
        main_worktree=main_worktree or os.getcwd(),
        workspace_root=workspace_root,
        owned_files=tuple(owned_files),
        task_id=task_id,
        parent_task_id=parent_task_id,
        worker_id=worker_id,
        attempt=attempt,
        worktree_root=worktree_root,
        branch_prefix=branch_prefix,
        merge_queue_id=merge_queue_id,
        fence_token=fence_token,
        graph_trace_ids=tuple(graph_trace_ids),
        branch_runtime_registration_ref=branch_runtime_registration_ref,
        branch_runtime_evidence=branch_runtime_evidence,
        runtime_context_id=runtime_context_id,
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        prompt=prompt,
        acceptance_criteria=tuple(acceptance_criteria),
        test_commands=tuple(test_commands),
        route_id=route_id,
        precheck_run_id=precheck_run_id,
        visible_injection_manifest_hash=visible_injection_manifest_hash,
    )
    result = build_observer_runtime_text_context(request)
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            f"observer runtime-text prepare: {result.get('status')} "
            f"project={project_id} backlog={backlog_id}"
        )
        click.echo(f"runtime_context_id: {result.get('runtime_context_id')}")
        click.echo(f"launch_text_hash: {result.get('launch_text_hash')}")
        if not result.get("ok"):
            validation = result.get("dispatch_gate_validation") or {}
            click.echo(
                f"error: {result.get('input_error') or validation.get('error') or 'runtime text rejected'}",
                err=True,
            )
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@main.group()
def mf():
    """Manual-fix workflow checks."""
    pass


@mf.command("precommit-check")
@click.option("--plugin-state", default="", help="Optional plugin update state JSON path.")
@click.option(
    "--route-consumption-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Optional route-context consumption evidence JSON path.",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def mf_precommit_check(plugin_state, route_consumption_file, json_output):
    """Run local MF pre-commit guards that do not mutate governance state."""
    from agent.plugin_installer import (
        format_plugin_update_state_status,
        plugin_update_state_status,
    )

    plugin_status = plugin_update_state_status(state_path=plugin_state or None)
    route_status = _mf_route_consumption_file_status(route_consumption_file)
    result = {
        "ok": bool(plugin_status.get("ok")) and bool(route_status.get("ok")),
        "checks": {
            "plugin_update_state": plugin_status,
            "route_context_consumption": route_status,
        },
    }
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo("Aming Claw MF precommit check")
        click.echo("")
        click.echo(format_plugin_update_state_status(plugin_status))
        if route_consumption_file:
            status = "pass" if route_status.get("ok") else "fail"
            click.echo(f"route context consumption: {status}")
            missing = route_status.get("missing_requirement_ids") or []
            if missing:
                click.echo(f"missing: {', '.join(missing)}")
    if not result["ok"]:
        raise click.exceptions.Exit(1)


def _mf_route_consumption_file_status(path: str) -> dict:
    if not path:
        return {"status": "skipped", "ok": True}
    from agent.governance.task_timeline import mf_route_context_gate_verification

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "fail", "ok": False, "error": f"invalid route consumption file: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "fail", "ok": False, "error": "route consumption file must be a JSON object"}
    raw_events = payload.get("timeline_evidence") or payload.get("events") or payload.get("route_events")
    if isinstance(raw_events, dict):
        events = [raw_events]
    elif isinstance(raw_events, list):
        events = [item for item in raw_events if isinstance(item, dict)]
    else:
        events = [payload] if any(key in payload for key in ("route_context_hash", "route_identity")) else []
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else payload
    gate = mf_route_context_gate_verification(events, contract=contract)
    return {
        "status": "pass" if gate.get("passed") else "fail",
        "ok": bool(gate.get("passed")),
        "required": bool(gate.get("required")),
        "missing_requirement_ids": gate.get("missing_requirement_ids") or [],
        "present_requirement_ids": gate.get("present_requirement_ids") or [],
        "topology_policy": gate.get("topology_policy") or {},
    }


@mf.command("dispatch-gate")
@click.option(
    "--contract-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Existing MF subagent dispatch contract JSON path.",
)
@click.option("--target-worktree", default="", help="Target worktree path to block same-worktree dispatch.")
@click.option("--main-worktree", default="", help="Main worktree path to block same-worktree dispatch.")
def mf_dispatch_gate(contract_file, target_worktree, main_worktree):
    """Validate MF subagent dispatch evidence before worker handoff."""
    from agent.governance.mf_subagent_contract import validate_mf_subagent_dispatch_gate

    try:
        payload = json.loads(Path(contract_file).read_text(encoding="utf-8"))
        result = validate_mf_subagent_dispatch_gate(
            payload,
            target_worktree_path=target_worktree,
            main_worktree_path=main_worktree,
        )
    except Exception as exc:
        click.echo(f"REJECT: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc

    click.echo(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
