# Test Coverage

Layer: L4 test coverage contract

Coverage map:

- `tests/test_service.py` covers pricing helpers, quote breakdown, and export
  payload fan-in.
- `tests/test_routes.py` covers route functions that share the quote breakdown.
- `tests/smoke.test.mjs` checks that Python, TypeScript, docs, schema, and state
  assets are all present for graph indexing.
- `tests/widget.test.mjs` checks that TypeScript fan-in and fan-out functions
  remain visible to the graph adapter.

Expected smoke commands:

```bash
python -m pytest tests -q
npm test
```
