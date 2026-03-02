import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="executor-gateway", version="0.1.0")

BASE_DIR = Path(__file__).resolve().parents[1]
ACTIONS_CONFIG_PATH = BASE_DIR / "config" / "actions.yaml"
EXECUTOR_TOKEN = os.getenv("EXECUTOR_API_TOKEN", "")


class ExecuteRequest(BaseModel):
    task_id: str
    action: str
    command_text: str


def load_actions() -> dict[str, dict[str, Any]]:
    with ACTIONS_CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("actions", {})


def resolve_script(script_path: str) -> Path:
    candidate = BASE_DIR / script_path
    resolved = candidate.resolve()
    executors_root = (BASE_DIR / "executors").resolve()
    if executors_root not in resolved.parents:
        raise HTTPException(status_code=400, detail="script path not allowed")
    if not resolved.exists():
        raise HTTPException(status_code=400, detail=f"script not found: {script_path}")
    return resolved


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/execute")
def execute(req: ExecuteRequest, x_executor_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    if not EXECUTOR_TOKEN:
        raise HTTPException(status_code=500, detail="EXECUTOR_API_TOKEN is missing")
    if x_executor_token != EXECUTOR_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")

    actions = load_actions()
    action_cfg = actions.get(req.action)
    if not action_cfg:
        return {"ok": False, "error": f"unsupported action: {req.action}"}

    script = resolve_script(action_cfg["script"])
    timeout_sec = int(action_cfg.get("timeout_sec", 60))
    payload = req.model_dump()

    try:
        proc = subprocess.run(
            ["python", str(script)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"action timeout after {timeout_sec}s"}

    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()[:1000] or "executor script failed"}

    try:
        output = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid executor script output"}

    return output
