# Context Registry

The context registry is a local role-scoped context layer for Aming Claw. It is
not a replacement for skills. Skills remain the public operating contract;
context packs add local, project-specific, or private judgment that can be
resolved at observer or worker startup.

## Why It Exists

Observer sessions often need more than source-controlled documentation. They may
need product principles, expert review rules, demo-specific intent, or private
operator preferences. Putting all of that into public skill files creates two
problems:

- private strategy can leak into plugin payloads, worker prompts, logs, or
  public docs;
- workers receive broad judgment context when they only need scoped contract
  facts.

The registry separates those layers.

## Context Classes

- `public_skill`: safe, source-controlled context that can appear in skills or
  docs.
- `internal_product`: product or project guidance that is safe for observers and
  selected internal workers.
- `task_context`: backlog-, mode-, or contract-bound context.
- `private_founder`: local observer-only context. V1 rejects attempts to allow
  this visibility for workers.

## Resolution Contract

Callers resolve context with:

- `project_id`
- `role` such as `observer`, `mf_sub`, `dev`, `test`, `qa`, or `merge`
- optional `mode`
- optional `backlog_id`

The resolver checks local DB packs first, then source-controlled fallback docs.
Resolution output includes pack ids, versions, hashes, source types, redaction
status, and a `context_text` field for prompt assembly. Resolution events store
pack metadata and hashes, not private pack bodies.

## Private Import Flow

Private context should be imported from a local file into the governance DB:

```text
context_pack_seed_private_file(
  project_id="aming-claw",
  source_path="/Users/yingzhang/private-notes.md"
)
```

The source file body is stored in the local governance database and marked
`private_founder`, `observer` only, `no_export=true`. The body must not be
committed to git or copied into public skill docs.

## Worker Injection Rule

Observer prompts may receive private observer-only packs. Worker prompts should
receive only resolved context whose visibility and role scope explicitly allow
that worker role. In V1, `private_founder` is blocked for every non-observer
role even if a caller tries to override `allowed_roles`.

The intended pattern is:

```text
private judgment -> observer interpretation -> scoped contract -> worker prompt
```

The worker receives the scoped contract, not the private reasoning.

## Source-Controlled Fallback

The default fallback document is:

```text
Archive/skills/aming-claw/references/observer-context-safe.md
```

It contains observer-safe expertise routing rules and can be shipped in the
plugin. The private strategy version stays outside git and can be imported into
the local DB when the owner wants observer sessions to use it.
