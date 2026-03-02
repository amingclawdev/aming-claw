import os
from pathlib import Path
from typing import Optional

from utils import load_json, save_json, tasks_root, utc_iso


def workspace_state_file() -> Path:
    return tasks_root() / "state" / "workspace_override.json"


def get_workspace_override() -> Optional[Path]:
    path = workspace_state_file()
    if not path.exists():
        return None
    try:
        val = str(load_json(path).get("workspace", "")).strip()
    except Exception:
        return None
    if not val:
        return None
    return Path(val)


def set_workspace_override(workspace: Path, changed_by: int) -> None:
    save_json(
        workspace_state_file(),
        {
            "workspace": str(workspace),
            "changed_by": int(changed_by),
            "updated_at": utc_iso(),
        },
    )


def clear_workspace_override(changed_by: int) -> None:
    save_json(
        workspace_state_file(),
        {
            "workspace": "",
            "changed_by": int(changed_by),
            "updated_at": utc_iso(),
        },
    )


def resolve_workspace_from_env() -> Path:
    configured = os.getenv("CODEX_WORKSPACE", "").strip()
    if configured:
        return Path(configured)
    return Path.cwd()


def resolve_active_workspace() -> Path:
    override = get_workspace_override()
    if override:
        return override
    return resolve_workspace_from_env()
