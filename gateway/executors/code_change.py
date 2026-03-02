import json
import os
import re
import sys
from pathlib import Path


def safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "-", text)[:60]


def main() -> int:
    payload = json.load(sys.stdin)
    task_id = payload.get("task_id", "task-unknown")
    command_text = (payload.get("command_text") or "").strip()
    workspace = Path(os.getenv("WORKSPACE_PATH", "/workspace"))
    output_dir = workspace / ".openclaw" / "proposals"
    output_dir.mkdir(parents=True, exist_ok=True)

    file_name = f"{safe_name(task_id)}.md"
    target = output_dir / file_name

    content = (
        f"# Proposal: {task_id}\n\n"
        f"## Request\n{command_text}\n\n"
        "## Suggested Steps\n"
        "1. Identify impacted files\n"
        "2. Apply minimal code change\n"
        "3. Add or update tests\n"
        "4. Validate with lint/test/build\n"
    )
    target.write_text(content, encoding="utf-8")

    result = {
        "ok": True,
        "summary": f"proposal written: {target}",
        "details": {"proposal_file": str(target)},
    }
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
