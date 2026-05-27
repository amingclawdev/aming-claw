# Show HN: I built a new multi-agent coding architecture: graph-bound contracts

Most multi-agent coding systems are still chat-centered.

A supervisor routes messages. Agents hand off context. A workflow engine shares
state. The transcript becomes the coordination surface.

Aming Claw uses a different coordination surface: graph-bound contracts.

One observer coordinates multiple coding agents. Each worker gets a contract,
owned files, a fence token, a trace ledger, and a close gate. All workers query
the same commit-bound project graph. The shared object is not the chat. The
shared object is the project graph.

If another open local coding-agent system is doing this exact model, I have not
found it. That is the claim I want people to challenge.

## The problem

Right now, when you ask an AI coding agent to ship a feature, you give it a
prompt and hope.

You hope it touched the right files. You hope it did not rebuild something the
project already had. You hope parallel agents did not collide. You hope the
tests were real. You hope the next agent is not reading stale project memory.

Hope is not an engineering control.

Aming Claw is my attempt to replace that hope with a local governance layer for
AI coding agents: contracts before work, evidence during work, and
commit-bound project memory after work.

## The new unit of collaboration

The unit is not "an agent conversation."

The unit is a graph-bound contract:

- a backlog row that says what work is allowed;
- target files and forbidden files;
- acceptance criteria and required evidence;
- worker identities with owned files and fence tokens;
- graph queries that produce server-resolvable trace ids;
- timeline events for dispatch, implementation, verification, replay, and
  close-ready state;
- a commit that lands the accepted evidence atomically.

The observer can be a human or an AI session in observer mode. The workers can
be Claude, Codex, scripted workers, or any compatible local process. The demo
does not require you to have two AI subscriptions. The default path uses
deterministic scripted workers so the governance protocol can be audited without
model randomness.

## Architecture at a glance

```text
                         observer
                            |
                  writes graph-bound contract
                            |
          +-----------------+-----------------+
          |                                   |
     worker A                             worker B
  owned_files=A                        owned_files=B
   fence_token=A                        fence_token=B
          |                                   |
   graph_query trace                    graph_query trace
          |                                   |
       PASS                           FAIL / INTERRUPT
                                              |
                                              v
                                      replay attempt 2
                                      same contract edge
                                      new fence + trace
                                              |
                                             PASS
          |                                   |
          +-----------------+-----------------+
                            |
                    one atomic commit
                            |
                  target graph reconcile
```

The key constraint is one-hop graph truth. A worker can produce candidate
evidence against the target commit graph. It cannot make its branch-local graph
canonical. After the ordered merge lands, the target ref reconciles once, and
the next agent reads that graph.

Without this rule, parallel AI work creates multiple plausible memories of the
project, not just multiple Git diffs.

## The case I want you to try

The HN demo now centers on a replayable during-work failure:

1. The fixture creates only a small project and an active graph.
2. The observer creates the backlog contract and worker fences.
3. Worker A passes with its own trace ids and owned files.
4. Worker B fails or is interrupted.
5. The observer replays B from the same contract evidence.
6. Replay attempt 2 passes with a new fence and new trace ids.
7. The accepted work lands in one commit with Chain trailers.
8. The target graph is reconciled after merge.

That is the coordination model in miniature. The important part is not that a
worker failed. Workers fail all the time. The important part is that failure is
not swallowed by chat history. It becomes a typed event with attempt number,
worker identity, owned files, fence token, graph traces, verification output,
and replay linkage.

This is the part ordinary chat-driven agent workflows struggle to reproduce:
the next attempt is not "try again, but please remember what happened." It is
"replay this bounded contract from this recorded evidence."

## The three fears

The demo is still organized around three ordinary developer fears:

- **Before work:** will the agent understand the project before editing, or
  invent a parallel architecture that already exists?
- **During work:** can I see which worker owned which files, which evidence each
  produced, where a worker failed, and how replay happened?
- **After work:** after the patch lands, do docs, tests, config, assets, graph
  state, and semantic memory reflect what actually changed?

Those map to three case pages:

- [Fear Before Work](cases/before-work.md)
- [Fear During Work](cases/during-work.md)
- [Fear After Work](cases/after-work.md)

The [detailed design story](design-story.md) keeps the longer explanation:
three fears, one-hop concurrent development, audit trail, boundaries, and the
earlier essays that led here.

## Try it

Install the plugin, then ask your current AI coding session to run:

```text
/aming-claw:aming-claw-hn-demo
```

That session becomes the observer. It reads the skill files, creates or uses the
isolated HN demo fixture, writes contracts, calls MCP/governance tools, produces
timeline evidence, and shows dashboard URLs. You review the evidence.

The user path is one prompt. The release gate is stricter: I run the same flow
inside Docker containers for Codex and Claude installs, then run the sandbox
audit against their install reports. Docker is not required for users; it is how
I stop my already-working local environment from faking a launch pass.

Start here:
[HN Fear Demo README](README.md)

## What is actually new

Supervisors are not new. Agent handoffs are not new. Workflow engines are not
new. Traces are not new. Code graphs are not new.

The claim is narrower and, I think, more interesting:

Aming Claw treats a commit-bound project graph plus per-worker contracts as the
coordination substrate for local multi-agent coding. The agents do not primarily
share a conversation. They share graph truth, fenced scope, and audit evidence.

That gives the observer a different job. The observer does not babysit every
implementation step. The observer scopes contracts, watches evidence, interrupts
bad attempts, replays bounded work, and decides when the commit can land.

If you think this already exists in an open local coding-agent system, I want the
link. If you think the abstraction is wrong, the replay case is the fastest way
to attack it.

## Boundaries

This is not a claim that humans disappear. The observer role is real work:
reviewing bindings, arbitrating drift, interrupting workers, and deciding when
to reconcile.

This is not a claim that graphs solve everything. A bad graph is worse than no
graph if agents treat it as authority. Aming Claw keeps graph repair
source-controlled: source hints, config, accepted review events, and reconcile,
not silent database edits.

This is also not a claim that every team needs this much governance. If you want
one agent to make one small edit while you watch the diff, this will feel heavy.
It starts to make sense when multiple agents are operating while the human stays
out of the implementation critical path.

## Links

- [Run the HN demo](README.md)
- [Detailed design story](design-story.md)
- [Before Work Architecture](architecture/before-work-architecture.md)
- [During Work Architecture](architecture/during-work-architecture.md)
- [After Work Architecture](architecture/after-work-architecture.md)
- [Earlier backlog/state-machine story](https://dev.to/amingin_ai/i-told-my-ai-to-build-a-feature-did-it-i-had-no-idea-1f1)
- [Earlier scenario-walk story](https://dev.to/amingin_ai/ai-proposed-5-components-for-my-parallel-system-after-walking-one-scenario-only-3-were-real-12nd)
- [Earlier graph-memory story](https://dev.to/amingin_ai/ais-tech-debt-is-invisible-even-to-ai-i-solved-it-at-the-architecture-layer-1nh1)
