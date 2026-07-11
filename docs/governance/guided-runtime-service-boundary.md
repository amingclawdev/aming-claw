# Guided Runtime Service Boundary

Status: Accepted service-boundary decision

Date: 2026-07-10

Backlog: `AC-CLI-AGENT-AUTHORITY-PROCESS-ADR-20260709`

## Purpose

This document defines how a host execution service participates in Aming Claw's
guided runtime without becoming a second governance runtime. It is the authority
map for integrations such as Judgment Brain, the CLI Agent Service, Desktop host
adapters, ServiceManager, and provider launchers.

## Canonical Authority Order

1. The source-backed `ContractRuntime.current_guide` or projected record is the
   sole next-action, line-progression, merge, and close authority.
2. A route-token gate answers whether a caller may attempt an action in a bounded
   scope. Passing authorization does not prove startup, implementation, testing,
   review, merge, or close.
3. The task timeline stores append-only evidence. Timeline presence alone cannot
   advance a contract or override a ContractRuntime decision.
4. Meta-contract checks are audit-only compatibility checks. They may warn or
   reject an incompatible audit shape, but they are never the primary decision
   source.

When projections disagree, consumers fail closed and identify ContractRuntime as
the recovery source. They do not choose the most permissive projection.

## Service Roles

| Component | Owns | Must not own |
| --- | --- | --- |
| ContractRuntime | Contract definition, current/projected state, next action, line acceptance, merge and close gates | Host credentials, PID/process groups, provider homes |
| Route-token service | Bounded action authorization, scope, expiry, opaque refs | Work completion, quality, merge or close facts |
| Timeline | Immutable evidence events and refs | Mutable workflow state or next-action calculation |
| Meta-contract layer | Audit compatibility and imported legacy diagnostics | Primary progression decisions |
| CLI Agent Service | Host-private profiles, credential refs, launch selection, PID/process groups, leases, heartbeats, cancellation, execution outcomes | Governance principals, runtime-context minting, backlog/task/merge/close/waiver/QA decisions |
| ServiceManager | Existing executor supervision and narrow manager control endpoints | ContractRuntime progression or CLI Agent Service run ownership |
| Judgment Brain | Requirements, route intent, dispatch policy, public-safe outcome consumption | Provider credentials, worker PIDs, implementation evidence, AC merge/close authority |
| Worker or QA principal | Its own runtime-context join and role-owned evidence | Parent, observer, or independent-principal evidence |

## Request Boundary

A launch request may contain only:

- a versioned `AIInvocationRequest`;
- project, role, capability, and privacy requirements;
- governance-issued runtime-context, task, route, and opaque token references;
- registered profile/endpoint pins when operator policy permits them;
- bounded cwd/worktree, timeout, output, and cancellation policy.

It may not contain arbitrary argv, shell text, raw environment maps, credential
values, a claimed governance role, or caller-authored merge/close decisions. The
service validates references but does not turn them into governance authority.

The service selects a registered profile and backend before creating the run.
Selection and the effective source/precedence explanation are persisted in the
immutable run record before process launch.

## Response Boundary

The service returns host facts, not governance conclusions:

- run, parent, and successor ids;
- selected profile, runtime, endpoint, model, launcher, and backend ids;
- PID/start identity/process-group or host-session identity;
- lease and heartbeat state;
- start, finish, cancellation, and classified failure times;
- command, prompt, output, and launch hashes;
- sanitized transcript/output references;
- metering when reported by the harness;
- explicit redaction and raw-persistence flags.

The result is represented through `AIInvocationResult` and versioned metadata.
Governance may project those facts into public-safe evidence. The service does not
append a worker's read receipt, startup attestation, implementation evidence,
finish attestation, QA result, merge result, or close result on that principal's
behalf.

## End-To-End Sequence

```text
1. Caller reads ContractRuntime current/projected state.
2. Governance authorizes the bounded launch action and issues refs.
3. CLI Agent Service resolves and pins one profile/backend.
4. Service creates one lease and spawns one process or host handoff.
5. The real host-created principal joins its runtime context.
6. The principal authors its own required evidence.
7. Timeline stores accepted evidence append-only.
8. ContractRuntime projects accepted source-backed lines and decides the next action.
9. Only governance performs merge, redeploy, reconcile, backlog close, or waiver.
```

Steps cannot be collapsed by treating a launch receipt or route token as work
proof. A Desktop handoff follows the same sequence even when the user triggers the
host launch.

## Supervision And Redeploy

The CLI Agent Service daemon is independently supervised and outlives governance
redeploy. It is not restarted by governance's redeploy endpoint, executor reload,
or ServiceManager sidecar restart. The dedicated host supervisor owns daemon
restart; the daemon owns only its registered agent process groups.

During a governance outage:

