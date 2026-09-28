# Snapshot duplicate maintenance

This is explicit operator maintenance through the native cleanup API. It does
not delete snapshot paths or content, install a timer, or run during graph
activation. `dimension=all` keeps its existing behavior and excludes COW.
Graph reads continue to use the local files after maintenance, even when the
archive is offline. Archive pruning is outside this feature.

Configure `project_config.governance.snapshot_cow_cleanup` in the registered
project configuration. `archive_root` names an existing writable directory on
the external APFS volume; `archive_mount` and `archive_volume_uuid` bind its
actual mounted identity. A directory on the source device is refused.
`max_snapshots` defaults to 1 (maximum 20), `max_pairs` to 2 (maximum 40), and
`max_hash_bytes` to 64 MiB (maximum 4 GiB). The byte budget counts both digest
reads. `snapshot_ids` optionally selects an exact nonempty subset of owned
superseded full snapshots within the snapshot limit; use it for a bounded
maintenance window in a larger history. Without selection, an over-limit
inventory or byte budget returns an incomplete preview with no applyable
omitted candidates. Change the selection or tighten the window and preview
again. Do not treat `st_blocks` as evidence that extents are already shared.
`bundle_manifests` optionally names at most 16 additional service-configured
manifest files; their snapshot identities are protected. The installed sealed
bundle manifest is also protected when present.

Authenticated observer/coordinator credentials are required for this dimension,
including read-only preview and recovery inspection. The credential's project
must match. Registered project root, governance storage world and physical DB
identity are checked; archive paths cannot be supplied in maintenance calls.

1. `GET /api/graph-governance/{project_id}/stale-artifact-cleanup?dimension=graph_snapshot_duplicates`
   returns an exact plan, opaque `operation_id`, `plan_revision`, `plan_hash`,
   candidate IDs, budgets, and logical/allocated estimates. It hashes only the
   recognized semantic graph/index pairs within its selected DB inventory.
2. `POST .../stale-artifact-cleanup/apply` supplies `dimension`, exact
   `operation_id`, `plan_revision`, `plan_hash`, and a nonempty explicit
   `candidate_ids` list from that plan. Native cleanup, SQLite writer and
   current-full locks cover live-pin revalidation and replacements. The native
   service DB descriptor is permitted; open pair files refuse replacement.
3. `POST .../stale-artifact-cleanup/recover` supplies only the project-scoped
   `operation_id` and `recovery_action` (`inspect` by default, `restore` explicit).
   Inspection exposes original and recovery journals and pending state.
   Restoration uses only known original/replacement or verified staged
   fingerprints and the retained verified archive. Unrelated drift, new live
   pins, archive/mount drift or pending persistence requires inspection and
   refuses overwrite. Original and recovery journals remain for diagnosis.

Both MCP adapters use `stale_artifact_cleanup` for preview. Use
`stale_artifact_cleanup_apply` with the same exact-plan fields for apply, or
with `dimension="graph_snapshot_duplicates"`, `mode="recover"`,
`operation_id`, and optional `recovery_action` for recovery. These modes retain
the existing bounded cleanup transport and timeout disposition. A transport
timeout is never a safe retry receipt; inspect the operation first.

For either MCP adapter, explicitly supply an existing observer/coordinator role
credential through the process environment variable `GOV_TOKEN`. Managed COW
dispatch requires it and sends it only in the HTTP `X-Gov-Token` header; the
standalone adapter uses its existing `GOV_TOKEN` header binding. The native
service validates credential lifetime, operator capability and project scope.
Keep the credential out of tool arguments, URLs, project configuration, receipts
and checked-in MCP configuration. Observer-session and route-token references
are separate identities and cannot substitute for this role credential. No
credential is issued automatically. Existing project/world and governance URL
checks remain in force; missing or rejected credentials do not grant anonymous
COW access. Ordinary cleanup dimensions keep their existing transport.

Before replacement, the service publishes a source-bound metadata/content backup
and verifies an isolated content/metadata restore on the configured external
volume. Apply forces `clonefile(CLONE_NOFOLLOW_ANY)` with no copy, symlink or
hardlink substitution. Each target keeps its own mode, uid/gid, mtime, flags,
ACL and xattrs; touched directories keep their identity and original mtime.
Target inode, birthtime and ctime necessarily change and are reported.
Recovery uses an ordinary verified content copy from the archive, not a dedupe
fallback. A matching completed exact operation replays without writes; a fresh
preview consults bounded per-pair native receipts to avoid cloning it again.

