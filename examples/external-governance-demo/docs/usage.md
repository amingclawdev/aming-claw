# Usage

The quote route accepts a JSON payload with an `items` list and returns the
calculated total.

```json
{
  "customer_tier": "member",
  "region": "CA-ON",
  "items": [
    { "sku": "book", "price": 10, "quantity": 3 },
    { "sku": "battery", "price": 5, "quantity": 2, "hazmat": true }
  ]
}
```

Available routes:

- `quote_order(payload)` returns the total only.
- `quote_breakdown_route(payload)` returns subtotal, discount, tax, shipping,
  total, and compliance flags.
- `quote_summary_route(payload)` returns a compact string for UI cards.

Related L4 assets:

- `docs/l4/quote-contract.md` explains the request and response contract.
- `docs/l4/pricing-state.md` describes pricing state and compliance flags.
- `docs/l4/test-coverage.md` maps smoke tests to the code paths.
- `contracts/quote.schema.json` is the file-backed schema used by dashboard
  smoke checks.
