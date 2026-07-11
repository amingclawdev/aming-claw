# CLI Agent Service Architecture

Status: Accepted architecture decision

Date: 2026-07-10

Backlog: `AC-CLI-AGENT-AUTHORITY-PROCESS-ADR-20260709`

## Context

Aming Claw needs a reliable host boundary for launching Codex, Claude, local-model,
and Desktop-hosted agent runs. Today, launch paths can inherit global provider
state, process ownership is fragmented, and a governance redeploy can be confused
with an agent-process restart. The reviewed design also identified missing
supervisor policy for lost runs, an unproven headless Claude subscription-auth
assumption, and the need to preserve the existing provider-neutral invocation
contract.

This decision separates host execution facts from governance decisions. It does
not replace ContractRuntime, route-token authorization, the task timeline, or
independent QA.

## Decision

Introduce a separately supervised **CLI Agent Service** daemon owned by Aming Claw.
The daemon is the host execution plane for registered coding-agent runtimes. It
owns only host-private profile, credential-reference, process, process-group,
lease, heartbeat, launch, cancellation, and execution facts.

The daemon cannot mint governance identities. It cannot create or reinterpret
runtime-context identity, route authority, backlog state, task authority,
ContractRuntime lines, merge decisions, close decisions, waivers, or QA
acceptance. It receives governance-issued references and reports sanitized host
facts. A worker or observer must still join the issued runtime context and author
its own governed evidence.

`ContractRuntime.current_guide` and the source-backed projected record are the sole
authority for next action, accepted line progression, merge eligibility, and close
eligibility. Route tokens authorize a requested action and scope; they do not prove
that work occurred. The task timeline is append-only evidence, not a state machine.
Meta-contract validation is audit-only and cannot override ContractRuntime.

## Topology

```text
Judgment Brain or operator
        |
        | requirements, never credentials
        v
Aming Claw governance / ContractRuntime
        | governance-issued refs and bounded request
        v
CLI Agent Service daemon ---------------- Provider CLI / local endpoint
        |                                      |
        | host process or host handoff          | model execution
        v                                      v
Codex / Claude / Desktop-created agent ---- sanitized run facts
        |
        | runtime-context join and worker-authored evidence
        v
ContractRuntime projection + append-only timeline
```

The daemon runs as its own launchd job, or an equivalent dedicated host
supervisor, under a narrow local service identity. It is not a thread or child of
the governance HTTP process. It survives governance redeploy. Governance and the
existing ServiceManager may probe its health or request a bounded lifecycle
operation, but neither a governance redeploy nor an executor reload implicitly
restarts it.

The first implementation uses a loopback or Unix-domain control endpoint with an
allowlisted protocol. No endpoint accepts arbitrary argv, shell text, raw
credentials, or unrestricted environment maps.

## Domain Model

An `AgentProfile` binds five independently versioned references:

```text
AgentProfile = HarnessRuntime
             + InferenceEndpoint
             + CredentialRef
             + LauncherAdapter
             + RolePolicy
```

- `HarnessRuntime` identifies the installed Codex, Claude, app-server, Desktop
  host, SDK, or local runtime and its capabilities.
- `InferenceEndpoint` identifies a first-party subscription/API, compatible
  gateway, local endpoint, or host-owned inference surface.
- `CredentialRef` is an opaque provider-home, Keychain, credential-helper, or
  host-owned reference. It never contains the credential value.
- `LauncherAdapter` renders a bounded process envelope or host handoff.
- `RolePolicy` defines certified roles, project eligibility, concurrency,
  supervision, cooldown, and successor budgets.

Each accepted launch creates one immutable `AgentRun`. The run pins profile,
runtime, endpoint, model, launcher, backend mode, project, requested role, and
governance references before any process is spawned. A different profile,
endpoint, account, launcher, or backend requires a successor run.

## State Ownership

| State or decision | Authoritative owner | CLI Agent Service behavior |
| --- | --- | --- |
| Next legal action and accepted contract line | ContractRuntime current/projected record | Reads a bounded projection; never computes or overrides it |
| Runtime-context, worker, observer, and QA identity | Aming Claw governance | Receives refs and supports join; never mints or impersonates principals |
| Route action authorization | Route-token service and consuming gate | Presents an opaque ref; never treats it as proof of work |
| Backlog, task, merge, close, waiver, and QA decisions | Aming Claw governance and ContractRuntime gates | Has no decision or mutation API |
| Timeline events | Append-only governance timeline | May submit sanitized host facts through approved facades; events remain evidence only |
| Meta-contract result | Audit compatibility layer | Records audit outcome only; never uses it as progression authority |
| Profile secrets and provider homes | Provider, Keychain, or host-private registry | Resolves immediately before launch; never projects values |
| PID, process group, lease, heartbeat, launch, cancellation | CLI Agent Service | Persists and reconciles these host-private facts |
| Invocation request/result wire shape | `AIInvocationRequest` / `AIInvocationResult` | Adapts to and from the existing versioned contract |
| Public run receipt | Governance public projection from sanitized service facts | Supplies hashes, refs, categories, versions, and explicit redaction flags |

Host-private state uses a dedicated SQLite database in WAL mode from the first
slice because lease and heartbeat writes are concurrent. Provider homes, private
logs, and run directories remain outside the repository and shared volume with
directory mode `0700` and private file mode `0600`.

## Process And Permission Boundary

The daemon is the sole owner of every process it launches. It records PID,
process start identity, process-group identity, argv hash, selected environment
key names, cwd, lease, and run id before declaring a run started. Cancellation
targets only the recorded process group and verifies start identity before
signalling it. Desktop hosts are never killed as run cleanup.

