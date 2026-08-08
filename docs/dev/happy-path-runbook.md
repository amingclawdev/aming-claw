# Aming Claw v2 happy-path and release runbook

This runbook is the operator boundary for the v2 release line. It turns the
successful `mf_parallel` and `mf_batch_parallel` dogfood worlds into a
repeatable release gate without treating chat memory, screenshots, or a bare
Git commit as authority.

## Invariants

- Keep `WIP=1` for release-blocking rows. Finish or formally archive the current
  row before entering mutation on the next one.
- Backlog-first and graph-first are mandatory before changing source, docs,
  packaging, Docker assets, or runtime state.
- A worker writes worker evidence. An observer does not synthesize read,
  startup, implementation, commit, attestation, or finish-gate PASS.
- A QA PASS uses `event_kind=independent_verification`, a top-level passing
  `status`, the full immutable candidate SHA, and an exact candidate graph
  trace. A nested verdict alone is not PASS.
- Raw session, route, worker, QA, and fence tokens never enter source, shell
  output, timeline payloads, backlog rows, or release artifacts. Persist only
  server-issued copy-safe refs.
- The merge queue is ordered shared state. Parallel implementation does not
  authorize parallel merge, graph activation, deployment, or close.
- Never edit `VERSION` manually. The release commit carries a canonical trailer,
  for example:

  ```text
  Chain-Source-Stage: observer-release
  ```

## Preflight

From a clean checkout:

```bash
aming-claw plugin doctor
python scripts/e2e-happy-path-smoke.py --preflight
python -m pytest -q agent/tests/test_release_artifacts.py
git diff --check
```

Then read governance through MCP:

1. `runtime_status(project_id="aming-claw")`
2. `graph_status(project_id="aming-claw")`
3. `graph_operations_queue(project_id="aming-claw")`
4. the exact backlog row and its current ContractRuntime/guide

The acceptable runtime identity is exact: Git HEAD, health
`runtime_loaded_version`, loaded source hash, and `runtime_stale=false` must
agree. Process start time and `.pyc` inspection are not deployment proof.

## `mf_parallel` gate order

1. Enter through `onboard_route_guide`; do not guess `target_ref`, worker count,
   or contract revision.
2. Allocate the bounded RuntimeContext and join using the exact server-issued
   worker/host identity.
3. Record context-local read receipt and startup before implementation.
4. Query the graph from the worker session and retain DB-verified trace IDs.
5. Submit canonical implementation results. JSON boolean `passed: true` is a
   verdict; integer `passed: 1` is not.
6. Commit the exact clean worker HEAD, then record finish-time attestation and
   finish gate.
7. Run independent QA against an exact non-activated candidate graph.
8. Materialize the merge-queue item as observer. Use the ContractRuntime
   execution id as `task_id`; a batch plan-row id is not a worker identity.
9. Run merge preview, merge once, current-full reconcile, and postmerge QA.
10. Append close-ready only after runtime/deployment and live regression evidence
    are current, then use protected backlog close.

## `mf_batch_parallel` gate order

The child lifecycle is identical, with these additional constraints:

1. The batch planner owns `merge_queue_id`, row order, overlap edges, and
   integration epoch. A caller never invents them.
2. Disjoint workers may implement concurrently. Overlapping file/node fences
   serialize in merge-queue order.
3. For observer materialize, use
   `{task_id: <child ContractRuntime execution>, require_finish_gate: true,
   checkpoint_id: <context checkpoint>, fence_token: null}`. The observer does
   not possess the worker fence.
4. Postmerge QA binds to the shared batch reconcile commit, not a child-local
   pre-final merge head.
5. The final batch reconcile is shared close authority. It must not grant merge
   credit to an explicitly released unlandable child.
6. A recoverable failed child gets a fresh `failed_qa_rework` task and worker
   identity. Preexisting task/worker identities, standalone rework markers, and
   route/queue authority changes at the write boundary fail before projection.

## Deterministic release replay

Run both layers:

