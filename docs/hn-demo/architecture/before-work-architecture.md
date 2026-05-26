# Before Work Architecture

## Fear

The fear before work is that the agent treats the repo as a bag of text. It may
find a relevant-looking file, miss the real owner, skip the test surface, or
invent a new subsystem when the project already has a pattern.

## Failure Mode

A plausible AI design can be wrong before code is written. The agent can build a
locally coherent component that ignores peer modules, existing service style,
owned files, accepted docs, or the required verification path.

## Architecture Invariants

**Graph before grep:** the agent should inspect commit-bound structure before
searching and editing. Grep is still useful, but it is not the authority for
ownership.

**Contract before mutation:** the work must have a backlog row or manual-fix
contract that names target files, acceptance criteria, required evidence, and
review boundaries.

**Existing pattern before new design:** the graph and contract should expose
peer modules, functions, docs, tests, and config so the agent sees what already
exists before inventing a new shape.

**Commit-bound truth:** the active graph is a projection of a specific commit.
Dirty workspace guesses do not become project memory.

## What The Dashboard Shows

- graph health and stale/current state;
- selected node, owner, files, functions, docs, tests, and config context;
- backlog contract with target files and acceptance criteria;
- runtime readiness, separated into V1 core health and advanced chain/executor
  readiness.

## What The Agent Can Do

- query graph structure and exact files;
- use grep and file reads for local evidence after graph lookup;
- propose a scoped plan tied to target files and acceptance criteria;
- ask for missing contract fields before implementation.

## What The Agent Cannot Do

- silently bootstrap a project or rewrite graph truth;
- treat a weak text match as ownership;
- start implementation without a work contract;
- treat dirty working-tree state as the active project model.

## Evidence In This Repo

- Demo case: [Fear Before Work](../cases/before-work.md)
- Demo entry: [HN Fear Demo](../README.md)
- Core reference: [System Architecture](../../architecture.md)
- Workflow reference: [Manual Fix SOP](../../governance/manual-fix-sop.md)

## Related Case Study

[AI proposed 5 components for my parallel system. After walking one scenario,
only 3 were real.](https://dev.to/amingin_ai/ai-proposed-5-components-for-my-parallel-system-after-walking-one-scenario-only-3-were-real-12nd)

That earlier story is the design-pressure version of this case: plausible
architecture is not enough until it survives a concrete project scenario and a
contract.
