import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> int:
    _payload = json.load(sys.stdin)
    workspace = Path(os.getenv("WORKSPACE_PATH", "/workspace"))
    cmd = os.getenv("SAFE_TEST_COMMAND", "pytest -q")
    args = shlex.split(cmd)

    proc = subprocess.run(
        args,
        cwd=str(workspace),
        text=True,
        capture_output=True,
        check=False,
    )

    details = {
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-3000:],
        "stderr": (proc.stderr or "")[-3000:],
        "command": args,
    }
    ok = proc.returncode == 0

    result = {
        "ok": ok,
        "summary": f"tests {'passed' if ok else 'failed'} (exit={proc.returncode})",
        "details": details,
    }
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
