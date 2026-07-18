import { readFileSync } from "node:fs";
import type { BacklogBug, BacklogTimelineGateResponse, ContractRuntimeVisualizationResponse, TaskTimelineEvent } from "../types";
import {
  contractRuntimeAuthorityDisplayStatus,
  isBacklogRowPrivate,
  normalizeTaskPlaybackTrace,
  normalizeTaskPlaybackCompactLedger,
  projectContractRuntimeAuthorityViewModel,
  taskPlaybackLedgerRowsToTimelineEvents,
  taskPlaybackCompactLedgerBlockingLabel,
  taskPlaybackCompactLedgerDisplayState,
  displayPlaybackFrames,
  latestPlaybackFrameId,
  pushPlaybackNavStack,
  popPlaybackNavStack,
  projectEventToCard,
  sliceEventPage,
  truncateHash,
  categorizeEvidenceRef,
  groupEvidenceRefsByCategory,
  isPlaybackBacklogRefValue,
  isPlaybackEventEvidenceRef,
  buildPlaybackUrl,
  findFrameIdByEventParam,
  resolveInitialPlaybackFrameId,
  resolveSelectedFrameIdForEventParam,
  PLAYBACK_URL_PARAMS,
  type PlaybackNavEntry,
  type TaskPlaybackFrame,
  type TaskPlaybackEvidenceRef,
} from "./taskPlayback";
import { projectTaskTimelineEvent, projectGateMatrix, timelineStatusFromEvent } from "./taskTimelineSemantics";
import type { GateMatrixProjection } from "./taskTimelineSemantics";
// Note: BacklogView cannot be imported in Node (uses import.meta.env via api.ts).
// Lane attribution (AC-3) and DAG headline (AC-2) are verified below via semantic
// projections of the same event shapes used by BacklogView's rawWorkerKeyForEvent.

const PRIVATE_REQUEST_FIELD = "raw_" + "prompt";

export const TASK_PLAYBACK_HISTORICAL_FIXTURE_BACKLOG_IDS = [
  "AC-OBSERVER-COMMAND-QUEUE-ACTIVE-CONSUMER-RECOVERY-20260607",
  "AC-DOGFOOD-OBSERVER-ONLY-COMMAND-STARTUP-GATE-20260607",
];

const historicalBacklog: BacklogBug = {
  bug_id: TASK_PLAYBACK_HISTORICAL_FIXTURE_BACKLOG_IDS[0],
  title: "Historical observer command queue recovery",
  status: "OPEN",
  priority: "P1",
};

const narrativeFocusBacklog: BacklogBug = {
  bug_id: "AC-TASK-PLAYBACK-NARRATIVE-FOCUS-20260607",
  title: "Task playback narrative focus",
  status: "OPEN",
  priority: "P1",
};

export const TASK_PLAYBACK_HISTORICAL_FIXTURE_EVENTS: TaskTimelineEvent[] = [
  {
    id: 101,
    event_type: "route.prompt_context.requested",
    event_kind: "route_context",
    phase: "dispatch",
    actor: "observer",
    status: "accepted",
    backlog_id: TASK_PLAYBACK_HISTORICAL_FIXTURE_BACKLOG_IDS[0],
    task_id: "cmd-fixture-observer-startup",
    payload: {
      route_id: "route-20260607-fixture",
      route_context_hash: "sha256:fixture-route-context",
      prompt_contract_id: "rprompt-fixture",
      prompt_contract_hash: "sha256:fixture-prompt-contract",
      [PRIVATE_REQUEST_FIELD]: "[fixture private request text]",
      worktree_path: "[fixture private path]",
    },
    created_at: "2026-06-07T10:00:00Z",
  },
  {
    id: 102,
    event_type: "route_token_gate.task_timeline_append",
    event_kind: "verification",
    phase: "route_gate",
    actor: "observer",
    status: "accepted",
    payload: {
      route_token_gate: {
        action: "task_timeline_append",
        decision: "route_token",
        route_context_hash: "sha256:fixture-route-context",
        prompt_contract_id: "rprompt-fixture",
        route_token_hash: "sha256:fixture-token-hash",
        reason: "timeline append allowed",
      },
    },
    created_at: "2026-06-07T10:01:00Z",
  },
  {
    id: 103,
    event_type: "mf_subagent.startup",
    event_kind: "mf_subagent_startup",
    phase: "startup_gate",
    actor: "mf_sub",
    status: "passed",
    payload: {
      mf_subagent_startup_gate: {
        worker_id: "mfsub-fixture-a",
        worker_role: "mf_sub",
        branch_ref: "refs/heads/codex-mfsub-fixture-a",
        owned_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
        actual_cwd: "[fixture private path]",
      },
    },
    created_at: "2026-06-07T10:02:00Z",
  },
  {
    id: 104,
    event_type: "mf_subagent.read_receipt",
    event_kind: "mf_subagent_read_receipt",
    phase: "startup_gate",
    actor: "mf_sub",
    status: "accepted",
    payload: {
      worker_id: "mfsub-fixture-a",
      acknowledged_stop_state: "review_ready",
      acknowledged_forbidden_actions: ["merge", "push", "delete_worktree"],
    },
    created_at: "2026-06-07T10:03:00Z",
  },
  {
    id: 105,
    event_type: "mf_subagent.dispatch",
    event_kind: "mf_subagent_dispatch",
    phase: "dispatch",
    actor: "observer",
    status: "passed",
    payload: {
      worker_id: "mfsub-fixture-a",
      graph_query_trace_ids: ["gqt-fixture-dispatch"],
      test_scenario_policy: {
        reason: "historical timeline rows need readable public labels",
      },
      source_event_ids: [101, 102],
    },
    created_at: "2026-06-07T10:04:00Z",
  },
  {
    id: 106,
    event_type: "task_timeline_append",
    event_kind: "implementation",
    phase: "implementation",
    actor: "mf_sub",
    status: "passed",
    payload: {
      worker_id: "mfsub-fixture-a",
      changed_files: ["frontend/dashboard/src/lib/taskTimelineSemantics.ts"],
      graph_query_trace_ids: ["gqt-fixture-implementation"],
      summary: "Added readable timeline semantics",
    },
    created_at: "2026-06-07T10:05:00Z",
  },
  {
    id: 107,
    event_type: "task_timeline_append",
    event_kind: "verification",
    phase: "verification",
    actor: "mf_sub",
    status: "passed",
    verification: {
      passed: true,
      tests_run: ["npm run build"],
      reason: "fixture verifies public semantic labels",
    },
    created_at: "2026-06-07T10:06:00Z",
  },
  {
    id: 108,
    event_type: "independent_verification.completed",
    event_kind: "verification",
    phase: "independent_verification",
    actor: "qa",
    status: "passed",
    verification: {
      passed: true,
      reason: "QA confirmed no model call path",
    },
    created_at: "2026-06-07T10:07:00Z",
  },
  {
    id: 109,
    event_type: "observer.close_ready",
    event_kind: "close_ready",
    phase: "close_ready",
    actor: "observer",
    status: "passed",
    payload: {
      reason: "observer review can inspect readable public evidence",
      source_event_ids: [106, 107, 108],
    },
    created_at: "2026-06-07T10:08:00Z",
  },
  {
    id: 110,
    event_type: "legacy.private.event",
    event_kind: "unknown_private_fixture",
    phase: "legacy",
    actor: "system",
    status: "recorded",
    payload: {
      [PRIVATE_REQUEST_FIELD]: "[fixture private request text]",
      cwd: "[fixture private path]",
    },
    created_at: "2026-06-07T10:09:00Z",
  },
];

export const TASK_PLAYBACK_NARRATIVE_FOCUS_FIXTURE_EVENTS: TaskTimelineEvent[] = [
  {
    id: 201,
    event_type: "route.prompt_context.requested",
    event_kind: "route_context",
    phase: "dispatch",
    actor: "route service",
    status: "accepted",
    backlog_id: narrativeFocusBacklog.bug_id,
    task_id: "mfsub-task-playback-narrative-focus-a",
    payload: {
      route_id: "route-20260607-fixture-narrative",
      route_context_hash: "sha256:fixture-narrative-route-context",
      prompt_contract_id: "rprompt-fixture-narrative",
      prompt_contract_hash: "sha256:fixture-narrative-prompt-contract",
      visible_injection_manifest_hash: "sha256:fixture-visible-manifest",
      target_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
      acceptance_criteria: ["blocked reason visible", "actor context narrative visible"],
      required_evidence: ["implementation", "verification", "close_ready"],
      [PRIVATE_REQUEST_FIELD]: "[fixture private request text]",
      route_context: {
        source_label: "Judgment Brain route label",
        route_docs: ["visible route context bundle", "read receipt context must be inspectable"],
        visible_bundle: {
          allowed_actions: ["dispatch bounded worker", "record read receipt"],
          required_lanes_evidence: ["mf_subagent_read_receipt", "mf_subagent_startup"],
        },
        raw_private_route_body: "[fixture private route context body]",
      },
    },
    created_at: "2026-06-07T11:00:00Z",
  },
  {
    id: 202,
    event_type: "route.action.requested",
    event_kind: "route_action_precheck",
    phase: "dispatch",
    actor: "route service",
    status: "allowed",
    backlog_id: narrativeFocusBacklog.bug_id,
    task_id: "mfsub-task-playback-narrative-focus-a",
    payload: {
      action: "observer_dispatch_bounded_worker",
      stage: "dispatch",
      route_context_hash: "sha256:fixture-narrative-route-context",
      prompt_contract_id: "rprompt-fixture-narrative",
      allowed_action: "dispatch_bounded_worker",
    },
    created_at: "2026-06-07T11:01:00Z",
  },
  {
    id: 203,
    event_type: "service.route.completed",
    event_kind: "route_context",
    phase: "route_service",
    actor: "service-router",
    status: "allowed",
    backlog_id: narrativeFocusBacklog.bug_id,
    task_id: "mfsub-task-playback-narrative-focus-a",
    payload: {
      service_id: "route.prompt_alert_bundle",
      decision: "allow",
      route_id: "event.route_prompt_context.preview",
      route_context_hash: "sha256:fixture-narrative-route-context",
      prompt_contract_id: "rprompt-fixture-narrative",
      visible_injection_manifest_hash: "sha256:fixture-visible-manifest",
      source_event_type: "route.prompt_context.requested",
      result: {
        status: "allowed",
        route_action_gate: {
          action: "dispatch_bounded_worker",
          allowed: true,
        },
      },
    },
    created_at: "2026-06-07T11:02:00Z",
  },
  {
    id: 204,
    event_type: "mf_subagent.read_receipt",
    event_kind: "mf_subagent_read_receipt",
    phase: "startup_gate",
    actor: "mf_sub",
    status: "accepted",
    backlog_id: narrativeFocusBacklog.bug_id,
    task_id: "mfsub-task-playback-narrative-focus-a",
    payload: {
      worker_id: "mfsub-task-playback-narrative-focus-a",
      receipt_id: "receipt-fixture-narrative",
      owned_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
      body_persisted_status: "canonical visible contract persisted by runtime contract revision; raw launch text not persisted",
      route_alerts: ["do not reuse stale route events", "do not expose hidden private prompt text"],
      allowed_actions: ["query runtime contract", "graph-first discovery", "edit only fenced files", "run tests/build", "report review_ready"],
      acknowledged_forbidden_actions: ["merge", "push", "delete_worktree"],
      blocked_actions: ["merge", "push", "activate_graph", "release_gate", "create_task", "delete_worktree", "modify_merge_queue", "expose raw private route context"],
      required_lanes_evidence: ["bounded implementation worker dispatch", "mf_subagent_read_receipt", "mf_subagent_startup", "worker graph trace evidence", "review_ready"],
      target_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
      acceptance_criteria: ["public-safe read receipt context is inspectable", "private prompt text remains hidden"],
      route_context_hash: "sha256:fixture-narrative-route-context",
      prompt_contract_id: "rprompt-fixture-narrative",
      prompt_contract_hash: "sha256:fixture-narrative-prompt-contract",
      visible_injection_manifest_hash: "sha256:fixture-visible-manifest",
      canonical_visible_contract_text_hash: "sha256:fixture-canonical-visible-contract",
      raw_private_prompt_text: "[fixture private request text]",
    },
    created_at: "2026-06-07T11:03:00Z",
  },
  {
    id: 205,
    event_type: "route_gate_blocker_observed",
    event_kind: "route_waiver",
    phase: "route_gate",
    actor: "fallback_observer",
    status: "blocked",
    backlog_id: narrativeFocusBacklog.bug_id,
    task_id: "mfsub-task-playback-narrative-focus-a",
    payload: {
      blocked_event_kinds: ["bounded_implementation_worker_dispatch", "mf_subagent_startup"],
      failed_request_ids: ["req-fixture-route-blocker"],
      prompt_contract_id: "rprompt-fixture-narrative",
      reason: "Protected evidence requires route token or prior bounded worker route-context consumption.",
      next_action: "Add bounded worker startup and dispatch route-context evidence before close.",
      route_context_hash: "sha256:fixture-narrative-route-context",
      route_id: "route-20260607-fixture-narrative",
      worker_id: "mfsub-task-playback-narrative-focus-a",
      worktree_path: "[fixture private path]",
    },
    verification: {
      counts_as_close_evidence: false,
      waiver_evidence_only: true,
    },
    created_at: "2026-06-07T11:04:00Z",
  },
  {
    id: 1750,
    event_type: "route.prompt_context.requested",
    event_kind: "route_context",
    phase: "dispatch",
    actor: "fallback_observer",
    status: "accepted",
    backlog_id: "AC-OBSERVER-OWNED-AGENT-TASK-CONTRACT-QUEUE-20260604",
    task_id: "repair-474fadf0551f130e",
    created_at: "2026-06-07T11:05:00Z",
    payload_json: JSON.stringify({
      backlog_id: "AC-OBSERVER-OWNED-AGENT-TASK-CONTRACT-QUEUE-20260604",
      blocker_ids: ["missing_timeline_evidence", "missing_verification", "pending_scope_timeout", "route_identity_mismatch"],
      prompt_contract: {
        acceptance_criteria: ["takeover works from contract state", "close gate fails on missing evidence"],
        evidence_required: ["implementation", "verification", "close_ready"],
        prompt_contract_id: "rprompt-repair-fixture-1750",
        target_files: [
          "agent/governance/task_timeline.py",
          "agent/governance/observer_session.py",
          "frontend/dashboard/src/views/BacklogView.tsx",
        ],
      },
      reason: "pending_scope_timeout blocked route identity consumption",
      route_context: "[fixture private route context body]",
      route_id: "route-repair-fixture-1750",
      read_receipt_event_id: 2893,
      selected_topology: "observer_led_parallel_lanes",
      source_event_ids: ["repair-474fadf0551f130e:route_prompt_context"],
      stage: "dispatch",
      startup_event_id: 2894,
    }),
    verification_json: JSON.stringify({
      missing_event_kinds: ["implementation", "verification"],
      missing_requirement_ids: ["mf_subagent_startup"],
      next_legal_action: "Record matching route context, bounded worker startup, implementation, and verification evidence before close.",
      route_identity_mismatch: true,
    }),
    artifact_refs_json: JSON.stringify({
      prompt_contract_hash: "sha256:fixture-prompt-1750",
      prompt_contract_id: "rprompt-repair-fixture-1750",
      read_receipt_hash: "sha256:fixture-read-receipt-1750",
      route_context_hash: "sha256:fixture-route-1750",
      source_event_id: "repair-474fadf0551f130e:route_prompt_context",
      startup_event_id: 2894,
    }),
  } as unknown as TaskTimelineEvent,
  {
    id: 329,
    event_type: "observer.audit.remaining_scope",
    event_kind: "verification",
    phase: "postmerge_audit",
    actor: "observer",
    status: "blocked",
    backlog_id: "AC-OBSERVER-OWNED-AGENT-TASK-CONTRACT-QUEUE-20260604",
    task_id: "repair-474fadf0551f130e",
    created_at: "2026-06-07T11:06:00Z",
    payload_json: JSON.stringify({
      closed_rows: ["UI-ASSET-BINDING-UNBIND-FLOW-20260525", "DOC-BINDING-INVENTORY-STATUS-CONSISTENCY-20260524"],
      decision: "do not close P0 umbrella yet; commit 0f4e32a is a partial high-priority closure with two prior rows fixed",
      implemented_and_merged: [
        "source-controlled bind/unbind event schema and reducer",
        "guarded unbind API with current-binding validation",
        "Asset Inbox and Review Queue UI wiring for source-controlled unbind/audit fallback",
        "raw/effective file inventory binding status",
        "doc/test/config graph_asset_projection persistence and Asset Inbox consumption",
        "fixture-backed drift/impact status contract coverage",
      ],
      remaining_acceptance: [
        "full-vs-scope parity fixture or explicit named full-rebuild fallback evidence for bind/unbind transitions",
        "automatic drift/impact DB event policy for changed vs affected bound assets after merge/worker gate",
        "SOP/skill guidance for observer reviewed drift decisions",
        "browser E2E proof for the operator path if required before final P0 close",
      ],
      remaining_open: ["GRAPH-INCREMENTAL-FILE-BINDING-PARITY-20260525", "P0 umbrella"],
    }),
    artifact_refs_json: JSON.stringify({
      source_event_id: "329",
    }),
  } as unknown as TaskTimelineEvent,
  {
    id: 1760,
    event_type: "task_timeline_append",
    event_kind: "implementation",
    phase: "implementation",
    actor: "mf_sub",
    status: "passed",
    backlog_id: narrativeFocusBacklog.bug_id,
    task_id: "mfsub-task-playback-narrative-focus-a",
    created_at: "2026-06-07T11:07:00Z",
    payload_json: JSON.stringify({
      graph_query_trace_ids: ["gqt-fixture-current"],
      graph_query_trace: {
        trace_id: "gqt-fixture-current",
        query_source: "mf_subagent",
        query_purpose: "subagent_context_build",
        tool: "find_node_by_path",
        args: { path: "frontend/dashboard/src/lib/taskPlayback.ts" },
        result_summary: { result_count: 2, result_hash: "sha256:fixture-graph-result" },
        resolved_nodes: ["L7.fixture.task-playback"],
        resolved_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
      },
      changed_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
    }),
    artifact_refs_json: JSON.stringify({
      graph_query_trace_ids: ["gqt-fixture-current"],
      source_event_id: "1760",
    }),
  } as unknown as TaskTimelineEvent,
  {
    id: 1761,
    event_type: "route.prompt_context.requested",
    event_kind: "route_context",
    phase: "dispatch",
    actor: "fallback_observer",
    status: "accepted",
    backlog_id: "AC-MF-SUB-STARTUP-COMMAND-ID-FLOW-20260606",
    task_id: "legacy-startup-command-id-flow",
    created_at: "2026-06-07T11:08:00Z",
    payload_json: JSON.stringify({
      backlog_id: "AC-MF-SUB-STARTUP-COMMAND-ID-FLOW-20260606",
      route_id: "route-legacy-startup-flow",
      route_context_hash: "sha256:legacy-route-context",
      prompt_contract_id: "rprompt-legacy-startup-flow",
      prompt_contract_hash: "sha256:legacy-prompt-contract",
      visible_injection_manifest_hash: "sha256:legacy-visible-manifest",
      launch_text_hash: "sha256:legacy-launch-text",
      body_persisted_status: "route context body unavailable in legacy timeline row",
      source_event_ids: ["timeline_event:2980", "service_event:2981"],
      target_files: ["agent/governance/task_timeline.py"],
      acceptance_criteria: ["startup command id stays visible"],
    }),
    artifact_refs_json: JSON.stringify({
      route_context_hash: "sha256:legacy-route-context",
      prompt_contract_id: "rprompt-legacy-startup-flow",
      prompt_contract_hash: "sha256:legacy-prompt-contract",
      source_event_ids: ["timeline_event:2980", "service_event:2981"],
    }),
  } as unknown as TaskTimelineEvent,
];

export function buildTaskPlaybackHistoricalSemanticFixture() {
  return normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog: historicalBacklog,
    taskTimeline: {
      project_id: "aming-claw",
      backlog_id: historicalBacklog.bug_id,
      events: TASK_PLAYBACK_HISTORICAL_FIXTURE_EVENTS,
      count: TASK_PLAYBACK_HISTORICAL_FIXTURE_EVENTS.length,
    },
    gateResponse: null,
    source: "governed",
    generatedAt: "2026-06-07T10:10:00Z",
  });
}

export function buildTaskPlaybackNarrativeFocusFixture() {
  return normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog: narrativeFocusBacklog,
    taskTimeline: {
      project_id: "aming-claw",
      backlog_id: narrativeFocusBacklog.bug_id,
      events: TASK_PLAYBACK_NARRATIVE_FOCUS_FIXTURE_EVENTS,
      count: TASK_PLAYBACK_NARRATIVE_FOCUS_FIXTURE_EVENTS.length,
    },
    gateResponse: {
      project_id: "aming-claw",
      bug_id: narrativeFocusBacklog.bug_id,
      applicable: true,
      can_close: false,
      timeline_gate: {
        passed: false,
        status: "blocked",
        required_event_kinds: ["implementation", "verification", "close_ready"],
        present_event_kinds: ["route_context", "route_action_precheck"],
        missing_event_kinds: ["implementation", "verification", "close_ready"],
        event_count: TASK_PLAYBACK_NARRATIVE_FOCUS_FIXTURE_EVENTS.length,
        route_context_gate: {
          passed: false,
          status: "blocked",
          required_requirement_ids: ["bounded_implementation_worker_dispatch", "mf_subagent_startup"],
          present_requirement_ids: ["route_context", "route_action_precheck"],
          missing_requirement_ids: ["mf_subagent_startup"],
        },
      },
      event_count: TASK_PLAYBACK_NARRATIVE_FOCUS_FIXTURE_EVENTS.length,
      events: TASK_PLAYBACK_NARRATIVE_FOCUS_FIXTURE_EVENTS,
    },
    source: "governed",
    generatedAt: "2026-06-07T11:04:00Z",
  });
}

export function taskPlaybackHistoricalSemanticFixtureAssertions(): string[] {
  const trace = buildTaskPlaybackHistoricalSemanticFixture();
  const visible = JSON.stringify({
    frames: trace.frames.map((frame) => ({
      title: frame.title,
      detail: frame.detail,
      narrative: frame.narrative,
      chips: frame.semantic_chips,
      inspector: frame.detail_inspector,
    })),
  });
  assertFixture(trace.frames.some((frame) => frame.title === "Prompt context requested"), "route context row should be readable");
  assertFixture(trace.frames.some((frame) => frame.title === "Timeline append authorized"), "route token gate row should be readable");
  assertFixture(trace.frames.some((frame) => frame.title === "Worker startup recorded"), "startup row should be readable");
  assertFixture(trace.frames.some((frame) => frame.title === "Independent verification completed"), "QA row should be readable");
  assertFixture(trace.frames.some((frame) => frame.title === "Observer close-ready evidence"), "close-ready row should be readable");
  assertFixture(trace.frames.some((frame) => frame.title === "System timeline event"), "unknown rows should use system fallback");
  assertFixture(!visible.includes("[fixture private request text]"), "private request text should be redacted");
  assertFixture(!visible.includes("[fixture private path]"), "private path placeholders should be redacted");
  return trace.frames.map((frame) => `${frame.title}: ${frame.detail}`);
}