Fsynced per-pair phases preserve completed and ambiguous progress. The first
failure stops the operation; no blind retry is supported. A partial recovery
record is visible through inspect and remains an explicit hold, not an
automatic second overwrite. No API here deletes the retained backups/journals.
Completion verifies data and metadata independently of the observed free-space
delta, including zero or negative observations. Whole-operation available-byte
change and per-pair observations are separate, include background activity,
and do not claim exclusive block attribution or guaranteed physical reclaim.

The source verification uses small isolated real APFS clone fixtures and mock
external volume boundaries, canonical DB schemas, HTTP authorization and both
MCP transports. It does not apply maintenance to production snapshots.

## Explicit periodic DEV maintenance

The certified native AC DEV governance process supports an optional registry
policy at `governance.snapshot_cow_cleanup_periodic`. It defaults to disabled.
The DEV service owns its maintenance thread only after the existing singleton
lease and manager certification. This narrow capability leaves general
`background_workers_enabled=false`; it starts no Redis, chain, graph, backfill,
cron, daemon or Judgment Brain worker. Stable processes cannot use this entrance
to operate on the DEV-owned AC database. Project root, governance world and
physical database are derived and verified by the service.

Use the existing operator role credential in the `X-Gov-Token` header for all
these endpoints, with the exact project `aming-claw`:

- `GET /api/graph-governance/{project_id}/snapshot-cow-periodic/config` returns
  the effective policy, revision and `source=aming_claw_registry`.
- `PUT .../snapshot-cow-periodic/config` accepts exactly `expected_revision`
  and `policy`. For example, `{"expected_revision":0,"policy":{"enabled":false}}`
  durably saves a disabled policy without activating cleanup. A later explicit
  enablement is a separate operator decision. The update uses a central locked
  compare-and-swap, atomic file replacement, file and parent-directory fsync,
  and exact readback. An old metadata/config writer cannot restore a prior
  enabled policy over an acknowledged disable. Workspace YAML is not a live
  update entrance for this timer.
- `GET .../snapshot-cow-periodic/status` distinguishes source `supported`,
  persisted `configured`, and actually in-flight `active`. It reports cadence,
  effective window, timestamps, outcome, operation ID, candidate/refusal counts,
  cursor/sweep completeness, digest bytes, hold status and the next action.
  It omits credentials, absolute storage paths and broad configuration blobs.
- `POST .../snapshot-cow-periodic/release-hold` releases an exact scheduler hold
  only after explicit native inspection/recovery and verified terminal proof.
  It does not apply, restore, delete, resume or replay any native operation.

Policy fields are strict and unknown fields refuse. `enabled` is a boolean
(default false); `interval_seconds` is an integer from 60 to 86400 (default
3600). `snapshot_ids` is an optional allowlist and `exclude_snapshot_ids` an
optional exclusion list, each at most 1000 unique valid snapshot components.
`max_snapshots_per_run` is 1..20 (default 1) and `max_pairs_per_run` is 2..40
(default 2), with at least two pairs per configured snapshot cap. These are
caps: this implementation actually selects **one complete two-pair snapshot
window per tick**, also narrowed by the engine's existing budgets. It never
widens those native budgets or swaps the engine configuration around a call.

`max_hash_bytes_per_run` is an aggregate digested-byte cap from 1 byte to
128 GiB (default 64 GiB). The immutable native candidate-byte budget is
`min(engine max_hash_bytes, aggregate cap / 32)`; the engine hard maximum
remains 4 GiB. A conservative 32 times source-plus-target byte reservation
precedes proof reads and apply admission. Every actual native digest read,
including repeated fresh, backup, copy verification and completion proofs,
debits a scoped run meter. A 64 GiB aggregate cap admits a candidate window of
about 1.60 GiB when the engine has separately been configured to admit that
size; 128 GiB can admit the 4 GiB native hard maximum. This arithmetic is not
proof of candidate eligibility, timing or physical space benefit. A 60-second
internal deadline gates page admission and native proof checkpoints; it never
forces cancellation of an irreversible file replacement or filesystem syscall.