- existing agent processes may continue local computation under their pinned run;
- the daemon continues lease/process supervision and stores sanitized host facts;
- no protected action advances and no governance evidence is fabricated;
- delivery retries are idempotent and resume only after governance health returns;
- expiry or route-renewal requirements still fail closed.

After daemon restart it reconciles the host-private database with PID, process
start identity, and process group. Exact matches are reattached to the same run.
Missing processes become terminal/lost. Ambiguous processes are quarantined.
Reconciliation never spawns a duplicate or adopts an unowned process.

## Failure Policy

Failure is classified before successor policy runs:

| Class | Run outcome | Profile effect | Successor owner |
| --- | --- | --- | --- |
| Process crash | `failed` or `lost` | Health penalty | L2 policy for workers/QA; requester policy for L2 |
| Stale heartbeat | `lost` | Temporary unhealthy | Same as process crash |
| Quota exhausted | `deferred` | `cooling_down` or `quota_exhausted` | Explicit new run after policy permits |
| Authentication failed/expired | `blocked` | `auth_required` | Operator/login controller, then explicit new run |
| Endpoint unavailable | `deferred` or `failed` | Endpoint unhealthy | Explicit new run; no automatic backend swap |
| Governance unavailable | Host supervision continues; protected progress blocks | None | Same run reconnects if still valid |
| Daemon circuit breaker | `blocked` | Service unhealthy | Operator intervention |

L2 observer policy may request an automatic successor only after evidence-first
re-onboarding and within a bounded resurrection budget aligned with loop policy.
L3 worker and QA loss is reported to the parent L2. QA remains a distinct
governance principal; same-profile QA requires an explicit evidence flag. A
harness-native nested agent is not a governed child unless it receives its own
runtime context and lineage.

## Immutable Migration Boundary

Migration is selected per run, before lease and spawn:

```text
run.backend_owner = legacy_launcher | cli_agent_service
```

The value is immutable. The following are forbidden:

- dual spawn for one run;
- dual lease or heartbeat ownership;
- dual gate/timeline writes for the same evidence item;
- switching provider profile, endpoint, launcher, or backend after startup;
- silent fallback to a legacy subprocess when the service is unavailable;
- retroactive adoption of work as a different run;
- post-hoc reconstruction of worker-authored evidence.

Read-only shadow resolution is allowed when it has no spawn, lease, cancellation,
heartbeat, timeline, or gate side effects. Rollback affects only future runs.
Existing runs finish, reconcile, or fail under the backend selected at creation.

`AIInvocationRequest` and `AIInvocationResult` are the compatibility boundary for
both backend owners. Migration must not create a second provider-neutral contract
or a second configuration authority. New fields are versioned metadata until an
explicit invocation-contract revision lands.

## Security Boundaries

- Provider credentials stay in provider homes, Keychain, or an approved helper.
- Raw worker session, fence, route, and provider tokens are never persisted in
  public evidence or prompt text.
- The daemon resolves credentials immediately before launch and records only
  reference ids and environment key names.
- Profile-scoped launch starts from an allowlisted environment and removes ambient
  provider and host-nesting variables.
- The control endpoint is local, authenticated, versioned, and capability-bounded.
- Desktop hosts remain host-owned and are never terminated by run cleanup.
- Local endpoints receive the same tool, worktree, route, and evidence controls as
  remote providers.

## Conformance Requirements

Before enabling a migrated cohort, tests must prove:

1. ContractRuntime remains the only next-action/merge/close decision source.
2. Route authorization cannot satisfy work evidence.
3. Timeline and meta-contract inputs cannot override ContractRuntime.
4. Governance redeploy leaves the daemon and an active run alive.
5. Daemon restart reconciles without duplicate spawn.
6. Conflicting parent provider variables cannot change the selected profile.
7. Service failure before spawn returns blocked without legacy fallback.
8. Quota/auth/provider failure creates an explicit terminal or successor lineage.
9. `AIInvocationRequest`/`Result` evidence remains redacted and schema-compatible.
10. A real principal, not the daemon, authors required runtime-context evidence.
11. L2 loss exercises bounded evidence-first successor policy.
12. Claude unattended subscription use remains disabled until the Keychain access
    spike passes under the actual daemon identity.

## Rejected Boundary Shapes

- A monolithic governance-plus-launch process, because deployment and permission
  domains would be coupled.
- A daemon-owned workflow state machine, because it duplicates ContractRuntime.
- Timeline-driven progression, because append-only evidence is not current state.
- Route-token-as-proof semantics, because authorization cannot attest execution.
- Automatic legacy fallback, because backend and evidence ownership would become
  ambiguous.
- Observer-authored worker or QA evidence, because identity separation is a close
  and independent-verification invariant.

This boundary intentionally accepts temporary blocking during outages. Honest
blocking is safer than silent identity, backend, or authority substitution.