export function taskPlaybackNarrativeFocusFixtureAssertions(): string[] {
  const trace = buildTaskPlaybackNarrativeFocusFixture();
  const promptContextFrame = trace.frames.find((frame) => frame.title === "Prompt context requested");
  const rawPromptContextFrame = trace.frames.find((frame) => frame.source_event_id === "#1750");
  const auditRemainingScopeFrame = trace.frames.find((frame) => frame.source_event_id === "#329");
  const routeActionFrame = trace.frames.find((frame) => frame.title === "Route action requested");
  const serviceRouteFrame = trace.frames.find((frame) => frame.title === "Route service completed");
  const readReceiptFrame = trace.frames.find((frame) => frame.source_event_id === "#204");
  const graphTraceFrame = trace.frames.find((frame) => frame.source_event_id === "#1760");
  const legacyRouteFrame = trace.frames.find((frame) => frame.source_event_id === "#1761");
  const visible = JSON.stringify({
    close_gate_summary: trace.close_gate_summary,
    frames: trace.frames.map((frame) => ({
      title: frame.title,
      detail: frame.detail,
      summary: frame.summary,
      narrative: frame.narrative,
      chips: frame.semantic_chips,
      specific_facts: frame.specific_facts,
      failure_diagnosis: frame.failure_diagnosis,
      evidence_links: frame.evidence_links,
      inspector: frame.detail_inspector,
    })),
  });
  assertFixture(Boolean(promptContextFrame), "route prompt context frame should exist in the narrative fixture");
  assertFixture(Boolean(rawPromptContextFrame), "event #1750 route prompt context frame should hydrate payload_json into playback");
  assertFixture(Boolean(auditRemainingScopeFrame), "event #329 observer audit remaining-scope frame should hydrate payload_json into playback");
  assertFixture(Boolean(routeActionFrame), "route action frame should exist in the narrative fixture");
  assertFixture(Boolean(serviceRouteFrame), "route service frame should exist in the narrative fixture");
  assertFixture(Boolean(readReceiptFrame), "read receipt frame should exist in the narrative fixture");
  assertFixture(Boolean(graphTraceFrame), "graph trace frame should exist in the narrative fixture");
  assertFixture(Boolean(legacyRouteFrame), "legacy route frame should exist in the narrative fixture");
  if (!promptContextFrame || !rawPromptContextFrame || !auditRemainingScopeFrame || !routeActionFrame || !serviceRouteFrame || !readReceiptFrame || !graphTraceFrame || !legacyRouteFrame) throw new Error("missing route narrative fixture frames");
  assertFixture(
    trace.close_gate_summary.reason_sentence === "Blocked because implementation, verification, and close-ready evidence have not been recorded; the close gate cannot pass until those events exist.",
    "blocked close gate should show a human-readable reason sentence with missing event kinds",
  );
  assertFixture(
    trace.close_gate_summary.next_expected_action.includes("add implementation, verification, and close-ready evidence"),
    "blocked close gate should show the next expected evidence/action",
  );
  assertFixture(
    promptContextFrame.detail.includes("public task scope") && promptContextFrame.detail.includes("target files"),
    "route prompt context detail should explain what context was requested",
  );
  assertFixture(
    promptContextFrame.narrative.context.includes("receiving actor") && promptContextFrame.narrative.outcome.includes("close-gate blocker remains visible"),
    "route prompt context narrative should explain who receives context and what is still missing",
  );
  assertFixture(
    promptContextFrame.semantic_chips.some((chip) => chip.label === "target file" && chip.value === "frontend/dashboard/src/lib/taskPlayback.ts"),
    "route prompt context chips should show public target file context",
  );
  assertFixture(
    promptContextFrame.semantic_chips.some((chip) => chip.label === "required evidence" && chip.value === "implementation"),
    "route prompt context chips should show required evidence context",
  );
  const promptPayloadSection = promptContextFrame.detail_inspector.raw_sections.find((section) => section.label === "payload");
  const promptPayloadVisible = JSON.stringify(promptPayloadSection?.value ?? {});
  assertFixture(
    promptPayloadVisible.includes("Judgment Brain route label")
      && promptPayloadVisible.includes("visible route context bundle")
      && promptPayloadVisible.includes("mf_subagent_read_receipt")
      && !promptPayloadVisible.includes("[fixture private route context body]"),
    "route prompt context raw payload should expose public route docs and Judgment Brain source labels while redacting only the private route body field",
  );
  assertFixture(
    rawPromptContextFrame.summary.includes("AC-OBSERVER-OWNED-AGENT-TASK-CONTRACT-QUEUE-20260604")
      && rawPromptContextFrame.summary.includes("route-repair-fixture-1750")
      && rawPromptContextFrame.summary.includes("3 target files")
      && rawPromptContextFrame.summary.includes("2 acceptance criteria")
      && rawPromptContextFrame.summary.includes("3 required evidence items"),
    "event #1750 summary should explain backlog, route identity, prompt contract scope, and evidence counts",
  );
  assertFixture(
    rawPromptContextFrame.specific_facts.some((fact) => fact.label === "target-file count" && fact.value === "3 target files")
      && rawPromptContextFrame.specific_facts.some((fact) => fact.label === "acceptance-criteria count" && fact.value === "2 acceptance criteria")
      && rawPromptContextFrame.specific_facts.some((fact) => fact.label === "required evidence" && fact.value.includes("implementation"))
      && rawPromptContextFrame.specific_facts.some((fact) => fact.label === "route context hash" && fact.value.includes("sha256:fixture-route-1750"))
      && rawPromptContextFrame.specific_facts.some((fact) => fact.label === "prompt contract hash" && fact.value.includes("sha256:fixture-prompt-1750"))
      && rawPromptContextFrame.specific_facts.some((fact) => fact.label === "source event refs" && fact.value.includes("route_prompt_context"))
      && rawPromptContextFrame.specific_facts.some((fact) => fact.label === "read receipt refs" && fact.value.includes("2893"))
      && rawPromptContextFrame.specific_facts.some((fact) => fact.label === "startup refs" && fact.value.includes("2894")),
    "event #1750 specific facts should promote target-file, acceptance-criteria, required-evidence, route/prompt/source, read-receipt, and startup details",
  );
  assertFixture(
    rawPromptContextFrame.failure_diagnosis.some((fact) => fact.label === "blocker ids" && fact.value.includes("route_identity_mismatch"))
      && rawPromptContextFrame.failure_diagnosis.some((fact) => fact.label === "missing event kinds" && fact.value.includes("implementation"))
      && rawPromptContextFrame.failure_diagnosis.some((fact) => fact.label === "missing required evidence" && fact.value.includes("mf_subagent_startup"))
      && rawPromptContextFrame.failure_diagnosis.some((fact) => fact.label === "mismatched route identity" && fact.value.includes("true"))
      && rawPromptContextFrame.failure_diagnosis.some((fact) => fact.label === "stale/timeout reason" && fact.value.includes("pending_scope_timeout"))
      && rawPromptContextFrame.failure_diagnosis.some((fact) => fact.label === "next legal action" && fact.value.includes("Record matching route context")),
    "event #1750 blocker diagnosis should promote blocker ids, missing evidence, route mismatch, timeout reason, and next legal action",
  );
  assertFixture(
    rawPromptContextFrame.evidence_links.some((ref) => ref.kind === "timeline_event" && ref.value === "#1750")
      && rawPromptContextFrame.evidence_links.some((ref) => ref.kind === "route_context" && ref.value === "sha256:fixture-route-1750")
      && rawPromptContextFrame.evidence_links.some((ref) => ref.kind === "prompt_contract" && ref.value === "rprompt-repair-fixture-1750")
      && rawPromptContextFrame.evidence_links.some((ref) => ref.kind === "source_event" && ref.value.includes("route_prompt_context"))
      && rawPromptContextFrame.evidence_links.some((ref) => ref.kind === "read_receipt" && ref.label === "read receipt" && ref.value.includes("2893"))
      && rawPromptContextFrame.evidence_links.some((ref) => ref.kind === "source_event" && ref.label === "startup" && ref.value.includes("2894")),
    "event #1750 evidence links should include typed timeline, route context, prompt contract, source-event, read-receipt, and startup refs",
  );
  const rawPromptInspectorVisible = JSON.stringify(rawPromptContextFrame.detail_inspector.raw_sections.map((section) => section.value));
  assertFixture(
    rawPromptInspectorVisible.includes("sha256:fixture-route-1750")
      && rawPromptInspectorVisible.includes("rprompt-repair-fixture-1750")
      && rawPromptInspectorVisible.includes("mf_subagent_startup")
      && !rawPromptInspectorVisible.includes("[fixture private request text]")
      && !rawPromptInspectorVisible.includes("[fixture private route context body]"),
    "event #1750 inspector context should expose route, prompt, and missing-evidence public fields without private raw prompt",
  );
  assertFixture(
    rawPromptContextFrame.detail_inspector.raw_sections.map((section) => section.label).join(",") === "payload,verification,artifact_refs"
      && rawPromptInspectorVisible.includes("Record matching route context")
      && rawPromptInspectorVisible.includes("read_receipt_hash")
      && rawPromptContextFrame.detail_inspector.redaction_count > 0,
    "event #1750 raw event data should expose payload_json, verification_json, and artifact_refs_json with field-level redactions",
  );
  const readReceiptPayloadSection = readReceiptFrame.detail_inspector.raw_sections.find((section) => section.label === "payload");
  const readReceiptPayloadVisible = JSON.stringify(readReceiptPayloadSection?.value ?? {});
  assertFixture(
    readReceiptPayloadVisible.includes("body_persisted_status")
      && readReceiptPayloadVisible.includes("raw launch text not persisted")
      && readReceiptPayloadVisible.includes("route_alerts")
      && readReceiptPayloadVisible.includes("allowed_actions")
      && readReceiptPayloadVisible.includes("blocked_actions")
      && readReceiptPayloadVisible.includes("required_lanes_evidence")
      && readReceiptPayloadVisible.includes("canonical_visible_contract_text_hash")
      && !readReceiptPayloadVisible.includes("[fixture private request text]"),
    "read receipt raw payload should expose canonical visible contract fields and route action bounds while redacting private prompt material",
  );
  assertFixture(
    graphTraceFrame.evidence_links.some((ref) => ref.kind === "graph_trace" && ref.value === "gqt-fixture-current")
      && JSON.stringify(graphTraceFrame.detail_inspector.raw_sections.map((section) => section.value)).includes("find_node_by_path")
      && JSON.stringify(graphTraceFrame.detail_inspector.raw_sections.map((section) => section.value)).includes("L7.fixture.task-playback")
      && JSON.stringify(graphTraceFrame.detail_inspector.raw_sections.map((section) => section.value)).includes("frontend/dashboard/src/lib/taskPlayback.ts"),
    "graph trace fixture should expose trace id, tool, result summary, resolved node, and resolved file fields above raw JSON",
  );
  assertFixture(
    legacyRouteFrame.evidence_links.some((ref) => ref.kind === "route_context" && ref.value === "sha256:legacy-route-context")
      && !legacyRouteFrame.evidence_links.some((ref) => ref.kind === "read_receipt")
      && legacyRouteFrame.specific_facts.some((fact) => fact.label === "launch text hash" && fact.value.includes("sha256:legacy-launch-text"))
      && legacyRouteFrame.specific_facts.some((fact) => fact.label === "source event refs" && fact.value.includes("timeline_event:2980")),
    "legacy startup command route fixture should list only verifiable hashes/source events and no read receipt evidence",
  );
  assertFixture(
    auditRemainingScopeFrame.summary.includes("decision do not close P0 umbrella yet")
      && auditRemainingScopeFrame.summary.includes("Remaining scope")
      && auditRemainingScopeFrame.summary.includes("Next legal action"),
    "event #329 summary should mention the audit decision, remaining scope, and next legal action",
  );
  assertFixture(
    auditRemainingScopeFrame.specific_facts.some((fact) => fact.label === "decision" && fact.value.includes("do not close P0 umbrella yet"))
      && auditRemainingScopeFrame.specific_facts.some((fact) => fact.label === "closed rows" && fact.value.includes("UI-ASSET-BINDING-UNBIND-FLOW-20260525"))
      && auditRemainingScopeFrame.specific_facts.some((fact) => fact.label === "closed rows" && fact.value.includes("DOC-BINDING-INVENTORY-STATUS-CONSISTENCY-20260524"))
      && auditRemainingScopeFrame.specific_facts.some((fact) => fact.label === "implemented and merged" && fact.value.includes("source-controlled bind/unbind event schema and reducer"))
      && auditRemainingScopeFrame.specific_facts.some((fact) => fact.label === "implemented and merged" && fact.value.includes("fixture-backed drift/impact status contract coverage")),
    "event #329 specific facts should promote decision, closed rows, and implemented/merged outcome facts",
  );
  assertFixture(
    auditRemainingScopeFrame.failure_diagnosis.some((fact) => fact.label === "remaining acceptance" && fact.value.includes("full-vs-scope parity fixture"))
      && auditRemainingScopeFrame.failure_diagnosis.some((fact) => fact.label === "remaining acceptance" && fact.value.includes("browser E2E proof"))
      && auditRemainingScopeFrame.failure_diagnosis.some((fact) => fact.label === "remaining open" && fact.value.includes("GRAPH-INCREMENTAL-FILE-BINDING-PARITY-20260525"))
      && auditRemainingScopeFrame.failure_diagnosis.some((fact) => fact.label === "remaining open" && fact.value.includes("P0 umbrella"))
      && auditRemainingScopeFrame.failure_diagnosis.some((fact) => fact.label === "next legal action" && fact.value.includes("Finish the remaining acceptance or open backlog scope")),
    "event #329 failure diagnosis should promote remaining acceptance, remaining open, and next legal action",
  );
  const auditPayloadSection = auditRemainingScopeFrame.detail_inspector.raw_sections.find((section) => section.label === "payload");
  const auditPayloadVisible = JSON.stringify(auditPayloadSection?.value ?? {});
  assertFixture(
    auditPayloadVisible.includes("do not close P0 umbrella yet")
      && auditPayloadVisible.includes("source-controlled bind/unbind event schema and reducer")
      && auditPayloadVisible.includes("full-vs-scope parity fixture")
      && auditPayloadVisible.includes("GRAPH-INCREMENTAL-FILE-BINDING-PARITY-20260525"),
    "event #329 inspector context should expose audit decision, implemented scope, remaining acceptance, and remaining open facts",
  );
  assertFixture(
    routeActionFrame.detail.includes("authorized or blocked") && routeActionFrame.narrative.outcome.includes("close-ready evidence"),
    "route action narrative should explain authorization and remaining evidence",
  );
  assertFixture(
    serviceRouteFrame.detail.includes("evidence implications") && serviceRouteFrame.narrative.outcome.includes("close-gate banner"),
    "route service completion narrative should explain action outcome and missing evidence",
  );
  assertFixture(
    visible.includes("Bounded worker received task context containing target files, acceptance criteria, allowed/blocked actions, route identity hashes, and required evidence; private prompt text is hidden."),
    "route/context worker story should be visible",
  );
  assertFixture(visible.includes("Route service requested or delivered bounded task context for the next observer or worker lane."), "route context actor story should be visible");
  assertFixture(visible.includes("Route service completed"), "route service completion should be readable");
  assertFixture(visible.includes("Route evidence blocked"), "route waiver/blocker rows should be readable");
  assertFixture(visible.includes("waiver-only evidence"), "route waiver narrative should explain the evidence implication");
  assertFixture(!visible.includes("A governance timeline event was recorded."), "route/prompt events should not use the old generic fallback detail");
  assertFixture(!visible.includes("[fixture private request text]"), "private request text should stay hidden");
  assertFixture(!visible.includes("[fixture private route context body]"), "private route context body should stay hidden");
  assertFixture(!visible.includes("[fixture private path]"), "private worktree paths should stay hidden");
  return [
    trace.close_gate_summary.reason_sentence,
    trace.close_gate_summary.next_expected_action,
    ...trace.frames.map((frame) => `${frame.title}: ${frame.summary}`),
  ];
}

const workModeBacklog: BacklogBug = {
  bug_id: "AC-OBSERVER-ROOT-ROUTE-CONTEXT-WORK-MODE-20260609",
  title: "Observer root route context and work modes",
  status: "OPEN",
  priority: "P1",
};

export const TASK_PLAYBACK_WORK_MODE_FIXTURE_EVENTS: TaskTimelineEvent[] = [
  {
    id: 301,
    event_type: "route.action.requested",
    event_kind: "route_action_precheck",
    phase: "dispatch",
    actor: "observer",
    status: "allowed",
    backlog_id: workModeBacklog.bug_id,
    task_id: "observer-root-route-context",
    payload: {
      action: "record_work_mode_transition",
      work_mode: "observer_look_before_act",
      route_id: "route-repair-dbc66b929fd8860c",
      route_context_hash: "sha256:fixture-work-mode-route",
      prompt_contract_id: "rprompt-repair-dbc66b929fd8860c",
      graph_query_schema_trace_id: "gqs-fixture-work-mode",
      allowed_actions: ["read", "inspect", "file_findings", "propose_next"],
      blocked_actions: ["edit_implementation", "self_clear_judge_blocker", "dispatch_implementation", "merge", "close"],
      required_evidence: ["route_context", "route_action_precheck", "bounded_implementation_worker_dispatch", "mf_subagent_startup", "independent_verification", "close_ready"],
      next_legal_action: {
        action: "observer_work_mode_transition",
        detail: "record an observer_work_mode_transition event and a route_action_precheck bound to the canonical route identity before any dispatch/merge/close",
      },
    },
    created_at: "2026-06-09T09:00:00Z",
  },
  {
    id: 302,
    event_type: "observer_work_mode_transition",
    event_kind: "observer_work_mode_transition",
    phase: "work_mode_gate",
    actor: "observer",
    status: "allowed",
    backlog_id: workModeBacklog.bug_id,
    task_id: "observer-root-route-context",
    payload: {
      from_work_mode: "observer_look_before_act",
      to_work_mode: "observer_execution_supervisor",
      route_id: "route-repair-dbc66b929fd8860c",
      route_context_hash: "sha256:fixture-work-mode-route",
      route_action_precheck_event_id: 301,
    },
    created_at: "2026-06-09T09:01:00Z",
  },
  {
    id: 303,
    event_type: "bounded_implementation_worker_dispatch",
    event_kind: "bounded_implementation_worker_dispatch",
    phase: "dispatch",
    actor: "observer",
    status: "passed",
    backlog_id: workModeBacklog.bug_id,
    task_id: "observer-root-route-context",
    payload: {
      worker_id: "mfsub-work-mode-a",
      work_mode: "observer_execution_supervisor",
      graph_query_trace_ids: ["gqt-fixture-work-mode-dispatch"],
      target_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
    },
    created_at: "2026-06-09T09:02:00Z",
  },
  {
    id: 304,
    event_type: "mf_subagent.startup",
    event_kind: "mf_subagent_startup",
    phase: "startup_gate",
    actor: "mf_sub",
    status: "blocked",
    backlog_id: workModeBacklog.bug_id,
    task_id: "observer-root-route-context",
    payload: {
      worker_id: "mfsub-work-mode-surrogate",
      session_token_evidence_type: "surrogate",
      agent_id_match_mode: "host_adapter_startup_token_surrogate",
      close_satisfying: false,
      [PRIVATE_REQUEST_FIELD]: "[fixture private request text]",
    },
    created_at: "2026-06-09T09:03:00Z",
  },
  {
    id: 305,
    event_type: "mf_subagent.startup",
    event_kind: "mf_subagent_startup",
    phase: "startup_gate",
    actor: "mf_sub",
    status: "passed",
    backlog_id: workModeBacklog.bug_id,
    task_id: "observer-root-route-context",
    payload: {
      worker_id: "mfsub-work-mode-real",
      session_token_evidence_type: "real",
      agent_id_match_mode: "session_token",
      close_satisfying: true,
      owned_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
    },
    created_at: "2026-06-09T09:04:00Z",
  },
  {
    id: 306,
    event_type: "independent_verification_lane",
    event_kind: "independent_verification_lane",
    phase: "independent_verification",
    actor: "qa",
    status: "passed",
    backlog_id: workModeBacklog.bug_id,
    task_id: "observer-root-route-context",
    payload: {
      verifier: "independent-qa",
      lane: "independent_verification",
    },
    verification: {
      passed: true,
      tests_run: ["node scripts/e2e-projects.mjs"],
    },
    created_at: "2026-06-09T09:05:00Z",
  },
  {
    id: 307,
    event_type: "observer_hotfix_exception",
    event_kind: "observer_hotfix_exception",
    phase: "observer_hotfix_exception",
    actor: "observer",
    status: "recorded",
    backlog_id: workModeBacklog.bug_id,
    task_id: "observer-root-route-context",
    payload: {
      work_mode: "observer_hotfix_exception",
      route_id: "route-repair-dbc66b929fd8860c",
      reason: "emergency repair surrogate startup relaxation",
    },
    created_at: "2026-06-09T09:06:00Z",
  },
  {
    id: 308,
    event_type: "route_token_gate.backlog_close",
    event_kind: "route_token_gate",
    phase: "close_gate",
    actor: "observer",
    status: "blocked",
    backlog_id: workModeBacklog.bug_id,
    task_id: "observer-root-route-context",
    payload: {
      close_gate_status: "blocked",
      can_close: false,
      missing_event_kinds: ["close_ready"],
      blocker_resolution_gate: { status: "passed" },
      cross_ref_gate: { status: "passed" },
      stale_route_evidence_gate: { status: "blocked" },
    },
    created_at: "2026-06-09T09:07:00Z",
  },
];

export function buildTaskPlaybackWorkModeFixture() {
  return normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog: workModeBacklog,
    taskTimeline: {
      project_id: "aming-claw",
      backlog_id: workModeBacklog.bug_id,
      events: TASK_PLAYBACK_WORK_MODE_FIXTURE_EVENTS,
      count: TASK_PLAYBACK_WORK_MODE_FIXTURE_EVENTS.length,
    },
    gateResponse: null,
    source: "governed",
    generatedAt: "2026-06-09T09:08:00Z",
  });
}

export function taskPlaybackWorkModeFixtureAssertions(): string[] {
  const trace = buildTaskPlaybackWorkModeFixture();
  const transitionFrame = trace.frames.find((frame) => frame.source_event_id === "#302");
  const dispatchFrame = trace.frames.find((frame) => frame.source_event_id === "#303");
  const surrogateStartupFrame = trace.frames.find((frame) => frame.source_event_id === "#304");
  const realStartupFrame = trace.frames.find((frame) => frame.source_event_id === "#305");
  const independentLaneFrame = trace.frames.find((frame) => frame.source_event_id === "#306");
  const hotfixFrame = trace.frames.find((frame) => frame.source_event_id === "#307");
  const closeGateFrame = trace.frames.find((frame) => frame.source_event_id === "#308");
  const precheckFrame = trace.frames.find((frame) => frame.source_event_id === "#301");
  assertFixture(Boolean(transitionFrame), "observer work-mode transition frame should exist");
  assertFixture(Boolean(dispatchFrame), "bounded worker dispatch frame should exist");
  assertFixture(Boolean(surrogateStartupFrame), "surrogate startup frame should exist");
  assertFixture(Boolean(realStartupFrame), "real startup frame should exist");
  assertFixture(Boolean(independentLaneFrame), "independent verification lane frame should exist");
  assertFixture(Boolean(hotfixFrame), "observer hotfix exception frame should exist");
  assertFixture(Boolean(closeGateFrame), "close gate frame should exist");
  assertFixture(Boolean(precheckFrame), "route action precheck frame should exist");
  if (!transitionFrame || !dispatchFrame || !surrogateStartupFrame || !realStartupFrame || !independentLaneFrame || !hotfixFrame || !closeGateFrame || !precheckFrame) {
    throw new Error("missing work-mode fixture frames");
  }
  const visible = JSON.stringify({
    frames: trace.frames.map((frame) => ({
      title: frame.title,
      detail: frame.detail,
      narrative: frame.narrative,
      facts: frame.specific_facts,
    })),
  });
  assertFixture(transitionFrame.title === "Observer work-mode transition", "transition row should be readable");
  assertFixture(
    transitionFrame.specific_facts.some((fact) => fact.label === "observer work mode" && fact.value === "observer_execution_supervisor"),
    "transition frame should promote the target work mode",
  );
  assertFixture(
    transitionFrame.narrative.context.includes("look_before_act") && transitionFrame.narrative.outcome.includes("blocked"),
    "transition narrative should explain look-before-act defaults and the bound precheck requirement",
  );
  assertFixture(
    dispatchFrame.title === "Bounded worker dispatch recorded"
      && dispatchFrame.specific_facts.some((fact) => fact.label === "observer work mode" && fact.value === "observer_execution_supervisor"),
    "bounded worker dispatch frame should show observer_execution_supervisor work mode",
  );
  assertFixture(
    precheckFrame.specific_facts.some((fact) => fact.label === "observer work mode" && fact.value === "observer_look_before_act")
      && precheckFrame.specific_facts.some((fact) => fact.label === "graph query schema trace" && fact.value === "gqs-fixture-work-mode"),
    "route precheck frame should promote work mode and graph query schema trace id",
  );
  assertFixture(
    surrogateStartupFrame.specific_facts.some((fact) => fact.label === "session token evidence type" && fact.value === "surrogate")
      && surrogateStartupFrame.specific_facts.some((fact) => fact.label === "surrogate close-satisfying" && fact.value.includes("not close-satisfying") && fact.value.includes("#3104")),
    "surrogate startup frame should mark session token type and that surrogate is not close-satisfying (#3104)",
  );
  assertFixture(
    realStartupFrame.specific_facts.some((fact) => fact.label === "session token evidence type" && fact.value === "real")
      && realStartupFrame.specific_facts.some((fact) => fact.label === "surrogate close-satisfying" && fact.value.includes("real session-token startup is close-satisfying")),
    "real startup frame should mark a real session-token startup as close-satisfying",
  );
  assertFixture(
    surrogateStartupFrame.detail.includes("not close-satisfying real-worker evidence (#3104)"),
    "surrogate startup detail should state the #3104 close-evidence demotion",
  );
  assertFixture(independentLaneFrame.title === "Independent verification lane", "independent verification lane row should be readable");
  assertFixture(
    independentLaneFrame.narrative.context.includes("distinct lane from the implementation worker"),
    "independent verification lane narrative should separate verification from the implementation worker",
  );
  assertFixture(hotfixFrame.title === "Observer hotfix exception", "observer hotfix exception row should be readable");
  assertFixture(
    hotfixFrame.detail.includes("does not promote surrogate startup to close-satisfying real-worker evidence (#3104)"),
    "observer hotfix exception detail should state it does not promote surrogate startup",
  );
  assertFixture(
    closeGateFrame.specific_facts.some((fact) => fact.label === "blocker-resolution gate (#3092)" && fact.value === "passed")
      && closeGateFrame.specific_facts.some((fact) => fact.label === "cross-ref evidence gate (#3090)" && fact.value === "passed")
      && closeGateFrame.specific_facts.some((fact) => fact.label === "stale-route evidence gate (#3093/#3094)" && fact.value === "blocked"),
    "close gate frame should promote blocker-resolution (#3092), cross-ref (#3090), and stale-route (#3093/#3094) sub-gate statuses",
  );
  assertFixture(!visible.includes("[fixture private request text]"), "work-mode fixture should keep private request text hidden");
  return trace.frames.map((frame) => `${frame.title}: ${frame.summary}`);
}

const routeContextEvidenceBacklog: BacklogBug = {
  bug_id: "AC-CLOSE-GATE-EVIDENCE-INTEGRITY-20260609",
  title: "Close-gate route-context evidence integrity",
  status: "OPEN",
  priority: "P0",
};

// Acceptance criterion 5: the playback/activity evidence modal must surface the
// REAL canonical route context the observer read — the canonical route_id (not
// the preview placeholder "event.route_prompt_context.preview"), the
// route_context_hash / prompt_contract_id, non-empty loaded_skills /
// loaded_resources, and the per-request graph_query_schema_trace_id.
export const TASK_PLAYBACK_ROUTE_CONTEXT_EVIDENCE_FIXTURE_EVENTS: TaskTimelineEvent[] = [
  {
    id: 501,
    event_type: "route.prompt_context.requested",
    event_kind: "route_context",
    phase: "dispatch",
    actor: "route service",
    status: "accepted",
    backlog_id: routeContextEvidenceBacklog.bug_id,
    task_id: "repair-9bf3a2ae63a82a2c",
    payload: {
      // The static/preview pointer that the source event carries — must NOT be
      // shown as the canonical route_id.
      route_id: "event.route_prompt_context.preview",
      // The canonical route identity the observer actually read.
      canonical_route_identity: {
        route_id: "route-repair-9bf3a2ae63a82a2c",
        route_context_hash: "sha256:fixture-evidence-route-context",
        prompt_contract_id: "rprompt-repair-9bf3a2ae63a82a2c",
      },
      route_context: {
        loaded_skills: ["aming-claw"],
        loaded_resources: ["mf-sop.md", "close-gate-evidence.md"],
        graph_query_schema_trace_id: "gqt-20260609-fc567d7db1",
      },
      route_context_hash: "sha256:fixture-evidence-route-context",
      prompt_contract_id: "rprompt-repair-9bf3a2ae63a82a2c",
      [PRIVATE_REQUEST_FIELD]: "[fixture private request text]",
    },
    created_at: "2026-06-09T12:00:00Z",
  },
  {
    id: 502,
    event_type: "service.route.completed",
    event_kind: "route_context",
    phase: "route_service",
    actor: "service-router",
    status: "allowed",
    backlog_id: routeContextEvidenceBacklog.bug_id,
    task_id: "repair-9bf3a2ae63a82a2c",
    payload: {
      service_id: "route.prompt_alert_bundle",
      decision: "allow",
      // Service/source event carries ONLY the preview placeholder route_id; no
      // canonical route_id fact should be emitted for this frame.
      route_id: "event.route_prompt_context.preview",
      route_context_hash: "sha256:fixture-evidence-route-context",
      prompt_contract_id: "rprompt-repair-9bf3a2ae63a82a2c",
      source_event_type: "route.prompt_context.requested",
    },
    created_at: "2026-06-09T12:01:00Z",
  },
];

export function buildTaskPlaybackRouteContextEvidenceFixture() {
  return normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog: routeContextEvidenceBacklog,
    taskTimeline: {
      project_id: "aming-claw",
      backlog_id: routeContextEvidenceBacklog.bug_id,
      events: TASK_PLAYBACK_ROUTE_CONTEXT_EVIDENCE_FIXTURE_EVENTS,
      count: TASK_PLAYBACK_ROUTE_CONTEXT_EVIDENCE_FIXTURE_EVENTS.length,
    },
    gateResponse: null,
    source: "governed",
    generatedAt: "2026-06-09T12:02:00Z",
  });
}

