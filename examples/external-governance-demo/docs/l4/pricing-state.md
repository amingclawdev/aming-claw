# Pricing State

Layer: L4 state asset

State file: `state/pricing-rules.json`

Primary code:

- `src/demo_app/service.py::discount_for`
- `src/demo_app/service.py::tax_for`
- `src/demo_app/service.py::shipping_for`
- `src/demo_app/service.py::compliance_flags`

The smoke fixture keeps pricing rules in a JSON state asset and mirrors those
values in tiny pure functions. This gives scope-reconcile a useful mix of code,
state, docs, and tests without requiring an external service or database.

The fan-out path starts at `quote_breakdown` and reaches discount, tax,
shipping, and compliance helpers. The fan-in path starts at routes, summaries,
exports, and checkout helpers, then converges on the same quote breakdown.
