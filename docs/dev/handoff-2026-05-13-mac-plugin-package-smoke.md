# Handoff: Mac plugin/package smoke and follow-up implementation

Date: 2026-05-13

Audience: Codex running on the Mac test machine.

## Current state

- GitHub `main` has been pushed to `c632ba3`.
- Windows workspace was clean after push.
- Backlog row `OPT-PACKAGING-PLUGIN-CROSS-PLATFORM-MVP` was closed as `FIXED`.
- Active graph snapshot for `aming-claw` was reconciled to `scope-c632ba3-cca7` with pending scope reconcile count `0`.
- Governance runtime was redeployed to `c632ba3`.
- ServiceManager runtime was still older (`549e6d1`) before handoff; this was pre-existing and not part of the package/plugin MF.

Implemented in `c632ba3`:

- Dashboard static assets are copied into `agent/governance/dashboard_dist` during `npm --prefix frontend/dashboard run build`.
- The Python package includes packaged dashboard static assets via `pyproject.toml` package data and `MANIFEST.in`.
- `/dashboard` can resolve either repo `frontend/dashboard/dist` or packaged `agent.governance.dashboard_dist`.
- `scripts/build_package.py` builds the dashboard if needed and then builds a wheel through the setuptools PEP 517 backend.
- `aming-claw start`, `aming-claw open`, and `aming-claw launcher` exist in `agent/cli.py`.
- Plugin/service startup policy is explicit: plugin load must not auto-start Governance, SM, or executor.
- `CLAUDE.md` documents project-level Claude Code usage.
- Directory picker fallback now covers Windows, macOS (`osascript`), Linux (`zenity`/`kdialog`), and manual entry.
- Skill docs were updated in `skills/aming-claw/SKILL.md` and `skills/aming-claw/references/plugin-packaging.md`.

## First steps on Mac

Clone or update the repo:

```bash
git clone git@github.com:web3ToolBoxDev/aming_claw.git
cd aming_claw
git checkout main
git pull --ff-only origin main
git rev-parse --short HEAD
```

Expected HEAD:

```text
c632ba3
```

Create a clean Python environment and install test/build dependencies using the project standard flow already used on the machine. If no local convention exists yet:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Install frontend dependencies if absent:

```bash
npm --prefix frontend/dashboard install
```

## Required Mac smoke tests

Run these before changing code.

### 1. Build dashboard and sync package static assets

```bash
npm --prefix frontend/dashboard run build
```

Expected:

- Vite build succeeds.
- `frontend/dashboard/dist` exists.
- `agent/governance/dashboard_dist/index.html` exists after sync.
- Do not commit generated dashboard files unless a future backlog explicitly changes that policy.

### 2. Build wheel with packaged dashboard

```bash
python scripts/build_package.py --skip-dashboard-build --wheel-dir dist/python
```

Expected:

- A wheel like `dist/python/aming_claw-0.1.0-py3-none-any.whl` is produced.
- Build output includes packaged dashboard files under `agent/governance/dashboard_dist`.

### 3. Clean install wheel outside the repo

Use a temp directory so Python cannot accidentally import the source checkout:

```bash
tmpdir="$(mktemp -d)"
python -m pip install --no-deps --target "$tmpdir/pkg" dist/python/aming_claw-0.1.0-py3-none-any.whl
cd "$tmpdir"
PYTHONPATH="$tmpdir/pkg" python - <<'PY'
from agent.governance import server
p = server._dashboard_dist_dir()
print(p)
assert (p / "index.html").is_file()
assert "dashboard_dist" in str(p)
PY
```

Expected:

- The printed path points inside the temp package install, not the repo checkout.
- `index.html` is found.

### 4. CLI launcher and no-autostart contract

From the repo or installed environment:

```bash
aming-claw launcher --output /tmp/aming-claw-launcher.html
```

If the console script is not installed in the active env, use:

```bash
python -m agent.cli launcher --output /tmp/aming-claw-launcher.html
```

Expected:

- HTML launcher file is created.
- The launcher contains start/status/dashboard guidance.
- It must not start Governance, ServiceManager, or executor by itself.

### 5. macOS directory picker fallback

With Governance running, test the dashboard project import "Choose directory" flow on macOS.

Start Governance explicitly:

```bash
aming-claw start --workspace . --port 40000
```

or:

```bash
python -m agent.cli start --workspace . --port 40000
```

Open:

```bash
aming-claw open --governance-url http://127.0.0.1:40000
```

Expected:

- Dashboard loads.
- Project import directory picker either opens native macOS picker through `osascript`, or returns a clear manual-entry fallback.
- No hidden service auto-start occurs from plugin load.