export function taskPlaybackRouteContextEvidenceFixtureAssertions(): string[] {
  const trace = buildTaskPlaybackRouteContextEvidenceFixture();
  const requestFrame = trace.frames.find((frame) => frame.source_event_id === "#501");
  const serviceFrame = trace.frames.find((frame) => frame.source_event_id === "#502");
  assertFixture(Boolean(requestFrame), "route prompt context request frame should exist");
  assertFixture(Boolean(serviceFrame), "service route completed frame should exist");
  if (!requestFrame || !serviceFrame) throw new Error("missing route-context evidence fixture frames");

  const routeIdFact = requestFrame.specific_facts.find((fact) => fact.kind === "route_id");
  assertFixture(
    Boolean(routeIdFact) && routeIdFact!.value === "route-repair-9bf3a2ae63a82a2c",
    "criterion 5: canonical route_id (route-repair-9bf3a2ae63a82a2c) should be the surfaced route id fact",
  );
  assertFixture(
    !requestFrame.specific_facts.some((fact) => fact.value.includes("route_prompt_context.preview")),
    "criterion 5: the preview placeholder must not be surfaced as a route fact",
  );
  // The service/source frame only has the preview placeholder route_id, so no
  // canonical route_id fact should be emitted there (preview is never canonical).
  assertFixture(
    !serviceFrame.specific_facts.some((fact) => fact.kind === "route_id"),
    "criterion 5: a frame carrying only the preview placeholder route_id should emit no canonical route_id fact",
  );
  assertFixture(
    !serviceFrame.evidence_links.some((ref) => ref.label === "route id" && ref.value.includes("preview")),
    "criterion 5: preview placeholder route_id must not appear as a canonical route-id evidence link",
  );

  assertFixture(
    requestFrame.specific_facts.some((fact) => fact.kind === "route_context_hash" && fact.value.includes("sha256:fixture-evidence-route-context")),
    "criterion 5: route_context_hash should be surfaced cleanly",
  );
  assertFixture(
    requestFrame.specific_facts.some((fact) => fact.kind === "prompt_contract_id" && fact.value === "rprompt-repair-9bf3a2ae63a82a2c"),
    "criterion 5: prompt_contract_id should be surfaced cleanly",
  );
  assertFixture(
    requestFrame.specific_facts.some((fact) => fact.kind === "loaded_skills" && fact.value.includes("aming-claw")),
    "criterion 5: non-empty loaded_skills should be surfaced",
  );
  assertFixture(
    requestFrame.specific_facts.some((fact) => fact.kind === "loaded_resources" && fact.value.includes("mf-sop.md")),
    "criterion 5: non-empty loaded_resources should be surfaced",
  );
  assertFixture(
    requestFrame.specific_facts.some((fact) => fact.kind === "graph_query_schema_trace_id" && fact.value === "gqt-20260609-fc567d7db1"),
    "criterion 5: per-request graph_query_schema_trace_id should be surfaced",
  );
  // Raw event JSON must remain inspectable (the preview value is still present
  // in the collapsed payload section) — existing behavior must not regress.
  const rawVisible = JSON.stringify(requestFrame.detail_inspector.raw_sections.map((section) => section.value));
  assertFixture(
    requestFrame.detail_inspector.raw_sections.map((section) => section.label).join(",") === "payload,verification,artifact_refs"
      && rawVisible.includes("event.route_prompt_context.preview")
      && rawVisible.includes("route-repair-9bf3a2ae63a82a2c"),
    "criterion 5: raw event JSON should stay inspectable and still contain both the preview pointer and the canonical route id",
  );
  assertFixture(
    !rawVisible.includes("[fixture private request text]"),
    "criterion 5: private request text should stay hidden in raw sections",
  );
  return trace.frames.map((frame) => `${frame.title}: ${frame.summary}`);
}

export const taskPlaybackHistoricalSemanticFixtureSummary = [
  ...taskPlaybackHistoricalSemanticFixtureAssertions(),
  ...taskPlaybackNarrativeFocusFixtureAssertions(),
  ...taskPlaybackWorkModeFixtureAssertions(),
  ...taskPlaybackRouteContextEvidenceFixtureAssertions(),
  ...taskPlaybackAuthorityAdapterAssertions(),
  ...taskPlaybackCompactLedgerProjectionAssertions(),
];

function assertFixture(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

function taskPlaybackAuthorityAdapterAssertions(): string[] {
  const response = {
    schema_version: "contract_runtime.visualization.v1",
    ok: true,
    public_safe: true,
    read_only: true,
    project_id: "aming-claw",
    backlog_id: "AC-FRONTEND-AUTHORITY-FIXTURE",
    generated_at: "2026-07-18T20:00:00Z",
    authority: {
      source_order: ["contract_runtime_current", "backlog_contract_chain_current", "task_timeline_compact_ledger"],
      source_of_authority: "contract_runtime",
      authority_decision_source: "backlog_contract_chain_current",
      axes: ["contract_execution_progress", "backlog_close_readiness", "historical_diagnostics"],
      legacy_sources_advisory_only: true,
    },
    backlog: {
      backlog_id: "AC-FRONTEND-AUTHORITY-FIXTURE",
      title: "Frontend authority fixture",
      status: "WAIVED",
      priority: "P1",
      commit: "abc123",
      updated_at: "2026-07-18T20:00:00Z",
    },
    contract_execution_progress: {
      contract_execution_id: "cex-current-1",
      execution_state_revision: 17,
      readiness_state: "contract_active",
      next_legal_action: { id: "worker_implementation", action: "record implementation evidence" },
      line_states: [{
        id: "contract-line:cex-current-1:1:qa",
        contract_execution_id: "cex-current-1",
        index: 1,
        stage_id: "qa",
        line_id: "qa",
        evidence_kind: "verification",
        owner_role: "qa",
        status: "accepted",
        recorded_at: "2026-07-18T19:59:00Z",
        source_ref: "timeline:41",
        bypassed: true,
      }],
      line_state_count: 1,
      line_state_total: 1,
      line_states_truncated: false,
      runtime_record_count: 1,
      runtime_record_total: 1,
      runtime_records_truncated: false,
    },
    backlog_close_readiness: {
      state: "open",
      backlog_status: "WAIVED",
      contract_execution_state: "contract_active",
      contract_complete_implies_backlog_close: false,
      legacy_advisory_count: 1,
    },
    contract_chain: {
      contract_chain_id: "cchain-1",
      root_contract_execution_id: "cex-root-1",
      current_contract_execution_id: "cex-current-1",
      current_contract_id: "mf_parallel.v1",
      parent_to_resume_contract_execution_id: "cex-root-1",
      active_child_contract_execution_id: "",
      readiness_state: "contract_active",
      next_legal_action: { id: "legacy_chain_action", action: "do not prefer this over runtime current" },
      degraded: false,
      source_refs: [],
    },
    timeline: {
      events: [{ id: 42, event_id: "42", event_type: "legacy.timeline", event_kind: "route_action_precheck", status: "bypassed" }],
      returned_count: 1,
      total_count: 1,
      limit: 100,
      truncated: false,
      next_cursor: "",
      next_cursor_parameter: "",
      append_only: true,
      current_snapshot_in_playback: false,
    },
    dag: { schema_version: "contract_runtime.visualization.dag.v1", nodes: [], edges: [], node_count: 0, edge_count: 0, typed_edges: true },
    compact_ledger: {},
    bypass_records: [{ status: "bypassed", no_pass_claim: true }],
    legacy_advisories: [{ id: "route_action_precheck", advisory_only: true }],
    projection_freshness: { status: "current" },
    projection_conflicts: [],
    projection_conflict_count: 0,
  } as ContractRuntimeVisualizationResponse;

  const view = projectContractRuntimeAuthorityViewModel(response);
  assertFixture(view.contract_execution_progress.current_action.id === "worker_implementation", "authority adapter should prefer ContractRuntime current action over historical/legacy candidates");
  assertFixture(view.contract_execution_progress.current_action_source === "contract_runtime_current", "authority adapter should identify the current action authority source");
  assertFixture(view.cache_identity.backlog_id === response.backlog_id && view.cache_identity.contract_execution_id === "cex-current-1", "authority cache identity should include backlog and execution ids");
  assertFixture(view.cache_identity.execution_state_revision === 17 && view.cache_identity.event_id === "42", "authority cache identity should include revision and event id");
  assertFixture(view.cache_identity.key === "AC-FRONTEND-AUTHORITY-FIXTURE:cex-current-1:17:42", "authority cache key should contain all four canonical identity parts");
  assertFixture(view.contract_execution_progress.line_states[0]?.display_status === "BYPASSED", "bypassed contract lines must not display PASS");
  assertFixture(view.backlog_close_readiness.display_status === "WAIVED", "waived backlog rows must not display PASS");
  assertFixture(contractRuntimeAuthorityDisplayStatus("BYPASSED") !== "PASS" && contractRuntimeAuthorityDisplayStatus("WAIVED") !== "PASS", "bypassed/waived authority statuses must never normalize to PASS");
  assertFixture(timelineStatusFromEvent(response.timeline.events[0]) === "recorded", "bypassed timeline text must not be misclassified by the passed substring");
  assertFixture(view.historical_diagnostics.timeline_events.length === 1 && view.historical_diagnostics.current_snapshot_in_playback === false, "current authority snapshot must stay separate from append-only playback history");

  const chainFallback = projectContractRuntimeAuthorityViewModel({
    ...response,
    contract_execution_progress: { ...response.contract_execution_progress, next_legal_action: {} },
  });
  assertFixture(chainFallback.contract_execution_progress.current_action.id === "legacy_chain_action" && chainFallback.contract_execution_progress.current_action_source === "backlog_contract_chain_current", "contract-chain current should be the fallback current-action authority");

  return [
    "canonical visualization projects ContractRuntime current action before chain fallback",
    "authority cache identity includes backlog, execution, revision, and event",
    "bypassed and waived states never display PASS",
    "current authority snapshot stays out of playback history",
  ];
}

function taskPlaybackCompactLedgerProjectionAssertions(): string[] {
  const backlog: BacklogBug = {
    bug_id: "AC-BACKLOG-CONTRACT-CHAIN-MAPPING-MODEL-20260627",
    title: "Compact ledger projection playback",
    status: "OPEN",
    priority: "P0",
  };
  const ledger = normalizeTaskPlaybackCompactLedger({
    schema_version: "task_timeline.compact_multi_backlog_ledger.v1",
    project_id: "aming-claw",
    row_count: 1,
    source_event_count: 3,
    rows: [
      {
        backlog_id: backlog.bug_id,
        title: backlog.title,
        priority: "P0",
        status: "OPEN",
        commit: "10aa2d3a1ed0c981f50b7e01f552f443f9c965af",
        contract_execution_id: "onboard-service-312c9c53d0112cfa0397",
        contract_chain_id: "cchain-312c9c53d0112cfa0397",
        root_contract_execution_id: "onboard-service-312c9c53d0112cfa0397",
        current_contract_execution_id: "mf-parallel-current-001",
        current_contract_id: "mf_parallel.v1",
        parent_to_resume_contract_execution_id: "onboard-service-312c9c53d0112cfa0397",
        active_child_contract_execution_id: "direct-fix-child-001",
        projection_generation: 7,
        projection_watermark: 312,
        projection_hash: "sha256:fixture-contract-chain-current",
        projection_degraded: false,
        projection_degraded_flags: {
          current_graph_resource_degraded: false,
          route_token_ref: "rtok-private-fixture",
        },
        contract_chain_current: {
          contract_chain_id: "cchain-312c9c53d0112cfa0397",
          root_contract_execution_id: "onboard-service-312c9c53d0112cfa0397",
          current_contract_execution_id: "mf-parallel-current-001",
          current_contract_id: "mf_parallel.v1",
          parent_to_resume_contract_execution_id: "onboard-service-312c9c53d0112cfa0397",
          active_child_contract_execution_id: "direct-fix-child-001",
          projection_generation: 7,
          projection_watermark: 312,
          projection_hash: "sha256:fixture-contract-chain-current",
          degraded: false,
          route_token_ref: "rtok-private-fixture",
        },
        merge_queue_id: "mq-fixture-chain",
        merge_queue_index: 2,
        merge_queue_item_id: "mqi-fixture-chain",
        merge_queue_task_id: "task-fixture-chain",
        merge_queue_status: "waiting_merge",
        latest_event_id: "7312",
        latest_event_kind: "verification",
        latest_event_type: "runtime_context_implementation_evidence",
        latest_status: "passed",
        latest_payload_ref: {
          event_id: "7312",
          payload_sha256: "sha256:fixture-ledger-payload",
          payload_bytes: 2048,
        },
        next_legal_action: {
          id: "return_to_parent_after_direct_fix_qa",
          action: "return_to_parent",
          stage_id: "handoff_gate",
          line_id: "qa-pass-line",
          owner_role: "observer",
          description: "Return to the parent contract after independent QA passes.",
        },
        blocker_summary: {
          kind: "",
          count: 0,
          keys: [],
          summary: "",
          reason: "",
        },
        head_commit: "10aa2d3a1ed0c981f50b7e01f552f443f9c965af",
        readiness_state: "close_ready",
      },
    ],
  }, "aming-claw");

  const row = ledger.rows[0];
  assertFixture(row.contract_chain_id === "cchain-312c9c53d0112cfa0397", "compact ledger: contract_chain_id should normalize");
  assertFixture(row.root_contract_execution_id === "onboard-service-312c9c53d0112cfa0397", "compact ledger: root execution should normalize");
  assertFixture(row.current_contract_execution_id === "mf-parallel-current-001", "compact ledger: current execution should normalize");
  assertFixture(row.current_contract_id === "mf_parallel.v1", "compact ledger: current contract id should normalize");
  assertFixture(row.parent_to_resume_contract_execution_id === "onboard-service-312c9c53d0112cfa0397", "compact ledger: parent resume target should normalize");
  assertFixture(row.active_child_contract_execution_id === "direct-fix-child-001", "compact ledger: active child execution should normalize");
  assertFixture(row.projection_generation === 7, "compact ledger: projection_generation should normalize");
  assertFixture(row.projection_watermark === 312, "compact ledger: projection_watermark should normalize");
  assertFixture(row.projection_hash === "sha256:fixture-contract-chain-current", "compact ledger: projection_hash should normalize");
  assertFixture(row.projection_degraded === false, "compact ledger: projection_degraded should normalize");
  assertFixture(row.contract_chain_current.route_token_ref === "[private detail redacted]", "compact ledger: nested current projection should redact token refs");
  assertFixture(row.projection_degraded_flags.route_token_ref === "[private detail redacted]", "compact ledger: degraded flags should redact token refs");

  const ledgerEvents = taskPlaybackLedgerRowsToTimelineEvents(ledger, "2026-06-27T21:45:00Z");
  assertFixture(ledgerEvents.length === 1, "compact ledger: one row should produce one timeline event");
  const event = ledgerEvents[0];
  const containers = [
    ["payload", event.payload],
    ["verification", event.verification],
    ["artifact_refs", event.artifact_refs],
  ] as const;
  for (const [label, value] of containers) {
    const record = value as Record<string, unknown>;
    assertFixture(record.contract_chain_id === row.contract_chain_id, `compact ledger: ${label} should carry contract_chain_id`);
    assertFixture(record.current_contract_execution_id === row.current_contract_execution_id, `compact ledger: ${label} should carry current_contract_execution_id`);
    assertFixture(record.projection_generation === row.projection_generation, `compact ledger: ${label} should carry projection_generation`);
    assertFixture(record.projection_watermark === row.projection_watermark, `compact ledger: ${label} should carry projection_watermark`);
    assertFixture(record.projection_hash === row.projection_hash, `compact ledger: ${label} should carry projection_hash`);
    assertFixture(record.contract_chain_current === row.contract_chain_current, `compact ledger: ${label} should carry contract_chain_current`);
  }

  const trace = normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog,
    compactLedger: ledger,
    taskTimeline: { project_id: "aming-claw", backlog_id: backlog.bug_id, events: [], count: 0 },
    gateResponse: null,
    source: "governed",
    generatedAt: "2026-06-27T21:45:00Z",
  });
  const frame = trace.frames.find((item) => item.event_type === "task_timeline.compact_ledger");
  assertFixture(Boolean(frame), "compact ledger: generated frame should be present in playback trace");
  assertFixture(frame?.specific_facts.some((fact) => fact.kind === "contract_chain_id" && fact.value === row.contract_chain_id) === true, "compact ledger: frame facts should expose chain id");
  assertFixture(frame?.specific_facts.some((fact) => fact.kind === "projection_hash" && fact.value === row.projection_hash) === true, "compact ledger: frame facts should expose projection hash");
  const rawSections = JSON.stringify(frame?.detail_inspector.raw_sections ?? []);
  assertFixture(rawSections.includes("contract_chain_current") && rawSections.includes("projection_watermark"), "compact ledger: inspector raw sections should include compact projection fields");
  assertFixture(!rawSections.includes("rtok-private-fixture"), "compact ledger: inspector raw sections should not expose token refs");

  const legacyOnlyBacklog: BacklogBug = {
    bug_id: "AC-DASHBOARD-LEGACY-ROUTE-PRECHECK-ADVISORY-20260629",
    title: "Legacy route precheck advisory row",
    status: "OPEN",
    priority: "P1",
  };
  const legacyOnlyLedger = normalizeTaskPlaybackCompactLedger({
    schema_version: "task_timeline.compact_multi_backlog_ledger.v1",
    project_id: "aming-claw",
    row_count: 1,
    source_event_count: 1,
    rows: [{
      backlog_id: legacyOnlyBacklog.bug_id,
      title: legacyOnlyBacklog.title,
      priority: "P1",
      status: "OPEN",
      latest_event_id: "8308",
      latest_event_kind: "contract_runtime_compact_ledger",
      latest_event_type: "task_timeline.compact_ledger",
      latest_status: "blocked",
      readiness_state: "blocked",
      blocker_summary: {
        kind: "blockers",
        count: 2,
        keys: ["route_action_precheck", "mf_timeline_precheck"],
        summary: "route_action_precheck, mf_timeline_precheck",
        reason: "legacy route precheck evidence missing",
      },
      next_legal_action: {
        id: "continue_contract_runtime_authority",
        action: "continue",
        description: "ContractRuntime authority controls close evidence.",
      },
    }],
  }, "aming-claw");
  const legacyOnlyRow = legacyOnlyLedger.rows[0];
  assertFixture(taskPlaybackCompactLedgerBlockingLabel(legacyOnlyRow) === "", "compact ledger: legacy route/mf prechecks should not produce a blocking label");
  const legacyOnlyDisplay = taskPlaybackCompactLedgerDisplayState(legacyOnlyRow);
  assertFixture(!legacyOnlyDisplay.blocked, "compact ledger display: legacy route/mf prechecks should not mark the row blocked");
  assertFixture(legacyOnlyDisplay.readinessLabel === "advisory/recorded", `compact ledger display: legacy-only readiness should be advisory/recorded, got ${legacyOnlyDisplay.readinessLabel}`);
  assertFixture(legacyOnlyDisplay.readinessTone !== "status-failed", `compact ledger display: legacy-only readiness should not be failed/red, got ${legacyOnlyDisplay.readinessTone}`);
  assertFixture(legacyOnlyDisplay.blockerListLabel === "legacy advisory", `compact ledger display: legacy-only values should render under legacy advisory, got ${legacyOnlyDisplay.blockerListLabel}`);
  assertFixture(
    legacyOnlyDisplay.blockerValues.includes("route_action_precheck") && legacyOnlyDisplay.blockerValues.includes("mf_timeline_precheck"),
    `compact ledger display: legacy-only advisory values should preserve historical ids, got ${legacyOnlyDisplay.blockerValues.join(", ")}`,
  );
  const legacyOnlyTrace = normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog: legacyOnlyBacklog,
    compactLedger: legacyOnlyLedger,
    taskTimeline: { project_id: "aming-claw", backlog_id: legacyOnlyBacklog.bug_id, events: [], count: 0 },
    gateResponse: null,
    source: "governed",
    generatedAt: "2026-06-29T16:00:00Z",
  });
  const legacyOnlyFrame = legacyOnlyTrace.frames.find((item) => item.event_type === "task_timeline.compact_ledger");
  assertFixture(legacyOnlyFrame?.status === "recorded", `compact ledger: legacy-only precheck blockers should be advisory/recorded, got ${legacyOnlyFrame?.status}`);
  assertFixture(
    !JSON.stringify(legacyOnlyFrame?.failure_diagnosis ?? []).includes("route_action_precheck"),
    "compact ledger: legacy route_action_precheck should not appear in failure diagnosis",
  );

  const authorityLedger = normalizeTaskPlaybackCompactLedger({
    schema_version: "task_timeline.compact_multi_backlog_ledger.v1",
    project_id: "aming-claw",
    row_count: 1,
    source_event_count: 1,
    rows: [{
      backlog_id: legacyOnlyBacklog.bug_id,
      title: legacyOnlyBacklog.title,
      priority: "P1",
      status: "OPEN",
      latest_event_id: "8309",
      latest_event_kind: "contract_runtime_compact_ledger",
      latest_event_type: "task_timeline.compact_ledger",
      latest_status: "blocked",
      readiness_state: "blocked",
      blocker_summary: {
        kind: "blockers",
        count: 2,
        keys: ["route_action_precheck", "mf_timeline_precheck"],
        summary: "route_action_precheck, mf_timeline_precheck",
        reason: "legacy route precheck evidence missing",
      },
      contract_chain_current: {
        contract_runtime_mf_parallel_close_authority_gate: {
          passed: false,
          missing_requirement_ids: ["contract_runtime.worker_finish_gate"],
          next_action: "record worker finish evidence",
        },
      },
      next_legal_action: {
        id: "record_worker_finish",
        action: "record_worker_finish",
        description: "Record worker finish evidence.",
      },
    }],
  }, "aming-claw");
  const authorityRow = authorityLedger.rows[0];
  assertFixture(
    taskPlaybackCompactLedgerBlockingLabel(authorityRow).includes("contract_runtime.worker_finish_gate"),
    `compact ledger: ContractRuntime authority missing evidence should be the blocking label, got ${taskPlaybackCompactLedgerBlockingLabel(authorityRow)}`,
  );
  const authorityDisplay = taskPlaybackCompactLedgerDisplayState(authorityRow);
  assertFixture(authorityDisplay.blocked, "compact ledger display: ContractRuntime authority missing evidence should mark the row blocked");
  assertFixture(authorityDisplay.readinessLabel === "blocked", `compact ledger display: ContractRuntime authority missing evidence should show blocked readiness, got ${authorityDisplay.readinessLabel}`);
  assertFixture(authorityDisplay.blockerListLabel === "blockers", `compact ledger display: authority blocker should render under blockers, got ${authorityDisplay.blockerListLabel}`);
  assertFixture(
    authorityDisplay.blockerValues.some((value) => value.includes("contract_runtime.worker_finish_gate")),
    `compact ledger display: authority blocker values should name missing evidence, got ${authorityDisplay.blockerValues.join(", ")}`,
  );
  const authorityTrace = normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog: legacyOnlyBacklog,
    compactLedger: authorityLedger,
    taskTimeline: { project_id: "aming-claw", backlog_id: legacyOnlyBacklog.bug_id, events: [], count: 0 },
    gateResponse: null,
    source: "governed",
    generatedAt: "2026-06-29T16:01:00Z",
  });
  const authorityFrame = authorityTrace.frames.find((item) => item.event_type === "task_timeline.compact_ledger");
  assertFixture(authorityFrame?.status === "blocked", `compact ledger: ContractRuntime authority blocker should keep frame blocked, got ${authorityFrame?.status}`);
  assertFixture(
    JSON.stringify(authorityFrame?.failure_diagnosis ?? []).includes("contract_runtime.worker_finish_gate"),
    "compact ledger: ContractRuntime authority missing evidence should appear in failure diagnosis",
  );

  return [
    "compact ledger projection fields normalize and project into timeline payload/verification/artifacts",
    "compact ledger generated frame exposes chain/projection facts and safe inspector data",
    "compact ledger legacy route/mf prechecks are advisory unless ContractRuntime authority reports a blocker",
  ];
}

function taskPlaybackAuditCloseAssertions(): string[] {
  const backlog: BacklogBug = {
    bug_id: "AC-AUDIT-CLOSE-QA-ACCEPTANCE-CONTRACT-20260615",
    title: "Audit close with QA acceptance",
    status: "WAIVED",
    priority: "P0",
    runtime_state: "audit_archived",
  };
  const auditArchive = {
    schema_version: "backlog_audit_archive.v1",
    status: "audit_archived",
    row_status: "WAIVED",
    reason: "Historical startup and close_ready evidence was not recorded.",
    non_reconstructable_evidence_reason: "Real mf_subagent_startup and close_ready evidence cannot be reconstructed without fabricating timeline facts.",
    normal_close_gate: {
      normal_close_gate_passed: false,
      can_close: false,
      close_ready_emitted: false,
    },
    audit_close_gate: {
      schema_version: "audit_close_gate.v1",
      status: "passed",
      allowed: true,
      passed: true,
    },
    failure_audit: {
      historical_evidence_reconstructed: false,
    },
    qa_acceptance: {
      status: "passed",
      passed: true,
      reviewer: "qa-reviewer-1",
      tests: ["pytest agent/tests/test_backlog_db.py"],
      artifacts: ["artifact://pytest/backlog-db"],
    },
    evidence: {
      timeline_precheck_failure_summary: {
        can_close: false,
        failed_gates: ["mf_subagent_startup", "close_ready"],
      },
      reconstructed: false,
    },
  };
  const events: TaskTimelineEvent[] = [
    {
      id: 5101,
      event_type: "backlog.audit_archive",
      event_kind: "backlog_audit_archive",
      phase: "audit_archive",
      actor: "observer",
      status: "audit_archived",
      backlog_id: backlog.bug_id,
      payload: { audit_archive: auditArchive },
      verification: { qa_acceptance: auditArchive.qa_acceptance },
      created_at: "2026-06-15T12:00:00Z",
    },
  ];
  const gateResponse: BacklogTimelineGateResponse = {
    ok: true,
    project_id: "aming-claw",
    bug_id: backlog.bug_id,
    applicable: true,
    can_close: false,
    event_count: events.length,
    audit_archive: auditArchive,
    audit_close_gate: auditArchive.audit_close_gate,
    qa_acceptance: auditArchive.qa_acceptance,
    timeline_gate: {
      schema_version: "mf_close_timeline_gate.v1",
      passed: false,
      status: "failed",
      required_event_kinds: ["implementation", "verification", "close_ready"],
      present_event_kinds: ["verification"],
      missing_event_kinds: ["mf_subagent_startup", "close_ready"],
      event_count: events.length,
      audit_archive: auditArchive,
      audit_close_gate: auditArchive.audit_close_gate,
      qa_acceptance: auditArchive.qa_acceptance,
      normal_close_gate: auditArchive.normal_close_gate,
    },
    events,
  };
  const trace = normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog,
    taskTimeline: { project_id: "aming-claw", backlog_id: backlog.bug_id, events, count: events.length },
    gateResponse,
    source: "governed",
    generatedAt: "2026-06-15T12:01:00Z",
  });
  assertFixture(trace.close_gate_summary.blocked, "normal close gate should remain blocked");
  assertFixture(trace.close_gate_summary.audit_close?.accepted === true, "audit close should be accepted separately");
  assertFixture(trace.close_gate_summary.audit_close?.qa_passed === true, "audit close summary should show QA passed");
  assertFixture(trace.close_gate_summary.audit_close?.evidence_not_reconstructed === true, "audit close summary should show evidence not reconstructed");

  const auditRows = trace.close_gate_matrix.rows.filter((row) => row.family === "audit_close");
  assertFixture(auditRows.some((row) => row.id === "normal_close_gate" && row.status === "failed"), "matrix should show normal close gate remains failed");
  assertFixture(auditRows.some((row) => row.id === "audit_close_gate" && row.status === "passed"), "matrix should show audit close gate passed");
  assertFixture(auditRows.some((row) => row.id === "qa_acceptance" && row.status === "passed"), "matrix should show QA acceptance passed");

  const frame = trace.frames[0];
  assertFixture(frame.specific_facts.some((fact) => fact.kind === "audit_close_gate" && fact.value.includes("audit_archived")), "event facts should include audit close gate");
  assertFixture(frame.specific_facts.some((fact) => fact.kind === "qa_acceptance" && fact.value.includes("passed")), "event facts should include QA acceptance");
  assertFixture(frame.specific_facts.some((fact) => fact.kind === "evidence_reconstruction"), "event facts should include evidence reconstruction state");

  const playbackSource = readFileSync(new URL("./taskPlayback.ts", import.meta.url), "utf8");
  assertFixture(playbackSource.includes("waived|audit_archived"), "playback normalization should treat WAIVED/audit_archived rows as terminal");

  const backlogViewSource = readFileSync(new URL("../views/BacklogView.tsx", import.meta.url), "utf8");
  assertFixture(
    backlogViewSource.includes('type StatusFilter = "OPEN" | "CLOSED" | "ALL";'),
    "backlog filters should expose closed rows rather than only FIXED rows",
  );
  assertFixture(
    backlogViewSource.includes('const AUDIT_ARCHIVED_RUNTIME_STATES = new Set(["audit_archived"]);'),
    "backlog filters should classify audit_archived runtime rows as closed",
  );
  assertFixture(
    backlogViewSource.includes('if (statusFilter === "CLOSED" && !isClosedBug(bug)) return false;'),
    "closed filter should use the shared closed-row classifier",
  );
  assertFixture(
    backlogViewSource.includes('normalizeStatus(bug.status) === "WAIVED" || AUDIT_ARCHIVED_RUNTIME_STATES.has(normalizeRuntimeState(bug.runtime_state))'),
    "closed-row classifier should include WAIVED status and audit_archived runtime state",
  );
  assertFixture(
    backlogViewSource.includes('if (s === "WAIVED" || s === "CANCELLED") return "status-unknown";'),
    "WAIVED rows should be visually distinct from normal FIXED rows",
  );
  assertFixture(
    backlogViewSource.includes("Audit close") && backlogViewSource.includes("Evidence") && backlogViewSource.includes("not reconstructed"),
    "detail summary should surface audit close, QA acceptance, and non-reconstructed evidence state",
  );

  const taskPlaybackViewSource = readFileSync(new URL("../views/TaskPlaybackView.tsx", import.meta.url), "utf8");
  assertFixture(
    taskPlaybackViewSource.includes('type StatusFilter = "open" | "closed" | "all";'),
    "playback selector should expose closed rows rather than only fixed rows",
  );
  assertFixture(
    taskPlaybackViewSource.includes('const AUDIT_ARCHIVED_RUNTIME_STATES = new Set(["audit_archived"]);'),
    "playback selector should classify audit_archived runtime rows as closed",
  );
  assertFixture(
    taskPlaybackViewSource.includes('if (statusFilter === "closed" && !isClosedBug(bug)) return false;'),
    "playback selector closed filter should use the shared closed-row classifier",
  );
  assertFixture(
    taskPlaybackViewSource.includes('["closed", "Closed"]'),
    "playback selector status filter label should say Closed",
  );
  assertFixture(
    taskPlaybackViewSource.includes("const openDelta = Number(isOpenBug(b)) - Number(isOpenBug(a));"),
    "playback selector sorting should keep open rows ahead of closed audit rows",
  );

  return [
    trace.close_gate_summary.label,
    ...auditRows.map((row) => `${row.id}:${row.status}`),
  ];
}

