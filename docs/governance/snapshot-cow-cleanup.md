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
