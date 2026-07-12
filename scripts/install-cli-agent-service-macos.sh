#!/bin/sh
set -eu

LABEL="dev.amingclaw.cli-agent-service"
PYTHON_BIN="$(command -v python3)"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="$HOME/Library/Application Support/AmingClaw/cli-agent-service"
LOG_DIR="$HOME/Library/Logs/AmingClaw"
DRY_RUN=0
UNINSTALL=0

usage() {
  printf '%s\n' "usage: $0 [--dry-run] [--uninstall] [--python PATH] [--repo-root PATH] [--state-dir PATH] [--label LABEL]"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run|--print-plist) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --python) PYTHON_BIN=$2; shift 2 ;;
    --repo-root) REPO_ROOT=$2; shift 2 ;;
    --state-dir) STATE_DIR=$2; shift 2 ;;
    --label) LABEL=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

render_plist() {
  python_xml=$(xml_escape "$PYTHON_BIN")
  agent_dir_xml=$(xml_escape "$REPO_ROOT/agent")
  state_xml=$(xml_escape "$STATE_DIR")
  stdout_xml=$(xml_escape "$LOG_DIR/cli-agent-service.log")
  stderr_xml=$(xml_escape "$LOG_DIR/cli-agent-service.err.log")
  label_xml=$(xml_escape "$LABEL")
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label_xml</string>
  <key>ProgramArguments</key>
  <array>
    <string>$python_xml</string>
    <string>-m</string>
    <string>cli_agent_service</string>
    <string>start</string>
    <string>--state-dir</string>
    <string>$state_xml</string>
  </array>
  <key>WorkingDirectory</key><string>$agent_dir_xml</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$stdout_xml</string>
  <key>StandardErrorPath</key><string>$stderr_xml</string>
</dict>
</plist>
EOF
}

if [ "$UNINSTALL" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'launchctl bootout gui/%s/%s\nrm -f %s\n' "$(id -u)" "$LABEL" "$PLIST_PATH"
    exit 0
  fi
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  rm -f "$PLIST_PATH"
  exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
  render_plist
  exit 0
fi

umask 077
mkdir -p "$PLIST_DIR" "$STATE_DIR" "$LOG_DIR"
chmod 700 "$STATE_DIR" "$LOG_DIR"
temporary="$PLIST_PATH.tmp.$$"
render_plist > "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$PLIST_PATH"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
printf 'installed %s\n' "$PLIST_PATH"
