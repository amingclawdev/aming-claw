# aming_claw

A Telegram-controlled AI task runner that wraps the Codex CLI with a robust pipeline: state machine, heartbeat monitoring, two-factor acceptance gates, and automatic archiving.

---

## Quick Start

```powershell
# 1. Download Python 3.12 + install all dependencies (one-time)
.\setup.ps1

# 2. Configure your environment
copy .env.example .env
notepad .env          # fill in TELEGRAM_BOT_TOKEN_CODEX and EXECUTOR_API_TOKEN

# 3. Launch all services
.\start.ps1
```

> **Requirements:** Windows 10/11. No Python installation needed — `setup.ps1` downloads an embedded Python 3.12 runtime into `runtime/python/`.

---

## Architecture

```
Telegram User
     │ sends message
     ▼
┌─────────────────┐
│   Coordinator   │  coordinator.py — Telegram polling, command handling,
│  (codex-team)   │  acceptance gates, timeout detection, archiving
└────────┬────────┘
         │ writes task file to shared-volume/codex-tasks/pending/
         ▼
┌─────────────────┐
│    Executor     │  executor.py — picks up pending tasks, invokes Codex CLI,
│  (codex-team)   │  streams output, writes results, updates heartbeat
└────────┬────────┘
         │ optional screenshot / file ops via HTTP
         ▼
┌─────────────────┐
│ Executor Gateway│  executor-gateway/ (FastAPI) — REST API for screenshots
│  (host process) │  and file operations on the host machine, port 8090
└─────────────────┘

Shared storage: shared-volume/codex-tasks/
  pending/      — task queue (JSON files)
  processing/   — tasks currently running
  results/      — completed tasks awaiting acceptance
  archive/      — accepted tasks (permanent)
  state/        — status.json, events.jsonl per task
  logs/         — run logs
  acceptance/   — acceptance documents and test cases
```

---

## Task Lifecycle

```
User sends message
       │
       ▼
  [ pending ]  ──────── task file written to pending/
       │
       ▼ Executor picks up task
  [ processing ]  ─────  heartbeat updated every 30s
       │
       ├─ Codex returns ACK-only? ──► retry (TASK_NOOP_RETRIES) ──► [ failed ]
       │
       ▼ Codex produces output
  [ pending_acceptance ]  ───  Telegram notification with Accept/Reject buttons
       │
       ├──── User rejects ──────────────────────► [ rejected ]
       │                                               │
       │                                   (stays in results/, iterable)
       │
       └──── User accepts (+ OTP if 2FA on) ──► [ accepted ] ──► [ archived ]
```

### Status fields in `state/task_state/{task_id}/status.json`

| Field | Description |
|---|---|
| `task_id` | Unique ID (`task-<ts>-<hex>`) |
| `task_code` | Short human-readable alias (e.g. `AB1`) |
| `status` | Current state (see lifecycle above) |
| `started_at` | When executor began processing |
| `ended_at` | When task reached terminal state |
| `progress` | 0–100 progress hint from executor |
| `worker_id` | Hostname of executor that ran the task |
| `attempt` | Number of execution attempts |
| `heartbeat_at` | Last heartbeat timestamp (updated every 30s) |
| `completion_notified_at` | When Telegram notification was sent |

---

## Two-Factor Authentication (2FA)

aming_claw uses TOTP (RFC 6238) to protect irreversible operations. Task acceptance is a destructive action — once accepted, the task is permanently archived. Enabling 2FA ensures no accidental clicks can commit that action.

### Setup

1. In Telegram, send `/auth_init` to the bot.
2. The bot replies with a base32 secret and an `otpauth://` URI. Scan it with any authenticator app (Google Authenticator, Authy, 1Password, etc.).
3. Enable strict acceptance in `.env`:
   ```
   TASK_STRICT_ACCEPTANCE=1
   ```
4. Restart services: `.\start.ps1 -Restart`

