#!/usr/bin/env bash
set -euo pipefail

PROJECT="aming-claw"
HEALTH_WAIT_SECONDS="90"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project|-Project)
      PROJECT="${2:-}"
      shift 2
      ;;
    --health-wait-seconds|-HealthWaitSeconds)
      HEALTH_WAIT_SECONDS="${2:-90}"
      shift 2
      ;;
    --takeover|-Takeover)
      echo "Takeover is not supported by scripts/start-manager.sh; stop processes explicitly from an ops shell." >&2
      exit 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

if [[ -x ".venv/bin/python" ]]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON="$(command -v python)"
else
  echo "Python not found." >&2
  exit 127
fi

export SHARED_VOLUME_PATH="${SHARED_VOLUME_PATH:-$REPO_ROOT/shared-volume}"
export GOVERNANCE_URL="${GOVERNANCE_URL:-http://localhost:40000}"
export MANAGER_URL="${MANAGER_URL:-http://127.0.0.1:40101}"
export CODEX_WORKSPACE="${CODEX_WORKSPACE:-$REPO_ROOT}"

STATE_DIR="$SHARED_VOLUME_PATH/codex-tasks/state"
LOG_DIR="$SHARED_VOLUME_PATH/codex-tasks/logs"
mkdir -p "$STATE_DIR" "$LOG_DIR"

LOCK_DIR="$STATE_DIR/manager-start.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Manager launcher lock is already held. Exit."
  exit 0
fi
cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

check_manager_health() {
  "$PYTHON" - "$MANAGER_URL/api/manager/health" <<'PY'
import json
import sys
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=2) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    raise SystemExit(0 if payload.get("ok") else 1)
except Exception:
    raise SystemExit(1)
PY
}

find_manager_pid() {
  pgrep -f "agent/service_manager.py" 2>/dev/null | head -n 1 || true
}

find_worker_pid() {
  pgrep -f "agent/executor_worker.py.*--project[[:space:]]+$PROJECT" 2>/dev/null | head -n 1 || true
}

if check_manager_health; then
  DEADLINE=$((SECONDS + HEALTH_WAIT_SECONDS))
  while (( SECONDS < DEADLINE )); do
    MANAGER_PID="$(find_manager_pid)"
    WORKER_PID="$(find_worker_pid)"
    if [[ -n "$MANAGER_PID" && -n "$WORKER_PID" ]]; then
      echo "Manager already healthy."
      echo "  manager: $MANAGER_PID"
      echo "  worker:  $WORKER_PID"
      exit 0
    fi
    sleep 1
  done
  echo "Manager sidecar is healthy but executor worker did not appear within $HEALTH_WAIT_SECONDS seconds." >&2
  echo "  manager: $(find_manager_pid)" >&2
  echo "  worker:  $(find_worker_pid)" >&2
  echo "Stop the stale manager host process and run this script again." >&2
  exit 1
fi

if ! "$PYTHON" -c "import requests" >/dev/null 2>&1; then
  echo "Installing agent dependencies..."
  "$PYTHON" -m pip install -r "$REPO_ROOT/agent/requirements.txt" --no-warn-script-location
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
STDOUT_LOG="$LOG_DIR/service-manager-start-$PROJECT-$STAMP.out.log"
STDERR_LOG="$LOG_DIR/service-manager-start-$PROJECT-$STAMP.err.log"

echo "Starting aming-claw host manager..."
echo "  project:    $PROJECT"
echo "  governance: $GOVERNANCE_URL"
echo "  manager:    $MANAGER_URL"
echo "  workspace:  $CODEX_WORKSPACE"
echo "  python:     $PYTHON"
echo "  stdout:     $STDOUT_LOG"
echo "  stderr:     $STDERR_LOG"

LAUNCHER_PID="$(
  "$PYTHON" - "$PYTHON" "$REPO_ROOT/agent/service_manager.py" "$PROJECT" "$GOVERNANCE_URL" "$CODEX_WORKSPACE" "$STDOUT_LOG" "$STDERR_LOG" <<'PY'
import os
import subprocess
import sys

python, script, project, governance_url, workspace, stdout_log, stderr_log = sys.argv[1:]
stdout_handle = open(stdout_log, "ab")
stderr_handle = open(stderr_log, "ab")
try:
    proc = subprocess.Popen(
        [
            python,
            script,
            "--project",
            project,
            "--governance-url",
            governance_url,
            "--workspace",
            workspace,
        ],
        cwd=os.path.dirname(os.path.dirname(script)),
        stdin=subprocess.DEVNULL,
        stdout=stdout_handle,
        stderr=stderr_handle,
        close_fds=True,
        start_new_session=True,
    )
finally:
    stdout_handle.close()
    stderr_handle.close()

print(proc.pid)
PY
)"
DEADLINE=$((SECONDS + HEALTH_WAIT_SECONDS))
while (( SECONDS < DEADLINE )); do
  MANAGER_PID="$(find_manager_pid)"
  WORKER_PID="$(find_worker_pid)"
  if [[ -n "$MANAGER_PID" && -n "$WORKER_PID" ]] && check_manager_health; then
    echo "Manager healthy."
    echo "  manager:  $MANAGER_PID"
    echo "  worker:   $WORKER_PID"
    echo "  launcher: $LAUNCHER_PID"
    exit 0
  fi
  sleep 1
done

echo "Managed executor worker did not appear within $HEALTH_WAIT_SECONDS seconds." >&2
echo "  launcher: $LAUNCHER_PID" >&2
echo "  stdout:   $STDOUT_LOG" >&2
echo "  stderr:   $STDERR_LOG" >&2
exit 1