### 6. Static route E2E probe

```bash
node frontend/dashboard/scripts/e2e-trunk.mjs --probe --static-route --build-dashboard --dashboard http://localhost:40000/dashboard
```

Expected:

```text
TRUNK E2E PROBE OK
```

## Focused regression tests

Run these focused tests before and after any package/plugin changes:

```bash
python -m pytest \
  agent/tests/test_package_install.py \
  agent/tests/test_dashboard_static_route.py \
  agent/tests/test_project_dashboard_config_api.py \
  agent/tests/test_cli.py \
  agent/tests/test_mcp_server_stdio.py \
  agent/tests/test_governance_host_migration_round1.py \
  -q
```

Windows result before handoff: `42 passed`.

Known caveat:

- Do not treat the full `agent/tests/test_config_validation.py` suite as a package/plugin blocker yet. Existing test `TestMcpJsonConfig::test_telegram_bot_token_present` still expects a committed real `TELEGRAM_BOT_TOKEN` in `.mcp.json`, which conflicts with plugin packaging secret hygiene.
- Follow-up backlog already filed: `BUG-MCP-CONFIG-VALIDATION-SECRET-PLACEHOLDER-POLICY`.

## MF rules for Mac follow-up

If any Mac smoke issue appears, follow MF strictly:

1. File or update a backlog row before code changes.
2. Query the graph before implementation. Prefer MCP graph tools if available; otherwise use the Governance graph API.
3. Identify reusable code and the impacted L7/L4 nodes.
4. Evaluate E2E impact before editing. If the change affects package startup, dashboard static routing, directory picker, project import, or plugin launcher behavior, update or add focused tests/E2E probes.
5. Implement narrowly.
6. Run focused tests and relevant E2E.
7. Commit with evidence and the backlog id.
8. Reconcile graph to the new commit.
9. Close the backlog row only after verification.

Commit trailer pattern:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: aming-claw
Chain-Bug-Id: <BACKLOG_ID>
```

## Suggested backlog if Mac finds issues

Use these ids if the issue matches exactly; otherwise create a more precise row.

- `BUG-MAC-PACKAGED-DASHBOARD-STATIC-ROUTE`
  - Use if installed wheel cannot serve `/dashboard` on macOS.
- `BUG-MAC-DIRECTORY-PICKER-FALLBACK`
  - Use if the macOS directory picker does not open or does not fall back cleanly.
- `BUG-PLUGIN-LAUNCHER-AUTOSTART-CONTRACT`
  - Use if plugin or launcher starts local services implicitly.
- `BUG-PACKAGE-CONSOLE-SCRIPT-MAC`
  - Use if `aming-claw start/open/launcher` console scripts fail after pip install.
- `BUG-MCP-CONFIG-VALIDATION-SECRET-PLACEHOLDER-POLICY`
  - Existing follow-up for `.mcp.json` secret placeholder policy.

## Files most likely relevant

- `agent/cli.py`
- `agent/governance/server.py`
- `start_governance.py`
- `scripts/build_package.py`
- `frontend/dashboard/package.json`
- `frontend/dashboard/scripts/sync-dist-to-python-package.mjs`
- `pyproject.toml`
- `MANIFEST.in`
- `CLAUDE.md`
- `skills/aming-claw/SKILL.md`
- `skills/aming-claw/references/plugin-packaging.md`
- `agent/tests/test_package_install.py`
- `agent/tests/test_dashboard_static_route.py`
- `agent/tests/test_cli.py`

## Do not accidentally commit

These are generated or environment-specific:

- `frontend/dashboard/dist/`
- `agent/governance/dashboard_dist/index.html`
- `agent/governance/dashboard_dist/assets/`
- `dist/`
- `aming_claw.egg-info/`
- virtualenvs and local package install temp dirs

`agent/governance/dashboard_dist/__init__.py` is the only tracked file expected under `dashboard_dist`.

## What "done" means on Mac

Mac validation is done when:

- Repo HEAD is `c632ba3` or newer.
- Focused tests pass.
- Dashboard build succeeds.
- Wheel builds.
- Clean wheel install resolves packaged dashboard static assets outside the repo checkout.
- `aming-claw launcher` creates a launcher artifact without starting services.
- Governance can be started explicitly and dashboard can be opened.
- macOS project import directory chooser either works or falls back clearly to manual path entry.
- Static route E2E probe passes.
- Any Mac-only bug discovered has a backlog row, graph-first investigation, test/E2E impact decision, commit, scope reconcile, and closure if fixed.
