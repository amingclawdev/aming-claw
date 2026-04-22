# Dual-Restart Runbook: governance + service_manager

## When This Applies

This manual procedure is required when `affected_services` includes **both**
`governance` and `service_manager`. Automated redeploy cannot handle this case
because:

1. Governance cannot restart itself (mutual-exclusion guard).
2. Service manager restart is triggered by governance.
3. If both need restart, there is no single orchestrator that survives the
   restart sequence.

## Symptoms

- `run_deploy` returns an error containing
  `"Cannot auto-redeploy governance + service_manager simultaneously"`
- Deploy report includes `dual_restart_required: true`

## Manual Procedure

### Prerequisites

- SSH/RDP access to the host machine
- Knowledge of the expected git HEAD (the commit that triggered the deploy)

### Steps

1. **Stop service_manager first**

   ```powershell
   # Windows
   Get-Process python | Where-Object { $_.CommandLine -match "service_manager" } | Stop-Process -Force

   # Linux
   pkill -f "agent.service_manager"
   ```

2. **Restart governance**

   ```powershell
   # Windows (host-mode)
   .\scripts\start-governance.ps1

   # Linux
   python -m agent.governance.server &
   ```

3. **Wait for governance health check**

   ```bash
   curl http://localhost:40000/api/health
   # Should return {"status": "ok", ...}
   ```

4. **Start service_manager**

   ```powershell
   # Windows
   .\scripts\start-manager.ps1

   # Linux
   python -m agent.service_manager &
   ```

5. **Verify both are healthy**

   ```bash
   curl http://localhost:40000/api/health
   # Check governance is up

   # Service manager health: check process tree
   # (ServiceManager does NOT bind any port — verify via process tree)
   ```

6. **Update chain_version in DB**

   ```bash
   curl -X POST http://localhost:40000/api/version-update/aming-claw \
     -H "Content-Type: application/json" \
     -d '{"chain_version": "<expected_head>", "updated_by": "manual-dual-restart"}'
   ```

## Post-Procedure Verification

- `GET /api/version-check/aming-claw` returns `ok: true, dirty: false`
- `GET /api/health` returns 200
- Service manager process is running (check process tree)
- Executor is claiming tasks normally

## Rollback

If the restart fails partway through:

1. Kill all Python processes related to aming-claw
2. Start governance first, then service_manager
3. Do NOT update chain_version until both are confirmed healthy

## Related

- OPT-BACKLOG-DEPLOY-DECOUPLE-MUTUAL-REDEPLOY (parent feature)
- PR-1: service_manager HTTP skeleton
- PR-2: governance redeploy endpoints (this PR)
- PR-3: full cut-over (removes legacy restart paths)
