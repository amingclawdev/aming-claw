import type { BacklogBug, TaskTimelineEvent } from "../types";
import { normalizeTaskPlaybackTrace } from "./taskPlayback";

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

export const taskPlaybackHistoricalSemanticFixtureSummary = [
  ...taskPlaybackHistoricalSemanticFixtureAssertions(),
  ...taskPlaybackNarrativeFocusFixtureAssertions(),
];

function assertFixture(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}