> **Note:** 2FA for acceptance is only enforced when **both** `TASK_STRICT_ACCEPTANCE=1` **and** the authenticator has been initialized via `/auth_init`. If either condition is missing, acceptance works without OTP.

### TOTP settings

| Variable | Default | Description |
|---|---|---|
| `AUTH_OTP_WINDOW` | `2` | Number of periods to accept on either side of current time |
| `AUTH_ALLOW_30_FALLBACK` | `1` | Also try 30-second TOTP if 60-second fails |
| `AUTH_AUTO_INIT` | `0` | Auto-initialize if no seed exists (not recommended for production) |

---

## Task Acceptance Flow

### Without 2FA (`TASK_STRICT_ACCEPTANCE=0`)

```
Task completes
    │
    ▼
Telegram: "Task [AB1] complete. Awaiting acceptance."
          [✅ Accept]  [❌ Reject]  [📊 Status]  [📋 Events]
    │
    ├── Click [✅ Accept]  →  Task immediately accepted and archived
    └── Click [❌ Reject]  →  Bot prompts: /reject AB1 <reason>
```

### With 2FA enabled (`TASK_STRICT_ACCEPTANCE=1` + `/auth_init` done)

```
Task completes
    │
    ▼
Telegram: "Task [AB1] complete. Awaiting acceptance."
          [✅ Accept]  [❌ Reject]  [📊 Status]  [📋 Events]
    │
    ├── Click [✅ Accept]
    │       │
    │       ▼
    │   Bot: "2FA required to accept task [AB1].
    │         Send: /accept AB1 <6-digit OTP>"
    │       │
    │       ▼
    │   User sends: /accept AB1 123456
    │       │
    │       ├── OTP valid   →  Task accepted and archived ✅
    │       └── OTP invalid →  "2FA failed: OTP invalid or expired." ❌
    │
    └── Click [❌ Reject]
            │
            ▼
        Bot: "2FA required to reject task [AB1].
              Send: /reject AB1 <6-digit OTP> [reason]"
            │
            ▼
        User sends: /reject AB1 123456 Implementation does not handle edge case
            │
            ├── OTP valid   →  Task rejected, stays in results/ for iteration ✅
            └── OTP invalid →  "2FA failed: OTP invalid or expired." ❌
```

---

## Command Reference

### Task commands

| Command | Description |
|---|---|
| `<any text>` | Create and queue a new Codex task |
| `/status` | List all active tasks (pending + in-progress + awaiting acceptance) |
| `/status <ref>` | Show detailed status for a task by ID or short code |
| `/accept <ref> [OTP]` | Accept task result and archive it (OTP required if 2FA enabled) |
| `/reject <ref> [OTP] [reason]` | Reject task result (OTP required if 2FA enabled) |
| `/events <ref>` | Show recent events for a task |

### Archive commands

| Command | Description |
|---|---|
| `/archive` | List recent archived tasks |
| `/archive_show <archive_id>` | Show details of an archived task |
| `/archive_search <keyword>` | Search archive by keyword |

### 2FA commands

| Command | Description |
|---|---|
| `/auth_init` | Initialize TOTP authenticator (generates secret + QR URI) |
| `/auth_status` | Show current 2FA configuration |
| `/auth_debug <OTP>` | Debug OTP verification (ops-only) |

### Ops commands (require OTP + whitelist)

| Command | Description |
|---|---|
| `/ops_restart <OTP>` | Restart all services |
| `/ops_set_workspace <path\|default> <OTP>` | Switch Codex working directory |
| `/ops_set_workspace_pick <n> <OTP>` | Pick from candidate workspaces |

---

## Configuration Reference

Copy `.env.example` to `.env` and fill in the required values.

