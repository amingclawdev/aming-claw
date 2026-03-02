import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import requests


def utc_ts_ms() -> int:
    return int(time.time() * 1000)


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def shared_root() -> Path:
    root = os.getenv("SHARED_VOLUME_PATH", "").strip()
    if not root:
        # Use repository-relative default, not process cwd, to avoid
        # reading/writing different shared-volume paths when started
        # from another directory.
        root = str((Path(__file__).resolve().parents[1] / "shared-volume").resolve())
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def tasks_root() -> Path:
    p = shared_root() / "codex-tasks"
    (p / "pending").mkdir(parents=True, exist_ok=True)
    (p / "processing").mkdir(parents=True, exist_ok=True)
    (p / "results").mkdir(parents=True, exist_ok=True)
    (p / "logs").mkdir(parents=True, exist_ok=True)
    (p / "archive").mkdir(parents=True, exist_ok=True)
    (p / "state").mkdir(parents=True, exist_ok=True)
    return p


def task_file(stage: str, task_id: str) -> Path:
    return tasks_root() / stage / (task_id + ".json")


def new_task_id() -> str:
    return "task-" + str(utc_ts_ms()) + "-" + uuid.uuid4().hex[:6]


def save_json(path: Path, obj: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def load_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def telegram_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN_CODEX", "").strip()
    if not token:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN_CODEX or TELEGRAM_BOT_TOKEN")
    return token


def tg_post(method: str, data: Dict, files: Optional[Dict] = None) -> Dict:
    payload: Dict[str, Any] = {}
    for k, v in (data or {}).items():
        if isinstance(v, (dict, list)):
            payload[k] = json.dumps(v, ensure_ascii=False)
        elif v is None:
            continue
        else:
            payload[k] = str(v)
    token = telegram_token()
    url = "https://api.telegram.org/bot{}/{}".format(token, method)
    resp = requests.post(url, data=payload, files=files, timeout=30)
    try:
        body = resp.json()
    except Exception:
        body = {"ok": False, "status_code": resp.status_code, "text": resp.text[:1000]}
    if resp.status_code >= 400 or not body.get("ok", False):
        raise RuntimeError("telegram {} failed: {}".format(method, body))
    return body


def send_text(
    chat_id: int,
    text: str,
    *,
    parse_mode: str = "",
    reply_markup: Optional[Dict[str, Any]] = None,
    disable_preview: bool = True,
) -> None:
    data: Dict[str, Any] = {"chat_id": str(chat_id), "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = reply_markup
    if disable_preview:
        data["disable_web_page_preview"] = "true"
    tg_post("sendMessage", data)


def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
    tg_post(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": "true" if show_alert else "false",
        },
    )


def send_document(chat_id: int, path: Path, caption: str = "") -> None:
    with path.open("rb") as f:
        tg_post(
            "sendDocument",
            {"chat_id": str(chat_id), "caption": caption},
            files={"document": f},
        )