Launching from the dedicated daemon also creates an explicit host permission
domain. Agent processes must not inherit the permission classifier or ambient
provider state of an interactive parent session. The launcher starts from a
versioned allowlist, removes provider-affecting and nesting variables, then
applies exactly one profile's rendered environment.

The daemon must remain alive across governance deployment. During governance
unavailability it may continue supervising already-owned host processes and retain
sanitized host facts locally. It must not manufacture timeline or ContractRuntime
evidence. Protected progress blocks until governance is reachable again.

## Failure And Restart Behavior

| Failure | Required behavior |
| --- | --- |
| Governance redeploy | Daemon and owned runs remain alive; reconnect and resume idempotent status delivery after governance health returns |
| Daemon restart | Reconcile persisted leases against PID, start identity, and process group; never duplicate-spawn an uncertain run |
| Unknown surviving process | Quarantine and report; do not adopt or kill without an exact ownership match |
| Worker process crash | Mark the run failed or lost, notify its requester, and preserve terminal host facts |
| Stale heartbeat | Apply the role policy and successor budget; do not rewrite the failed run |
| Quota or auth failure | Record a classified terminal/deferred outcome and cooldown; no account rotation inside the run |
| Endpoint or provider failure | Fail or defer honestly; no silent backend fallback |
| Lost L2 observer | Notify requester and allow evidence-first auto-successor re-onboarding within a bounded loop budget |
| Lost L3 worker or QA | Notify the parent L2; the parent requests any successor under fresh governance authority |
| Circuit-breaker exhaustion | Stop automatic restarts and expose an operator-visible blocked state |

A successor receives a new run id and explicit `successor_of_run_id`. Governance
identity is renewed when required by ContractRuntime or route scope. No restart
path reconstructs worker-authored evidence after the fact.

Run receipts include harness-reported duration, token usage, and cost when
available, plus a flag describing unavailable metering. Metering is diagnostic;
it cannot satisfy governance work evidence.

## Authentication And Privacy

The service stores only credential references. Codex profile isolation uses
profile-scoped `CODEX_HOME`. Claude subscription authentication requires a
pre-implementation spike proving that the daemon identity can access the intended
Keychain item without an interactive ACL prompt. Until that proof exists, Claude
subscription profiles are `host_owned`; unattended Claude runs require a proven
API-key/gateway helper or Desktop handoff.

Raw credentials, refresh tokens, API keys, cookies, private prompts, and raw
provider output never enter backlog, timeline, graph, shared volume, command
display, or public projections. Evidence contains only stable profile ids,
runtime/endpoint/model ids, auth-status categories, hashes, sanitized refs, and
explicit raw-data persistence flags.

## Compatibility Contract

`AIInvocationRequest` and `AIInvocationResult` remain the single provider-neutral
compatibility contract. The CLI Agent Service adds adapters; it does not introduce
a competing request/result schema. Existing prompt hashing, route identity,
environment-key-only evidence, command redaction, output hashing, and raw-output
flags remain required.

Compatibility is bidirectional during migration: legacy callers can produce an
`AIInvocationRequest`, and service results return as `AIInvocationResult`. New
profile and lease metadata lives in versioned metadata/extensions until a
deliberate contract revision is accepted.

## Migration Rules

1. Backend selection is immutable per run and is persisted before spawn.
2. A run uses either the legacy launcher or CLI Agent Service, never both.
3. Shadow comparison is read-only: it may compare resolution, but cannot lease,
   spawn, cancel, heartbeat, or write gate evidence.
4. There is no dual gate write. One selected path reports sanitized facts, and
   ContractRuntime remains the only progression writer.
5. Service unavailability before launch returns a blocked result. It does not
   silently fall back to a direct subprocess.
6. Failure after launch cannot switch profile, account, endpoint, launcher, or
   backend. Recovery is same-run reconciliation or an explicit successor.
7. Rollback changes selection only for runs not yet created. Existing runs finish
   or fail under their pinned backend.
8. Canonical routing configuration is introduced atomically; legacy and new
   configuration cannot both be authoritative.

Delivery order is: canonical schemas and resolver; C0 single inherited-profile
lease/supervision; the Claude headless-auth spike; managed profiles and auth; full
CLI scheduling; local certification; Desktop adapters; then Judgment Brain L2/L3
integration after close-authority and lane-catalog dependencies pass.

## Rejected Alternatives

- **Run inside governance.** Rejected because governance redeploy would terminate
  runs and inherit the interactive permission domain.
- **Extend the existing executor ServiceManager in place.** Rejected because its
  current ownership is executor lifecycle and its self-redeploy guard is a useful
  failure boundary. CLI agent supervision needs independent persistence and
  lifecycle.
- **Let Judgment Brain spawn agents directly.** Rejected because it would own
  credentials and host process facts and bypass Aming Claw's bounded runtime.
- **Make the daemon a governance authority.** Rejected because it creates two
  next-action/merge/close state machines.
- **Use route tokens as completion proof.** Rejected because authorization is not
  evidence that execution occurred.
- **Dual-spawn with legacy fallback.** Rejected because duplicate processes and
  evidence cannot be reconciled safely.
- **Copy provider auth between profiles.** Rejected because provider-owned state
  and Keychain ACLs are the security boundary.
- **Treat a healthy local model as role-certified.** Rejected because observer,
  worker, and QA eligibility require accumulated capability evidence.

## Consequences

Aming Claw gains one durable host execution plane while keeping governance
authority singular. The design requires a new daemon, host-private database,
strict launcher adapters, reconciliation tests, and operator-visible blocked
states. It also makes failure honest: a provider, daemon, or governance outage may
pause progress, but cannot silently change identity or create competing authority.