### Required

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN_CODEX` | Telegram bot token (dedicated bot recommended) |
| `EXECUTOR_API_TOKEN` | Shared secret for executor-gateway authentication |

### Execution

| Variable | Default | Description |
|---|---|---|
| `CODEX_BIN` | `codex.cmd` | Codex CLI binary name |
| `CODEX_WORKSPACE` | cwd | Directory Codex operates in |
| `CODEX_TIMEOUT_SEC` | `900` | Max seconds for a single Codex run |
| `CODEX_TIMEOUT_RETRIES` | `1` | Retry count on timeout |
| `CODEX_MODEL` | | Override Codex model (leave blank for default) |
| `CODEX_DANGEROUS` | `1` | Pass `--dangerously-auto-approve` to Codex |

### Acceptance & 2FA

| Variable | Default | Description |
|---|---|---|
| `TASK_STRICT_ACCEPTANCE` | `1` | Require explicit `/accept` before archiving |
| `AUTH_OTP_WINDOW` | `2` | OTP time window tolerance (periods) |
| `AUTH_ALLOW_30_FALLBACK` | `1` | Try 30s TOTP period as fallback |
| `AUTH_AUTO_INIT` | `0` | Auto-initialize 2FA seed on first run |

### Timeouts & polling

| Variable | Default | Description |
|---|---|---|
| `TASK_TIMEOUT_SEC` | `1800` | Seconds before a stuck task is marked `timeout` |
| `EXECUTOR_HEARTBEAT_SEC` | `30` | Heartbeat interval in seconds |
| `TASK_NOOP_RETRIES` | `1` | Retries when Codex returns acknowledgement-only output |
| `COORDINATOR_POLL_INTERVAL_SEC` | `1` | Telegram update polling interval |
| `EXECUTOR_POLL_SEC` | `1` | Task queue polling interval |

### Storage paths

| Variable | Default | Description |
|---|---|---|
| `SHARED_VOLUME_PATH` | `<cwd>/shared-volume` | Root of task storage |
| `WORKSPACE_PATH` | cwd | Host workspace path for executor-gateway |
| `EXECUTOR_BASE_URL` | `http://127.0.0.1:8090` | Gateway URL used by coordinator |

---

## File Structure

```
aming_claw/
├── setup.ps1                  # One-time: download Python + install deps
├── start.ps1                  # Launch all three services
├── .env.example               # Configuration template
├── codex-team/
│   ├── coordinator.py         # Telegram bot + task lifecycle management
│   ├── executor.py            # Task runner (invokes Codex CLI)
│   ├── task_runtime.py        # State machine, events, heartbeat functions
│   ├── common.py              # Shared utilities (atomic JSON, Telegram API)
│   ├── auth_executor.py       # TOTP-based 2FA implementation
│   └── requirements.txt
├── executor-gateway/
│   ├── app/main.py            # FastAPI service for screenshots & file ops
│   └── requirements.txt
├── scripts/
│   ├── _get_python.ps1        # Returns bundled python.exe path
│   ├── run-codex-coordinator.ps1
│   ├── run-codex-executor.ps1
│   └── run-executor-host.ps1
└── runtime/
    └── python/                # Downloaded by setup.ps1, excluded from git
```

---

## Troubleshooting

**Bot doesn't respond to messages**
- Check `TELEGRAM_BOT_TOKEN_CODEX` is set correctly in `.env`
- Ensure the coordinator service is running (check the coordinator terminal window)

**Tasks stuck in `processing`**
- The coordinator auto-marks tasks as `timeout` after `TASK_TIMEOUT_SEC` seconds without a heartbeat
- Check executor terminal for errors; verify `CODEX_BIN` is accessible

**"2FA failed: OTP invalid or expired"**
- Ensure your device clock is synchronized (NTP)
- Increase `AUTH_OTP_WINDOW` (e.g. `AUTH_OTP_WINDOW=3`) to tolerate clock skew
- Run `/auth_debug <OTP>` to see detailed verification info

**Codex returns acknowledgement without acting**
- This is a model behavior issue. The pipeline detects ACK-only messages and retries once (`TASK_NOOP_RETRIES=1`)
- If it persists, the task is marked `failed`; retry by sending the task again
