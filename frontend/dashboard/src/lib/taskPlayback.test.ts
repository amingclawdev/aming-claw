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
      route_context: "[fixture private route context body]",
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
      acknowledged_forbidden_actions: ["merge", "push", "delete_worktree"],
      route_context_hash: "sha256:fixture-narrative-route-context",
      prompt_contract_id: "rprompt-fixture-narrative",
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
  const visible = JSON.stringify({
    close_gate_summary: trace.close_gate_summary,
    frames: trace.frames.map((frame) => ({
      title: frame.title,
      detail: frame.detail,
      narrative: frame.narrative,
      chips: frame.semantic_chips,
      inspector: frame.detail_inspector,
    })),
  });
  assertFixture(
    trace.close_gate_summary.reason_sentence === "Blocked because implementation, verification, and close-ready evidence have not been recorded; the close gate cannot pass until those events exist.",
    "blocked close gate should show a human-readable reason sentence with missing event kinds",
  );
  assertFixture(
    trace.close_gate_summary.next_expected_action.includes("add implementation, verification, and close-ready evidence"),
    "blocked close gate should show the next expected evidence/action",
  );
  assertFixture(
    visible.includes("Bounded worker received task context containing target files, acceptance criteria, allowed/blocked actions, route identity hashes, and required evidence; private prompt text is hidden."),
    "route/context worker story should be visible",
  );
  assertFixture(visible.includes("Route service requested or delivered bounded task context."), "route context actor story should be visible");
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
    ...trace.frames.map((frame) => `${frame.title}: ${frame.narrative.context}`),
  ];
}

export const taskPlaybackHistoricalSemanticFixtureSummary = [
  ...taskPlaybackHistoricalSemanticFixtureAssertions(),
  ...taskPlaybackNarrativeFocusFixtureAssertions(),
];

function assertFixture(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}
