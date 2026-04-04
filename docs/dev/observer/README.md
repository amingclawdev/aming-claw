# Observer Runtime Notes

This directory holds local observer runtime helpers and logs for the docs-architecture monitoring flow.

Rules:
- Runtime helpers for observer-only monitoring can live here.
- Generated logs should stay under `docs/dev/observer/logs/`.
- Do not treat runtime log files as canonical product documentation.

Memory guidance:
- Good candidates for long-term memory are stable workflow lessons, recurring blockers, and approved operating rules.
- Bad candidates are transient task IDs, short-lived queue states, and one-off observer polling snapshots.
