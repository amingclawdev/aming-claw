# Review Contract Packs

Status: V1 implementation notes, 2026-05-30

Backlog:

- `REVIEW-CONTRACT-PACKS-V1-20260528`
- `AMING-DEV-EXPERT-REVIEW-PACKS-20260530`

## Decision

Specialized reviews use source-controlled review packs instead of ad hoc prompt
text. A pack declares the review purpose, required inputs, artifact references,
forbidden assumptions, review dimensions, machine-readable output fields,
allowed severities, gate decisions, and backlog conversion hints.

The V1 pack registry is read-only and file-backed. It does not add a mutable
template authoring UI, project override store, or automatic approval path.

## Pack Files

Review packs live in:

```text
agent/governance/contract_templates
```

The first development packs are:

- `architecture_data_continuity_review.v1`
- `frontend_ui_implementation_review.v1`
- `qa_evidence_gate_review.v1`

`review_pack.schema.json` documents the JSON shape. The contract template
registry intentionally skips `*.schema.json` files so schema artifacts do not
load as executable templates.

## Runtime Helpers

`agent.governance.review_contracts` exposes deterministic helpers:

- `list_review_packs`
- `get_review_pack`
- `resolve_review_pack`
- `validate_review_output`

Validation checks the pack id, gate decision, finding shape, severity,
`evidence_refs`, `acceptance_impact`, and each finding's
`backlog_conversion_hints`.

## MCP Surface

The MCP dispatcher exposes read-only review pack tools:

- `review_pack_list`
- `review_pack_get`
- `review_pack_resolve`
- `review_pack_validate_output`

These tools do not run a model and do not mutate governance state. They make the
selected contract and validation result explicit before observer, expert, or QA
lanes use review findings.

## Observer Flow

1. Resolve the pack by `template_id`, `task_type`, `stage`, or `version`.
2. Collect concrete artifacts listed by the pack.
3. Run the review outside implementation.
4. Validate the structured review output.
5. Convert accepted findings into backlog rows, acceptance criteria, or close
   gate follow-ups.

Blocking findings remain a gate input. A review pack never auto-approves product
or architecture decisions.
