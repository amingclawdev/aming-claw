# External Governance Demo

Small mixed-language fixture project used to verify that aming-claw can scan,
index, bootstrap, and inspect an external repository without relying on its own
governance files.

It intentionally includes:

- Python package code under `src/demo_app/`
- Python tests under `tests/`
- TypeScript dashboard/client code under `web/`
- Contract and state assets under `contracts/` and `state/`
- L4-style docs under `docs/l4/`
- A local `.aming-claw.yaml` project config

Smoke commands:

```bash
python -m pytest tests -q
npm test
```

Graph smoke coverage:

- Python fan-out: `quote_breakdown` calls pricing, shipping, tax, and compliance
  helpers.
- Python fan-in: routes and UI summary helpers all converge on the same quote
  breakdown contract.
- TypeScript fan-out: `buildQuoteView` fans out to subtotal, discount, tax, and
  badge helpers.
- TypeScript fan-in: checkout helpers and renderers reuse the quote view.
- L4 assets: quote schema, pricing state, and coverage docs are present so the
  dashboard can display code, docs, tests, config, and contract files together.