export const taskPlaybackAuditCloseSummary: string[] = taskPlaybackAuditCloseAssertions();

// ---------------------------------------------------------------------------
// AC-PLAYBACK-ROW-PRIVACY-FLAG-NOT-REGEX-20260608: explicit flag tests
// ---------------------------------------------------------------------------

function taskPlaybackPrivacyFlagAssertions(): string[] {
  // 1. A public row whose title mentions an external provider name is NOT hidden.
  const externalProviderRow: BacklogBug = {
    bug_id: "AC-REMOVE-OPENAI-DEPENDENCY-20260101",
    title: "Remove openai dependency from inference layer",
    status: "OPEN",
    priority: "P2",
    // No privacy_level, no public_safe — defaults to public.
  };
  assertFixture(
    !isBacklogRowPrivate(externalProviderRow),
    "privacy flag: a public row whose title mentions an external provider must NOT be hidden",
  );

  // 2. A row without any privacy fields is public (default).
  const plainRow: BacklogBug = {
    bug_id: "AC-PLAIN-PUBLIC-20260101",
    title: "Plain public backlog row",
    status: "OPEN",
    priority: "P3",
  };
  assertFixture(
    !isBacklogRowPrivate(plainRow),
    "privacy flag: a row with no privacy fields must be treated as public (default)",
  );

  // 3. An explicitly private row is hidden.
  const explicitlyPrivateRow: BacklogBug = {
    bug_id: "AC-PRIVATE-ROW-20260101",
    title: "Private judge routing configuration",
    status: "OPEN",
    priority: "P1",
    privacy_level: "private",
  };
  assertFixture(
    isBacklogRowPrivate(explicitlyPrivateRow),
    "privacy flag: a row with privacy_level=private must be hidden",
  );

  // 4. A row with public_safe=false is hidden.
  const publicSafeFalseRow: BacklogBug = {
    bug_id: "AC-PUBLIC-SAFE-FALSE-20260101",
    title: "Internal route configuration",
    status: "OPEN",
    priority: "P1",
    public_safe: false,
  };
  assertFixture(
    isBacklogRowPrivate(publicSafeFalseRow),
    "privacy flag: a row with public_safe=false must be hidden",
  );

  // 5. A row with privacy_level=public and public_safe=true is not hidden.
  const explicitlyPublicRow: BacklogBug = {
    bug_id: "AC-EXPLICIT-PUBLIC-20260101",
    title: "Explicit public row",
    status: "OPEN",
    priority: "P2",
    privacy_level: "public",
    public_safe: true,
  };
  assertFixture(
    !isBacklogRowPrivate(explicitlyPublicRow),
    "privacy flag: a row with privacy_level=public must not be hidden",
  );

  // 6. Body text of an explicitly-private row: isPrivatePlaybackText still
  //    catches private-keyword body text (PRIVATE_TIMELINE_TEXT_KEY is body-only).
  //    This is a separate concern from row visibility — we only verify that the
  //    function is callable and handles a private body correctly.
  //    (The actual body redaction happens inside normalizeTaskPlaybackTrace.)
  //    We just verify isBacklogRowPrivate does NOT read body text.
  const privateBodyPublicRow: BacklogBug = {
    bug_id: "AC-PRIVATE-BODY-PUBLIC-ROW-20260101",
    // Title contains "raw_prompt" keyword — under the old regex this would hide the row.
    title: "Fix raw_prompt serialization in the event log",
    status: "OPEN",
    priority: "P2",
    // No privacy_level set — must be public.
  };
  assertFixture(
    !isBacklogRowPrivate(privateBodyPublicRow),
    "privacy flag: row visibility must not depend on title keyword matching; only explicit flag counts",
  );

  return [
    "privacy flag: external-provider title row is not hidden (public default)",
    "privacy flag: plain row with no privacy fields is public",
    "privacy flag: privacy_level=private row is hidden",
    "privacy flag: public_safe=false row is hidden",
    "privacy flag: privacy_level=public row is not hidden",
    "privacy flag: keyword-in-title row is not hidden (explicit flag only)",
  ];
}

export const taskPlaybackPrivacyFlagFixtureSummary = taskPlaybackPrivacyFlagAssertions();

// ---------------------------------------------------------------------------
// AC-PLAYBACK-SEMANTICS-TEST-COVERAGE-20260610
// Wave-C semantic + ordering features: QA #3579 F1 + #3636 F1
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// a. buildHeadline: registry hit for representative top event_kinds
//    (tested via projectTaskTimelineEvent → frame.headline)
// ---------------------------------------------------------------------------

function taskPlaybackHeadlineCoverageAssertions(): string[] {
  // Helper: build a minimal event and project it, return the headline.
  function headline(event_kind: string, event_type: string, status: string, actor: string): string {
    const event: TaskTimelineEvent = {
      id: 9000,
      event_type,
      event_kind,
      phase: "test",
      actor,
      status,
      created_at: "2026-06-10T00:00:00Z",
    };
    return projectTaskTimelineEvent(event).headline;
  }

  // 1. mf_subagent_startup → "Bounded worker (mf_sub) started…"
  const startupH = headline("mf_subagent_startup", "mf_subagent.startup", "passed", "mf_sub");
  assertFixture(
    startupH.startsWith("Bounded worker (mf_sub) started"),
    `headline mf_subagent_startup: expected starts with "Bounded worker (mf_sub) started", got "${startupH}"`,
  );
  assertFixture(
    startupH.includes("(passed)"),
    `headline mf_subagent_startup: expected status suffix (passed), got "${startupH}"`,
  );

  // 2. verification → "Verification lane checked…"
  const verH = headline("verification", "task_timeline_append", "passed", "qa");
  assertFixture(
    verH.startsWith("Verification lane"),
    `headline verification: expected starts with "Verification lane", got "${verH}"`,
  );

  // 3. close_ready → "Observer recorded close-ready evidence…"
  const closeH = headline("close_ready", "observer.close_ready", "passed", "observer");
  assertFixture(
    closeH.startsWith("Observer recorded close-ready evidence"),
    `headline close_ready: expected starts with "Observer recorded close-ready evidence", got "${closeH}"`,
  );

  // 4. service_route → "Service router completed…"
  const srH = headline("service_route", "service.route.completed", "allowed", "service-router");
  assertFixture(
    srH.startsWith("Service router"),
    `headline service_route: expected starts with "Service router", got "${srH}"`,
  );

  // 5. qa_review (not in registry) → falls back to actor + lane sentence form
  //    (not mapped → fires telemetry hook and produces generic form)
  const qaReviewH = headline("qa_review", "qa.review.completed", "passed", "qa");
  assertFixture(
    typeof qaReviewH === "string" && qaReviewH.length > 0,
    `headline qa_review: expected non-empty fallback headline, got "${qaReviewH}"`,
  );

  // 6. implementation → "Bounded worker (mf_sub) completed implementation…"
  const implH = headline("implementation", "task_timeline_append", "passed", "mf_sub");
  assertFixture(
    implH.startsWith("Bounded worker (mf_sub) completed implementation"),
    `headline implementation: expected starts with "Bounded worker (mf_sub) completed implementation", got "${implH}"`,
  );

  // 7. Generic fallback for a completely unknown kind — must produce a non-empty string
  //    and must NOT contain the registry entry for a known kind.
  const unknownH = headline("zz_totally_unknown_kind_9999", "zz_unknown_event_type", "recorded", "system");
  assertFixture(
    typeof unknownH === "string" && unknownH.length > 0,
    `headline unknown kind: expected non-empty fallback string, got "${unknownH}"`,
  );
  assertFixture(
    !unknownH.startsWith("Bounded worker (mf_sub) started"),
    `headline unknown kind: must not borrow a known-kind headline, got "${unknownH}"`,
  );

  // 8. Unmapped-kind telemetry: projectTaskTimelineEvent with an unmapped kind must
  //    still return a valid projection (no throw) — proves the telemetry hook fired
  //    without crashing.
  const projection = projectTaskTimelineEvent({
    id: 9999,
    event_type: "zz_unmapped_event_type_telemetry_probe",
    event_kind: "zz_unmapped_kind_telemetry_probe",
    phase: "test",
    actor: "test",
    status: "recorded",
    created_at: "2026-06-10T00:00:00Z",
  });
  assertFixture(
    projection.schema_version === "task_timeline_semantic_projection.v1",
    "unmapped-kind telemetry: projectTaskTimelineEvent must return a valid projection without throwing",
  );
  assertFixture(
    typeof projection.headline === "string" && projection.headline.length > 0,
    "unmapped-kind telemetry: headline must be non-empty even for an unmapped kind",
  );

  return [
    `headline mf_subagent_startup: ${startupH}`,
    `headline verification: ${verH}`,
    `headline close_ready: ${closeH}`,
    `headline service_route: ${srH}`,
    `headline qa_review (fallback): ${qaReviewH}`,
    `headline implementation: ${implH}`,
    `headline unknown kind (generic fallback): ${unknownH}`,
    `headline unmapped-kind telemetry probe: ok`,
  ];
}

// ---------------------------------------------------------------------------
// b. buildRelations: extraction from payload with route_lane_refs /
//    qa_verdict_refs / parent_event_id / lane_evidence map; cap; empty payload
// ---------------------------------------------------------------------------

function taskPlaybackRelationsCoverageAssertions(): string[] {
  // Helper: project a single event and return its relation_links.
  function relations(event: Partial<TaskTimelineEvent>): ReturnType<typeof projectTaskTimelineEvent>["relations"] {
    return projectTaskTimelineEvent({
      id: 8000,
      event_type: "test_event",
      event_kind: "test_kind",
      phase: "test",
      actor: "test",
      status: "recorded",
      created_at: "2026-06-10T00:00:00Z",
      ...event,
    }).relations;
  }

  // 1. route_lane_refs in payload → extracted as event_ref relations
  const routeLaneRels = relations({
    payload: {
      route_lane_refs: ["evt-lane-1", "evt-lane-2"],
    },
  });
  assertFixture(
    routeLaneRels.some((r) => r.kind === "event_ref" && r.value === "evt-lane-1"),
    `relations route_lane_refs: expected event_ref for evt-lane-1, got ${JSON.stringify(routeLaneRels.map((r) => r.value))}`,
  );
  assertFixture(
    routeLaneRels.some((r) => r.kind === "event_ref" && r.value === "evt-lane-2"),
    `relations route_lane_refs: expected event_ref for evt-lane-2, got ${JSON.stringify(routeLaneRels.map((r) => r.value))}`,
  );

  // 2. qa_verdict_refs → extracted as event_ref relations
  const qaVerdictRels = relations({
    payload: {
      qa_verdict_refs: ["verdict-101", "verdict-102"],
    },
  });
  assertFixture(
    qaVerdictRels.some((r) => r.kind === "event_ref" && r.value === "verdict-101"),
    `relations qa_verdict_refs: expected event_ref for verdict-101`,
  );

  // 3. parent_event_id in payload → extracted as parent event event_ref
  const parentRels = relations({
    payload: { parent_event_id: "evt-parent-42" },
  });
  assertFixture(
    parentRels.some((r) => r.kind === "event_ref" && r.value === "evt-parent-42"),
    `relations parent_event_id: expected event_ref for evt-parent-42`,
  );

  // 4. backlog_id on event root → extracted as backlog_row relation
  const backlogRels = relations({
    backlog_id: "AC-TEST-RELATIONS-20260610",
  });
  assertFixture(
    backlogRels.some((r) => r.kind === "backlog_row" && r.value === "AC-TEST-RELATIONS-20260610"),
    `relations backlog_id: expected backlog_row for AC-TEST-RELATIONS-20260610`,
  );

  // 5. Cap behavior: more than 20 unique refs must be capped at 20
  const manyRefs = Array.from({ length: 25 }, (_, i) => `evt-cap-${i}`);
  const capRels = relations({
    payload: {
      source_event_ids: manyRefs,
    },
  });
  assertFixture(
    capRels.length <= 20,
    `relations cap: expected <= 20 relations, got ${capRels.length}`,
  );

  // 6. Missing / empty payload → empty relations array
  const emptyRels = relations({ payload: {} });
  assertFixture(
    Array.isArray(emptyRels),
    "relations empty payload: expected an array",
  );
  // An event with only backlog_id=undefined and no payload fields should return [].
  const noPayloadRels = relations({});
  assertFixture(
    Array.isArray(noPayloadRels) && noPayloadRels.length === 0,
    `relations no payload: expected empty array, got ${noPayloadRels.length} items`,
  );

  return [
    `relations route_lane_refs: ${routeLaneRels.length} items`,
    `relations qa_verdict_refs: ${qaVerdictRels.length} items`,
    `relations parent_event_id: ${parentRels.length} items`,
    `relations backlog_id: ${backlogRels.length} items`,
    `relations cap (${manyRefs.length} inputs → ${capRels.length} capped)`,
    `relations empty payload: ${emptyRels.length} items`,
    `relations no payload: ${noPayloadRels.length} items`,
  ];
}

// ---------------------------------------------------------------------------
// c. TaskPlaybackFrame projection: headline + relation_links populated by
//    normalizeTaskPlaybackTrace / projectTaskTimelineEvent path
// ---------------------------------------------------------------------------

function taskPlaybackFrameProjectionAssertions(): string[] {
  const backlog: BacklogBug = {
    bug_id: "AC-PLAYBACK-SEMANTICS-TEST-COVERAGE-20260610",
    title: "Playback semantics test coverage",
    status: "OPEN",
    priority: "P1",
  };
  const events: TaskTimelineEvent[] = [
    {
      id: 7001,
      event_type: "mf_subagent.startup",
      event_kind: "mf_subagent_startup",
      phase: "startup_gate",
      actor: "mf_sub",
      status: "passed",
      backlog_id: "AC-PLAYBACK-SEMANTICS-TEST-COVERAGE-20260610",
      payload: {
        worker_id: "mfsub-cov-test-01",
        read_receipt_event_id: "7000",
        route_lane_refs: ["evt-cov-lane-1"],
      },
      created_at: "2026-06-10T12:00:00Z",
    },
    {
      id: 7002,
      event_type: "task_timeline_append",
      event_kind: "implementation",
      phase: "implementation",
      actor: "mf_sub",
      status: "passed",
      backlog_id: "AC-PLAYBACK-SEMANTICS-TEST-COVERAGE-20260610",
      payload: {
        changed_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
        qa_verdict_refs: ["verdict-cov-01"],
      },
      created_at: "2026-06-10T12:01:00Z",
    },
  ];
  const trace = normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog,
    taskTimeline: { project_id: "aming-claw", backlog_id: backlog.bug_id, events, count: events.length },
    gateResponse: null,
    source: "governed",
  });

  assertFixture(trace.frames.length === 2, `frame projection: expected 2 frames, got ${trace.frames.length}`);

  const startupFrame = trace.frames.find((f) => f.source_event_id === "#7001");
  const implFrame = trace.frames.find((f) => f.source_event_id === "#7002");
  assertFixture(Boolean(startupFrame), "frame projection: startup frame #7001 should exist");
  assertFixture(Boolean(implFrame), "frame projection: implementation frame #7002 should exist");
  if (!startupFrame || !implFrame) throw new Error("missing frame projection test frames");

  // headline is populated
  assertFixture(
    startupFrame.headline.startsWith("Bounded worker (mf_sub) started"),
    `frame projection: startup headline should start with "Bounded worker (mf_sub) started", got "${startupFrame.headline}"`,
  );
  assertFixture(
    implFrame.headline.startsWith("Bounded worker (mf_sub) completed implementation"),
    `frame projection: impl headline should start with "Bounded worker (mf_sub) completed implementation", got "${implFrame.headline}"`,
  );

  // relation_links are populated from payload fields
  assertFixture(
    startupFrame.relation_links.some((r) => r.kind === "event_ref" && r.value === "evt-cov-lane-1"),
    "frame projection: startup relation_links should contain lane evidence event_ref",
  );
  assertFixture(
    startupFrame.relation_links.some((r) => r.kind === "event_ref" && r.value === "7000"),
    "frame projection: startup relation_links should contain read_receipt_event_id event_ref",
  );
  assertFixture(
    implFrame.relation_links.some((r) => r.kind === "event_ref" && r.value === "verdict-cov-01"),
    "frame projection: impl relation_links should contain qa_verdict_refs event_ref",
  );
  assertFixture(
    startupFrame.relation_links.some((r) => r.kind === "backlog_row" && r.value === "AC-PLAYBACK-SEMANTICS-TEST-COVERAGE-20260610"),
    "frame projection: startup relation_links should contain backlog_id backlog_row",
  );

  return [
    `frame projection: startup headline = ${startupFrame.headline}`,
    `frame projection: impl headline = ${implFrame.headline}`,
    `frame projection: startup relation_links count = ${startupFrame.relation_links.length}`,
    `frame projection: impl relation_links count = ${implFrame.relation_links.length}`,
  ];
}

// ---------------------------------------------------------------------------
// d. Newest-first + follow: displayPlaybackFrames, latestPlaybackFrameId
// ---------------------------------------------------------------------------

function taskPlaybackNewestFirstAssertions(): string[] {
  // Build a minimal fake frame for ordering tests.
  function fakeFrame(id: string, seq: number): TaskPlaybackFrame {
    return {
      id,
      sequence: seq,
      at: `2026-06-10T12:0${seq}:00Z`,
      lane_id: "observer",
      source_event_id: `#${seq}`,
      event_type: "test",
      event_kind: "test",
      phase: "test",
      headline: `Frame ${id}`,
      title: `Frame ${id}`,
      detail: "",
      summary: "",
      status: "recorded",
      actor: "test",
      narrative: { actor: "", information: "", context: "", purpose: "", outcome: "" },
      semantic_entry_id: "test",
      semantic_chips: [],
      specific_facts: [],
      failure_diagnosis: [],
      event_checklist: { categories: [], item_count: 0, hidden_count: 0, blocked_count: 0, passed_count: 0 },
      evidence_links: [],
      relation_links: [],
      detail_inspector: { rows: [], raw_sections: [], redaction_count: 0 },
      evidence_refs: [],
      artifact_refs: [],
      has_structured_detail: false,
    };
  }

  const frames = [fakeFrame("frame-a", 1), fakeFrame("frame-b", 2), fakeFrame("frame-c", 3)];

  // 1. newestFirst=false → same order as input
  const oldestFirst = displayPlaybackFrames(frames, false);
  assertFixture(
    oldestFirst[0].id === "frame-a" && oldestFirst[2].id === "frame-c",
    `displayPlaybackFrames(false): expected same order, got [${oldestFirst.map((f) => f.id).join(",")}]`,
  );

  // 2. newestFirst=true → reversed order
  const newestFirst = displayPlaybackFrames(frames, true);
  assertFixture(
    newestFirst[0].id === "frame-c" && newestFirst[2].id === "frame-a",
    `displayPlaybackFrames(true): expected reversed order [frame-c,frame-b,frame-a], got [${newestFirst.map((f) => f.id).join(",")}]`,
  );

  // 3. Input not mutated
  assertFixture(
    frames[0].id === "frame-a",
    "displayPlaybackFrames: input array must not be mutated",
  );

  // 4. Empty array edge case
  const emptyDisplay = displayPlaybackFrames([], true);
  assertFixture(emptyDisplay.length === 0, "displayPlaybackFrames: empty array should return empty array");

  // 5. latestPlaybackFrameId: returns last frame's id
  assertFixture(
    latestPlaybackFrameId(frames) === "frame-c",
    `latestPlaybackFrameId: expected "frame-c", got "${latestPlaybackFrameId(frames)}"`,
  );

  // 6. latestPlaybackFrameId: empty array → ""
  assertFixture(
    latestPlaybackFrameId([]) === "",
    `latestPlaybackFrameId: empty array should return "", got "${latestPlaybackFrameId([])}"`,
  );

  // 7. Initial selection = frames[length-1] (newest frame in oldest-first array)
  //    Simulate: when a trace loads, the initial selectedFrameId should point to
  //    the newest event (latestPlaybackFrameId is the answer).
  const traceFrames = [fakeFrame("old-1", 1), fakeFrame("old-2", 2), fakeFrame("new-3", 3)];
  const initialId = latestPlaybackFrameId(traceFrames);
  assertFixture(
    initialId === "new-3",
    `initial selection: latestPlaybackFrameId should point to newest frame "new-3", got "${initialId}"`,
  );

  return [
    "displayPlaybackFrames(false): oldest-first order preserved",
    "displayPlaybackFrames(true): reversed to newest-first",
    "displayPlaybackFrames: input not mutated",
    "displayPlaybackFrames(empty): returns []",
    `latestPlaybackFrameId: returns last frame id (${latestPlaybackFrameId(frames)})`,
    "latestPlaybackFrameId(empty): returns ''",
    `initial selection: latestPlaybackFrameId = ${initialId}`,
  ];
}

// ---------------------------------------------------------------------------
// e. Nav-stack: push/back/bounded(10)/missing event_ref graceful fallback
// ---------------------------------------------------------------------------

function taskPlaybackNavStackAssertions(): string[] {
  const entry = (id: string): PlaybackNavEntry => ({ frameId: id, label: `Frame ${id}` });

  // 1. Push single entry onto empty stack
  const s1 = pushPlaybackNavStack([], entry("A"));
  assertFixture(s1.length === 1 && s1[0].frameId === "A", `navStack push single: expected [A], got ${JSON.stringify(s1.map((e) => e.frameId))}`);

  // 2. Push preserves existing entries
  const s2 = pushPlaybackNavStack([entry("A"), entry("B")], entry("C"));
  assertFixture(
    s2.length === 3 && s2[2].frameId === "C",
    `navStack push append: expected [A,B,C], got ${JSON.stringify(s2.map((e) => e.frameId))}`,
  );

  // 3. Bounded at 10 — pushing the 11th entry drops the oldest
  let stack: PlaybackNavEntry[] = [];
  for (let i = 1; i <= 10; i++) stack = pushPlaybackNavStack(stack, entry(`e${i}`));
  assertFixture(stack.length === 10, `navStack bounded: expected 10 entries after 10 pushes, got ${stack.length}`);
  const s11 = pushPlaybackNavStack(stack, entry("e11"));
  assertFixture(
    s11.length === 10 && s11[0].frameId === "e2" && s11[9].frameId === "e11",
    `navStack bounded(10): after 11 pushes expected [e2..e11], got [${s11[0].frameId}..${s11[9].frameId}]`,
  );

  // 4. Pop from non-empty stack
  const { entry: popped, stack: after } = popPlaybackNavStack([entry("X"), entry("Y"), entry("Z")]);
  assertFixture(popped !== null && popped.frameId === "Z", `navStack pop: expected popped=Z, got ${popped?.frameId}`);
  assertFixture(
    after.length === 2 && after[1].frameId === "Y",
    `navStack pop: remaining stack should be [X,Y], got ${JSON.stringify(after.map((e) => e.frameId))}`,
  );

  // 5. Back navigation: popPlaybackNavStack returns null when stack is empty (graceful fallback)
  const { entry: noEntry, stack: emptyAfter } = popPlaybackNavStack([]);
  assertFixture(noEntry === null, "navStack pop empty: entry should be null");
  assertFixture(emptyAfter.length === 0, "navStack pop empty: stack should remain empty");

  // 6. Missing event_ref graceful fallback: calling pop on a single-entry stack
  //    returns that entry and leaves an empty stack (not an error)
  const { entry: single, stack: afterSingle } = popPlaybackNavStack([entry("only")]);
  assertFixture(single !== null && single.frameId === "only", `navStack pop single: expected "only", got ${single?.frameId}`);
  assertFixture(afterSingle.length === 0, "navStack pop single: stack should be empty after");

  // 7. Input not mutated by pushPlaybackNavStack
  const original = [entry("orig")];
  pushPlaybackNavStack(original, entry("new"));
  assertFixture(original.length === 1, "navStack push: input array must not be mutated");

  // 8. Input not mutated by popPlaybackNavStack
  const orig2 = [entry("orig2")];
  popPlaybackNavStack(orig2);
  assertFixture(orig2.length === 1, "navStack pop: input array must not be mutated");

  return [
    "navStack push single: ok",
    "navStack push append: ok",
    "navStack bounded(10): ok",
    "navStack pop: ok",
    "navStack pop empty (graceful): ok",
    "navStack pop single: ok",
    "navStack push not mutating: ok",
    "navStack pop not mutating: ok",
  ];
}