The first due time is now plus the interval, including after enablement or
cadence changes. There is no startup replay or accumulated catch-up queue.
Each due tick reads a bounded page of 20 superseded full snapshot metadata rows
ordered by `created_at,snapshot_id`. Protected, completed, excluded, missing or
oversized rows advance the cursor, so an oldest unsuitable row cannot starve
later history. The cursor wraps only after a proved end of inventory. A page
or sweep that remains incomplete is reported as incomplete. Previously
protected rows can be revisited on a later sweep. No operational snapshot IDs
are embedded in source.

Explicit enabled configuration durably delegates only this native maintenance
capability to the certified same-world process. A bounded hash of the operator
principal and the server-derived custody are stored; no role/session/route
token is stored, renewed or minted. Revoking the configuration caller's token
alone does not revoke this durable policy. Use authenticated `enabled=false`
to stop future admissions. A running call keeps its frozen policy/selection;
normal shutdown wakes the loop, stops admission and joins the owned call before
releasing manager/singleton custody.

If the native source-custody guard reports an exact source-binding/identity
unavailability before any run intent or engine admission (for example during
an uncommitted source update), status reports temporary zero-effect deferral.
A later clean/synced ordinary tick can proceed. Existing held or pending intent
still dominates this availability result and remains held.

A process-safe non-expiring advisory maintenance lock serializes periodic
admission, while the existing native destructive-cleanup, SQLite writer,
current-full and stable-queue locks still cover apply and resolution checks.
Before apply, the scheduler fsyncs exact running intent with operation, plan,
selection, policy revision and custody. Native engine policy/root/world/DB drift,
live pins, APFS/archive mount/UUID/device/capacity, ACL, backup and readback
checks remain authoritative. Timer-only changes do not alter the engine policy
hash or invalidate legacy native manual receipts and explicit recovery.

Running/partial/ambiguous state, scheduler or engine pending persistence,
unknown journals, incomplete readback or unexpected exceptions require explicit
inspection. Restart preserves that hold and performs no blind automatic apply,
restore or recomputation. The readiness inventory reads at most 1000 native
metadata directory entries, counting original/recovery journals, pending
variants and completion indexes together; an unvisited suffix refuses. It does
not rehash completed historical snapshot bodies. A completion index alone is
not terminal proof for an unresolved original operation.

A known native pre-journal environmental refusal such as an absent external
archive is deferred only after verifying no journal/pending/stage/effect and
unchanged source/target fingerprints. It can be reevaluated at a later ordinary
interval. A generic `writes_performed=false` wrapper error is not that proof.
Budget-incomplete windows also defer without effects and advance the cursor.
Exhaustion or uncertainty after mutation creates an inspection hold.

After explicit native inspection and, if needed, authenticated native restore,
status advertises `hold_release_available=true` and an exact
`release_hold_request` only when current terminal fingerprints and a complete
bounded inventory verify. POST that request with `operation_id`,
`expected_scheduler_revision`, `expected_scheduler_receipt_hash`, and
`expected_policy_revision`. Stale or mismatched expectations refuse without
clearing the hold. Admissible proofs are a completed original operation, a
completed recovery bound to the retained original journal (which may still say
partial), or exact original preimages with no unknown stage (`original_noop`).
All candidates, source/directory identities and current content must verify.

Release fsyncs an append-only acknowledgement bound to held intent, custody,
original/recovery journal hashes, exact resolved candidates and operator
principal hash before advancing the scheduler revision. Journals and backups
remain retained. Every later tick/restart validates those acknowledgements,
classifies the retained resolved originals and skips their exact old candidates;
changing interval or disabling/re-enabling does not discard this proof or cause
restored old work to replay. Missing, invalid or drifted acknowledgements block
readiness. Uncertain release persistence retains the inspection hold.

`complete` proves native data/metadata readback. `observed_net_delta`, if present,
is a separate free-space observation, and no status field claims reclaimed
physical bytes. Source delivery and tests do not enable any actual policy or
apply cleanup to operational snapshots.
