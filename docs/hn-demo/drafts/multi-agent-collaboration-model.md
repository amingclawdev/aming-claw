# Draft: Multi-Agent Collaboration Model Claim

Draft only. Not linked from public docs.

## Working Thesis

Aming Claw is not just another multi-agent coding orchestrator. The claim worth
testing is narrower and sharper:

> Aming Claw defines a contract-and-graph collaboration model for coding agents:
> one observer coordinates multiple agents, each under a different contract,
> against the same commit-bound project graph, with per-worker fences, graph
> query traces, and close gates.

## Why This Is Different

Most multi-agent coding tools I have seen still coordinate agents through
conversation: a supervisor routes messages, hands off context, or shares
workflow state. That is useful, but the shared object is still the conversation
or the workflow state.

Aming Claw coordinates coding agents through contracts over the same
commit-bound project graph. The shared object is not the chat. The shared object
is the project graph.

One observer can dispatch multiple coding agents at once. Each agent gets a
different contract, different owned files, a different fence token, and its own
trace ledger. They do not become trustworthy because they saw the same chat.
They become reviewable because each one proves what it did against the same
graph.

If there is another open local coding-agent system doing this exact thing, I
want to see it. I have not found one.

## Strong Version

This is the part I think is actually new: graph-bound agent contracts.

Most systems coordinate agents by passing messages, context, state, or handoff
history around. Aming Claw coordinates coding agents by making each worker prove
its work against the same commit-bound graph, under its own contract.

## Safer Version

I am not claiming Aming Claw invented supervisors, handoffs, tracing, or
multi-agent orchestration. Those exist.

The claim is narrower: I have not seen another open local coding-agent system
where one observer coordinates multiple contracted workers against the same
commit-bound project graph, with per-worker fences and reviewable evidence as
the coordination substrate.

## Possible Article Placement

Best location if used: after "A note on who operates this" and before "The
three fears." It reframes the rest of the article before the before/during/after
structure begins.

## Risks To Discuss

- "New paradigm" is more attractive but invites comparisons to LangGraph,
  AutoGen, CrewAI, and OpenAI Agents SDK.
- The defensible distinction is not "multi-agent exists / does not exist." The
  distinction is the coordination substrate: chat/workflow state versus
  contracts over a commit-bound project graph.
- The strongest sentence is probably: "The shared object is not the chat. The
  shared object is the project graph."
