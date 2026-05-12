# Quote Contract

Layer: L4 domain contract

Contract id: `quote.v1`

Primary code:

- `src/demo_app/service.py::quote_breakdown`
- `src/demo_app/service.py::export_quote_payload`
- `web/checkout.ts::createCheckoutPayload`

Tests:

- `tests/test_service.py`
- `tests/test_routes.py`
- `tests/smoke.test.mjs`
- `tests/widget.test.mjs`

The quote contract accepts a list of item lines and returns subtotal, discount,
tax, shipping, total, and compliance flags. Python routes and TypeScript
checkout helpers both fan in to this same contract shape so the dashboard can
verify that docs, code, tests, and schema assets are attached to the same
feature area.

Schema asset: `contracts/quote.schema.json`