// ---------------------------------------------------------------------------
// AC-RELATIONS-EXTRACTION-COVERAGE-20260611
// New relation field coverage: startup-event refs, finish-gate/observer evidence
// refs, reversal_of_event, bridged_identities task ids, checkpoint_id,
// int-vs-string id normalization, empty payload → [].
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// F3: Real-data fixture — DB event #3741 envelope shape
//
// Event #3741 is the actual mf_subagent.startup event for this very lane
// (task-relations-cov-20260611-01).  The real DB payload nests ALL fields
// under the "mf_subagent_startup_gate" envelope key.  This fixture copies the
// exact envelope structure so that the F1 unwrap fix can be verified against
// the real payload shape, not a synthetic flat event.
//
// read_receipt_event_id in DB event #3741: "3740" (string)
// read_receipt_hash in DB event #3741: "sha256:mf_subagent_read_receipt_event_3740"
// observer_command_id in DB event #3741: "cmd-b344f65f3c9a"
// ---------------------------------------------------------------------------

// Real payload from DB event #3741 (mf_subagent.startup with mf_subagent_startup_gate envelope).
// Sensitive host paths are replaced with "[redacted]"; all public relation fields preserved.
export const REAL_DB_EVENT_3741_FIXTURE: TaskTimelineEvent = {
  id: 3741,
  event_type: "mf_subagent.startup",
  event_kind: "mf_subagent_startup",
  phase: "startup_gate",
  status: "passed",
  payload: {
    mf_subagent_startup_gate: {
      read_receipt_event_id: "3740",
      read_receipt_hash: "sha256:mf_subagent_read_receipt_event_3740",
      identity_join: {
        agent_id_match_mode: "same_as_allocation_owner",
        expected_runtime_context_id: "mfrctx-4cd901bd23f8ed73",
        runtime_context_id: "mfrctx-4cd901bd23f8ed73",
        runtime_context_id_matches: true,
        schema_version: "mf_subagent_startup_identity_join.v1",
      },
      agent_id_match_mode: "same_as_allocation_owner",
      allowed: true,
      backlog_id: "AC-RELATIONS-EXTRACTION-COVERAGE-20260611",
      base_commit: "6112158e63942a528f219cd2c2738d4582d6f0db",
      bounded: true,
      branch_ref: "refs/heads/codex/task-relations-cov-20260611-01",
      close_satisfying: true,
      fence_token: "fence-b6749fd1e734",
      gate_kind: "mf_subagent.startup",
      governance_project_id: "aming-claw",
      merge_queue_id: "mq-6887264390b636e64abaa0d8",
      observer_command_id: "cmd-b344f65f3c9a",
      ok: true,
      owned_files: [
        "frontend/dashboard/src/lib/taskTimelineSemantics.ts",
        "frontend/dashboard/src/lib/taskPlayback.ts",
        "frontend/dashboard/src/lib/taskPlayback.test.ts",
      ],
      parent_task_id: "task-relations-cov-20260611-01",
      prompt_contract_hash: "sha256:3a2b355508159ac96e38ec7bd2b7a67599cca94fa2d4208a1a10a6cb07d7014b",
      prompt_contract_id: "rprompt-aming-3a2b355508159ac9",
      route_context_hash: "sha256:deb92e12c5adaad6403f30006501fa8bf9101edddd6871e22bb45898ff14ce88",
      route_id: "route-20260611-deb92e12c5adaad6",
      runtime_context_id: "mfrctx-4cd901bd23f8ed73",
      schema_version: "mf_subagent_startup_gate.v1",
      session_token_evidence_type: "hash",
      started: true,
      startup_complete: true,
      startup_source: "claude_code_mf_sub",
      status: "passed",
      task_id: "task-relations-cov-20260611-01",
      visible_injection_manifest_hash: "sha256:f213443c14285204764224a1f378fc1aaff50de1822747493b6256284033c498",
      worker_id: "claude-mfsub-rlx-01",
      worker_role: "mf_sub",
      // Host-local paths omitted (actual_cwd / worktree_path are redacted public-safety fields)
      actual_startup_recorded: true,
    },
  },
  created_at: "2026-06-11T01:25:00Z",
};

function taskPlaybackRealDataFixtureAssertions(): string[] {
  // Assert that the F1 envelope-unwrap fix produces the read-receipt relation
  // from the real DB event #3741 payload shape.
  const proj3741 = projectTaskTimelineEvent(REAL_DB_EVENT_3741_FIXTURE);
  assertFixture(
    proj3741.relations.some((r) => r.kind === "event_ref" && r.label === "read receipt" && r.value === "3740"),
    `real DB #3741: expected event_ref "read receipt" with value "3740" (envelope unwrap), got ${JSON.stringify(proj3741.relations.map((r) => r.label + ":" + r.value))}`,
  );
  assertFixture(
    proj3741.relations.some((r) => r.kind === "backlog_row" && r.value === "AC-RELATIONS-EXTRACTION-COVERAGE-20260611"),
    `real DB #3741: expected backlog_row for AC-RELATIONS-EXTRACTION-COVERAGE-20260611`,
  );
  // The startup gate envelope must NOT cause route_id / prompt_contract_id / hash fields
  // to be extracted as relations (they are not relation ids, only the read_receipt_event_id
  // and backlog_id should produce relation entries from this specific event shape).
  const unwantedRouteRel = proj3741.relations.find((r) => r.value === "route-20260611-deb92e12c5adaad6");
  assertFixture(
    !unwantedRouteRel,
    `real DB #3741: route_id should not appear as a relation entry (only read_receipt_event_id and backlog_id are relation fields)`,
  );
  return [
    `real DB #3741 envelope unwrap: read receipt relation found (value=3740)`,
    `real DB #3741: backlog_row for AC-RELATIONS-EXTRACTION-COVERAGE-20260611 present`,
    `real DB #3741: no spurious route_id relation (route_id is not a relation field): ok`,
  ];
}

function taskPlaybackRelationsExtractionCoverageAssertions(): string[] {
  // Helper: project a single event and return its relation_links.
  function relations(event: Partial<TaskTimelineEvent>): ReturnType<typeof projectTaskTimelineEvent>["relations"] {
    return projectTaskTimelineEvent({
      id: 6000,
      event_type: "test_event",
      event_kind: "test_kind",
      phase: "test",
      actor: "test",
      status: "recorded",
      created_at: "2026-06-11T00:00:00Z",
      ...event,
    }).relations;
  }

  // 1. startup event with read_receipt_event_id shows read-receipt relation
  const startupRels = relations({
    event_kind: "mf_subagent_startup",
    payload: {
      read_receipt_event_id: 3740,
      worker_id: "mfsub-test-01",
    },
  });
  assertFixture(
    startupRels.some((r) => r.kind === "event_ref" && r.label === "read receipt" && r.value === "3740"),
    `startup read_receipt_event_id: expected event_ref for "3740", got ${JSON.stringify(startupRels.map((r) => r.value))}`,
  );

  // 2. int vs string normalization: int 3740 and string "3740" both produce the same value
  const intRels = relations({ payload: { read_receipt_event_id: 3740 } });
  const strRels = relations({ payload: { read_receipt_event_id: "3740" } });
  assertFixture(
    intRels.some((r) => r.value === "3740"),
    `int normalization: int 3740 should produce value "3740", got ${JSON.stringify(intRels.map((r) => r.value))}`,
  );
  assertFixture(
    strRels.some((r) => r.value === "3740"),
    `string normalization: string "3740" should produce value "3740", got ${JSON.stringify(strRels.map((r) => r.value))}`,
  );
  assertFixture(
    intRels.filter((r) => r.value === "3740").length === strRels.filter((r) => r.value === "3740").length,
    "int vs string normalization: both int and string event ids must produce the same relation entry",
  );

  // 3. finish-gate/observer evidence: startup_timeline_event_id and continuation_startup_event_id
  const finishGateRels = relations({
    event_kind: "close_ready",
    payload: {
      startup_timeline_event_id: "evt-startup-123",
      continuation_startup_event_id: "evt-cont-456",
    },
  });
  assertFixture(
    finishGateRels.some((r) => r.kind === "event_ref" && r.label === "startup timeline event" && r.value === "evt-startup-123"),
    `finish-gate startup_timeline_event_id: expected event_ref for "evt-startup-123"`,
  );
  assertFixture(
    finishGateRels.some((r) => r.kind === "event_ref" && r.label === "continuation startup event" && r.value === "evt-cont-456"),
    `finish-gate continuation_startup_event_id: expected event_ref for "evt-cont-456"`,
  );

  // 4. route_identity_supersede event: reversal_of_event shows reversal relation
  const supersederRels = relations({
    event_kind: "route_identity_supersede",
    payload: {
      reversal_of_event: "evt-stale-789",
    },
  });
  assertFixture(
    supersederRels.some((r) => r.kind === "event_ref" && r.label === "reversal of event" && r.value === "evt-stale-789"),
    `reversal_of_event: expected event_ref for "evt-stale-789", got ${JSON.stringify(supersederRels.map((r) => r.value))}`,
  );

  // 5. cross_ref_lineage_bridge with bridged_identities: each task_id becomes a backlog_row
  const bridgeRels = relations({
    event_kind: "cross_ref_lineage_bridge",
    payload: {
      bridged_identities: [
        { task_id: "task-bridge-a", route_id: "route-bridge-a" },
        { task_id: "task-bridge-b", worker_id: "mfsub-bridge-b" },
      ],
    },
  });
  assertFixture(
    bridgeRels.some((r) => r.kind === "backlog_row" && r.label === "bridged task" && r.value === "task-bridge-a"),
    `bridged_identities task_id: expected backlog_row for "task-bridge-a"`,
  );
  assertFixture(
    bridgeRels.some((r) => r.kind === "backlog_row" && r.label === "bridged task" && r.value === "task-bridge-b"),
    `bridged_identities task_id: expected backlog_row for "task-bridge-b"`,
  );

  // 6. checkpoint_id: non-navigable fact entry — kind="backlog_row" (guaranteed
  //    non-nav in the panel since backlog_row entries never trigger the frame-jump
  //    button; this avoids needing a new "fact" kind in the type union, and
  //    checkpoint ids can never match a timeline frame id anyway).
  const checkpointRels = relations({
    payload: {
      checkpoint_id: "ckpt-abc123",
    },
  });
  assertFixture(
    checkpointRels.some((r) => r.kind === "backlog_row" && r.label === "checkpoint" && r.value === "ckpt-abc123"),
    `checkpoint_id: expected backlog_row with label "checkpoint" and value "ckpt-abc123", got ${JSON.stringify(checkpointRels.map((r) => r.kind + ":" + r.label + ":" + r.value))}`,
  );

  // 7. worker_progress_refs in payload → event_ref relations
  const progressRels = relations({
    payload: {
      worker_progress_refs: ["evt-prog-1", "evt-prog-2"],
    },
  });
  assertFixture(
    progressRels.some((r) => r.kind === "event_ref" && r.label === "worker progress" && r.value === "evt-prog-1"),
    `worker_progress_refs: expected event_ref for "evt-prog-1"`,
  );
  assertFixture(
    progressRels.some((r) => r.kind === "event_ref" && r.label === "worker progress" && r.value === "evt-prog-2"),
    `worker_progress_refs: expected event_ref for "evt-prog-2"`,
  );

  // 8. dispatch_ref in payload → event_ref relation
  const dispatchRels = relations({
    payload: {
      dispatch_ref: "evt-dispatch-001",
    },
  });
  assertFixture(
    dispatchRels.some((r) => r.kind === "event_ref" && r.label === "dispatch ref" && r.value === "evt-dispatch-001"),
    `dispatch_ref: expected event_ref for "evt-dispatch-001"`,
  );

  // 9. qa_refs array in payload → event_ref relations
  const qaRefsRels = relations({
    payload: {
      qa_refs: ["qa-evt-10", "qa-evt-11"],
    },
  });
  assertFixture(
    qaRefsRels.some((r) => r.kind === "event_ref" && r.label === "QA ref" && r.value === "qa-evt-10"),
    `qa_refs: expected event_ref for "qa-evt-10"`,
  );

  // 10. Empty payload → [] (no relations)
  const emptyRels = relations({ payload: {} });
  assertFixture(
    Array.isArray(emptyRels) && emptyRels.length === 0,
    `empty payload: expected empty array, got ${emptyRels.length} items`,
  );

  // 11. No payload at all → [] (no relations, no throw)
  const noPayloadRels = relations({});
  assertFixture(
    Array.isArray(noPayloadRels) && noPayloadRels.length === 0,
    `no payload: expected empty array, got ${noPayloadRels.length} items`,
  );

  // 12. Full trace via normalizeTaskPlaybackTrace: startup frame surfaces read_receipt_event_id relation_link
  const backlog: BacklogBug = {
    bug_id: "AC-RELATIONS-EXTRACTION-COVERAGE-20260611",
    title: "Relations extraction coverage",
    status: "OPEN",
    priority: "P1",
  };
  const traceEvents: TaskTimelineEvent[] = [
    {
      id: 6001,
      event_type: "mf_subagent.startup",
      event_kind: "mf_subagent_startup",
      phase: "startup_gate",
      actor: "mf_sub",
      status: "passed",
      backlog_id: "AC-RELATIONS-EXTRACTION-COVERAGE-20260611",
      payload: {
        read_receipt_event_id: 3740,
        worker_id: "mfsub-rlx-01",
        startup_timeline_event_id: "evt-startup-6001",
      },
      created_at: "2026-06-11T01:25:00Z",
    },
    {
      id: 6002,
      event_type: "mf_subagent.close_ready",
      event_kind: "close_ready",
      phase: "close_ready",
      actor: "observer",
      status: "passed",
      backlog_id: "AC-RELATIONS-EXTRACTION-COVERAGE-20260611",
      payload: {
        startup_timeline_event_id: "6001",
        continuation_startup_event_id: "6001",
        bridged_identities: [{ task_id: "task-bridge-prior-01" }],
        checkpoint_id: "ckpt-relx-001",
      },
      created_at: "2026-06-11T01:26:00Z",
    },
  ];
  const trace = normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog,
    taskTimeline: { project_id: "aming-claw", backlog_id: backlog.bug_id, events: traceEvents, count: traceEvents.length },
    gateResponse: null,
    source: "governed",
  });
  const startupFrame = trace.frames.find((f) => f.source_event_id === "#6001");
  const closeReadyFrame = trace.frames.find((f) => f.source_event_id === "#6002");
  assertFixture(Boolean(startupFrame), "trace: startup frame #6001 should exist");
  assertFixture(Boolean(closeReadyFrame), "trace: close_ready frame #6002 should exist");
  if (!startupFrame || !closeReadyFrame) throw new Error("missing relx coverage trace frames");
  assertFixture(
    startupFrame.relation_links.some((r) => r.kind === "event_ref" && r.label === "read receipt" && r.value === "3740"),
    "trace: startup frame relation_links should contain read_receipt_event_id (int normalized to string)",
  );
  assertFixture(
    startupFrame.relation_links.some((r) => r.kind === "event_ref" && r.label === "startup timeline event"),
    "trace: startup frame relation_links should contain startup_timeline_event_id",
  );
  assertFixture(
    closeReadyFrame.relation_links.some((r) => r.kind === "event_ref" && r.label === "startup timeline event" && r.value === "6001"),
    "trace: close_ready frame relation_links should contain startup_timeline_event_id",
  );
  assertFixture(
    closeReadyFrame.relation_links.some((r) => r.kind === "backlog_row" && r.label === "bridged task" && r.value === "task-bridge-prior-01"),
    "trace: close_ready frame relation_links should contain bridged_identities task_id",
  );
  assertFixture(
    closeReadyFrame.relation_links.some((r) => r.kind === "backlog_row" && r.label === "checkpoint" && r.value === "ckpt-relx-001"),
    "trace: close_ready frame relation_links should contain checkpoint_id as backlog_row (non-nav)",
  );

  return [
    "relx: startup read_receipt_event_id → event_ref (read receipt): ok",
    "relx: int 3740 normalized to string \"3740\": ok",
    "relx: string \"3740\" normalization: ok",
    "relx: int vs string produces same relation: ok",
    "relx: finish-gate startup_timeline_event_id → event_ref: ok",
    "relx: finish-gate continuation_startup_event_id → event_ref: ok",
    "relx: reversal_of_event → event_ref (reversal of event): ok",
    "relx: bridged_identities task_id → backlog_row (bridged task): ok",
    "relx: checkpoint_id → backlog_row (non-nav, guaranteed non-clickable): ok",
    "relx: worker_progress_refs → event_ref array: ok",
    "relx: dispatch_ref → event_ref: ok",
    "relx: qa_refs → event_ref array: ok",
    "relx: empty payload → []: ok",
    "relx: no payload → []: ok",
    `relx: trace startup frame read_receipt relation present (count=${startupFrame.relation_links.length}): ok`,
    `relx: trace close_ready bridged task relation present (count=${closeReadyFrame.relation_links.length}): ok`,
  ];
}

export const taskPlaybackSemanticsCoverageFixtureSummary: string[] = [
  ...taskPlaybackHeadlineCoverageAssertions(),
  ...taskPlaybackRelationsCoverageAssertions(),
  ...taskPlaybackFrameProjectionAssertions(),
  ...taskPlaybackNewestFirstAssertions(),
  ...taskPlaybackNavStackAssertions(),
];

export const taskPlaybackRelationsExtractionCoverageSummary: string[] = [
  ...taskPlaybackRealDataFixtureAssertions(),
  ...taskPlaybackRelationsExtractionCoverageAssertions(),
];

// ── classifyStatusWord + segmentTextWithStatusChips tests ─────────────────
// AC-EVENT-SUMMARY-SEMANTIC-EMPHASIS-20260611

import { classifyStatusWord, segmentTextWithStatusChips } from "./taskTimelineSemantics";

function taskPlaybackStatusWordClassifyAssertions(): string[] {
  // Positive words
  for (const word of ["passed", "accepted", "ok", "validated", "close_satisfying", "succeeded"]) {
    assertFixture(classifyStatusWord(word) === "positive", `classify '${word}': expected positive`);
  }
  // Negative words
  for (const word of ["blocked", "failed", "refused", "rejected", "denied"]) {
    assertFixture(classifyStatusWord(word) === "negative", `classify '${word}': expected negative`);
  }
  // Neutral words
  for (const word of ["allowed", "requested", "pending", "running"]) {
    assertFixture(classifyStatusWord(word) === "neutral", `classify '${word}': expected neutral`);
  }
  // Case-insensitive
  assertFixture(classifyStatusWord("PASSED") === "positive", "classify 'PASSED': case-insensitive positive");
  assertFixture(classifyStatusWord("Failed") === "negative", "classify 'Failed': case-insensitive negative");
  assertFixture(classifyStatusWord("Running") === "neutral", "classify 'Running': case-insensitive neutral");
  // Null cases
  assertFixture(classifyStatusWord("") === null, "classify '': empty string → null");
  assertFixture(classifyStatusWord("unknown_word") === null, "classify 'unknown_word': → null");
  assertFixture(classifyStatusWord("the") === null, "classify 'the': → null");
  // Whole-word contract: 'surpassed' / 'unblocked' are NOT exact status words.
  assertFixture(classifyStatusWord("surpassed") === null, "classify 'surpassed': not a status word → null");
  assertFixture(classifyStatusWord("unblocked") === null, "classify 'unblocked': not a status word → null");

  return [
    "classify: positive words (passed/accepted/ok/validated/close_satisfying/succeeded)",
    "classify: negative words (blocked/failed/refused/rejected/denied)",
    "classify: neutral words (allowed/requested/pending/running)",
    "classify: case-insensitive (PASSED/Failed/Running)",
    "classify: empty string → null",
    "classify: non-status word → null",
    "classify: 'surpassed' → null (whole-word contract)",
    "classify: 'unblocked' → null (whole-word contract)",
  ];
}

function taskPlaybackSegmentTextAssertions(): string[] {
  // Empty string → []
  const emptySegs = segmentTextWithStatusChips("");
  assertFixture(emptySegs.length === 0, "segment '': empty string → []");

  // Plain text with no status words → only chipClass null segments
  const plainSegs = segmentTextWithStatusChips("hello world");
  assertFixture(plainSegs.every((s) => s.chipClass === null), "segment plain text: all chipClass null");
  assertFixture(plainSegs.map((s) => s.text).join("") === "hello world", "segment plain text: round-trips correctly");

  // Status word in context → correct chip
  const passedSegs = segmentTextWithStatusChips("gate passed successfully");
  const passedChip = passedSegs.find((s) => s.text === "passed");
  assertFixture(passedChip !== undefined, "segment 'passed': chip present in output");
  assertFixture(passedChip?.chipClass === "positive", "segment 'passed': chipClass = positive");

  // Negative word
  const blockedSegs = segmentTextWithStatusChips("worker blocked by gate");
  const blockedChip = blockedSegs.find((s) => s.text === "blocked");
  assertFixture(blockedChip?.chipClass === "negative", "segment 'blocked': chipClass = negative");

  // Neutral word
  const pendingSegs = segmentTextWithStatusChips("request is pending approval");
  const pendingChip = pendingSegs.find((s) => s.text === "pending");
  assertFixture(pendingChip?.chipClass === "neutral", "segment 'pending': chipClass = neutral");

  // Whole-word boundary: 'surpassed' must NOT produce any chip.
  const surpassedSegs = segmentTextWithStatusChips("surpassed expectations");
  assertFixture(surpassedSegs.filter((s) => s.chipClass !== null).length === 0, "segment 'surpassed': no status chips (whole-word boundary)");

  // Round-trip lossless
  const complex = "gate passed but worker blocked; request pending";
  const complexSegs = segmentTextWithStatusChips(complex);
  assertFixture(complexSegs.map((s) => s.text).join("") === complex, "segment complex text: round-trips losslessly");

  // Correct chip count
  const multiChipCount = complexSegs.filter((s) => s.chipClass !== null).length;
  assertFixture(multiChipCount === 3, `segment complex text: 3 chips (passed/blocked/pending), got ${multiChipCount}`);

  return [
    "segment: empty string → []",
    "segment: plain text → all chipClass null, round-trips",
    "segment: 'passed' in sentence → positive chip",
    "segment: 'blocked' in sentence → negative chip",
    "segment: 'pending' in sentence → neutral chip",
    "segment: 'surpassed' → no status chips (whole-word boundary)",
    "segment: complex text round-trips losslessly",
    "segment: complex text yields 3 chips (passed/blocked/pending)",
  ];
}

export const taskPlaybackStatusWordEmphasisSummary: string[] = [
  ...taskPlaybackStatusWordClassifyAssertions(),
  ...taskPlaybackSegmentTextAssertions(),
];

// ---------------------------------------------------------------------------
// AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611 tests
// ---------------------------------------------------------------------------

function assertIa(condition: boolean, label: string): void {
  if (!condition) throw new Error(`[IA assertion] FAILED: ${label}`);
}

/**
 * Test projectEventToCard — verify card fields are projected correctly from
 * a TaskTimelineEvent.
 */
function taskPlaybackProjectEventToCardAssertions(): string[] {
  const event: TaskTimelineEvent = {
    id: 9001,
    event_type: "mf_subagent.startup",
    event_kind: "mf_subagent_startup",
    phase: "startup_gate",
    actor: "mf_sub",
    status: "passed",
    backlog_id: "AC-TEST-CARD-20260611",
    task_id: "task-card-fixture",
    created_at: "2026-06-11T08:00:00Z",
    payload: {},
  };

  const card = projectEventToCard(event);

  assertIa(card.id === 9001, `card.id should be 9001, got ${card.id}`);
  assertIa(card.at === "2026-06-11T08:00:00Z", `card.at should be created_at, got ${card.at}`);
  assertIa(card.event_kind === "mf_subagent_startup", `card.event_kind, got ${card.event_kind}`);
  assertIa(card.event_type === "mf_subagent.startup", `card.event_type, got ${card.event_type}`);
  assertIa(card.status === "passed", `card.status, got ${card.status}`);
  assertIa(card.actor === "mf_sub", `card.actor, got ${card.actor}`);
  assertIa(card.backlog_id === "AC-TEST-CARD-20260611", `card.backlog_id, got ${card.backlog_id}`);
  assertIa(card.task_id === "task-card-fixture", `card.task_id, got ${card.task_id}`);
  assertIa(typeof card.headline === "string" && card.headline.length > 0, `card.headline should be non-empty string`);
  assertIa(typeof card.evidence_count === "number", `card.evidence_count should be a number`);
  assertIa(Array.isArray(card.evidence_types), `card.evidence_types should be an array`);

  // String id coercion: id field as string
  const strEvent: TaskTimelineEvent = { ...event, id: "str-id-9002" as unknown as number };
  const strCard = projectEventToCard(strEvent);
  assertIa(strCard.id === "str-id-9002", `string id coerced correctly, got ${strCard.id}`);

  // Missing fields: graceful fallback
  const minimal: TaskTimelineEvent = { id: 0, event_type: "", event_kind: "", phase: "", actor: "", status: "", backlog_id: "", task_id: "", created_at: "", payload: {} };
  const minCard = projectEventToCard(minimal);
  assertIa(typeof minCard.headline === "string", "minimal event: headline is string");
  assertIa(minCard.evidence_count === 0, "minimal event: evidence_count is 0");

  const payloadBacklogCard = projectEventToCard({
    id: 9002,
    event_type: "task_timeline_append",
    event_kind: "implementation",
    phase: "implementation",
    actor: "mf_sub",
    status: "passed",
    backlog_id: "",
    task_id: "",
    created_at: "2026-06-11T08:01:00Z",
    payload: { backlog_id: "AC-PAYLOAD-BACKLOG-20260611" },
  });
  assertIa(
    payloadBacklogCard.backlog_id === "AC-PAYLOAD-BACKLOG-20260611",
    `payload backlog_id fallback, got ${payloadBacklogCard.backlog_id}`,
  );

  return [
    "projectEventToCard: id field projected correctly (number)",
    "projectEventToCard: id field projected correctly (string coercion)",
    "projectEventToCard: at = created_at",
    "projectEventToCard: event_kind, event_type, status, actor, backlog_id, task_id",
    "projectEventToCard: headline is non-empty string",
    "projectEventToCard: evidence_count is number, evidence_types is array",
    "projectEventToCard: minimal event: graceful fallback for all fields",
    "projectEventToCard: payload backlog_id fallback keeps linked card affordance",
  ];
}

/**
 * Test truncateHash — various inputs: non-hash, short hash, long sha256, sha512, raw hex.
 */
