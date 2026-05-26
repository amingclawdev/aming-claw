# HN Demo Audits

This directory stores repeatable launch-rehearsal reports for the HN demo.

Run from the Aming Claw plugin checkout while governance is already running:

```bash
node frontend/dashboard/scripts/e2e-hn-demo.mjs --sandbox-audit --no-browser
```

The sandbox audit uses a run-specific fixture project and writes:

- `latest.md` - human-readable report with raw evidence, machine audit,
  same-observer self-review, and launch recommendation.
- `latest.json` - machine-readable evidence bundle.
- `<run-id>.md` / `<run-id>.json` - immutable run artifacts when the default
  report path is used.

The fixture setup must stay empty: it creates only a demo project, baseline git
commit, project bootstrap, and active graph. Backlog rows, timeline events,
contracts, worker fences, trace ids, tests, reconcile evidence, and review
judgment must be produced by the observer path during the audit.
