# Codex Bootstrap Scripts

Raw installer copy-paste scripts for when `aming-claw` is not on `PATH` yet and
you want a clean install without Codex doing the work itself. The main README's
[Quick Start](../../README.md#quick-start) and
[Install Details](../../README.md#install-details) explain the high-level flow;
this doc holds the verbatim bootstrap text.

## Codex prompt (Windows PowerShell)

Paste the following into Codex when `aming-claw` is not available on `PATH`:

````text
Please install Aming Claw from GitHub:
https://github.com/amingclawdev/aming-claw

If `aming-claw` is not available on PATH, run this Windows PowerShell bootstrap:
$installRoot="$env:USERPROFILE\.aming-claw\plugins"
$root=Join-Path $installRoot "aming-claw"
$dst="$env:TEMP\install_aming_claw.py"
$py="python"
$version=& $py --version 2>&1
if ($version -notmatch "Python 3\.(9|1[0-9])|Python [4-9]\.") {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $candidate=& py -3 -c "import sys; print(sys.executable)"
    if ($candidate) { $py=$candidate.Trim(); $version=& $py --version 2>&1 }
  }
}
if ($version -notmatch "Python 3\.(9|1[0-9])|Python [4-9]\.") {
  throw "Aming Claw requires Python 3.9+. Current python is: $version. Install Python 3.9+ or pass a full Python path."
}
Invoke-WebRequest https://raw.githubusercontent.com/amingclawdev/aming-claw/main/scripts/install_from_git.py -OutFile $dst
& $py $dst https://github.com/amingclawdev/aming-claw --install-root $installRoot --python $py

Run the read-only aftercare check:
cd "$root"
& $py -m agent.cli plugin doctor --plugin-root "$root" --skip-governance --python $py

Do not run `aming-claw start` or installer `--start` inline in Codex unless you
only need the idempotent health check. If governance is already healthy it exits
after printing the dashboard URL; otherwise it starts the foreground service and
should run in a separate terminal/window:
Start-Process powershell -ArgumentList "-NoExit","-Command","cd `"$root`"; & `"$py`" -m agent.cli start"

Reload Codex or open a new Codex session after plugin install; the current
thread may not hot-load newly installed skills or MCP tools.

After install, open http://localhost:40000/dashboard, load the Aming Claw
skill/MCP in a new session, then check `runtime_status(project_id="<id>")`,
`graph_status`, and backlog before changing code.
````

## macOS / Linux (raw installer)

```bash
curl -fsSL https://raw.githubusercontent.com/amingclawdev/aming-claw/main/scripts/install_from_git.py \
  -o /tmp/install_aming_claw.py
python3 /tmp/install_aming_claw.py https://github.com/amingclawdev/aming-claw
cd ~/.aming-claw/plugins/aming-claw
python3 -m agent.cli plugin doctor --plugin-root ~/.aming-claw/plugins/aming-claw --skip-governance
nohup python3 -m agent.cli start > ~/.aming-claw/aming-claw.log 2>&1 &
```

If `python3 --version` is below 3.9, rerun the installer and doctor with a
Python 3.9+ executable path using `--python /path/to/python3.9-or-newer`.

## What the installer does

`aming-claw plugin install <git-url>` and `scripts/install_from_git.py` do the
same end-to-end work:

- Clone or update the plugin checkout under `~/.aming-claw/plugins/<slug>`.
- Install the Python package (`pip install -e .`) so the CLI and MCP server are
  importable.
- Write Codex config + a generated local marketplace + a versioned plugin
  cache at `~/.codex/plugins/cache/aming-claw-local/aming-claw/<version>/` that
  real Codex CLI startup reads.

Run `aming-claw plugin doctor` after install to verify all of the above landed
correctly.