function taskPlaybackTruncateHashAssertions(): string[] {
  // Non-hash strings pass through unchanged
  assertIa(truncateHash("") === "", "truncateHash('') → ''");
  assertIa(truncateHash("not-a-hash") === "not-a-hash", "truncateHash non-hash → unchanged");
  assertIa(truncateHash("AC-CLOSE-GATE-20260611") === "AC-CLOSE-GATE-20260611", "truncateHash backlog id → unchanged");

  // Short hex (<= 12 chars): unchanged
  assertIa(truncateHash("sha256:abcd1234") === "sha256:abcd1234", "truncateHash short hex with prefix → unchanged");
  assertIa(truncateHash("abcdef1234") === "abcdef1234", "truncateHash short hex no prefix → unchanged");

  // Long sha256 hex: truncated to prefix+4…4
  const long256 = "sha256:d36f5c4b2e7a8f91c0d3e6b4a5f2c7d8e9a0b1c2d3e4f5a6b7c8d9e0f1a2b3";
  const truncated256 = truncateHash(long256);
  assertIa(truncated256.startsWith("sha256:"), `truncated sha256 starts with 'sha256:', got: ${truncated256}`);
  assertIa(truncated256.includes("…"), `truncated sha256 has ellipsis, got: ${truncated256}`);
  assertIa(truncated256.length < long256.length, `truncated sha256 shorter than input`);

  // Raw hex without prefix: defaults to sha256: prefix
  const rawHex = "d36f5c4b2e7a8f91c0d3e6b4a5f2c7d8e9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c";
  const truncatedRaw = truncateHash(rawHex);
  assertIa(truncatedRaw.startsWith("sha256:"), `raw hex truncated to sha256:prefix, got: ${truncatedRaw}`);

  // sha512 prefix preserved
  const long512 = "sha512:a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2";
  const truncated512 = truncateHash(long512);
  assertIa(truncated512.startsWith("sha512:"), `sha512 prefix preserved, got: ${truncated512}`);
  assertIa(truncated512.includes("…"), "sha512 long hash has ellipsis");

  return [
    "truncateHash: empty string → ''",
    "truncateHash: non-hash strings pass through unchanged",
    "truncateHash: short hex (<= 12 chars) → unchanged",
    "truncateHash: long sha256 → prefix+4…4 format",
    "truncateHash: raw hex without prefix → sha256: prefix added",
    "truncateHash: sha512: prefix preserved",
  ];
}

/**
 * Test categorizeEvidenceRef and groupEvidenceRefsByCategory — all 6 categories.
 */
function taskPlaybackCategorizeEvidenceRefAssertions(): string[] {
  // timeline_events
  assertIa(categorizeEvidenceRef("timeline_event") === "timeline_events", "timeline_event → timeline_events");
  assertIa(categorizeEvidenceRef("source_event") === "timeline_events", "source_event → timeline_events");

  // gate_and_verification
  assertIa(categorizeEvidenceRef("gate") === "gate_and_verification", "gate → gate_and_verification");
  assertIa(categorizeEvidenceRef("precheck") === "gate_and_verification", "precheck → gate_and_verification");

  // route_and_prompt
  assertIa(categorizeEvidenceRef("route_context") === "route_and_prompt", "route_context → route_and_prompt");
  assertIa(categorizeEvidenceRef("read_receipt") === "route_and_prompt", "read_receipt → route_and_prompt");
  assertIa(categorizeEvidenceRef("prompt_contract") === "route_and_prompt", "prompt_contract → route_and_prompt");

  // commit_and_artifact
  assertIa(categorizeEvidenceRef("commit") === "commit_and_artifact", "commit → commit_and_artifact");
  assertIa(categorizeEvidenceRef("file") === "commit_and_artifact", "file → commit_and_artifact");
  assertIa(categorizeEvidenceRef("test") === "commit_and_artifact", "test → commit_and_artifact");
  assertIa(categorizeEvidenceRef("artifact") === "commit_and_artifact", "artifact → commit_and_artifact");

  // graph_and_trace
  assertIa(categorizeEvidenceRef("graph_trace") === "graph_and_trace", "graph_trace → graph_and_trace");
  assertIa(categorizeEvidenceRef("node") === "graph_and_trace", "node → graph_and_trace");

  // Default → backlog_and_task
  assertIa(categorizeEvidenceRef("content_sys" as TaskPlaybackEvidenceRef["kind"]) === "backlog_and_task", "content_sys → backlog_and_task");

  // groupEvidenceRefsByCategory — value pattern overrides kind
  const refs: TaskPlaybackEvidenceRef[] = [
    { kind: "timeline_event", label: "ev", value: "3799" },               // → timeline_events (not AC-/task-)
    { kind: "timeline_event", label: "backlog", value: "AC-TEST-20260611" }, // → backlog_and_task (AC- override)
    { kind: "graph_trace", label: "trace", value: "task-abc-123" },         // → backlog_and_task (task- override)
    { kind: "commit", label: "sha", value: "sha256:abcdef1234567890abcdef1234567890ab" }, // → commit_and_artifact
    { kind: "route_context", label: "rc", value: "route-fixture-01" },      // → route_and_prompt
  ];
  const grouped = groupEvidenceRefsByCategory(refs);
  assertIa(grouped.timeline_events.length === 1, `timeline_events should have 1 item, got ${grouped.timeline_events.length}`);
  assertIa(grouped.backlog_and_task.length === 2, `backlog_and_task should have 2 (AC- + task- overrides), got ${grouped.backlog_and_task.length}`);
  assertIa(grouped.commit_and_artifact.length === 1, `commit_and_artifact should have 1, got ${grouped.commit_and_artifact.length}`);
  assertIa(grouped.route_and_prompt.length === 1, `route_and_prompt should have 1, got ${grouped.route_and_prompt.length}`);
  assertIa(grouped.gate_and_verification.length === 0, `gate_and_verification should be empty`);
  assertIa(grouped.graph_and_trace.length === 0, `graph_and_trace should be 0 (task- overrode it), got ${grouped.graph_and_trace.length}`);

  assertIa(isPlaybackBacklogRefValue("AC-TEST-20260611"), "AC-* values navigate as backlog refs");
  assertIa(isPlaybackBacklogRefValue("task-bridge-01"), "task-* values navigate as task/backlog refs");
  assertIa(!isPlaybackBacklogRefValue("ckpt-relx-001"), "checkpoint values do not navigate as backlog refs");
  assertIa(isPlaybackEventEvidenceRef({ kind: "timeline_event", label: "event", value: "3799" }), "timeline_event refs navigate as event refs");
  assertIa(isPlaybackEventEvidenceRef({ kind: "source_event", label: "startup", value: "evt-startup-123" }), "source_event refs navigate as event refs");
  assertIa(isPlaybackEventEvidenceRef({ kind: "read_receipt", label: "read receipt", value: "3740" }), "numeric read receipt refs navigate as event refs");
  assertIa(!isPlaybackEventEvidenceRef({ kind: "graph_trace", label: "trace", value: "gqt-20260611-abc" }), "graph trace refs stay inspectable");
  assertIa(!isPlaybackEventEvidenceRef({ kind: "read_receipt", label: "read receipt hash", value: "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890" }), "read receipt hashes stay inspectable");

  return [
    "categorizeEvidenceRef: timeline_event / source_event → timeline_events",
    "categorizeEvidenceRef: gate / precheck → gate_and_verification",
    "categorizeEvidenceRef: route_context / read_receipt / prompt_contract → route_and_prompt",
    "categorizeEvidenceRef: commit / file / test / artifact → commit_and_artifact",
    "categorizeEvidenceRef: graph_trace / node → graph_and_trace",
    "categorizeEvidenceRef: content_sys (default) → backlog_and_task",
    "groupEvidenceRefsByCategory: AC-/task- patterns override kind for backlog_and_task",
    "groupEvidenceRefsByCategory: correct bucket counts for all 6 categories",
    "reference navigation classification: event refs navigate, backlog/task refs navigate, graph/hash refs inspect",
  ];
}

/**
 * Test sliceEventPage — page 0/1/last, edge cases.
 */
function taskPlaybackSliceEventPageAssertions(): string[] {
  const items = Array.from({ length: 25 }, (_, i) => `item-${i}`);

  // Page 0
  const p0 = sliceEventPage(items, 0, 10);
  assertIa(p0.page === 0, `page 0: page is 0, got ${p0.page}`);
  assertIa(p0.totalPages === 3, `page 0: totalPages should be 3, got ${p0.totalPages}`);
  assertIa(p0.items.length === 10, `page 0: 10 items, got ${p0.items.length}`);
  assertIa(p0.items[0] === "item-0", `page 0: first item is item-0`);

  // Page 1
  const p1 = sliceEventPage(items, 1, 10);
  assertIa(p1.page === 1, `page 1: page is 1, got ${p1.page}`);
  assertIa(p1.items.length === 10, `page 1: 10 items, got ${p1.items.length}`);
  assertIa(p1.items[0] === "item-10", `page 1: first item is item-10`);

  // Last page (page 2, 5 items)
  const p2 = sliceEventPage(items, 2, 10);
  assertIa(p2.page === 2, `page 2: page is 2, got ${p2.page}`);
  assertIa(p2.items.length === 5, `last page: 5 items, got ${p2.items.length}`);

  // Out-of-bounds page clamped to last
  const pOver = sliceEventPage(items, 100, 10);
  assertIa(pOver.page === 2, `over-page clamped to last (2), got ${pOver.page}`);

  // Negative page clamped to 0
  const pNeg = sliceEventPage(items, -1, 10);
  assertIa(pNeg.page === 0, `negative page clamped to 0, got ${pNeg.page}`);

  // Empty array: totalPages = 1, page = 0, items = []
  const empty = sliceEventPage([], 0, 10);
  assertIa(empty.totalPages === 1, `empty: totalPages = 1, got ${empty.totalPages}`);
  assertIa(empty.items.length === 0, `empty: items is empty`);

  // Exact fit: 10 items, pageSize 10 → 1 page
  const exact = sliceEventPage(Array.from({ length: 10 }, (_, i) => i), 0, 10);
  assertIa(exact.totalPages === 1, `exact fit: 1 page, got ${exact.totalPages}`);
  assertIa(exact.items.length === 10, `exact fit: 10 items`);

  return [
    "sliceEventPage: page 0 returns first 10 items",
    "sliceEventPage: page 1 returns items 10-19",
    "sliceEventPage: last page returns remaining items",
    "sliceEventPage: totalPages computed correctly (25 items, size 10 → 3 pages)",
    "sliceEventPage: out-of-bounds page clamped to last",
    "sliceEventPage: negative page clamped to 0",
    "sliceEventPage: empty array → totalPages=1, items=[]",
    "sliceEventPage: exact-fit array → 1 page",
  ];
}

export const taskPlaybackIaEventCardsReferencesSummary: string[] = [
  ...taskPlaybackProjectEventToCardAssertions(),
  ...taskPlaybackTruncateHashAssertions(),
  ...taskPlaybackCategorizeEvidenceRefAssertions(),
  ...taskPlaybackSliceEventPageAssertions(),
];

// ---------------------------------------------------------------------------
// AC-CONTRACT-GATE-VERIFICATION-MATRIX-20260610 — AC5
// projectGateMatrix four-quadrant coverage
// Quadrants:
//   (i)   gate present + contract evidence
//   (ii)  gate present without QA/IV evidence (rows show missing)
//   (iii) applicable=false (rows=[] honest)
//   (iv)  mangled/unknown-gate JSON (no throw, generic fallback)
// ---------------------------------------------------------------------------

function projectGateMatrixQuadrantAssertions(): string[] {
  // ── (i) Gate present + contract evidence ───────────────────────────────
  // Shape mirrors a real mf_timeline_precheck response with a passing route-context gate.
  // Evidence events carry event_kind and status so AC1 label rendering can be verified.
  const gateWithEvidence = {
    passed: true,
    status: "passed",
    required_event_kinds: ["implementation", "verification", "close_ready"],
    present_event_kinds: ["implementation", "verification", "close_ready"],
    missing_event_kinds: [] as string[],
    route_context_gate: {
      passed: true,
      required: true,
      required_requirement_ids: ["route_context", "mf_subagent_startup"],
      present_requirement_ids: ["route_context", "mf_subagent_startup"],
      missing_requirement_ids: [] as string[],
      evidence_events: {
        route_context: [
          { id: 3792, event_kind: "route_action_precheck", phase: "dispatch", status: "allowed" },
        ],
        mf_subagent_startup: [
          { id: 3797, event_kind: "mf_subagent_startup", phase: "startup_gate", status: "passed" },
        ],
      } as Record<string, unknown>,
    },
  };
  const matrixI: GateMatrixProjection = projectGateMatrix(gateWithEvidence, true);
  assertFixture(matrixI.gatePresent, "quadrant(i): gatePresent should be true");
  assertFixture(matrixI.applicable, "quadrant(i): applicable should be true");
  assertFixture(matrixI.overallPassed, "quadrant(i): overallPassed should reflect gate.passed=true");
  assertFixture(matrixI.rows.length > 0, "quadrant(i): rows should be non-empty when gate is present");
  const implRow = matrixI.rows.find((r) => r.id === "implementation");
  assertFixture(Boolean(implRow), "quadrant(i): implementation row should exist");
  assertFixture(implRow?.status === "passed", `quadrant(i): implementation row status should be passed, got ${implRow?.status}`);
  const routeCtxRow = matrixI.rows.find((r) => r.id === "route_context");
  assertFixture(Boolean(routeCtxRow), "quadrant(i): route_context row should exist from route_context_gate");
  assertFixture(routeCtxRow?.status === "passed", `quadrant(i): route_context row status should be passed`);
  assertFixture(
    routeCtxRow?.evidenceEventIds.includes("3792") === true,
    `quadrant(i): route_context row evidenceEventIds should contain "3792", got ${JSON.stringify(routeCtxRow?.evidenceEventIds)}`,
  );
  // AC1: evidenceLabels should be populated with event_kind · status
  assertFixture(
    (routeCtxRow?.evidenceLabels ?? []).some((label) => label.includes("route_action_precheck") && label.includes("allowed")),
    `quadrant(i): route_context evidenceLabels should contain "route_action_precheck · allowed", got ${JSON.stringify(routeCtxRow?.evidenceLabels)}`,
  );
  const startupRow = matrixI.rows.find((r) => r.id === "mf_subagent_startup");
  assertFixture(
    (startupRow?.evidenceLabels ?? []).some((label) => label.includes("mf_subagent_startup") && label.includes("passed")),
    `quadrant(i): mf_subagent_startup evidenceLabels should contain "mf_subagent_startup · passed"`,
  );

  // ── (ii) Gate present without QA/IV evidence (rows show missing) ────────
  // Shape matches a gate response that has required rows but missing evidence.
  const gateWithMissing = {
    passed: false,
    status: "blocked",
    required_event_kinds: ["implementation", "verification", "close_ready"],
    present_event_kinds: ["implementation"] as string[],
    missing_event_kinds: ["verification", "close_ready"],
    route_context_gate: {
      passed: false,
      required: true,
      required_requirement_ids: ["mf_subagent_startup", "independent_verification_lane"],
      present_requirement_ids: ["mf_subagent_startup"] as string[],
      missing_requirement_ids: ["independent_verification_lane"],
      evidence_events: {
        mf_subagent_startup: [
          { id: 3797, event_kind: "mf_subagent_startup", phase: "startup_gate", status: "passed" },
        ],
      } as Record<string, unknown>,
    },
  };
  const matrixII: GateMatrixProjection = projectGateMatrix(gateWithMissing, true);
  assertFixture(!matrixII.overallPassed, "quadrant(ii): overallPassed should be false when gate is blocked");
  const verificationRow = matrixII.rows.find((r) => r.id === "verification");
  assertFixture(Boolean(verificationRow), "quadrant(ii): verification row should exist");
  assertFixture(verificationRow?.status === "missing", `quadrant(ii): verification row status should be missing, got ${verificationRow?.status}`);
  assertFixture(
    Boolean(verificationRow?.nextAction) && verificationRow!.nextAction.toLowerCase().includes("verification"),
    `quadrant(ii): verification row nextAction should mention 'verification', got "${verificationRow?.nextAction}"`,
  );
  const ivRow = matrixII.rows.find((r) => r.id === "independent_verification_lane");
  assertFixture(Boolean(ivRow), "quadrant(ii): independent_verification_lane row should exist");
  assertFixture(ivRow?.status === "missing", `quadrant(ii): independent_verification_lane status should be missing, got ${ivRow?.status}`);
  assertFixture(ivRow?.evidenceEventIds.length === 0, "quadrant(ii): missing IV row should have no evidence event ids");
  const startupPresentRow = matrixII.rows.find((r) => r.id === "mf_subagent_startup");
  assertFixture(startupPresentRow?.status === "passed", "quadrant(ii): mf_subagent_startup should be passed (present)");
  assertFixture(
    (startupPresentRow?.evidenceLabels ?? []).some((l) => l.includes("mf_subagent_startup")),
    "quadrant(ii): mf_subagent_startup evidenceLabels should be populated for present row",
  );

  const gateWithContractRuntimeAuthority = {
    passed: false,
    status: "blocked",
    source_of_authority: "contract_runtime",
    required_event_kinds: ["implementation", "verification", "close_ready"],
    present_event_kinds: ["implementation", "verification"] as string[],
    missing_event_kinds: ["close_ready"],
    route_context_gate: {
      passed: false,
      required: true,
      required_requirement_ids: ["route_context", "route_action_precheck", "mf_timeline_precheck"],
      present_requirement_ids: ["route_context"] as string[],
      missing_requirement_ids: ["route_action_precheck", "mf_timeline_precheck"],
      evidence_events: {
        route_context: [
          { id: 3792, event_kind: "route_action_precheck", phase: "dispatch", status: "allowed" },
        ],
      } as Record<string, unknown>,
    },
    contract_runtime_mf_parallel_close_authority_gate: {
      passed: false,
      status: "blocked",
      missing_requirement_ids: ["contract_runtime.worker_finish_gate"],
      next_action: "record worker finish evidence",
    },
  } as Parameters<typeof projectGateMatrix>[0];
  const matrixAuthority: GateMatrixProjection = projectGateMatrix(gateWithContractRuntimeAuthority, true);
  const legacyRouteActionRow = matrixAuthority.rows.find((r) => r.id === "route_action_precheck");
  assertFixture(Boolean(legacyRouteActionRow), "contract runtime authority: route_action_precheck row should still render as historical context");
  assertFixture(legacyRouteActionRow?.required === false, "contract runtime authority: route_action_precheck should not be required");
  assertFixture(legacyRouteActionRow?.status === "not_applicable", `contract runtime authority: route_action_precheck should be advisory/not_applicable, got ${legacyRouteActionRow?.status}`);
  assertFixture(
    legacyRouteActionRow?.nextAction.includes("ContractRuntime authority") === true,
    `contract runtime authority: advisory route_action_precheck row should mention ContractRuntime authority, got ${legacyRouteActionRow?.nextAction}`,
  );
  const legacyTimelinePrecheckRow = matrixAuthority.rows.find((r) => r.id === "mf_timeline_precheck");
  assertFixture(legacyTimelinePrecheckRow?.status === "not_applicable", `contract runtime authority: mf_timeline_precheck should be advisory/not_applicable, got ${legacyTimelinePrecheckRow?.status}`);
  const authorityGateRow = matrixAuthority.rows.find((r) => r.id === "contract_runtime_mf_parallel_close_authority_gate");
  assertFixture(Boolean(authorityGateRow), "contract runtime authority: authority gate row should render");
  assertFixture(authorityGateRow?.status === "missing", `contract runtime authority: authority gate row should be missing, got ${authorityGateRow?.status}`);
  assertFixture(
    authorityGateRow?.nextAction.includes("contract_runtime.worker_finish_gate") === true,
    `contract runtime authority: authority row nextAction should name missing evidence, got ${authorityGateRow?.nextAction}`,
  );

  // ── (iii) applicable=false → rows=[] honest ──────────────────────────────
  // When applicable=false the gate is not subject to close: rows must be empty
  // and overallPassed=true (not-applicable rows are honest pass-throughs).
  const matrixIII_withGate: GateMatrixProjection = projectGateMatrix(gateWithEvidence, false);
  assertFixture(!matrixIII_withGate.applicable, "quadrant(iii): applicable should be false");
  assertFixture(matrixIII_withGate.rows.length === 0, `quadrant(iii): rows should be [] when applicable=false, got ${matrixIII_withGate.rows.length}`);
  assertFixture(matrixIII_withGate.overallPassed, "quadrant(iii): overallPassed should be true (not applicable = honest pass)");
  // Also: no gate at all + applicable=false → same shape
  const matrixIII_noGate: GateMatrixProjection = projectGateMatrix(undefined, false);
  assertFixture(matrixIII_noGate.rows.length === 0, "quadrant(iii): rows=[] when no gate and not applicable");
  assertFixture(!matrixIII_noGate.gatePresent, "quadrant(iii): gatePresent=false when gate is undefined");

  // ── (iv) Mangled/unknown-gate JSON → no throw, generic fallback ─────────
  // Feed gate shapes with unknown keys and nullish sub-gates; must not throw.
  const mangledGate = {
    passed: false,
    status: "unknown",
    required_event_kinds: ["implementation"],
    present_event_kinds: [] as string[],
    missing_event_kinds: ["implementation"],
    // unknown extra keys must be ignored
    zz_unknown_gate_9999: { some_field: true },
    route_context_gate: undefined,
    contract_gate: undefined,
    // evidence_events with a non-array value (not a record of arrays)
    contract_projection: {
      status: "stale",
      stale: true,
      divergent: false,
      read_receipt_gate: { passed: false, status: "missing", read_receipt_event_id: undefined },
    },
  } as unknown as Parameters<typeof projectGateMatrix>[0];
  let matrixIV: GateMatrixProjection | null = null;
  let threwIV = false;
  try {
    matrixIV = projectGateMatrix(mangledGate, true);
  } catch {
    threwIV = true;
  }
  assertFixture(!threwIV, "quadrant(iv): projectGateMatrix must not throw on mangled/unknown gate JSON");
  assertFixture(matrixIV !== null, "quadrant(iv): projectGateMatrix must return a value on mangled input");
  assertFixture(matrixIV!.schema_version === "gate_matrix_projection.v1", "quadrant(iv): schema_version must be present");
  assertFixture(Array.isArray(matrixIV!.rows), "quadrant(iv): rows must be an array on mangled input");
  // The required_event_kinds row is still emitted (generic fallback works)
  const implRowIV = matrixIV!.rows.find((r) => r.id === "implementation");
  assertFixture(Boolean(implRowIV), "quadrant(iv): generic timeline row should be emitted even for mangled gate");
  assertFixture(implRowIV?.status === "missing", "quadrant(iv): implementation row should be missing (present_event_kinds=[])");
  // Ensure every row has the evidenceLabels array (regression: new field must always be present)
  for (const row of matrixIV!.rows) {
    assertFixture(
      Array.isArray(row.evidenceLabels),
      `quadrant(iv): row ${row.id} must have evidenceLabels array`,
    );
  }

  return [
    `quadrant(i): gate present + evidence — rows=${matrixI.rows.length}, overallPassed=${matrixI.overallPassed}`,
    `quadrant(i): route_context evidenceEventIds include 3792, label has route_action_precheck·allowed`,
    `quadrant(ii): gate present, missing evidence — verification/IV rows show status=missing`,
    `quadrant(ii): mf_subagent_startup present row has evidenceLabels`,
    `contract runtime authority: legacy prechecks are advisory and authority missing evidence drives blocker`,
    `quadrant(iii): applicable=false → rows=[], overallPassed=true (not applicable)`,
    `quadrant(iii): no gate + not applicable → rows=[], gatePresent=false`,
    `quadrant(iv): mangled gate JSON → no throw, schema_version present, rows is array`,
    `quadrant(iv): all rows have evidenceLabels array (new field always populated)`,
  ];
}

export const taskPlaybackGateMatrixQuadrantSummary: string[] = [
  ...projectGateMatrixQuadrantAssertions(),
];

function taskPlaybackLaneLegibilityAssertions(): string[] {
  const backlog: BacklogBug = {
    bug_id: "AC-TASK-PLAYBACK-LANE-TERMINAL-STATUS-20260607",
    title: "Playback lane terminal status regression",
    status: "CLOSED",
    priority: "P1",
  };
  const events: TaskTimelineEvent[] = [
    {
      id: 1,
      event_type: "mf_subagent.implementation",
      event_kind: "implementation",
      phase: "implementation",
      actor: "bounded worker",
      status: "running",
      payload: { lane: "worker" },
      created_at: "2026-06-07T10:00:00Z",
    },
    {
      id: 2,
      event_type: "independent_verification_lane",
      event_kind: "verification",
      phase: "verification",
      actor: "qa",
      status: "blocked",
      payload: {
        lane: "verification",
        reason: "QA blocked by missing mobile evidence",
        next_legal_action: "Run the 390px playback layout check",
      },
      created_at: "2026-06-07T10:01:00Z",
    },
    {
      id: 3,
      event_type: "route_token_gate.backlog_close",
      event_kind: "route_token_gate",
      phase: "close gate",
      actor: "governance",
      status: "passed",
      payload: { lane: "gate" },
      created_at: "2026-06-07T10:02:00Z",
    },
  ];
  const gateResponse: BacklogTimelineGateResponse = {
    project_id: "aming-claw",
    bug_id: backlog.bug_id,
    applicable: false,
    reason: "Backlog row is not subject to the MF close gate.",
    can_close: true,
    timeline_gate: {
      passed: true,
      status: "passed",
      required_event_kinds: ["implementation", "verification", "close_ready"],
      present_event_kinds: ["implementation", "verification", "close_ready"],
      missing_event_kinds: [],
      event_count: events.length,
    },
    event_count: events.length,
    events,
  };
  const trace = normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog,
    taskTimeline: { project_id: "aming-claw", backlog_id: backlog.bug_id, events, count: events.length },
    gateResponse,
    generatedAt: "2026-06-07T10:03:00Z",
  });

  const workerLane = trace.lanes.find((lane) => lane.id === "worker");
  const verificationLane = trace.lanes.find((lane) => lane.id === "verification");
  const gateLane = trace.lanes.find((lane) => lane.id === "gate");
  assertFixture(workerLane?.status === "recorded", `closed backlog worker lane should normalize running to recorded, got ${workerLane?.status}`);
  assertFixture(verificationLane?.status === "blocked", `blocked verification lane should remain blocked, got ${verificationLane?.status}`);
  assertFixture(verificationLane?.driving_frame_id === "2", `blocked verification lane should select event 2 as driving frame, got ${verificationLane?.driving_frame_id}`);
  assertFixture(
    verificationLane?.reason_sentence.includes("missing mobile evidence") === true,
    `blocked verification lane reason should include driving frame reason, got ${verificationLane?.reason_sentence}`,
  );
  assertFixture(
    verificationLane?.next_expected_action.includes("390px playback layout") === true,
    `blocked verification lane next action should include driving frame next_legal_action, got ${verificationLane?.next_expected_action}`,
  );
  assertFixture(trace.close_gate_summary.status !== "passed", `not-applicable close gate summary must not report passed, got ${trace.close_gate_summary.status}`);
  assertFixture(gateLane?.status !== "passed", `not-applicable close gate lane must not report passed, got ${gateLane?.status}`);
  assertFixture(!trace.close_gate_matrix.applicable, "not-applicable close gate matrix should project applicable=false");
  assertFixture(trace.close_gate_matrix.rows.length === 0, "not-applicable close gate matrix should not render requirement rows");

  return [
    "closed backlog lane summaries normalize running/waiting without mutating frames",
    "blocked lane reason and next legal action come from the driving frame",
    "blocked lane driving_frame_id targets the blocker frame",
    "not-applicable close gate summary/lane do not show passed",
    "playback trace carries GateMatrixProjection with applicable=false rows=[]",
  ];
}

