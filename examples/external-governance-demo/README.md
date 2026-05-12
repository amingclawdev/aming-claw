# External Governance Demo

Small mixed-language fixture project used to verify that aming-claw can scan,
index, bootstrap, and inspect an external repository without relying on its own
governance files.

It intentionally includes:

- Python package code under `src/demo_app/`
- Python tests under `tests/`
- TypeScript dashboard/client code under `web/`
- A local `.aming-claw.yaml` project config

Smoke commands:

```bash
python -m pytest tests -q
npm test
```
