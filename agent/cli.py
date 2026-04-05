"""CLI entry point for aming-claw.

Usage:
    aming-claw init            — create .aming-claw.yaml in current directory
    aming-claw bootstrap       — bootstrap an external project
    aming-claw status          — show governance status
    aming-claw run-executor    — start executor worker
"""

import os
import sys
import logging

try:
    import click
except ImportError:
    # Provide a helpful error when click isn't installed
    print("Error: 'click' package is required. Install with: pip install click", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger(__name__)

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
    """aming-claw — governance-driven workflow platform."""
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


@main.command("run-executor")
def run_executor():
    """Start the executor worker."""
    from agent.executor_worker import main as worker_main
    worker_main()


if __name__ == "__main__":
    main()
