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
- Use the same created environment for the direct-main, MF Parallel, and MF
  Batch Parallel panels.

## Browser-created environment

1. Confirm the Daily Planner Lite template and full Owner runtime RC are
   visible before creation.
2. Click **Create environment** once. Do not substitute a CLI or API call.
3. Wait for the environment card to report **Ready**.
4. Confirm the card shows the same full Owner runtime RC separately from the
   shortened fixture Baseline.
5. Capture `01-environment-created.png` with project, environment, Owner
   runtime RC, Baseline, and Ready status visible.

Stop on a create error, runtime mismatch, stale runtime, missing RC identity,
unexpected duplicate environment, or any credential-shaped text in the view.

## Three simultaneously running lanes

Launch each copy-safe panel through its displayed instructions and normal
Aming Claw authority:

1. Direct Main
2. MF Parallel
3. MF Batch Parallel

All three lanes must be running at the same time for the dashboard captures.
Each screenshot must visibly bind one distinct lane/task/runtime identity to
the same environment and immutable Owner runtime RC:

- `02-direct-main-running.png`
- `03-mf-parallel-running.png`
- `04-mf-batch-parallel-running.png`

Refresh immediately before each capture. Stop instead of retrying or guessing
authority when a lane, RuntimeContext, worker host envelope, merge queue, graph,
or ContractRuntime gate rejects the next action.

## Privacy and evidence checklist

- [ ] No raw session, fence, route, QA, observer, API, or bearer credentials.
- [ ] No private token-shaped identifiers, home-directory secrets, or terminal
      environment values.
- [ ] Full Owner runtime RC is visible and identical in all four images.
- [ ] Fixture Baseline is visibly separate from Owner runtime RC.
- [ ] Each running image shows a distinct lane/task/runtime identity and state.
- [ ] Filenames use the exact numbered convention above.
- [ ] Captions bind the images to the same Browser-created environment.
- [ ] Ordinary ContractRuntime, graph, test, QA, and timeline refs are recorded
      separately; screenshots are supplementary only.

The public Slack community URL must be supplied explicitly by the operator
before it is linked from this runbook or the root README. Never infer it from a
screenshot, local configuration, or an unrelated community link.
