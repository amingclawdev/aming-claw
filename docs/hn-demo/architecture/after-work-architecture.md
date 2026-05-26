# After Work Architecture

## Fear

The fear after work is that the patch lands but the project forgets what changed.
Docs may be unbound, tests may be stale, config may drift, generated assets may
look like source, and the next agent may reason from old graph memory.

## Failure Mode

After a diff lands, weak evidence can be promoted too early. A doc can mention a
module without being a trusted governance record for that module. A path match
can be a useful hint without being strong enough to enter review impact scope.
An AI semantic proposal can be helpful without being accepted project memory.

## Architecture Invariants

**Source records before derived views:** committed files, source-controlled
hints, config, accepted bindings, review decisions, and timeline events are
durable inputs. Asset Inbox rows, graph snapshots, semantic projections,
candidate bindings, and operations-queue state are derived views.

**Asset state before node binding:** a changed doc/test/config file first becomes
a commit-bound asset with hash, status, and provenance. It becomes node impact
scope only after a reviewed binding, accepted proposal, or source-controlled
hint.

**Weak evidence stays weak:** path matches, mentions, and AI guesses are
proposal/status state until reviewed. They do not silently become trusted graph
truth.

**Reconcile before reuse:** after source changes land, the target graph and
semantic projection must be reconciled before the next agent treats project
memory as current.

## What The Dashboard Shows

- Asset Inbox state for docs, tests, config, generated assets, ignored assets,
  candidates, accepted bindings, and stale mappings;
- Review Queue boundaries for proposals and reminders;
- operations queue state for reconcile and semantic work;
- graph stale/current state after source changes.

## What The Agent Can Do

- surface changed docs/tests/config as assets;
- propose bindings or review items;
- use accepted bindings as impact-scope evidence;
- run or request reconcile after commits.

## What The Agent Cannot Do

- treat every mention as a trusted doc/test/config binding;
- silently move weak evidence into graph truth;
- assume old semantic memory is current after a source change;
- skip post-work asset review when docs/tests/config changed.

## Evidence In This Repo

- Demo case: [Fear After Work](../cases/after-work.md)
- Demo entry: [HN Fear Demo](../README.md)
- Asset reference: [Asset Inbox API Contract](../../api/asset-inbox-contract.md)
- Reconcile reference: [Reconcile Workflow](../../governance/reconcile-workflow.md)
- Workflow reference: [Manual Fix SOP](../../governance/manual-fix-sop.md)

## Related Case Study

[AI's tech debt is invisible - even to AI. I solved it at the architecture
layer.](https://dev.to/amingin_ai/ais-tech-debt-is-invisible-even-to-ai-i-solved-it-at-the-architecture-layer-1nh1)

That earlier story is the graph-memory side of this case. This architecture note
extends the same idea to docs, tests, config, weak bindings, and review impact
scope.
