# External Governance Demo

Small mixed-language fixture project used to verify that aming-claw can scan,
index, bootstrap, and inspect an external repository without relying on its own
governance files.

It intentionally includes:

- Python package code under `src/demo_app/`
- Python tests under `tests/`
- TypeScript dashboard/client code under `web/`
- JavaScript compatibility shim under `web/widget.js`
- Contract and state assets under `contracts/` and `state/`
- L4-style docs under `docs/l4/`
- A local `.aming-claw.yaml` project config

Smoke commands:

```bash
npm run generate
python -m pytest tests -q
npm test
```

The fixture is materialized from the aming-claw repo-level artifact
`docs/fixtures/external-governance-demo/l4-smoke-fixture.md`. Add future E2E
scenarios by extending the `governance-hint` block and the fenced file blocks in
that L4 artifact, then run `npm run generate`.

Graph smoke coverage:

- Python fan-out: `quote_breakdown` calls pricing, shipping, tax, and compliance
  helpers.
- Python fan-in: routes and UI summary helpers all converge on the same quote
  breakdown contract.
- TypeScript fan-out: `buildQuoteView` fans out to subtotal, discount, tax, and
  badge helpers.
- TypeScript fan-in: checkout helpers and renderers reuse the quote view.
- JavaScript support: `web/widget.js` gives the graph builder a plain JS file
  beside the TypeScript sources.
- L4 assets: quote schema, pricing state, and coverage docs are present so the
  dashboard can display code, docs, tests, config, and contract files together.
