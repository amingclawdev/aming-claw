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

PLANE_ENDPOINTS="$(
  "$PYTHON" -c 'from agent.runtime_plane import resolve_runtime_plane; import sys; plane = resolve_runtime_plane(sys.argv[1]); print(f"{plane.governance_url}\t{plane.manager_url}\t{plane.name}")' "$PROJECT"
)"
IFS=$'\t' read -r DEFAULT_GOVERNANCE_URL DEFAULT_MANAGER_URL RUNTIME_PLANE <<< "$PLANE_ENDPOINTS"
if [[ -n "${GOVERNANCE_URL:-}" && "$GOVERNANCE_URL" != "$DEFAULT_GOVERNANCE_URL" ]]; then
  echo "Configured GOVERNANCE_URL crosses the $PROJECT runtime plane." >&2
  exit 2
fi
if [[ -n "${MANAGER_URL:-}" && "$MANAGER_URL" != "$DEFAULT_MANAGER_URL" ]]; then
  echo "Configured MANAGER_URL crosses the $PROJECT runtime plane." >&2
  exit 2
fi

if [[ "$RUNTIME_PLANE" == "dev" ]]; then
  DEV_BINDING="$(
    "$PYTHON" -c 'from agent.governance.db import verified_stable_database_binding; from agent.runtime_plane import resolve_ac_dev_storage_root; from pathlib import Path; binding = verified_stable_database_binding(); stable = Path(str(binding["shared_volume_path"])); dev = resolve_ac_dev_storage_root(stable); runtime = dev / "runtime"; print(f"{stable}\t{dev}\t{runtime}")'
  )"
  IFS=$'\t' read -r DEFAULT_STABLE_SHARED_VOLUME DEFAULT_DEV_STORAGE_ROOT DEFAULT_SHARED_VOLUME_PATH <<< "$DEV_BINDING"
  if [[ -n "${AMING_CLAW_SHARED_VOLUME:-}" && "$AMING_CLAW_SHARED_VOLUME" != "$DEFAULT_STABLE_SHARED_VOLUME" ]]; then
    echo "Configured AMING_CLAW_SHARED_VOLUME crosses the $PROJECT runtime plane." >&2
    exit 2
  fi
  if [[ -n "${AMING_CLAW_DEV_STORAGE_ROOT:-}" && "$AMING_CLAW_DEV_STORAGE_ROOT" != "$DEFAULT_DEV_STORAGE_ROOT" ]]; then
    echo "Configured AMING_CLAW_DEV_STORAGE_ROOT crosses the $PROJECT runtime plane." >&2
    exit 2
  fi
  if [[ -n "${SHARED_VOLUME_PATH:-}" && "$SHARED_VOLUME_PATH" != "$DEFAULT_SHARED_VOLUME_PATH" ]]; then
    echo "Configured SHARED_VOLUME_PATH crosses the $PROJECT runtime plane." >&2
    exit 2
  fi
  export AMING_CLAW_SHARED_VOLUME="$DEFAULT_STABLE_SHARED_VOLUME"
  export AMING_CLAW_DEV_STORAGE_ROOT="$DEFAULT_DEV_STORAGE_ROOT"
else
  DEFAULT_SHARED_VOLUME_PATH="${SHARED_VOLUME_PATH:-$REPO_ROOT/shared-volume}"
fi

export SHARED_VOLUME_PATH="$DEFAULT_SHARED_VOLUME_PATH"
export GOVERNANCE_URL="$DEFAULT_GOVERNANCE_URL"
export MANAGER_URL="$DEFAULT_MANAGER_URL"
export PROJECT_ID="$PROJECT"
export EXECUTOR_PROJECT_ID="$PROJECT"
export CODEX_WORKSPACE="${CODEX_WORKSPACE:-$REPO_ROOT}"

STATE_DIR="$SHARED_VOLUME_PATH/codex-tasks/state"
LOG_DIR="$SHARED_VOLUME_PATH/codex-tasks/logs"
mkdir -p "$STATE_DIR" "$LOG_DIR"

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
  pgrep -f "agent/service_manager.py|-m agent.service_manager" 2>/dev/null | head -n 1 || true
}

find_worker_pid() {
  pgrep -f "agent/executor_worker.py.*--project[[:space:]]+$PROJECT" 2>/dev/null | head -n 1 || true
}

report_healthy_manager() {
  local message="$1"
  local launcher_pid="${2:-}"
  local manager_pid
  local worker_pid
  manager_pid="$(find_manager_pid)"
  worker_pid="$(find_worker_pid)"

  echo "$message"
  echo "  manager:       ${manager_pid:-not_observed}"
  if [[ -n "$worker_pid" ]]; then
    echo "  executor_state: optional_present"
    echo "  worker:        $worker_pid"
  else
    echo "  executor_state: waived_or_degraded"
    echo "  worker:        not_observed (optional)"
  fi
  if [[ -n "$launcher_pid" ]]; then
    echo "  launcher:      $launcher_pid"
  fi
}

wait_for_manager_health() {
  local deadline=$((SECONDS + HEALTH_WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if check_manager_health; then
      return 0
    fi
    sleep 1
  done
  return 1
}

if check_manager_health; then
  report_healthy_manager "Manager already healthy."
  exit 0
fi

LOCK_DIR="$STATE_DIR/manager-start.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Manager launcher lock is already held; waiting for ServiceManager health."
  if wait_for_manager_health; then
    report_healthy_manager "Manager became healthy while another launcher held the lock."
    exit 0
  fi
  echo "ServiceManager health did not become healthy within $HEALTH_WAIT_SECONDS seconds while the launcher lock was held." >&2
  exit 1
fi
cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

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
  "$PYTHON" - "$PYTHON" "agent.service_manager" "$REPO_ROOT" "$PROJECT" "$GOVERNANCE_URL" "$CODEX_WORKSPACE" "$STDOUT_LOG" "$STDERR_LOG" <<'PY'
import os
import subprocess
import sys

python, module, repo_root, project, governance_url, workspace, stdout_log, stderr_log = sys.argv[1:]
stdout_handle = open(stdout_log, "ab")
stderr_handle = open(stderr_log, "ab")
try:
    proc = subprocess.Popen(
        [
            python,
            "-m",
            module,
            "--project",
            project,
            "--governance-url",
            governance_url,
            "--workspace",
            workspace,
        ],
        cwd=repo_root,
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
if wait_for_manager_health; then
  report_healthy_manager "Manager healthy." "$LAUNCHER_PID"
  exit 0
fi

echo "ServiceManager health did not become healthy within $HEALTH_WAIT_SECONDS seconds after launch." >&2
echo "  launcher: $LAUNCHER_PID" >&2
echo "  stdout:   $STDOUT_LOG" >&2
echo "  stderr:   $STDERR_LOG" >&2
exit 1