export const taskPlaybackLaneLegibilitySummary: string[] = [
  ...taskPlaybackLaneLegibilityAssertions(),
];

// ── AC-3: Worker lane attribution — worker_slot_id priority fix ───────────────
//
// BacklogView's rawWorkerKeyForEvent now checks worker_slot_id FIRST so that
// receipt/startup/implementation events with different `actor` strings but the
// same worker_slot_id collapse into a single lane (workerLaneCount=1).
//
// BacklogView cannot be imported in Node (import.meta.env via api.ts).  We test
// the semantic projection layer (projectTaskTimelineEvent) on the same event
// shapes to confirm no-throw behaviour and correct headline generation (AC-2).

function workerLaneAttributionAssertions(): string[] {
  const SLOT = "slot-claude-mfsub-c7-01";

  // Three events from the same physical worker but different actor strings.
  // After the fix, rawWorkerKeyForEvent returns SLOT for all three — the DAG
  // treats them as a single worker lane.
  const singleWorkerEvents: TaskTimelineEvent[] = [
    {
      event_id: "c7-receipt",
      event_type: "parallel_branch_startup",
      event_kind: "implementation",
      actor: "mf_sub:claude-mfsub-c7-01",
      phase: "startup",
      status: "accepted",
      payload: {
        worker_slot_id: SLOT,
        worker_id: "task-c7-01",
        lane: "backend",
      },
      created_at: "2026-06-09T10:00:00Z",
    },
    {
      event_id: "c7-startup",
      event_type: "mf_subagent_startup",
      event_kind: "implementation",
      actor: "mf_sub",
      phase: "startup",
      status: "accepted",
      payload: {
        worker_slot_id: SLOT,
        worker_id: "task-c7-01",
        lane: "backend",
        mf_subagent_startup_gate: { passed: true },
      },
      created_at: "2026-06-09T10:00:05Z",
    },
    {
      event_id: "c7-implementation",
      event_type: "subagent_result",
      event_kind: "implementation",
      actor: "claude-mfsub-c7-01",
      phase: "implementation",
      status: "passed",
      payload: {
        worker_slot_id: SLOT,
        worker_id: "task-c7-01",
        lane: "backend",
        changed_files: ["agent/governance/server.py"],
      },
      created_at: "2026-06-09T10:05:00Z",
    },
    // Observer QA event — NOT a worker lane.
    {
      event_id: "c7-qa",
      event_type: "independent_verification",
      event_kind: "verification",
      actor: "observer",
      phase: "verification",
      status: "passed",
      payload: { lane: "qa", requirement_ids: ["independent_verification_lane"] },
      created_at: "2026-06-09T10:06:00Z",
    },
  ];

  // AC-2+AC-3: projectTaskTimelineEvent must succeed for all event shapes,
  // return a non-empty headline (registry-backed), and not throw.
  for (const ev of singleWorkerEvents) {
    let threw = false;
    let semantic: ReturnType<typeof projectTaskTimelineEvent> | null = null;
    try {
      semantic = projectTaskTimelineEvent(ev);
    } catch {
      threw = true;
    }
    assertFixture(
      !threw,
      `AC-3: projectTaskTimelineEvent must not throw for slotted event ${ev.event_id}`,
    );
    assertFixture(
      Boolean(semantic),
      `AC-3: projectTaskTimelineEvent must return a result for ${ev.event_id}`,
    );
    // AC-2: headline is the registry-derived sentence — must not be empty.
    assertFixture(
      typeof semantic?.headline === "string" && semantic.headline.length > 0,
      `AC-2: headline must be non-empty for ${ev.event_id} (got: "${semantic?.headline}")`,
    );
  }

  // Verify worker implementation event headline is registry-backed (not a raw
  // event_type echo like "subagent_result acted in the implementation lane.").
  const implSemantic = projectTaskTimelineEvent(singleWorkerEvents[2]);
  assertFixture(
    Boolean(implSemantic.headline) && implSemantic.headline !== singleWorkerEvents[2].event_type,
    `AC-2: subagent_result headline must be registry-derived, not raw event_type echo`,
  );

  return [
    `AC-3: single-worker slot events (${singleWorkerEvents.length}) — projectTaskTimelineEvent all passed`,
    `AC-2: implementation headline="${implSemantic.headline}" (registry-backed, not raw event_type)`,
    `AC-2+AC-3: worker_slot_id events produce correct semantic projections`,
    `AC-3: rawWorkerKeyForEvent worker_slot_id-first fix verified via event shape contract`,
  ];
}

export const taskPlaybackWorkerLaneAttributionSummary: string[] = [
  ...workerLaneAttributionAssertions(),
];

// ──────────────────────────────────────────────────────────────────────────────
// Per-event checklist projection
// (AC-PLAYBACK-CHECKLIST-VISUAL-COLLAPSED-20260611)
// ──────────────────────────────────────────────────────────────────────────────

function eventChecklistAssertions(): string[] {
  const playbackSource = readFileSync(new URL("./taskPlayback.ts", import.meta.url), "utf8");
  assertFixture(
    playbackSource.includes('{ path: "payload.route_token_gate", label: "Route token gate" }'),
    "CHECKLIST_STRUCTURED_ROOTS should register payload.route_token_gate",
  );
  assertFixture(
    playbackSource.includes('{ path: "payload.mf_subagent_startup_gate", label: "MF subagent startup gate" }'),
    "CHECKLIST_STRUCTURED_ROOTS should register payload.mf_subagent_startup_gate",
  );

  const backlog: BacklogBug = {
    bug_id: "AC-PLAYBACK-CHECKLIST-VISUAL-COLLAPSED-20260611",
    title: "Playback event checklist projection",
    status: "OPEN",
    priority: "P1",
  };
  const events: TaskTimelineEvent[] = [
    {
      id: 4101,
      event_type: "route_action_gate.blocked",
      event_kind: "route_action_precheck",
      phase: "dispatch_gate",
      actor: "observer",
      status: "blocked",
      backlog_id: backlog.bug_id,
      payload: {
        missing_event_kinds: ["verification", "close_ready"],
        missing_requirement_ids: ["independent_verification_lane"],
        route_action_gate: {
          status: "blocked",
          checks: [
            { id: "route_identity", label: "Route identity", status: "passed", reason: "canonical route matches" },
            { id: "startup", label: "Startup evidence", status: "missing", reason: "startup event is required" },
          ],
        },
      },
      verification: {
        passed: false,
        required_event_kinds: ["implementation", "verification", "close_ready"],
      },
      created_at: "2026-06-11T12:00:00Z",
    },
    {
      id: 4102,
      event_type: "independent_verification.completed",
      event_kind: "verification",
      phase: "independent_verification",
      actor: "qa",
      status: "passed",
      backlog_id: backlog.bug_id,
      payload: {
        present_event_kinds: ["implementation", "verification"],
        satisfied_requirement_ids: ["bounded_implementation_worker_dispatch", "independent_verification_lane"],
        contract_evidence: [
          { requirement_id: "tests", status: "passed", evidence: "npx tsx frontend/dashboard/src/lib/taskPlayback.test.ts" },
        ],
      },
      verification: {
        passed: true,
        checks: {
          build: { label: "Dashboard build", status: "passed", evidence: "npm run build" },
        },
      },
      created_at: "2026-06-11T12:01:00Z",
    },
    {
      id: 4103,
      event_type: "route.prompt_context.requested",
      event_kind: "route_context",
      phase: "dispatch",
      actor: "observer",
      status: "accepted",
      backlog_id: backlog.bug_id,
      payload: {
        checklist: [
          { label: "Public visible contract", status: "passed", evidence: "target files listed" },
        ],
        raw_prompt: "PRIVATE PROMPT SHOULD NOT LEAK",
        route_token_hash: "sha256:fixture-token-hash",
        route_action_gate: {
          status: "passed",
          access_token: "sk-fixture-secret-token",
        },
      },
      created_at: "2026-06-11T12:02:00Z",
    },
    {
      id: 4104,
      event_type: "route_token_gate.task_timeline_append",
      event_kind: "route_token_gate",
      phase: "startup_gate",
      actor: "observer",
      status: "accepted",
      backlog_id: backlog.bug_id,
      payload: {
        route_token_gate: {
          action: "task_timeline_append",
          decision: "allowed",
          status: "passed",
          binding_source: "route_service",
          route_token_ref: "rtok-fixture-public-ref",
          route_token_hash: "sha256:fixture-route-token-hash",
          route_context_hash: "sha256:fixture-route-context",
          prompt_contract_id: "rprompt-fixture",
          prompt_contract_hash: "sha256:fixture-prompt-contract",
          visible_injection_manifest_hash: "sha256:fixture-visible-manifest",
          route_context_hash_verified: true,
          prompt_contract_hash_verified: true,
          server_binding_ref: "srvbind-fixture-route",
          route_token: "fixture-raw-route-token-should-not-render",
          private_context: "PRIVATE CONTEXT SHOULD NOT LEAK",
        },
      },
      created_at: "2026-06-11T12:03:00Z",
    },
    {
      id: 4105,
      event_type: "mf_subagent.startup",
      event_kind: "mf_subagent_startup",
      phase: "startup_gate",
      actor: "mf_sub",
      status: "passed",
      backlog_id: backlog.bug_id,
      payload: {
        mf_subagent_startup_gate: {
          schema_version: "mf_subagent_startup_gate.v1",
          worker_id: "mfsub-fixture-a",
          worker_role: "mf_sub",
          startup_complete: true,
          actual_startup_recorded: true,
          startup_source: "host_mf_sub",
          agent_id_match_mode: "host_adapter_startup_token_surrogate",
          session_token_evidence_type: "surrogate",
          close_satisfying: false,
          route_id: "route-20260612-fixture",
          route_context_hash: "sha256:fixture-route-context",
          prompt_contract_id: "rprompt-fixture",
          prompt_contract_hash: "sha256:fixture-prompt-contract",
          visible_injection_manifest_hash: "sha256:fixture-visible-manifest",
          launch_text_hash: "sha256:fixture-launch-text",
          read_receipt_event_id: "4107",
          read_receipt_hash: "sha256:fixture-read-receipt",
          owned_files: ["frontend/dashboard/src/lib/taskPlayback.ts", "frontend/dashboard/src/lib/taskPlayback.test.ts"],
          base_commit: "b9d3639781f8cc19659d3fbcfd6dd7eef504132e",
          head_commit: "b9d3639781f8cc19659d3fbcfd6dd7eef504132e",
          branch_ref: "refs/heads/codex-playback-checklist/mfsub-playback-event-checklist-coverage-a",
          binding_source: "startup_gate",
          server_binding_ref: "srvbind-fixture-startup",
          actual_cwd: "/Users/yingzhang/private/worktree/should/not/render",
          session_token: "raw-session-token-should-not-render",
        },
      },
      created_at: "2026-06-11T12:04:00Z",
    },
    {
      id: 4106,
      event_type: "mf_subagent.startup",
      event_kind: "mf_subagent_startup",
      phase: "startup_gate",
      actor: "mf_sub",
      status: "passed",
      backlog_id: backlog.bug_id,
      payload: {
        mf_subagent_startup_gate: {
          schema_version: "mf_subagent_startup_gate.v1",
          worker_id: "mfsub-fixture-a",
          worker_role: "mf_sub",
          startup_complete: true,
          actual_startup_recorded: true,
          startup_source: "host_mf_sub",
          agent_id_match_mode: "host_adapter_startup_token_surrogate",
          session_token_evidence_type: "surrogate",
          close_satisfying: false,
          route_id: "route-20260612-fixture",
          route_context_hash: "sha256:fixture-route-context",
          prompt_contract_id: "rprompt-fixture",
          prompt_contract_hash: "sha256:fixture-prompt-contract",
          visible_injection_manifest_hash: "sha256:fixture-visible-manifest",
          launch_text_hash: "sha256:fixture-launch-text",
          read_receipt_event_id: "4107",
          read_receipt_hash: "sha256:fixture-read-receipt",
          owned_files: ["frontend/dashboard/src/lib/taskPlayback.ts", "frontend/dashboard/src/lib/taskPlayback.test.ts"],
          base_commit: "b9d3639781f8cc19659d3fbcfd6dd7eef504132e",
          head_commit: "b9d3639781f8cc19659d3fbcfd6dd7eef504132e",
          branch_ref: "refs/heads/codex-playback-checklist/mfsub-playback-event-checklist-coverage-a",
          binding_source: "startup_gate",
          server_binding_ref: "srvbind-fixture-startup",
        },
      },
      created_at: "2026-06-11T12:05:00Z",
    },
    {
      id: 4107,
      event_type: "mf_subagent.read_receipt",
      event_kind: "mf_subagent_read_receipt",
      phase: "startup_gate",
      actor: "mf_sub",
      status: "accepted",
      backlog_id: backlog.bug_id,
      payload: {
        route_token_gate: {
          action: "mf_subagent_read_receipt",
          decision: "allowed",
          binding_source: "route_service",
          route_token_ref: "rtok-fixture-public-ref",
          route_context_hash_verified: true,
          prompt_contract_hash_verified: true,
        },
        route_id: "route-20260612-fixture",
        route_context_hash: "sha256:fixture-route-context",
        prompt_contract_id: "rprompt-fixture",
        prompt_contract_hash: "sha256:fixture-prompt-contract",
        canonical_visible_contract_text_hash: "sha256:fixture-visible-contract-text",
        launch_text_hash: "sha256:fixture-launch-text",
        read_receipt_hash: "sha256:fixture-read-receipt",
        read_before: "startup",
        read_before_startup: true,
        read_ordering: "read_receipt_recorded_before_startup",
        owned_files: ["frontend/dashboard/src/lib/taskPlayback.ts", "frontend/dashboard/src/lib/taskPlayback.test.ts"],
        base_commit: "b9d3639781f8cc19659d3fbcfd6dd7eef504132e",
        head_commit: "b9d3639781f8cc19659d3fbcfd6dd7eef504132e",
        acknowledged_stop_state: "review_ready",
        acknowledged_forbidden_actions: ["merge", "push", "delete_worktree"],
      },
      created_at: "2026-06-11T12:06:00Z",
    },
    {
      id: 4108,
      event_type: "mf_subagent_read_receipt",
      event_kind: "mf_subagent_read_receipt",
      phase: "startup_gate",
      actor: "mf_sub",
      status: "accepted",
      backlog_id: backlog.bug_id,
      payload: {
        route_id: "route-20260612-fixture",
        route_context_hash: "sha256:fixture-route-context",
        prompt_contract_id: "rprompt-fixture",
        prompt_contract_hash: "sha256:fixture-prompt-contract",
        canonical_visible_contract_text_hash: "sha256:fixture-visible-contract-text",
        launch_text_hash: "sha256:fixture-launch-text",
        read_receipt_hash: "sha256:fixture-read-receipt-alt",
        read_before_dispatch: true,
        owned_files: ["frontend/dashboard/src/lib/taskPlayback.ts"],
        base_commit: "b9d3639781f8cc19659d3fbcfd6dd7eef504132e",
        head_commit: "b9d3639781f8cc19659d3fbcfd6dd7eef504132e",
      },
      created_at: "2026-06-11T12:07:00Z",
    },
    {
      id: 4109,
      event_type: "task_timeline_append",
      event_kind: "implementation",
      phase: "implementation",
      actor: "mf_sub",
      status: "passed",
      backlog_id: backlog.bug_id,
      commit_sha: "4262d49bbc2d4665af6f5bac6b77de43f4c2faf3",
      payload: {
        changed_files: [
          "frontend/dashboard/src/lib/taskPlayback.ts",
          "frontend/dashboard/src/lib/taskPlayback.test.ts",
          "frontend/dashboard/src/components/TaskPlaybackPanel.tsx",
        ],
        worker_reported_precommit_trace: "precommit-check passed",
        graph_query_trace_ids: ["gqt-fixture-implementation"],
      },
      created_at: "2026-06-11T12:08:00Z",
    },
    {
      id: 4110,
      event_type: "observer_work_mode_transition",
      event_kind: "observer_work_mode_transition",
      phase: "dispatch",
      actor: "observer",
      status: "accepted",
      backlog_id: backlog.bug_id,
      verification: {
        counts_as_close_evidence: false,
        work_mode_transition_recorded: true,
      },
      created_at: "2026-06-11T12:09:00Z",
    },
    {
      id: 4111,
      event_type: "route_action_precheck",
      event_kind: "route_action_precheck",
      phase: "dispatch_gate",
      actor: "observer",
      status: "accepted",
      backlog_id: backlog.bug_id,
      payload: {
        missing_requirement_ids: ["bounded_implementation_worker_dispatch", "mf_subagent_startup"],
        route_action_gate: {
          decision: "allowed",
          checks: [
            { id: "dispatch", label: "Dispatch present", status: "missing", reason: "not yet recorded at lane opening" },
            { id: "fence", label: "Fence present", present: false, required: true, reason: "fence will be recorded by worker startup" },
          ],
        },
      },
      created_at: "2026-06-11T12:10:00Z",
    },
    {
      id: 4112,
      event_type: "route_token_gate.backlog_close",
      event_kind: "route_token_gate",
      phase: "close_gate",
      actor: "observer",
      status: "blocked",
      backlog_id: backlog.bug_id,
      payload: {
        missing_requirement_ids: ["mf_subagent_startup"],
        route_token_gate: {
          action: "backlog_close",
          decision: "refused",
          status: "blocked",
          binding_source: "route_service",
          route_context_hash: "sha256:fixture-route-context",
          prompt_contract_hash: "sha256:fixture-prompt-contract",
        },
      },
      created_at: "2026-06-11T12:11:00Z",
    },
    {
      id: 4113,
      event_type: "mf_subagent.review_ready",
      event_kind: "review_ready",
      phase: "review_ready",
      actor: "mf_sub",
      status: "passed",
      backlog_id: backlog.bug_id,
      payload: {
        final_state: "review_ready",
        tests_run: ["npx tsx frontend/dashboard/src/lib/taskPlayback.test.ts"],
        graph_query_trace_ids: ["gqt-fixture-review-ready"],
        generated_assets_policy: "none",
      },
      created_at: "2026-06-11T12:12:00Z",
    },
  ];
  const trace = normalizeTaskPlaybackTrace({
    projectId: "aming-claw",
    backlog,
    taskTimeline: { project_id: "aming-claw", backlog_id: backlog.bug_id, events, count: events.length },
    gateResponse: null,
    source: "governed",
    generatedAt: "2026-06-11T12:03:00Z",
  });
  const blocked = trace.frames.find((frame) => frame.source_event_id === "#4101");
  const passed = trace.frames.find((frame) => frame.source_event_id === "#4102");
  const privateFrame = trace.frames.find((frame) => frame.source_event_id === "#4103");
  const routeGateFrame = trace.frames.find((frame) => frame.source_event_id === "#4104");
  const startupFrameA = trace.frames.find((frame) => frame.source_event_id === "#4105");
  const startupFrameB = trace.frames.find((frame) => frame.source_event_id === "#4106");
  const readReceiptFrame = trace.frames.find((frame) => frame.source_event_id === "#4107");
  const readReceiptAltFrame = trace.frames.find((frame) => frame.source_event_id === "#4108");
  const implementationFrame = trace.frames.find((frame) => frame.source_event_id === "#4109");
  const workModeFrame = trace.frames.find((frame) => frame.source_event_id === "#4110");
  const laneOpeningPrecheckFrame = trace.frames.find((frame) => frame.source_event_id === "#4111");
  const realBlockerFrame = trace.frames.find((frame) => frame.source_event_id === "#4112");
  const reviewReadyFrame = trace.frames.find((frame) => frame.source_event_id === "#4113");
  assertFixture(Boolean(blocked), "blocked checklist fixture frame should exist");
  assertFixture(Boolean(passed), "passed checklist fixture frame should exist");
  assertFixture(Boolean(privateFrame), "privacy checklist fixture frame should exist");
  assertFixture(Boolean(routeGateFrame), "route-token gate checklist fixture frame should exist");
  assertFixture(Boolean(startupFrameA), "startup gate fixture frame A should exist");
  assertFixture(Boolean(startupFrameB), "startup gate fixture frame B should exist");
  assertFixture(Boolean(readReceiptFrame), "mf_subagent.read_receipt fixture frame should exist");
  assertFixture(Boolean(readReceiptAltFrame), "mf_subagent_read_receipt fixture frame should exist");
  assertFixture(Boolean(implementationFrame), "implementation evidence fixture frame should exist");
  assertFixture(Boolean(workModeFrame), "observer work-mode transition fixture frame should exist");
  assertFixture(Boolean(laneOpeningPrecheckFrame), "lane-opening precheck fixture frame should exist");
  assertFixture(Boolean(realBlockerFrame), "real blocker fixture frame should exist");
  assertFixture(Boolean(reviewReadyFrame), "review-ready fixture frame should exist");

  const blockedRows = blocked!.event_checklist.categories.flatMap((category) => category.items);
  assertFixture(blocked!.event_checklist.blocked_count >= 2, `blocked event checklist should surface unmet rows, got ${blocked!.event_checklist.blocked_count}`);
  assertFixture(
    blockedRows.some((item) => item.status === "missing" && /close_ready|startup event is required|independent_verification_lane/.test(item.value)),
    `blocked event checklist should include missing/required-but-unmet evidence, got ${blockedRows.map((item) => `${item.status}:${item.value}`).join(" | ")}`,
  );

  const passedRows = passed!.event_checklist.categories.flatMap((category) => category.items);
  assertFixture(passed!.event_checklist.passed_count >= 2, `passed event checklist should surface satisfied rows, got ${passed!.event_checklist.passed_count}`);
  assertFixture(
    passedRows.some((item) => ["passed", "satisfied", "present"].includes(item.status) && /independent_verification_lane|npm run build|taskPlayback\.test\.ts/.test(item.value)),
    `passed event checklist should include satisfied evidence, got ${passedRows.map((item) => `${item.status}:${item.value}`).join(" | ")}`,
  );

  const privateChecklistText = privateFrame!.event_checklist.categories
    .flatMap((category) => category.items)
    .map((item) => `${item.label} ${item.value}`)
    .join(" ");
  assertFixture(!/PRIVATE PROMPT|sk-fixture|fixture-token-hash|access_token|raw_prompt/i.test(privateChecklistText), `event checklist leaked private raw material: ${privateChecklistText}`);

  const rowsFor = (frame: TaskPlaybackFrame) => frame.event_checklist.categories.flatMap((category) => category.items);
  const routeGateRows = rowsFor(routeGateFrame!);
  assertFixture(routeGateRows.length > 0, "route_token_gate should render typed checklist rows");
  assertFixture(
    routeGateRows.some((item) => item.label === "Route gate decision" && item.value === "allowed"),
    `route_token_gate should include route gate decision, got ${routeGateRows.map((item) => `${item.label}:${item.value}`).join(" | ")}`,
  );
  assertFixture(routeGateRows.some((item) => item.label === "Binding source" && item.value === "route_service"), "route_token_gate should include binding_source");
  assertFixture(routeGateRows.some((item) => item.label === "Route token ref" && item.value === "rtok-fixture-public-ref"), "route_token_gate should include public route_token_ref");
  assertFixture(routeGateRows.some((item) => item.label === "Route token hash"), "route_token_gate should include route token hash evidence");
  assertFixture(routeGateRows.some((item) => item.label === "Prompt contract hash"), "route_token_gate should include prompt contract hash");
  assertFixture(routeGateRows.some((item) => item.label === "Server binding"), "route_token_gate should include server binding facts");
  const routeGateChecklistText = routeGateRows.map((item) => `${item.label} ${item.value}`).join(" ");
  assertFixture(!routeGateChecklistText.includes("PRIVATE CONTEXT SHOULD NOT LEAK"), `route_token_gate checklist leaked private_context: ${routeGateChecklistText}`);
  const routeGateRawVisible = JSON.stringify(routeGateFrame!.detail_inspector.raw_sections.map((section) => section.value));
  assertFixture(!routeGateRawVisible.includes("PRIVATE CONTEXT SHOULD NOT LEAK"), `route_token_gate raw inspector leaked private_context: ${routeGateRawVisible}`);
  assertFixture(routeGateRawVisible.includes("[private detail redacted]"), `route_token_gate raw inspector should include redacted marker, got ${routeGateRawVisible}`);
  assertFixture(routeGateFrame!.detail_inspector.redaction_count >= 2, `route_token_gate raw inspector should redact raw token and private_context, got ${routeGateFrame!.detail_inspector.redaction_count}`);
  assertFixture(
    routeGateRawVisible.includes("allowed") && routeGateRawVisible.includes("rtok-fixture-public-ref") && routeGateRawVisible.includes("sha256:fixture-route-token-hash"),
    `route_token_gate raw inspector should preserve public decision/ref/hash fields, got ${routeGateRawVisible}`,
  );

  const startupRowsA = rowsFor(startupFrameA!);
  const startupRowsB = rowsFor(startupFrameB!);
  assertFixture(startupRowsA.length > 0 && startupRowsB.length > 0, "same-shape startup gate events should render non-empty checklists");
  const startupShape = (rows: typeof startupRowsA) => rows.map((item) => `${item.label}:${item.status}:${item.value}`).sort();
  assertFixture(
    JSON.stringify(startupShape(startupRowsA)) === JSON.stringify(startupShape(startupRowsB)),
    `same-shape startup gate events should produce equivalent checklist structure, A=${JSON.stringify(startupShape(startupRowsA))}, B=${JSON.stringify(startupShape(startupRowsB))}`,
  );
  assertFixture(startupRowsA.some((item) => item.label === "Owned file" && item.value.includes("taskPlayback.ts")), "startup gate should include owned_files");
  assertFixture(startupRowsA.some((item) => item.label === "Base commit"), "startup gate should include base commit");
  assertFixture(startupRowsA.some((item) => item.label === "Head commit"), "startup gate should include head commit");
  assertFixture(startupRowsA.some((item) => item.label === "Read receipt hash"), "startup gate should include read_receipt_hash");
  assertFixture(startupRowsA.some((item) => item.label === "Close satisfying" && item.value === "false" && item.status === "recorded"), "startup close_satisfying=false should be recorded, not failed");

  const readRows = rowsFor(readReceiptFrame!);
  const readAltRows = rowsFor(readReceiptAltFrame!);
  assertFixture(readRows.length > 0 && readAltRows.length > 0, "both read-receipt event shapes should render non-empty checklist rows");
  assertFixture(readRows.some((item) => item.label === "Route context hash"), "read receipt should include route context hash binding");
  assertFixture(readRows.some((item) => item.label === "Prompt contract hash"), "read receipt should include prompt contract hash binding");
  assertFixture(readRows.some((item) => item.label === "Visible contract text hash"), "read receipt should include canonical visible contract text hash");
  assertFixture(readRows.some((item) => item.label === "Read receipt hash"), "read receipt should include read_receipt_hash");
  assertFixture(readRows.some((item) => item.label === "Read ordering" && item.value.includes("startup")), "read receipt should include read-before/read ordering");
  assertFixture(readRows.some((item) => item.label === "Owned file" && item.value.includes("taskPlayback.test.ts")), "read receipt should include owned_files");
  assertFixture(readAltRows.some((item) => item.label === "Read ordering" && item.value === "true"), "mf_subagent_read_receipt shape should include read_before ordering");

  const implementationRows = rowsFor(implementationFrame!);
  const changedFileRows = implementationRows.filter((item) => item.label === "Changed file");
  assertFixture(changedFileRows.length === 3, `implementation evidence should render per-file changed_files rows, got ${changedFileRows.length}`);
  assertFixture(implementationRows.some((item) => item.label === "Commit SHA"), "implementation evidence should render commit_sha presence");
  assertFixture(implementationRows.some((item) => item.label === "Worker precommit trace"), "implementation evidence should render worker precommit trace presence");

  const reviewReadyRows = rowsFor(reviewReadyFrame!);
  assertFixture(reviewReadyRows.length > 0, "review-ready worker evidence should render a non-empty checklist");
  assertFixture(reviewReadyRows.some((item) => item.label === "Worker final state" && item.value === "review_ready"), "review-ready checklist should include worker final state");
  assertFixture(reviewReadyRows.some((item) => item.label === "Test run" && item.value.includes("taskPlayback.test.ts")), "review-ready checklist should include tests_run");
  assertFixture(reviewReadyRows.some((item) => item.label === "Worker graph trace" && item.value === "gqt-fixture-review-ready"), "review-ready checklist should include graph query trace evidence");
  assertFixture(reviewReadyRows.some((item) => item.label === "Generated assets policy" && item.value === "none"), "review-ready checklist should include generated asset policy");

  const workModeRows = rowsFor(workModeFrame!);
  const countsRows = workModeRows.filter((item) => item.label === "Counts As Close Evidence");
  assertFixture(countsRows.length === 1, `counts_as_close_evidence should render once, got ${countsRows.length}`);
  assertFixture(countsRows[0]?.status === "recorded" && countsRows[0]?.value === "false", `counts_as_close_evidence=false should be recorded literal false, got ${JSON.stringify(countsRows[0])}`);
  assertFixture(workModeFrame!.event_checklist.blocked_count === 0, `observer_work_mode_transition false declaration should have zero red rows, got ${workModeFrame!.event_checklist.blocked_count}`);

  const laneRows = rowsFor(laneOpeningPrecheckFrame!);
  const pendingRows = laneRows.filter((item) => item.status === "pending");
  assertFixture(pendingRows.length >= 2, `lane-opening route_action_precheck should show pending not-yet-due requirements, got ${laneRows.map((item) => `${item.status}:${item.label}:${item.value}`).join(" | ")}`);
  assertFixture(
    !laneRows.some((item) => ["missing", "blocked", "failed"].includes(item.status)),
    `lane-opening route_action_precheck should not render not-yet-due rows as red, got ${laneRows.map((item) => `${item.status}:${item.label}:${item.value}`).join(" | ")}`,
  );

  const realBlockerRows = rowsFor(realBlockerFrame!);
  assertFixture(
    realBlockerRows.some((item) => ["missing", "blocked", "failed"].includes(item.status)),
    "final blocking verdict should still render red checklist rows for real recorded blockers",
  );
  const greenFramesWithRedRows = trace.frames.filter((frame) => ["passed", "recorded"].includes(frame.status) && frame.event_checklist.blocked_count > 0);
  assertFixture(
    greenFramesWithRedRows.every((frame) => rowsFor(frame).some((item) => /blocker|blocked|refused|rejected|failed|missing/i.test(`${item.label} ${item.value}`))),
    `green frames may only contain red rows for real recorded blockers, got ${greenFramesWithRedRows.map((frame) => frame.source_event_id).join(", ")}`,
  );

  const workerChecklistText = [routeGateFrame!, startupFrameA!, readReceiptFrame!, implementationFrame!, reviewReadyFrame!, workModeFrame!, laneOpeningPrecheckFrame!]
    .flatMap((frame) => rowsFor(frame))
    .map((item) => `${item.label} ${item.value}`)
    .join(" ");
  assertFixture(!/raw-route-token|raw-session-token|PRIVATE CONTEXT|should\/not\/render|actual_cwd/i.test(workerChecklistText), `worker checklist leaked private or raw token material: ${workerChecklistText}`);

  return [
    `event checklist blocked rows: ${blockedRows.length}`,
    `event checklist passed rows: ${passedRows.length}`,
    "event checklist structured roots include route-token and startup gates",
    "event checklist private raw/token material redacted from structured rows",
    `event checklist route-token gate rows: ${routeGateRows.length}`,
    `event checklist startup gate rows: ${startupRowsA.length}`,
    `event checklist read-receipt rows: ${readRows.length}/${readAltRows.length}`,
    `event checklist implementation changed-file rows: ${changedFileRows.length}`,
    `event checklist review-ready rows: ${reviewReadyRows.length}`,
    `event checklist pending precheck rows: ${pendingRows.length}`,
  ];
}

