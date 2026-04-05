"""aming-claw — governance-driven workflow platform.

Public API
----------
- AmingConfig:        project configuration (env > yaml > defaults)
- bootstrap_project:  bootstrap a new or external project
- create_task:        create a governance task
"""

from agent.config import AmingConfig


def bootstrap_project(workspace_path: str = ".", project_name: str = "", config_override: dict = None):
    """Bootstrap a project into aming-claw governance."""
    from agent.governance.project_service import bootstrap_project as _bp
    return _bp(workspace_path=workspace_path, project_name=project_name, config_override=config_override)


def create_task(conn, project_id: str, prompt: str, **kwargs):
    """Create a governance task."""
    from agent.governance.task_registry import create_task as _ct
    return _ct(conn=conn, project_id=project_id, prompt=prompt, **kwargs)


__all__ = ["AmingConfig", "bootstrap_project", "create_task"]
