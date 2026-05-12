# Dashboard E2E Demo

Small isolated project for dashboard semantic, cancel, review, and project
switching tests. The parent `aming-claw` graph excludes `examples/**`; register
and bootstrap this directory as its own governance project before broad UI E2E:

```bash
curl -s -X POST http://localhost:40000/api/project/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"workspace_path":"examples/dashboard-e2e-demo","project_name":"dashboard-e2e-demo"}'

cd frontend/dashboard
node scripts/e2e-semantic.mjs --project dashboard-e2e-demo --probe
```
