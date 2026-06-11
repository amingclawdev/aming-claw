import type { BacklogBug, TaskTimelineEvent } from "../types";
import {
  isBacklogRowPrivate,
  normalizeTaskPlaybackTrace,
  displayPlaybackFrames,
  latestPlaybackFrameId,
  pushPlaybackNavStack,
  popPlaybackNavStack,
  type PlaybackNavEntry,
  type TaskPlaybackFrame,
} from "./taskPlayback";
import { projectTaskTimelineEvent } from "./taskTimelineSemantics";

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
];

function assertFixture(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

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