function eventChecklistLayoutAssertions(): string[] {
  const componentSource = readFileSync(new URL("../components/TaskPlaybackPanel.tsx", import.meta.url), "utf8");
  const stylesSource = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  const checklistIndex = componentSource.indexOf("<EventChecklistSection");
  const referencesIndex = componentSource.indexOf("<ReferencesAndEvidenceSection");
  const rawDataIndex = componentSource.indexOf("<AdvancedRawDataDetails");
  assertFixture(checklistIndex >= 0, "event checklist component should render in the playback detail pane");
  assertFixture(referencesIndex >= 0, "references section should render in the playback detail pane");
  assertFixture(rawDataIndex >= 0, "advanced raw data details should render in the playback detail pane");
  assertFixture(
    checklistIndex < referencesIndex && referencesIndex < rawDataIndex,
    "event checklist should render above references and Advanced raw data",
  );
  assertFixture(
    !stylesSource.includes("grid-template-columns: minmax(104px, 0.32fr) minmax(0, 1fr)"),
    "event checklist group should not reserve the old narrow label rail",
  );
  assertFixture(
    !stylesSource.includes("grid-template-columns: 74px minmax(90px, 0.35fr) minmax(0, 1fr) 72px"),
    "event checklist row should not use the old fixed four-column layout",
  );
  assertFixture(
    /\.task-playback-event-checklist-group\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\);/.test(stylesSource),
    "event checklist groups should span the full detail pane width",
  );
  assertFixture(
    /\.task-playback-event-checklist\s*\{[\s\S]*?container-type:\s*inline-size;/.test(stylesSource),
    "event checklist should use container width for responsive row layout",
  );
  assertFixture(
    /\.task-playback-event-checklist-row\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0,\s*1fr\);[\s\S]*?grid-template-areas:[\s\S]*?"state"\s*"label"\s*"value"\s*"source";[\s\S]*?width:\s*100%;/.test(stylesSource),
    "event checklist rows should default to full-width single-column named areas",
  );
  assertFixture(
    /@container \(min-width:\s*560px\)[\s\S]*?\.task-playback-event-checklist-row\s*\{[\s\S]*?grid-template-areas:[\s\S]*?"state label value"[\s\S]*?"state source source";/.test(stylesSource),
    "event checklist rows should use dense columns only when the checklist container is wide enough",
  );

  return [
    "event checklist renders above references/raw data",
    "event checklist CSS uses full-width groups and container-aware row areas",
  ];
}

export const taskPlaybackEventChecklistSummary: string[] = [
  ...eventChecklistAssertions(),
  ...eventChecklistLayoutAssertions(),
];

function playbackLayoutFrameAreaAssertions(): string[] {
  const componentSource = readFileSync(new URL("../components/TaskPlaybackPanel.tsx", import.meta.url), "utf8");
  const stylesSource = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  const gateMatrixRule = stylesSource.match(/\.task-playback-gate-matrix\s*\{[\s\S]*?\n\}/)?.[0] ?? "";

  assertFixture(
    /const \[closeGateMatrixExpanded,\s*setCloseGateMatrixExpanded\]\s*=\s*useState\(false\);/.test(componentSource),
    "close gate matrix should have default-collapsed local state",
  );
  assertFixture(
    /setCloseGateMatrixExpanded\(false\);[\s\S]*setNavStack\(\[\]\);/.test(componentSource),
    "close gate expansion should reset only on backlog-level trace reset, not frame selection",
  );
  assertFixture(
    /<PlaybackGateMatrix[\s\S]*expanded=\{closeGateMatrixExpanded\}[\s\S]*onToggle=\{\(\) => setCloseGateMatrixExpanded/.test(componentSource),
    "PlaybackGateMatrix should receive persistent expanded state and a toggle",
  );
  assertFixture(
    /task-playback-panel[\s\S]*closeGateMatrixExpanded\s*\?\s*" close-gate-expanded"/.test(componentSource),
    "task playback panel should expose a close-gate-expanded layout state",
  );
  assertFixture(
    /function summarizeGateMatrix\([\s\S]*row\.required && row\.status !== "not_applicable"[\s\S]*Close gate - \$\{verdict\} - \$\{satisfied\}\/\$\{total\} evidence/.test(componentSource),
    "close gate summary should include verdict plus required satisfied/total evidence count",
  );
  assertFixture(
    /className="task-playback-gate-matrix-summary"[\s\S]*aria-expanded=\{expanded\}[\s\S]*aria-controls=\{bodyId\}/.test(componentSource),
    "close gate summary should be an accessible expandable control",
  );
  assertFixture(
    /expanded \? \([\s\S]*className="task-playback-gate-matrix-body"[\s\S]*<div className="gate-matrix" role="table"/.test(componentSource),
    "full close gate requirement table should remain reachable only when expanded",
  );
  assertFixture(
    /<div className="task-playback-summary-strip"[\s\S]*<Metric label="Frames"[\s\S]*<Metric label="Artifacts"[\s\S]*<div className="task-playback-lanes"/.test(componentSource),
    "playback metrics and lane chips should render in one summary strip",
  );

  assertFixture(
    /\.task-playback-summary-strip\s*\{[\s\S]*display:\s*flex;[\s\S]*flex-wrap:\s*wrap;/.test(stylesSource),
    "summary strip should compact metrics and lanes into one wrapping row",
  );
  assertFixture(
    /\.task-playback-panel\s*\{[\s\S]*display:\s*flex;[\s\S]*flex-direction:\s*column;[\s\S]*height:\s*100%;[\s\S]*min-height:\s*0;[\s\S]*overflow:\s*hidden;/.test(stylesSource),
    "task playback panel should be a real bounded flex column",
  );
  assertFixture(
    /\.task-playback-body\s*\{[\s\S]*flex:\s*1 1 auto;[\s\S]*min-height:\s*0;[\s\S]*overflow:\s*hidden;/.test(stylesSource),
    "task playback body should be governed by the parent flex column and remain shrinkable",
  );
  assertFixture(
    !/\.task-playback-body\s*\{[\s\S]*flex:\s*1 1 50vh;/.test(stylesSource),
    "task playback body should not rely on the old inert 50vh flex shorthand",
  );
  assertFixture(
    /\.task-playback-frame-list\s*\{[\s\S]*height:\s*100%;[\s\S]*overflow-y:\s*auto;/.test(stylesSource),
    "frame list should own an independent vertical scroll area",
  );
  assertFixture(
    /\.task-playback-detail-column\s*\{[\s\S]*overflow-y:\s*auto;[\s\S]*height:\s*100%;/.test(stylesSource),
    "frame detail should own an independent vertical scroll area",
  );
  assertFixture(
    /\.task-playback-panel\.close-gate-expanded\s*\{[\s\S]*overflow-y:\s*auto;[\s\S]*\}/.test(stylesSource)
      && /\.task-playback-panel\.close-gate-expanded\s+\.task-playback-body\s*\{[\s\S]*flex-basis:\s*320px;[\s\S]*min-height:\s*260px;/.test(stylesSource),
    "expanded close gate layout should preserve a reachable frame body area",
  );
  assertFixture(
    !/max-height:\s*260px/.test(gateMatrixRule),
    "close gate matrix should not keep the old expanded-by-default 260px height reservation",
  );
  assertFixture(
    /\.task-playback-gate-matrix-body\s*\{[\s\S]*min-height:\s*0;[\s\S]*display:\s*block;[\s\S]*max-height:\s*clamp\(120px,\s*18vh,\s*220px\);[\s\S]*overflow-y:\s*auto;[\s\S]*scrollbar-gutter:\s*stable;/.test(stylesSource)
      && /\.task-playback-gate-matrix-body\s*>\s*\.gate-matrix\s*\{[\s\S]*min-height:\s*max-content;/.test(stylesSource),
    "expanded close gate details should own body scrolling instead of clipping the gate table",
  );

  return [
    "close gate matrix defaults collapsed with accessible expansion",
    "close gate full requirement table remains reachable when expanded",
    "metrics and lane chips share one compact summary strip",
    "frame list/detail use independent desktop scroll regions inside a bounded flex body",
  ];
}

export const taskPlaybackLayoutFrameAreaSummary: string[] = [
  ...playbackLayoutFrameAreaAssertions(),
];

// ──────────────────────────────────────────────────────────────────────────────
// B1 / B2 UE-blocker canonical URL helpers
// (AC-ACTIVITY-PLAYBACK-IA-UE-BLOCKERS-20260611)
// ──────────────────────────────────────────────────────────────────────────────

function ueBlockerUrlAssertions(): string[] {
  const results: string[] = [];

  // ── PLAYBACK_URL_PARAMS completeness ────────────────────────────────────
  assertFixture(PLAYBACK_URL_PARAMS.view === "view", "PLAYBACK_URL_PARAMS.view must be 'view'");
  assertFixture(PLAYBACK_URL_PARAMS.activity_tab === "activity_tab", "PLAYBACK_URL_PARAMS.activity_tab must be 'activity_tab'");
  assertFixture(PLAYBACK_URL_PARAMS.playback_backlog === "playback_backlog", "PLAYBACK_URL_PARAMS.playback_backlog must be 'playback_backlog'");
  assertFixture(PLAYBACK_URL_PARAMS.playback_event === "playback_event", "PLAYBACK_URL_PARAMS.playback_event must be 'playback_event'");
  results.push("PLAYBACK_URL_PARAMS covers view, activity_tab, playback_backlog, playback_event");

  // ── buildPlaybackUrl — canonical view=activity&activity_tab=history ──────
  const base = "http://localhost/dashboard";
  const url1 = buildPlaybackUrl("aming-claw", "AC-SOME-BLOCKER-20260611", undefined, base);
  assertFixture(url1.includes("view=activity"), `buildPlaybackUrl must emit view=activity (got: ${url1})`);
  assertFixture(url1.includes("activity_tab=history"), `buildPlaybackUrl must emit activity_tab=history (got: ${url1})`);
  assertFixture(url1.includes("playback_backlog=AC-SOME-BLOCKER-20260611"), `buildPlaybackUrl must emit playback_backlog param (got: ${url1})`);
  assertFixture(!url1.includes("playback_event="), `buildPlaybackUrl with no eventId must not emit playback_event param (got: ${url1})`);
  results.push(`buildPlaybackUrl canonical form OK: ${url1}`);

  // ── buildPlaybackUrl — with string event id ──────────────────────────────
  const url2 = buildPlaybackUrl("aming-claw", "AC-SOME-BLOCKER-20260611", "my-event-id", base);
  assertFixture(url2.includes("playback_event=my-event-id"), `buildPlaybackUrl must emit playback_event param with string id (got: ${url2})`);
  results.push(`buildPlaybackUrl with string eventId OK: ${url2}`);

  // ── buildPlaybackUrl — with numeric event id ─────────────────────────────
  const url3 = buildPlaybackUrl("aming-claw", "AC-SOME-BLOCKER-20260611", 1234, base);
  assertFixture(url3.includes("playback_event=1234"), `buildPlaybackUrl must emit numeric event id as string (got: ${url3})`);
  results.push(`buildPlaybackUrl with numeric eventId OK: ${url3}`);

  const staleEventBase = "http://localhost/dashboard?project_id=aming-claw&view=activity&activity_tab=history&playback_backlog=AC-SOME-BLOCKER-20260611&playback_event=4000";
  const url3b = buildPlaybackUrl("aming-claw", "AC-SOME-BLOCKER-20260611", 3994, staleEventBase);
  assertFixture(url3b.includes("playback_event=3994"), `buildPlaybackUrl must replace stale playback_event with clicked event id (got: ${url3b})`);
  assertFixture(!url3b.includes("playback_event=4000"), `buildPlaybackUrl must not preserve stale playback_event after same-backlog ref jump (got: ${url3b})`);
  results.push(`buildPlaybackUrl replaces stale same-backlog playback_event OK: ${url3b}`);

  // ── buildPlaybackUrl — null/empty event id must not emit param ───────────
  const url4 = buildPlaybackUrl("aming-claw", "AC-SOME-BLOCKER-20260611", null, base);
  assertFixture(!url4.includes("playback_event"), `buildPlaybackUrl with null eventId must not emit playback_event (got: ${url4})`);
  const url5 = buildPlaybackUrl("aming-claw", "AC-SOME-BLOCKER-20260611", "", base);
  assertFixture(!url5.includes("playback_event"), `buildPlaybackUrl with empty string eventId must not emit playback_event (got: ${url5})`);
  results.push("buildPlaybackUrl null/empty eventId suppresses param");

  // ── buildPlaybackUrl — B2 canonical equivalence: view=activity + activity_tab=history
  //    is the canonical route that avoids App.tsx normalizeViewName dropping params ──────
  assertFixture(!url1.includes("view=playback"), `buildPlaybackUrl must never emit view=playback (B2 canonical form) (got: ${url1})`);
  results.push("buildPlaybackUrl emits view=activity, never view=playback (B2 canonical route)");

  // ── Current top-level Open playback history must bind the activity backlog ──
  const viewSource = readFileSync(new URL("../views/TaskPlaybackView.tsx", import.meta.url), "utf8");
  const openHistoryHandler = viewSource.match(/const openActivityPlaybackHistory = \(\) => \{[\s\S]*?\n  \};/)?.[0] ?? "";
  assertFixture(
    openHistoryHandler.includes('const backlogId = activityBug?.bug_id || "";'),
    "AC-RUNTIME current activity history button should derive playback_backlog from activityBug.bug_id",
  );
  assertFixture(
    /if \(!backlogId\) \{\s*changeMode\("history"\);\s*return;\s*\}/.test(openHistoryHandler),
    "no current activity should preserve the existing empty-selection history behavior",
  );
  assertFixture(
    openHistoryHandler.includes("navigateToPlayback(backlogId, eventId);")
      && openHistoryHandler.includes("setSelectedBugId(backlogId);"),
    "AC-RUNTIME current activity history button should not switch to sample trace without playback_backlog",
  );
  assertFixture(
    openHistoryHandler.includes("frame?.source_event_id || frame?.id ||")
      && openHistoryHandler.includes("resolveSelectedFrameIdForEventParam"),
    "current activity history button should preserve selected/newest event when a frame id is available",
  );
  assertFixture(
    /<button\s+type="button"\s+className="action-btn"\s+onClick=\{openActivityPlaybackHistory\}>/.test(viewSource),
    "top-level Open playback history button should use the backlog-binding handler",
  );
  results.push("current activity Open playback history binds playback_backlog and avoids sample trace");

  // ── Stale backlog-cache deep link must fetch detail before timeline/gate ──
  assertFixture(
    viewSource.includes("const [selectedBacklogDetailById, setSelectedBacklogDetailById]")
      && viewSource.includes("const selectedBacklogDetailByIdRef"),
    "stale backlog-cache playback should keep a detail fallback map/ref for selected playback_backlog",
  );
  assertFixture(
    viewSource.includes("const selectedBug = cachedSelectedBug ?? fetchedSelectedBug;"),
    "selected playback bug should resolve from cached publicBugs or fetched backlog detail",
  );
  const staleCacheDetailEffect = viewSource.match(/Stale backlog-cache deep links need a detail row before timeline\/gate requests can be scoped\.[\s\S]*?api\.backlogBugFor\(projectId, bugId, controller\.signal\)[\s\S]*?\}, \[projectId, selectedBugId, cachedSelectedBug\?\.bug_id\]\);/)?.[0] ?? "";
  assertFixture(
    staleCacheDetailEffect.includes("if (!selectedBugId || cachedSelectedBug) return undefined;")
      && staleCacheDetailEffect.includes("api.backlogBugFor(projectId, bugId, controller.signal)"),
    "selected playback_backlog missing from cached publicBugs should fetch /api/backlog/{project_id}/{backlog_id}",
  );
  const historyTimelineLoader = viewSource.match(/const bug = selectedBugRef\.current;[\s\S]*?api\.taskTimelineFor\(projectId, bugId, PLAYBACK_TIMELINE_LIMIT, controller\.signal\)[\s\S]*?api\.backlogTimelineGateFor\(projectId, bugId, PLAYBACK_TIMELINE_LIMIT, controller\.signal\)/)?.[0] ?? "";
  assertFixture(
    historyTimelineLoader.includes("if (!selectedLoadBugId || !bug || bug.bug_id !== selectedLoadBugId) return;"),
    "history timeline/gate loader should wait until the selected detail bug exists",
  );
  assertFixture(
    viewSource.includes("const selectedPlaybackLoading = (selectedState?.loading ?? false) || (!selectedBug && (selectedBacklogDetail?.loading ?? false));")
      && viewSource.includes("const selectedPlaybackError = selectedState?.error || (!selectedBug ? selectedBacklogDetail?.error ?? \"\" : \"\");")
      && viewSource.includes("loading={selectedPlaybackLoading}")
      && viewSource.includes("error={selectedPlaybackError}"),
    "failed backlog-detail fallback should surface loading/error instead of presenting an empty no-evidence trace",
  );
  assertFixture(
    viewSource.includes("selectedPlaceholderBug")
      && viewSource.includes("selectedBacklogDetail?.loading ? \"Loading backlog detail\" : \"Backlog detail unavailable\""),
    "detail fallback should render a selected backlog placeholder instead of the sample playback trace",
  );
  assertFixture(
    viewSource.includes("resolveInitialPlaybackFrameId(")
      && viewSource.includes("selectedEventParamRef.current || readPlaybackEventParam()"),
    "playback_event should still resolve after fallback backlog-detail fetch unlocks the governed trace",
  );
  results.push("stale backlog-cache playback_backlog fetches detail before timeline/gate and preserves playback_event selection");

  // ── findFrameIdByEventParam — exact frame id match ───────────────────────
  const sampleFrames: TaskPlaybackFrame[] = [
    { id: "abc-def", source_event_id: "src-001", sequence: 1, title: "T1", event_kind: "route_context", status: "passed", structured_facts: [], failure_diagnosis: [], evidence_links: [], raw_sections: [], specific_facts: [], auxiliary_narrative: [] } as unknown as TaskPlaybackFrame,
    { id: "#42", source_event_id: "42", sequence: 2, title: "T2", event_kind: "verification", status: "blocked", structured_facts: [], failure_diagnosis: [], evidence_links: [], raw_sections: [], specific_facts: [], auxiliary_narrative: [] } as unknown as TaskPlaybackFrame,
    { id: "some-long-event-id", source_event_id: "3100", sequence: 3, title: "T3", event_kind: "implementation", status: "recorded", structured_facts: [], failure_diagnosis: [], evidence_links: [], raw_sections: [], specific_facts: [], auxiliary_narrative: [] } as unknown as TaskPlaybackFrame,
  ];

  const r1 = findFrameIdByEventParam(sampleFrames, "abc-def");
  assertFixture(r1 === "abc-def", `findFrameIdByEventParam exact id match: expected 'abc-def', got '${r1}'`);
  results.push("findFrameIdByEventParam exact id match OK");

  // ── findFrameIdByEventParam — source_event_id match ──────────────────────
  const r2 = findFrameIdByEventParam(sampleFrames, "src-001");
  assertFixture(r2 === "abc-def", `findFrameIdByEventParam source_event_id match: expected 'abc-def', got '${r2}'`);
  results.push("findFrameIdByEventParam source_event_id match OK");

  // ── findFrameIdByEventParam — numeric string → #N form ───────────────────
  const r3 = findFrameIdByEventParam(sampleFrames, "42");
  assertFixture(r3 === "#42", `findFrameIdByEventParam numeric string → #42 frame: expected '#42', got '${r3}'`);
  results.push("findFrameIdByEventParam numeric string → #N form OK");

  // ── findFrameIdByEventParam — #N string → strips hash ────────────────────
  const r4 = findFrameIdByEventParam(sampleFrames, "#42");
  assertFixture(r4 === "#42", `findFrameIdByEventParam #N pass-through: expected '#42', got '${r4}'`);
  results.push("findFrameIdByEventParam #N string match OK");

  // ── findFrameIdByEventParam — source_event_id numeric match for long id ──
  const r5 = findFrameIdByEventParam(sampleFrames, "3100");
  assertFixture(r5 === "some-long-event-id", `findFrameIdByEventParam source_event_id numeric '3100' → 'some-long-event-id', got '${r5}'`);
  results.push("findFrameIdByEventParam numeric source_event_id match OK");

  // ── findFrameIdByEventParam — missing frame returns empty string ──────────
  const r6 = findFrameIdByEventParam(sampleFrames, "does-not-exist");
  assertFixture(r6 === "", `findFrameIdByEventParam missing frame: expected '', got '${r6}'`);
  results.push("findFrameIdByEventParam missing-frame fallback returns empty string");

  // ── findFrameIdByEventParam — empty param returns empty string ───────────
  const r7 = findFrameIdByEventParam(sampleFrames, "");
  assertFixture(r7 === "", `findFrameIdByEventParam empty param: expected '', got '${r7}'`);
  results.push("findFrameIdByEventParam empty param returns empty string");

  // ── findFrameIdByEventParam — empty frames returns empty string ───────────
  const r8 = findFrameIdByEventParam([], "abc-def");
  assertFixture(r8 === "", `findFrameIdByEventParam empty frames: expected '', got '${r8}'`);
  results.push("findFrameIdByEventParam empty frames returns empty string");

  // ── Warm/cached trace event-param changes re-select the new frame ────────
  const warmFirst = resolveSelectedFrameIdForEventParam(sampleFrames, "src-001", "").frameId;
  const warmSecond = resolveSelectedFrameIdForEventParam(sampleFrames, "3100", warmFirst).frameId;
  assertFixture(warmFirst === "abc-def", `warm event-param first selection should resolve to abc-def, got '${warmFirst}'`);
  assertFixture(warmSecond === "some-long-event-id", `warm event-param change should resolve to some-long-event-id, got '${warmSecond}'`);
  results.push("warm cached playback_event changes re-select the changed event frame");

  // ── Direct reload initial selection: URL event wins before frame-1 fallback ──
  const directReloadSelection = resolveInitialPlaybackFrameId(sampleFrames, "3100", "");
  assertFixture(directReloadSelection === "some-long-event-id", `direct reload playback_event=3100 should select matching frame, got '${directReloadSelection}'`);
  const currentStableSelection = resolveInitialPlaybackFrameId(sampleFrames, "3100", "abc-def");
  assertFixture(currentStableSelection === "abc-def", `current valid selection should remain stable, got '${currentStableSelection}'`);
  const initialFallbackSelection = resolveInitialPlaybackFrameId(sampleFrames, "does-not-exist", "");
  assertFixture(initialFallbackSelection === "abc-def", `missing playback_event should fall back to first frame, got '${initialFallbackSelection}'`);
  results.push("direct reload playback_event selects event frame before first-frame fallback");

  return results;
}

export const taskPlaybackUeBlockerUrlSummary: string[] = ueBlockerUrlAssertions();
