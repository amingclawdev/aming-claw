# Three-lane Browser dogfood

This runbook captures the visual supplement for the formal R5 release gate. It
does not replace ContractRuntime, graph, tests, independent QA, or close-gate
evidence.

## Release identity and scope

- Open `http://127.0.0.1:40000/dashboard?project_id=aming-claw&view=demo` in the
  in-app Browser.
- Stop if runtime status is not strict, the Demo page does not show the full
  40-character **Owner runtime RC**, or that value differs from the immutable
  release candidate under review.
- Treat **Baseline** as the generated Daily Planner fixture commit. It is not
  the owner governance runtime or release candidate.
- Create three independent environments, one for each lane. Pin all three to
  the same exact Owner runtime RC, then run them strictly in this order:
  Direct Main -> MF Parallel -> MF Batch Parallel.
- One environment is exclusive to one lane. Never create overlapping OPEN
  rows for multiple lanes in the same environment.

## Browser-created environments

For each lane, immediately before starting that lane:

1. Confirm the Daily Planner Lite template and full Owner runtime RC are
   visible before creation.
2. Click **Create environment** once. Do not substitute a CLI or API call.
3. Wait for the new environment card to report **Ready**.
4. Confirm the card shows the same full Owner runtime RC separately from the
   shortened fixture Baseline.
5. Bind that environment to exactly one lane and do not use either of the
   other two launch panels on it.

At the end of the run there must be three distinct environment ids. Their full
Owner runtime RC values must be identical to the release candidate under
review.

Stop on a create error, runtime mismatch, stale runtime, missing RC identity,
unexpected duplicate environment, or any credential-shaped text in the view.

## Three strictly sequential lanes

Launch the server-provided copy-safe panel through normal Aming Claw authority.
Finish and close each lane before creating or starting the next one:

1. Direct Main in environment A.
2. MF Parallel in environment B, only after Direct Main is closed.
3. MF Batch Parallel in environment C, only after MF Parallel is closed.

Capture Ready, Running, and Closed states for each lane. Each image must bind
one distinct lane/task/runtime identity to its exclusive environment and the
same immutable Owner runtime RC:

- `01-direct-main-environment-ready.png`
- `02-direct-main-running.png`
- `03-direct-main-closed.png`
- `04-mf-parallel-environment-ready.png`
- `05-mf-parallel-running.png`
- `06-mf-parallel-closed.png`
- `07-mf-batch-parallel-environment-ready.png`
- `08-mf-batch-parallel-running.png`
- `09-mf-batch-parallel-closed.png`

Refresh immediately before each capture. Stop instead of retrying or guessing
authority when a lane, RuntimeContext, worker host envelope, merge queue, graph,
or ContractRuntime gate rejects the next action.

The overlapping-row duplicate Gate is frozen for this run. Do not use
`force_admit`, collapse lanes with `merge_into`, camouflage a duplicate by
changing its title, or turn a bypass/no-PASS result into warranty evidence.
`direct_fix` is retired and is never a recovery path. A Gate rejection ends the
affected lane until its Contract-consistent cause is corrected.

## Privacy and evidence checklist

- [ ] No raw session, fence, route, QA, observer, API, or bearer credentials.
- [ ] No private token-shaped identifiers, home-directory secrets, or terminal
      environment values.
- [ ] Full Owner runtime RC is visible and identical in all nine images.
- [ ] Fixture Baseline is visibly separate from Owner runtime RC.
- [ ] Each running image shows a distinct lane/task/runtime identity and state.
- [ ] Three distinct environment ids are visible; each is bound to one lane.
- [ ] Direct Main closed before MF Parallel started, and MF Parallel closed
      before MF Batch Parallel started.
- [ ] Filenames use the exact numbered convention above.
- [ ] Captions bind each lane to its own Browser-created environment.
- [ ] Ordinary ContractRuntime, graph, test, QA, and timeline refs are recorded
      separately; screenshots are supplementary only.

The public Slack community URL must be supplied explicitly by the operator
before it is linked from this runbook or the root README. Never infer it from a
screenshot, local configuration, or an unrelated community link.