```bash
python scripts/e2e-happy-path-smoke.py \
  --base-url http://127.0.0.1:40000 \
  --run-regressions \
  --output /tmp/aming-claw-v2-happy-path.json
```

The result must contain:

- `passed: true`
- exactly two worlds: `mf_parallel` and `mf_batch_parallel`
- every backlog `FIXED` at its expected full close commit
- `formal_no_pass_event_count: 0` for every row
- active graph commit equal to the close commit, `graph_stale: false`, and
  pending scope reconcile count zero
- all isolated route regression node IDs passed inside the bounded timeout
- `raw_credentials_required: false` and
  `writes_performed_by_http_replay: false`

This is evidence replay plus isolated logic replay. It does not reopen or write
the canonical dogfood projects.

## Docker self-smoke

The compose service is opt-in so it cannot silently replace host governance:

```bash
export AMING_CLAW_BUILD_COMMIT="$(git rev-parse HEAD)"
export GOVERNANCE_PORT="${GOVERNANCE_PORT:-40001}"
docker compose -f docker-compose.governance.yml \
  --profile governance-demo config
docker compose -f docker-compose.governance.yml \
  --profile governance-demo build governance
docker compose -f docker-compose.governance.yml \
  --profile governance-demo up -d redis governance
curl --fail "http://127.0.0.1:${GOVERNANCE_PORT}/api/health"
docker compose -f docker-compose.governance.yml \
  --profile governance-demo down -v
```

The profile uses the `governance-demo-data` named volume and does not mount the
host `shared-volume` tree. For a bootstrap demo, use a disposable clean Git
repository inside the container; never point a container smoke at the host
`aming-claw` governance DB. The minimum assertions are install success,
governance health, project bootstrap, graph status, and backlog write/read in
the isolated project. The release evidence replay remains read-only. `down -v`
removes only the profile's disposable container state.

## Independent QA and release

1. Freeze the full candidate SHA and build a non-activated exact candidate
   graph.
2. Register bounded QA for the exact backlog/task/commit tuple.
3. Run release artifact tests, plugin validation, compose config, happy-path
   replay, focused governance suites, and a strictly bounded full graph file.
4. Materialize QA PASS through the managed QA session. Do not use observer
   route authority as QA authority.
5. Merge or commit with the chain trailer, redeploy through ServiceManager, and
   re-read runtime status.
6. Activate the exact current-full graph and require pending scope reconcile
   zero.
7. Rerun the happy-path smoke after deploy.
8. Push the exact deployed commit and create annotated tag `v0.2.1` only if its
   target equals the deployed/full-graph commit. The audit-archived `v0.2.0`
   tag is immutable and must never move.
9. Install the local Codex plugin cache from that exact tag with the
   plugin-creator cachebuster workflow, validate the manifest, reinstall from
   the configured local marketplace, require plugin doctor to pass, and test
   from a new Codex task.
10. Append postdeploy verification and close-ready. Only after that evidence,
    close release rows normally; the publish, install, and doctor evidence must
    already be durable.

## Troubleshooting and known baselines

- `/api/health` healthy but `/dashboard` is `503`: dashboard assets are absent;
  restore `agent/governance/dashboard_dist/index.html` or build the frontend.
- Port check passes but clients reset: inspect file-descriptor limits and the
  manager-owned process; a listening socket is not request health.
- `HEAD != CHAIN_VERSION`: confirm the release commit trailer. Deprecated
  version-sync DB writes are not chain authority.
- QA graph rejects a non-descendant candidate: build an exact non-activated
  candidate snapshot; do not reuse an old active graph as PASS authority.
- `route_token_ref_unknown` or QA ref unknown across processes: register the
  managed ref in the process that performs the protected write. Never expose a
  raw token to move it between processes.
- Full `test_graph_governance_api.py` exceeds the release time budget: terminate
  it at the declared bound, record progress/failures/residual-process state, and
  compare any failures against the exact immutable parent. Do not call a
  bounded partial run an unbounded full-suite PASS.
- Historical bypass diagnostics remain audit-only. Archive them with no-PASS
  evidence; never relabel them `FIXED` or synthesize successful worker/QA lines.
