import type { BacklogBug, BacklogTimelineGateResponse, TaskTimelineEvent, TaskTimelineResponse } from "../types";
import {
  isPrivateTimelineText,
  projectTaskTimelineEvent,
  timelineStatusFromEvent,
  type TaskTimelineEvidenceInspector,
  type TaskTimelineSemanticChip,
  type TaskTimelineSemanticNarrative,
  type TaskTimelineSemanticProjection,
} from "./taskTimelineSemantics";

export const TASK_PLAYBACK_TRACE_SCHEMA = "task_playback_trace.v1";

export type TaskPlaybackSource = "governed" | "governed_partial" | "fallback_sample";
export type TaskPlaybackFrameStatus = "passed" | "blocked" | "failed" | "running" | "waiting" | "missing" | "recorded" | "unknown";
export type TaskPlaybackLaneFamily = "observer" | "worker" | "verification" | "gate" | "content_sys";

export interface TaskPlaybackEvidenceRef {
  kind:
    | "timeline_event"
    | "route_context"
    | "read_receipt"
    | "prompt_contract"
    | "graph_trace"
    | "precheck"
    | "source_event"
    | "artifact"
    | "commit"
    | "test"
    | "file"
    | "node"
    | "gate"
    | "content_sys";
  label: string;
  value: string;
}

export interface TaskPlaybackArtifactRef {
  kind: "file" | "test" | "screenshot" | "graph" | "commit" | "content_sys" | "artifact";
  value: string;
}

export interface TaskPlaybackStructuredFact {
  kind: string;
  label: string;
  value: string;
  source: "event" | "payload" | "verification" | "artifact_refs" | "semantic";
}

export interface TaskPlaybackFrame {
  id: string;
  sequence: number;
  at: string;
  lane_id: string;
  source_event_id: string;
  event_type: string;
  event_kind: string;
  phase: string;
  title: string;
  detail: string;
  summary: string;
  status: TaskPlaybackFrameStatus;
  actor: string;
  narrative: TaskTimelineSemanticNarrative;
  semantic_entry_id: string;
  semantic_chips: TaskTimelineSemanticChip[];
  specific_facts: TaskPlaybackStructuredFact[];
  failure_diagnosis: TaskPlaybackStructuredFact[];
  evidence_links: TaskPlaybackEvidenceRef[];
  detail_inspector: TaskTimelineEvidenceInspector;
  evidence_refs: TaskPlaybackEvidenceRef[];
  artifact_refs: TaskPlaybackArtifactRef[];
  has_structured_detail: boolean;
}

export interface TaskPlaybackLane {
  id: string;
  label: string;
  family: TaskPlaybackLaneFamily;
  status: TaskPlaybackFrameStatus;
  frame_count: number;
  latest_at: string;
}

export interface TaskPlaybackCloseGateSummary {
  applicable: boolean;
  can_close: boolean;
  status: TaskPlaybackFrameStatus;
  label: string;
  missing_event_kinds: string[];
  missing_requirement_ids: string[];
  missing_requirement_count: number;
  reason_sentence: string;
  next_expected_action: string;
  next_expected_evidence: string[];
  blocked: boolean;
  event_count: number;
}

export interface TaskPlaybackPrivacyBoundary {
  raw_prompt_text: "not_displayed";
  host_private_paths: "redacted";
  private_provider_context: "not_displayed";
  evidence_scope: "aming_claw_content_sys_public";
}

export interface TaskPlaybackStatusSummary {
  total_frames: number;
  by_status: Record<TaskPlaybackFrameStatus, number>;
  blocked_gate: boolean;
  has_timeline: boolean;
  has_governed_data: boolean;
}

export interface TaskPlaybackTrace {
  schema_version: typeof TASK_PLAYBACK_TRACE_SCHEMA;
  project_id: string;
  backlog_id: string;
  backlog_title: string;
  source: TaskPlaybackSource;
  generated_at: string;
  frames: TaskPlaybackFrame[];
  lanes: TaskPlaybackLane[];
  statuses: TaskPlaybackStatusSummary;
  evidence_refs: TaskPlaybackEvidenceRef[];
  artifact_refs: TaskPlaybackArtifactRef[];
  privacy_boundary: TaskPlaybackPrivacyBoundary;
  close_gate_summary: TaskPlaybackCloseGateSummary;
}

export interface NormalizeTaskPlaybackInput {
  projectId: string;
  backlog: BacklogBug;
  taskTimeline?: TaskTimelineResponse | null;
  gateResponse?: BacklogTimelineGateResponse | null;
  source?: TaskPlaybackSource;
  generatedAt?: string;
}

const FRAME_STATUS_ORDER: TaskPlaybackFrameStatus[] = ["blocked", "failed", "missing", "running", "waiting", "passed", "recorded", "unknown"];
export const PRIVATE_EVIDENCE_KEY =
  /(^|[._\s-])(raw_prompt|raw_private_prompt_text|private_prompt|prompt_text|prompt_body|prompt_payload|hidden_prompt|hidden_context|system_prompt|developer_prompt|secret|credential|credentials|password|api_key|access_token|refresh_token|auth_token|one_time_auth|filesystem|cwd|worktree_path|host_path|host_paths|host_home|raw_private_context|raw_private_route_body|private_route_context_body|private_body|observer_only_context|unmanifested_prompt_text)([._\s-]|$)|(^|[._\s-])token([._\s-]|$)(?!hash)/i;
const ABSOLUTE_HOST_PATH = /(^|\s)(\/Users\/[^\s,;:]+|\/home\/[^\s,;:]+|\/var\/folders\/[^\s,;:]+|[A-Za-z]:\\[^\s,;:]+)/g;
const TOKEN_VALUE = /\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{8,}\b/g;

export function isPrivatePlaybackText(value?: string | null): boolean {
  return isPrivateTimelineText(value);
}

/**
 * Returns true when a backlog row should be hidden from public playback views.
 *
 * Visibility is driven ONLY by the explicit privacy_level / public_safe flags
 * that the backend compact-bug serialiser emits.  Name/title/chain-stage
 * substring heuristics must not be used here: a public row whose title
 * mentions an external provider or tool name must not be silently hidden.
 */
export function isBacklogRowPrivate(bug: BacklogBug): boolean {
  return bug.privacy_level === "private" || bug.public_safe === false;
}

export function sanitizeTaskPlaybackEvidenceText(value: string, path = ""): string {
  return sanitizeEvidenceString(value, path);
}

export function normalizeTaskPlaybackTrace(input: NormalizeTaskPlaybackInput): TaskPlaybackTrace {
  const timelineEvents = input.taskTimeline?.events ?? [];
  const gateEvents = input.gateResponse?.events ?? [];
  const events = mergeTimelineEvents(timelineEvents, gateEvents);
  const frames = events.map((event, index) => frameFromEvent(event, index));
  const lanes = lanesFromFrames(frames);
  const closeGateSummary = closeGateSummaryFrom(input.gateResponse);
  const source = input.source ?? (input.taskTimeline || input.gateResponse ? "governed" : "fallback_sample");
  const evidenceRefs = stableEvidence(frames.flatMap((frame) => frame.evidence_refs));
  const artifactRefs = stableArtifacts(frames.flatMap((frame) => frame.artifact_refs));
  const statuses = summarizeFrames(frames, closeGateSummary, source);

  return {
    schema_version: TASK_PLAYBACK_TRACE_SCHEMA,
    project_id: input.projectId,
    backlog_id: input.backlog.bug_id,
    backlog_title: safeText(input.backlog.title || input.backlog.bug_id),
    source,
    generated_at: input.generatedAt ?? new Date().toISOString(),
    frames,
    lanes,
    statuses,
    evidence_refs: evidenceRefs,
    artifact_refs: artifactRefs,
    privacy_boundary: {
      raw_prompt_text: "not_displayed",
      host_private_paths: "redacted",
      private_provider_context: "not_displayed",
      evidence_scope: "aming_claw_content_sys_public",
    },
    close_gate_summary: closeGateSummary,
  };
}

export function emptyTaskPlaybackTrace(projectId: string, backlog: BacklogBug): TaskPlaybackTrace {
  return normalizeTaskPlaybackTrace({
    projectId,
    backlog,
    taskTimeline: { project_id: projectId, backlog_id: backlog.bug_id, events: [], count: 0 },
    gateResponse: null,
    source: "governed",
  });
}

export function fallbackTaskPlaybackSampleTrace(projectId: string): TaskPlaybackTrace {
  const backlog: BacklogBug = {
    bug_id: "CONTENT-SYS-DEMO-TASK-PLAYBACK-SAMPLE",
    title: "content-sys governed task playback sample",
    status: "DEMO",
    priority: "P2",
  };
  const events: TaskTimelineEvent[] = [
    {
      event_id: "sample-backlog-selected",
      event_type: "backlog.selected",
      event_kind: "planning",
      phase: "backlog",
      actor: "Aming Claw dashboard",
      status: "recorded",
      payload: {
        lane: "observer",
        artifact_refs: ["content-sys backlog row"],
        privacy_boundary: "public",
      },
      created_at: "2026-06-01T12:00:00Z",
    },
    {
      event_id: "sample-worker-evidence",
      event_type: "task.timeline.appended",
      event_kind: "implementation",
      phase: "implementation",
      actor: "bounded worker",
      status: "passed",
      payload: {
        lane: "worker",
        changed_files: ["frontend/dashboard/src/views/TaskPlaybackView.tsx"],
        graph_trace_ids: ["gqt-sample-content-sys"],
      },
      verification: { passed: true, tests_run: ["dashboard build"] },
      created_at: "2026-06-01T12:01:00Z",
    },
    {
      event_id: "sample-close-gate",
      event_type: "task.close_gate",
      event_kind: "verification",
      phase: "close gate",
      actor: "Aming Claw governance",
      status: "blocked",
      payload: {
        lane: "gate",
        missing_event_kinds: ["close_ready"],
        artifact_refs: ["content-sys playback panel"],
      },
      created_at: "2026-06-01T12:02:00Z",
    },
  ];
  return normalizeTaskPlaybackTrace({
    projectId,
    backlog,
    taskTimeline: { project_id: projectId, backlog_id: backlog.bug_id, events, count: events.length },
    gateResponse: {
      project_id: projectId,
      bug_id: backlog.bug_id,
      applicable: true,
      can_close: false,
      timeline_gate: {
        passed: false,
        status: "blocked",
        required_event_kinds: ["implementation", "verification", "close_ready"],
        present_event_kinds: ["implementation", "verification"],
        missing_event_kinds: ["close_ready"],
        event_count: events.length,
      },
      event_count: events.length,
      events,
    },
    source: "fallback_sample",
  });
}

function mergeTimelineEvents(primary: TaskTimelineEvent[], secondary: TaskTimelineEvent[]): TaskTimelineEvent[] {
  const seen = new Set<string>();
  return [...primary, ...secondary]
    .filter((event, index) => {
      const key = eventIdentity(event, index);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort(compareTimelineEvents);
}

function frameFromEvent(event: TaskTimelineEvent, index: number): TaskPlaybackFrame {
  const publicEvent = hydrateTimelineEventJson(event);
  const semantic = projectTaskTimelineEvent(publicEvent, index);
  const status = timelineStatusFromEvent(publicEvent);
  const artifactRefs = artifactsFromEvent(publicEvent, semantic);
  const specificFacts = specificFactsFromEvent(publicEvent, semantic);
  const failureDiagnosis = failureDiagnosisFromEvent(publicEvent, status);
  const evidenceRefs = evidenceFromEvent(publicEvent, semantic);
  const evidenceLinks = evidenceLinksFromEvent(publicEvent, semantic, evidenceRefs, artifactRefs);
  return {
    id: eventIdentity(publicEvent, index),
    sequence: index + 1,
    at: publicEvent.created_at || "",
    lane_id: semantic.lane_id,
    source_event_id: eventDisplayId(publicEvent),
    event_type: semantic.event_type_label,
    event_kind: semantic.event_kind_label,
    phase: semantic.phase_label,
    title: semantic.title,
    detail: semantic.detail,
    summary: eventSummaryFromEvent(publicEvent, semantic, status, specificFacts, failureDiagnosis),
    status,
    actor: semantic.actor_label,
    narrative: semantic.narrative,
    semantic_entry_id: semantic.catalog_entry_id,
    semantic_chips: semantic.chips,
    specific_facts: specificFacts,
    failure_diagnosis: failureDiagnosis,
    evidence_links: evidenceLinks,
    detail_inspector: playbackInspectorFromEvent(publicEvent, semantic),
    evidence_refs: evidenceRefs,
    artifact_refs: artifactRefs,
    has_structured_detail: specificFacts.length > 0 || failureDiagnosis.length > 0 || evidenceLinks.length > 1,
  };
}

function hydrateTimelineEventJson(event: TaskTimelineEvent): TaskTimelineEvent {
  const row = event as unknown as Record<string, unknown>;
  return {
    ...event,
    payload: hydratedRecord(event.payload, row.payload_json),
    verification: hydratedRecord(event.verification, row.verification_json),
    artifact_refs: hydratedRecord(event.artifact_refs, row.artifact_refs_json),
  };
}

function playbackInspectorFromEvent(
  event: TaskTimelineEvent,
  semantic: TaskTimelineSemanticProjection,
): TaskTimelineEvidenceInspector {
  const payload = sanitizePlaybackInspectorValue(event.payload ?? {}, "payload");
  const verification = sanitizePlaybackInspectorValue(event.verification ?? {}, "verification");
  const artifactRefs = sanitizePlaybackInspectorValue(event.artifact_refs ?? {}, "artifact_refs");
  const rows = stableInspectorRows(semantic.inspector.rows
    .map((row) => ({
      kind: sanitizeEvidenceString(row.kind, "inspector.kind"),
      label: sanitizeEvidenceString(row.label, "inspector.label"),
      value: sanitizeEvidenceString(row.value, "inspector.value"),
    }))
    .filter((row) => row.kind && row.label && row.value && row.value !== "[private detail redacted]"));
  return {
    rows,
    raw_sections: [
      { label: "payload", value: payload.value, redacted: payload.redaction_count > 0 },
      { label: "verification", value: verification.value, redacted: verification.redaction_count > 0 },
      { label: "artifact_refs", value: artifactRefs.value, redacted: artifactRefs.redaction_count > 0 },
    ],
    redaction_count: payload.redaction_count + verification.redaction_count + artifactRefs.redaction_count,
  };
}

function sanitizePlaybackInspectorValue(value: unknown, path: string): { value: unknown; redaction_count: number } {
  if (value == null || value === "") return { value, redaction_count: 0 };
  if (isSensitiveEvidencePath(path)) return { value: "[private detail redacted]", redaction_count: 1 };
  if (Array.isArray(value)) {
    let redactionCount = 0;
    const items = value.map((item, index) => {
      const result = sanitizePlaybackInspectorValue(item, `${path}.${index}`);
      redactionCount += result.redaction_count;
      return result.value;
    });
    return { value: items, redaction_count: redactionCount };
  }
  if (typeof value === "object") {
    let redactionCount = 0;
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      const nextPath = `${path}.${key}`;
      const result = sanitizePlaybackInspectorValue(item, nextPath);
      out[key] = result.value;
      redactionCount += result.redaction_count;
    }
    return { value: out, redaction_count: redactionCount };
  }
  const text = String(value);
  const safe = sanitizeEvidenceString(text, path);
  return {
    value: safe,
    redaction_count: safe !== text ? 1 : 0,
  };
}

function hydratedRecord(value: unknown, rawJson: unknown): Record<string, unknown> {
  const record = asRecord(value);
  if (Object.keys(record).length > 0) return record;
  return parseJsonRecord(rawJson);
}

function parseJsonRecord(value: unknown): Record<string, unknown> {
  if (typeof value !== "string" || !value.trim()) return {};
  try {
    const parsed = JSON.parse(value) as unknown;
    return asRecord(parsed);
  } catch {
    return {};
  }
}

function specificFactsFromEvent(event: TaskTimelineEvent, semantic: TaskTimelineSemanticProjection): TaskPlaybackStructuredFact[] {
  const facts: TaskPlaybackStructuredFact[] = [];
  pushFact(facts, "actor", "actor", semantic.actor_label || stringFrom(event.actor), "semantic");
  const receiver = firstPublicValueAtPaths(event, [
    "payload.receiver",
    "payload.target_lane",
    "payload.worker_lane",
    "payload.agent_lane",
    "payload.lane",
    "payload.worker_id",
    "payload.worker_role",
    "payload.agent_id",
    "verification.receiver",
    "verification.lane",
  ]);
  const laneReceiver = receiver ? `${semantic.lane_label} / ${receiver.value}` : semantic.lane_label;
  pushFact(facts, "lane_receiver", "lane/receiver", laneReceiver, receiver?.source ?? "semantic");
  pushFirstFact(facts, event, "backlog_id", "backlog id", [
    "backlog_id",
    "payload.backlog_id",
    "payload.bug_id",
    "payload.root_backlog_ids",
    "verification.backlog_id",
    "artifact_refs.backlog_id",
  ]);
  // Route identity must show the CANONICAL route_id (e.g. "route-repair-…"),
  // never the preview/static placeholder ("event.route_prompt_context.preview")
  // that some source/service events carry as their route_id. Prefer the
  // canonical_route_identity / route_context bundle the observer actually read,
  // and drop any preview placeholder so it is never displayed as canonical.
  pushCanonicalRouteIdFact(facts, event);
  pushFirstFact(facts, event, "route_context_hash", "route context hash", [
    "payload.route_context_hash",
    "payload.route_identity.route_context_hash",
    "verification.route_context_hash",
    "verification.route_identity.route_context_hash",
    "artifact_refs.route_context_hash",
  ]);
  pushFirstFact(facts, event, "prompt_contract_id", "prompt contract id", [
    "payload.prompt_contract_id",
    "payload.prompt_contract.prompt_contract_id",
    "payload.route_identity.prompt_contract_id",
    "verification.prompt_contract_id",
    "verification.route_identity.prompt_contract_id",
    "artifact_refs.prompt_contract_id",
  ]);
  pushFirstFact(facts, event, "prompt_contract_hash", "prompt contract hash", [
    "payload.prompt_contract_hash",
    "payload.prompt_contract.prompt_contract_hash",
    "payload.route_identity.prompt_contract_hash",
    "verification.prompt_contract_hash",
    "verification.route_identity.prompt_contract_hash",
    "artifact_refs.prompt_contract_hash",
  ]);
  pushFirstFact(facts, event, "visible_injection_manifest_hash", "visible injection manifest", [
    "payload.visible_injection_manifest_hash",
    "payload.route_identity.visible_injection_manifest_hash",
    "verification.visible_injection_manifest_hash",
    "verification.route_identity.visible_injection_manifest_hash",
    "artifact_refs.visible_injection_manifest_hash",
  ]);
  pushFirstFact(facts, event, "launch_text_hash", "launch text hash", [
    "payload.launch_text_hash",
    "payload.route_identity.launch_text_hash",
    "payload.revision_receipt.launch_text_hash",
    "verification.launch_text_hash",
    "verification.route_identity.launch_text_hash",
    "artifact_refs.launch_text_hash",
  ]);
  pushFirstFact(facts, event, "stage", "stage", [
    "payload.stage",
    "payload.lifecycle_state",
    "payload.failing_stage",
    "verification.stage",
    "phase",
  ]);
  pushFirstFact(facts, event, "work_mode", "observer work mode", [
    "payload.work_mode",
    "payload.to_work_mode",
    "payload.observer_work_mode",
    "payload.route_context.work_mode",
    "payload.next_legal_action.work_mode",
    "verification.work_mode",
    "verification.to_work_mode",
    "artifact_refs.work_mode",
  ]);
  pushFirstFact(facts, event, "work_mode_transition", "work-mode transition", [
    "payload.from_work_mode",
    "payload.work_mode_transition",
    "verification.from_work_mode",
  ]);
  pushFirstFact(facts, event, "graph_query_schema_trace_id", "graph query schema trace", [
    "payload.graph_query_schema_trace_id",
    "payload.query_schema_trace_id",
    "payload.route_context.graph_query_schema_trace_id",
    "payload.canonical_route_identity.graph_query_schema_trace_id",
    "verification.graph_query_schema_trace_id",
    "verification.query_schema_trace_id",
    "artifact_refs.graph_query_schema_trace_id",
  ]);
  // The observer root route context surfaces the skills/resources it actually
  // loaded for the lane. Show them when non-empty so the evidence modal proves
  // what runtime context was read, instead of leaving it in raw JSON.
  const loadedSkills = publicValuesAtPaths(event, [
    "payload.loaded_skills",
    "payload.route_context.loaded_skills",
    "payload.route_context.visible_bundle.loaded_skills",
    "verification.loaded_skills",
    "artifact_refs.loaded_skills",
  ]);
  if (loadedSkills.length > 0) {
    pushFact(facts, "loaded_skills", "loaded skills", formatCompactList(loadedSkills), sourceForPath(loadedSkills[0].path));
  }
  const loadedResources = publicValuesAtPaths(event, [
    "payload.loaded_resources",
    "payload.route_context.loaded_resources",
    "payload.route_context.visible_bundle.loaded_resources",
    "verification.loaded_resources",
    "artifact_refs.loaded_resources",
  ]);
  if (loadedResources.length > 0) {
    pushFact(facts, "loaded_resources", "loaded resources", formatCompactList(loadedResources), sourceForPath(loadedResources[0].path));
  }
  pushFirstFact(facts, event, "agent_id_match_mode", "agent-id match mode", [
    "payload.agent_id_match_mode",
    "payload.mf_subagent_startup_gate.agent_id_match_mode",
    "payload.identity_join.agent_id_match_mode",
    "verification.agent_id_match_mode",
    "verification.identity_join.agent_id_match_mode",
  ]);
  pushSurrogateCloseEvidenceFact(facts, event);
  pushCloseSubGateFacts(facts, event);
  pushFirstFact(facts, event, "topology", "topology", [
    "payload.selected_topology",
    "payload.recommended_topology",
    "payload.topology",
    "payload.template_id",
  ]);
  pushCountFact(facts, event, "target_file_count", "target-file count", "target file", "target files", [
    "payload.target_files",
    "payload.owned_files",
    "payload.prompt_contract.target_files",
    "verification.target_files",
    "artifact_refs.target_files",
    "artifact_refs.owned_files",
  ]);
  pushCountFact(facts, event, "acceptance_criteria_count", "acceptance-criteria count", "acceptance criterion", "acceptance criteria", [
    "payload.acceptance_criteria",
    "payload.prompt_contract.acceptance_criteria",
    "payload.requirements",
    "verification.acceptance_criteria",
  ]);
  const requiredEvidence = publicValuesAtPaths(event, [
    "payload.required_evidence",
    "payload.evidence_required",
    "payload.required_event_kinds",
    "payload.prompt_contract.required_evidence",
    "payload.prompt_contract.evidence_required",
    "verification.required_evidence",
    "verification.required_event_kinds",
  ]);
  if (requiredEvidence.length > 0) {
    pushFact(facts, "required_evidence", "required evidence", formatCompactList(requiredEvidence), sourceForPath(requiredEvidence[0].path));
  }
  const requiredLaneEvidence = publicValuesAtPaths(event, [
    "payload.required_lanes_evidence",
    "payload.required_lanes",
    "payload.route_context.required_lanes_evidence",
    "payload.route_context.visible_bundle.required_lanes_evidence",
    "payload.visible_bundle.required_lanes_evidence",
    "verification.required_lanes_evidence",
    "artifact_refs.required_lanes_evidence",
  ]);
  if (requiredLaneEvidence.length > 0) {
    pushFact(facts, "required_lanes_evidence", "required lanes/evidence", formatCompactList(requiredLaneEvidence), sourceForPath(requiredLaneEvidence[0].path));
  }
  const routeAlerts = publicValuesAtPaths(event, [
    "payload.route_alerts",
    "payload.alerts",
    "payload.route_context.alerts",
    "payload.route_context.route_alerts",
    "verification.route_alerts",
    "artifact_refs.route_alerts",
  ]);
  if (routeAlerts.length > 0) {
    pushFact(facts, "route_alerts", "route alerts", formatCompactList(routeAlerts), sourceForPath(routeAlerts[0].path));
  }
  const allowedActions = publicValuesAtPaths(event, [
    "payload.allowed_actions",
    "payload.route_context.allowed_actions",
    "payload.route_context.visible_bundle.allowed_actions",
    "payload.visible_bundle.allowed_actions",
    "verification.allowed_actions",
  ]);
  if (allowedActions.length > 0) {
    pushFact(facts, "allowed_actions", "allowed actions", formatCompactList(allowedActions), sourceForPath(allowedActions[0].path));
  }
  const blockedActions = publicValuesAtPaths(event, [
    "payload.blocked_actions",
    "payload.acknowledged_forbidden_actions",
    "payload.forbidden_actions",
    "payload.route_context.blocked_actions",
    "payload.route_context.visible_bundle.blocked_actions",
    "payload.visible_bundle.blocked_actions",
    "verification.blocked_actions",
  ]);
  if (blockedActions.length > 0) {
    pushFact(facts, "blocked_actions", "blocked actions", formatCompactList(blockedActions), sourceForPath(blockedActions[0].path));
  }
  pushOutcomeFact(facts, event, "decision", "decision", [
    "payload.decision",
    "payload.audit.decision",
    "payload.outcome.decision",
    "payload.result.decision",
    "payload.remaining_scope.decision",
    "verification.decision",
    "verification.audit.decision",
    "verification.outcome.decision",
    "verification.result.decision",
    "verification.remaining_scope.decision",
    "artifact_refs.decision",
    "artifact_refs.audit.decision",
    "artifact_refs.outcome.decision",
    "artifact_refs.remaining_scope.decision",
  ]);
  pushOutcomeFact(facts, event, "closed_rows", "closed rows", [
    "payload.closed_rows",
    "payload.audit.closed_rows",
    "payload.outcome.closed_rows",
    "payload.result.closed_rows",
    "payload.remaining_scope.closed_rows",
    "verification.closed_rows",
    "verification.audit.closed_rows",
    "verification.outcome.closed_rows",
    "verification.result.closed_rows",
    "verification.remaining_scope.closed_rows",
    "artifact_refs.closed_rows",
    "artifact_refs.audit.closed_rows",
    "artifact_refs.outcome.closed_rows",
    "artifact_refs.remaining_scope.closed_rows",
  ]);
  pushOutcomeFact(facts, event, "implemented_and_merged", "implemented and merged", [
    "payload.implemented_and_merged",
    "payload.audit.implemented_and_merged",
    "payload.outcome.implemented_and_merged",
    "payload.result.implemented_and_merged",
    "payload.remaining_scope.implemented_and_merged",
    "verification.implemented_and_merged",
    "verification.audit.implemented_and_merged",
    "verification.outcome.implemented_and_merged",
    "verification.result.implemented_and_merged",
    "verification.remaining_scope.implemented_and_merged",
    "artifact_refs.implemented_and_merged",
    "artifact_refs.audit.implemented_and_merged",
    "artifact_refs.outcome.implemented_and_merged",
    "artifact_refs.remaining_scope.implemented_and_merged",
  ]);
  const sourceEvents = publicValuesAtPaths(event, [
    "payload.source_event_id",
    "payload.source_event_ids",
    "payload.source_event_refs",
    "payload.source_event_type",
    "payload.source_events",
    "verification.source_event_id",
    "artifact_refs.source_event_id",
    "artifact_refs.source_event_refs",
  ]);
  if (sourceEvents.length > 0) {
    pushFact(facts, "source_event_refs", "source event refs", formatCompactList(sourceEvents), sourceForPath(sourceEvents[0].path));
  }
  const readReceiptRefs = publicValuesAtPaths(event, [
    "payload.read_receipt_event_id",
    "payload.read_receipt_event_ids",
    "payload.read_receipt_event_ref",
    "payload.read_receipt_event_refs",
    "payload.read_receipt_hash",
    "payload.receipt_id",
    "verification.read_receipt_event_id",
    "verification.read_receipt_event_refs",
    "verification.read_receipt_hash",
    "artifact_refs.read_receipt_event_id",
    "artifact_refs.read_receipt_event_refs",
    "artifact_refs.read_receipt_hash",
  ]);
  if (readReceiptRefs.length > 0) {
    pushFact(facts, "read_receipt_refs", "read receipt refs", formatCompactList(readReceiptRefs), sourceForPath(readReceiptRefs[0].path));
  }
  const startupRefs = publicValuesAtPaths(event, [
    "payload.startup_event_id",
    "payload.startup_event_ids",
    "payload.startup_event_ref",
    "payload.startup_event_refs",
    "payload.startup_intent_event_id",
    "payload.startup_intent_event_generated",
    "verification.startup_event_id",
    "verification.startup_event_refs",
    "artifact_refs.startup_event_id",
    "artifact_refs.startup_event_refs",
  ]);
  if (startupRefs.length > 0) {
    pushFact(facts, "startup_refs", "startup refs", formatCompactList(startupRefs), sourceForPath(startupRefs[0].path));
  }
  return stableFacts(facts).slice(0, 24);
}

// The preview/static placeholder route id (e.g. "event.route_prompt_context.preview")
// is a source-event preview pointer, not the canonical external route identity the
// observer read. It must never be surfaced as the canonical route_id (regression:
// preview value leaking into the close-gate evidence modal as canonical identity).
const ROUTE_ID_PREVIEW_PLACEHOLDER = /(^|[._])route_prompt_context[._]preview$|^event\.route|(^|[._])preview$/i;

function isPreviewRouteId(value: string): boolean {
  const normalized = safeText(value).trim();
  if (!normalized) return true;
  // Canonical route ids look like "route-…" / "route-repair-…"; anything that
  // is a preview/static placeholder pointer is rejected as non-canonical.
  if (/^route-/i.test(normalized)) return false;
  return ROUTE_ID_PREVIEW_PLACEHOLDER.test(normalized);
}

// Choose the canonical route_id, preferring the canonical_route_identity /
// route_context bundle, then any explicit route_id, while skipping the
// preview/static placeholder so it is never displayed as canonical. If only a
// preview placeholder exists, no route_id fact is emitted (the preview value is
// still inspectable in the collapsed raw event JSON).
function pushCanonicalRouteIdFact(facts: TaskPlaybackStructuredFact[], event: TaskTimelineEvent): void {
  const candidates = publicValuesAtPaths(event, [
    "payload.canonical_route_identity.route_id",
    "payload.route_context.canonical_route_identity.route_id",
    "verification.canonical_route_identity.route_id",
    "verification.route_context.canonical_route_identity.route_id",
    "artifact_refs.canonical_route_identity.route_id",
    "payload.route_identity.route_id",
    "payload.route_context.route_id",
    "verification.route_identity.route_id",
    "artifact_refs.route_identity.route_id",
    "payload.route_id",
    "verification.route_id",
    "artifact_refs.route_id",
  ]);
  const canonical = candidates.find((item) => !isPreviewRouteId(item.value));
  if (canonical) pushFact(facts, "route_id", "route id", canonical.value, canonical.source);
}

// A host-adapter surrogate startup (session_token_evidence_type === "surrogate")
// is never close-satisfying real bounded-worker evidence (regression #3104), even
// under an observer_hotfix_exception. Surface the close-satisfying boolean as a
// plain fact so the evidence modal states it explicitly instead of leaving it in
// raw JSON.
function pushSurrogateCloseEvidenceFact(facts: TaskPlaybackStructuredFact[], event: TaskTimelineEvent): void {
  // session_token_evidence_type lives under a key containing "token", which the
  // private-evidence path filter redacts. The category value ("surrogate" |
  // "real") is itself public and load-bearing for the #3104 close-evidence
  // demotion, so read it directly from the (already public-safe) payload /
  // verification objects rather than through the path-redacting helpers.
  const tokenType = firstNestedStringByKey(event, "session_token_evidence_type");
  const matchMode = firstPublicValueAtPaths(event, [
    "payload.agent_id_match_mode",
    "payload.mf_subagent_startup_gate.agent_id_match_mode",
    "payload.identity_join.agent_id_match_mode",
    "verification.agent_id_match_mode",
    "verification.identity_join.agent_id_match_mode",
  ]);
  const tokenTypeNormalized = (tokenType?.value || "").toLowerCase();
  if (tokenType) {
    pushFact(facts, "session_token_evidence_type", "session token evidence type", tokenType.value, tokenType.source);
  }
  const isSurrogate =
    tokenTypeNormalized === "surrogate"
    || (matchMode?.value || "").toLowerCase() === "host_adapter_startup_token_surrogate";
  if (isSurrogate) {
    pushFact(
      facts,
      "surrogate_close_satisfying",
      "surrogate close-satisfying",
      "no — host-adapter surrogate startup is not close-satisfying real-worker evidence (#3104)",
      tokenType?.source ?? matchMode?.source ?? "semantic",
    );
    return;
  }
  const declared = firstNestedStringByKey(event, "close_satisfying");
  // Only annotate the real-worker case when a startup gate explicitly recorded a
  // real session-token type, so we never invent close-satisfying state.
  if (tokenTypeNormalized === "real") {
    pushFact(
      facts,
      "surrogate_close_satisfying",
      "surrogate close-satisfying",
      declared && declared.value.toLowerCase() === "false"
        ? "no — startup gate recorded close_satisfying=false"
        : "yes — real session-token startup is close-satisfying",
      tokenType?.source ?? "semantic",
    );
  }
}

// Read the first value for a leaf key from the public payload/verification
// containers, bypassing path-level token redaction for known-safe category
// fields. The value is still safeText-sanitized before use.
function firstNestedStringByKey(event: TaskTimelineEvent, key: string): PublicFieldValue | null {
  const containers: Array<{ source: TaskPlaybackStructuredFact["source"]; record: Record<string, unknown> }> = [
    { source: "payload", record: asRecord(event.payload) },
    { source: "verification", record: asRecord(event.verification) },
  ];
  for (const { source, record } of containers) {
    const found = findLeafValue(record, key, 0);
    if (found) {
      const safe = safeText(found);
      if (safe && safe !== "[private detail redacted]") return { value: safe, path: source, source };
    }
  }
  return null;
}

function findLeafValue(record: Record<string, unknown>, key: string, depth: number): string {
  if (depth > 3) return "";
  const direct = record[key];
  if (typeof direct === "string" || typeof direct === "number" || typeof direct === "boolean") return String(direct);
  for (const value of Object.values(record)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const nested = findLeafValue(value as Record<string, unknown>, key, depth + 1);
      if (nested) return nested;
    }
  }
  return "";
}

// Close-gate sub-gate status facts: blocker-resolution self-clear gate (#3092),
// cross-ref evidence gate (#3090), and stale-route evidence gate (#3093/#3094).
// These mirror the backend mf_close_gate_verification sub-gates so the close
// banner / evidence modal can show which integrity sub-gate passed or blocked.
function pushCloseSubGateFacts(facts: TaskPlaybackStructuredFact[], event: TaskTimelineEvent): void {
  const subGates: Array<{ kind: string; label: string; paths: string[] }> = [
    {
      kind: "blocker_resolution_gate",
      label: "blocker-resolution gate (#3092)",
      paths: [
        "payload.blocker_resolution_gate.status",
        "payload.close_gate.blocker_resolution_gate.status",
        "verification.blocker_resolution_gate.status",
        "verification.close_gate.blocker_resolution_gate.status",
      ],
    },
    {
      kind: "cross_ref_gate",
      label: "cross-ref evidence gate (#3090)",
      paths: [
        "payload.cross_ref_gate.status",
        "payload.close_gate.cross_ref_gate.status",
        "verification.cross_ref_gate.status",
        "verification.close_gate.cross_ref_gate.status",
      ],
    },
    {
      kind: "stale_route_evidence_gate",
      label: "stale-route evidence gate (#3093/#3094)",
      paths: [
        "payload.stale_route_evidence_gate.status",
        "payload.close_gate.stale_route_evidence_gate.status",
        "verification.stale_route_evidence_gate.status",
        "verification.close_gate.stale_route_evidence_gate.status",
      ],
    },
  ];
  for (const gate of subGates) {
    const status = firstPublicValueAtPaths(event, gate.paths);
    if (status) pushFact(facts, gate.kind, gate.label, status.value, status.source);
  }
}

function failureDiagnosisFromEvent(event: TaskTimelineEvent, status: TaskPlaybackFrameStatus): TaskPlaybackStructuredFact[] {
  const diagnosis: TaskPlaybackStructuredFact[] = [];
  const blockerIds = publicValuesAtPaths(event, [
    "payload.blocker_ids",
    "payload.blockers",
    "payload.failed_request_ids",
    "verification.blocker_ids",
    "verification.blockers",
    "artifact_refs.blocker_ids",
  ]);
  if (blockerIds.length > 0) {
    pushFact(diagnosis, "blocker_ids", "blocker ids", formatCompactList(blockerIds), sourceForPath(blockerIds[0].path));
  }
  const missingEventKinds = publicValuesAtPaths(event, [
    "payload.missing_event_kinds",
    "payload.blocked_event_kinds",
    "payload.blocked_protected_event_kinds",
    "payload.required_before_protected_evidence",
    "verification.missing_event_kinds",
    "verification.blocked_event_kinds",
  ]);
  if (missingEventKinds.length > 0) {
    pushFact(diagnosis, "missing_event_kinds", "missing event kinds", formatCompactList(missingEventKinds), sourceForPath(missingEventKinds[0].path));
  }
  const missingRequirements = publicValuesAtPaths(event, [
    "payload.missing_required_evidence",
    "payload.missing_requirement_ids",
    "payload.missing_protected_lanes",
    "payload.required_before_protected_evidence",
    "payload.required_missing_evidence",
    "payload.route_context_gate.missing_requirement_ids",
    "payload.route_context_gate.missing_required_evidence",
    "payload.contract_gate.missing_requirement_ids",
    "payload.contract_gate.missing_required_evidence",
    "verification.missing_required_evidence",
    "verification.missing_requirement_ids",
    "verification.required_before_protected_evidence",
    "verification.required_missing_evidence",
    "verification.route_context_gate.missing_requirement_ids",
    "verification.route_context_gate.missing_required_evidence",
    "verification.contract_gate.missing_requirement_ids",
    "verification.contract_gate.missing_required_evidence",
  ]);
  if (missingRequirements.length > 0) {
    pushFact(diagnosis, "missing_required_evidence", "missing required evidence", formatCompactList(missingRequirements), sourceForPath(missingRequirements[0].path));
  }
  const routeMismatch = publicValuesAtPaths(event, [
    "payload.route_identity_mismatch",
    "payload.mismatched_route_identity",
    "payload.route_identity.mismatch",
    "payload.identity_recovery.route_identity_mismatch",
    "payload.route_context_gate.route_identity_mismatch",
    "payload.contract_gate.route_identity_mismatch",
    "verification.route_identity_mismatch",
    "verification.mismatched_route_identity",
    "verification.route_identity.mismatch",
    "verification.identity_recovery.route_identity_mismatch",
    "verification.route_context_gate.route_identity_mismatch",
    "verification.contract_gate.route_identity_mismatch",
  ]);
  const blockerValues = blockerIds.map((item) => item.value.toLowerCase());
  if (routeMismatch.length > 0) {
    pushFact(diagnosis, "mismatched_route_identity", "mismatched route identity", formatCompactList(routeMismatch), sourceForPath(routeMismatch[0].path));
  } else if (blockerValues.some((value) => value.includes("route_identity_mismatch") || value.includes("mismatched_route_identity"))) {
    pushFact(diagnosis, "mismatched_route_identity", "mismatched route identity", "route_identity_mismatch", "payload");
  }
  const staleReasons = publicValuesAtPaths(event, [
    "payload.stale_reason",
    "payload.stale_route_context_reason",
    "payload.route_context_stale_reason",
    "payload.timeout_reason",
    "payload.route_context_timeout_reason",
    "payload.route_token_timeout_reason",
    "payload.route_token_expired_reason",
    "payload.pending_scope_timeout",
    "payload.failure_reason",
    "payload.reason",
    "payload.last_error",
    "payload.route_context_gate.stale_reason",
    "payload.route_context_gate.timeout_reason",
    "payload.contract_gate.stale_reason",
    "payload.contract_gate.timeout_reason",
    "verification.stale_reason",
    "verification.route_context_stale_reason",
    "verification.timeout_reason",
    "verification.route_context_timeout_reason",
    "verification.route_token_timeout_reason",
    "verification.route_token_expired_reason",
    "verification.reason",
    "verification.errors",
    "verification.route_context_gate.stale_reason",
    "verification.route_context_gate.timeout_reason",
    "verification.contract_gate.stale_reason",
    "verification.contract_gate.timeout_reason",
  ]);
  const staleBlockers = blockerIds.filter((item) => /stale|timeout|timed_out|pending_scope/.test(item.value.toLowerCase()));
  if (staleReasons.length > 0) {
    pushFact(diagnosis, "stale_timeout_reason", "stale/timeout reason", formatCompactList(staleReasons), sourceForPath(staleReasons[0].path));
  } else if (staleBlockers.length > 0) {
    pushFact(diagnosis, "stale_timeout_reason", "stale/timeout reason", formatCompactList(staleBlockers), sourceForPath(staleBlockers[0].path));
  }
  pushOutcomeFact(diagnosis, event, "remaining_acceptance", "remaining acceptance", [
    "payload.remaining_acceptance",
    "payload.audit.remaining_acceptance",
    "payload.outcome.remaining_acceptance",
    "payload.result.remaining_acceptance",
    "payload.remaining_scope.remaining_acceptance",
    "verification.remaining_acceptance",
    "verification.audit.remaining_acceptance",
    "verification.outcome.remaining_acceptance",
    "verification.result.remaining_acceptance",
    "verification.remaining_scope.remaining_acceptance",
    "artifact_refs.remaining_acceptance",
    "artifact_refs.audit.remaining_acceptance",
    "artifact_refs.outcome.remaining_acceptance",
    "artifact_refs.remaining_scope.remaining_acceptance",
  ]);
  pushOutcomeFact(diagnosis, event, "remaining_open", "remaining open", [
    "payload.remaining_open",
    "payload.audit.remaining_open",
    "payload.outcome.remaining_open",
    "payload.result.remaining_open",
    "payload.remaining_scope.remaining_open",
    "verification.remaining_open",
    "verification.audit.remaining_open",
    "verification.outcome.remaining_open",
    "verification.result.remaining_open",
    "verification.remaining_scope.remaining_open",
    "artifact_refs.remaining_open",
    "artifact_refs.audit.remaining_open",
    "artifact_refs.outcome.remaining_open",
    "artifact_refs.remaining_scope.remaining_open",
  ]);
  const nextAction = firstPublicValueAtPaths(event, [
    "payload.next_legal_action.description",
    "payload.next_legal_action.action",
    "payload.remaining_scope.next_legal_action.description",
    "payload.remaining_scope.next_legal_action.action",
    "payload.next_legal_action",
    "payload.next_action",
    "payload.next_expected_action",
    "payload.legal_next_action",
    "payload.recovery_action",
    "payload.recovery_options",
    "verification.next_legal_action.description",
    "verification.next_legal_action.action",
    "verification.remaining_scope.next_legal_action.description",
    "verification.remaining_scope.next_legal_action.action",
    "verification.next_legal_action",
    "verification.next_action",
    "verification.next_expected_action",
    "verification.legal_next_action",
    "artifact_refs.next_legal_action.description",
    "artifact_refs.next_legal_action.action",
    "artifact_refs.remaining_scope.next_legal_action.description",
    "artifact_refs.remaining_scope.next_legal_action.action",
    "artifact_refs.next_legal_action",
  ]);
  if (nextAction) {
    pushFact(diagnosis, "next_legal_action", "next legal action", nextAction.value, nextAction.source);
  } else if (diagnosis.length > 0 || ["blocked", "failed", "missing"].includes(status)) {
    pushFact(diagnosis, "next_legal_action", "next legal action", inferredNextLegalAction(diagnosis, status), "semantic");
  }
  return stableFacts(diagnosis).slice(0, 12);
}

function evidenceLinksFromEvent(
  event: TaskTimelineEvent,
  _semantic: TaskTimelineSemanticProjection,
  evidenceRefs: TaskPlaybackEvidenceRef[],
  artifactRefs: TaskPlaybackArtifactRef[],
): TaskPlaybackEvidenceRef[] {
  const links: TaskPlaybackEvidenceRef[] = [
    { kind: "timeline_event", label: "timeline event", value: eventDisplayId(event) },
  ];
  pushEvidenceValues(links, "route_context", "route id", event, [
    "payload.canonical_route_identity.route_id",
    "payload.route_context.canonical_route_identity.route_id",
    "payload.route_identity.route_id",
    "payload.route_context.route_id",
    "verification.canonical_route_identity.route_id",
    "verification.route_identity.route_id",
    "payload.route_id",
    "verification.route_id",
    "artifact_refs.route_id",
  ], isPreviewRouteId);
  pushEvidenceValues(links, "route_context", "route context", event, [
    "payload.route_context_hash",
    "payload.route_identity.route_context_hash",
    "verification.route_context_hash",
    "verification.route_identity.route_context_hash",
    "artifact_refs.route_context_hash",
  ]);
  pushEvidenceValues(links, "prompt_contract", "prompt contract", event, [
    "payload.prompt_contract_id",
    "payload.prompt_contract.prompt_contract_id",
    "payload.route_identity.prompt_contract_id",
    "verification.prompt_contract_id",
    "verification.route_identity.prompt_contract_id",
    "artifact_refs.prompt_contract_id",
  ]);
  pushEvidenceValues(links, "prompt_contract", "prompt contract hash", event, [
    "payload.prompt_contract_hash",
    "payload.prompt_contract.prompt_contract_hash",
    "payload.route_identity.prompt_contract_hash",
    "verification.prompt_contract_hash",
    "verification.route_identity.prompt_contract_hash",
    "artifact_refs.prompt_contract_hash",
  ]);
  pushEvidenceValues(links, "precheck", "precheck", event, [
    "payload.precheck_id",
    "payload.action_precheck_id",
    "payload.route_action_precheck_id",
    "verification.precheck_id",
    "artifact_refs.precheck_id",
  ]);
  pushEvidenceValues(links, "source_event", "source event", event, [
    "payload.source_event_id",
    "payload.source_event_ids",
    "payload.source_event_refs",
    "payload.source_event_type",
    "payload.source_events",
    "verification.source_event_id",
    "verification.source_event_refs",
    "artifact_refs.source_event_id",
    "artifact_refs.source_event_refs",
  ]);
  pushEvidenceValues(links, "read_receipt", "read receipt", event, [
    "payload.read_receipt_event_id",
    "payload.read_receipt_event_ids",
    "payload.read_receipt_event_ref",
    "payload.read_receipt_event_refs",
    "payload.read_receipt_hash",
    "verification.read_receipt_event_id",
    "verification.read_receipt_event_refs",
    "artifact_refs.read_receipt_event_id",
    "artifact_refs.read_receipt_event_refs",
  ]);
  pushEvidenceValues(links, "source_event", "startup", event, [
    "payload.startup_event_id",
    "payload.startup_event_ids",
    "payload.startup_event_ref",
    "payload.startup_event_refs",
    "payload.startup_intent_event_id",
    "verification.startup_event_id",
    "verification.startup_event_refs",
    "artifact_refs.startup_event_id",
    "artifact_refs.startup_event_refs",
  ]);
  links.push(...evidenceRefs);
  links.push(...artifactRefs.slice(0, 12).map((ref) => ({
    kind: evidenceKindFromArtifact(ref),
    label: ref.kind,
    value: ref.value,
  })));
  return stableEvidence(links.filter((ref) => Boolean(ref.value))).slice(0, 24);
}

function eventSummaryFromEvent(
  event: TaskTimelineEvent,
  semantic: TaskTimelineSemanticProjection,
  status: TaskPlaybackFrameStatus,
  facts: TaskPlaybackStructuredFact[],
  diagnosis: TaskPlaybackStructuredFact[],
): string {
  const actor = factValue(facts, "actor") || semantic.actor_label || "Aming Claw";
  const backlog = factValue(facts, "backlog_id");
  const route = factValue(facts, "route_id");
  const promptContract = factValue(facts, "prompt_contract_id");
  const targetCount = factValue(facts, "target_file_count");
  const criteriaCount = factValue(facts, "acceptance_criteria_count");
  const decision = factValue(facts, "decision");
  const closedRows = factValue(facts, "closed_rows");
  const implementedAndMerged = factValue(facts, "implemented_and_merged");
  const remainingAcceptance = factValue(diagnosis, "remaining_acceptance");
  const remainingOpen = factValue(diagnosis, "remaining_open");
  const requiredEvidenceCount = firstCountAtPaths(event, [
    "payload.required_evidence",
    "payload.evidence_required",
    "payload.prompt_contract.required_evidence",
    "payload.prompt_contract.evidence_required",
  ])?.count;
  if (semantic.catalog_entry_id === "route.prompt_context.requested" || event.event_type === "route.prompt_context.requested") {
    const identity = [route ? `route ${route}` : "", promptContract ? `prompt contract ${promptContract}` : ""].filter(Boolean).join(" and ");
    const scope = [
      targetCount,
      criteriaCount,
      requiredEvidenceCount ? formatCount(requiredEvidenceCount, "required evidence item", "required evidence items") : "",
    ].filter(Boolean);
    return `Route service requested prompt context${backlog ? ` for backlog ${backlog}` : ""}${identity ? ` using ${identity}` : ""}; the public scope includes ${scope.length > 0 ? scope.join(", ") : "the visible task contract fields"}.`;
  }
  const blocker = factValue(diagnosis, "blocker_ids") || factValue(diagnosis, "missing_event_kinds") || factValue(diagnosis, "missing_required_evidence");
  const nextAction = factValue(diagnosis, "next_legal_action");
  if (hasOutcomeAuditFacts(event, decision, closedRows, implementedAndMerged, remainingAcceptance, remainingOpen)) {
    const completed = [
      closedRows ? `closed rows: ${closedRows}` : "",
      implementedAndMerged ? `implemented and merged: ${implementedAndMerged}` : "",
    ].filter(Boolean);
    const remaining = [
      remainingAcceptance ? `remaining acceptance: ${remainingAcceptance}` : "",
      remainingOpen ? `remaining open: ${remainingOpen}` : "",
    ].filter(Boolean);
    const statusText = decision ? ` with decision ${decision}` : "";
    const completedText = completed.length > 0 ? ` Completed scope: ${completed.join("; ")}.` : "";
    const remainingText = remaining.length > 0 ? ` Remaining scope: ${remaining.join("; ")}.` : "";
    const actionText = nextAction ? ` Next legal action: ${nextAction}.` : "";
    return `${actor} recorded ${lowercaseFirst(semantic.title)}${backlog ? ` for backlog ${backlog}` : ""}${statusText}.${completedText}${remainingText}${actionText}`.replace(/\s+/g, " ").trim();
  }
  if (blocker || ["blocked", "failed", "missing"].includes(status)) {
    return `${actor} recorded ${lowercaseFirst(semantic.title)}${backlog ? ` for backlog ${backlog}` : ""}; ${blocker ? `the blocker diagnosis is ${blocker}` : `the status is ${status}`}${nextAction ? `, and the next legal action is ${nextAction}` : ""}.`;
  }
  const identityFacts = [route ? `route ${route}` : "", promptContract ? `prompt contract ${promptContract}` : "", targetCount || ""].filter(Boolean);
  if (identityFacts.length > 0 || backlog) {
    return `${actor} recorded ${lowercaseFirst(semantic.title)}${backlog ? ` for backlog ${backlog}` : ""}${identityFacts.length > 0 ? ` with ${formatCompactList(identityFacts)}` : ""}.`;
  }
  return semantic.detail;
}

function lanesFromFrames(frames: TaskPlaybackFrame[]): TaskPlaybackLane[] {
  const grouped = new Map<string, TaskPlaybackFrame[]>();
  for (const frame of frames) grouped.set(frame.lane_id, [...(grouped.get(frame.lane_id) ?? []), frame]);
  return Array.from(grouped.entries())
    .map(([id, laneFrames]) => {
      const latest = laneFrames[laneFrames.length - 1];
      return {
        id,
        label: laneLabel(id),
        family: laneFamily(id),
        status: aggregateStatus(laneFrames.map((frame) => frame.status)),
        frame_count: laneFrames.length,
        latest_at: latest?.at || "",
      };
    })
    .sort((a, b) => laneSort(a.id) - laneSort(b.id) || a.id.localeCompare(b.id));
}

function closeGateSummaryFrom(response?: BacklogTimelineGateResponse | null): TaskPlaybackCloseGateSummary {
  const gate = response?.timeline_gate;
  if (!response || !gate) {
    return {
      applicable: false,
      can_close: false,
      status: "missing",
      label: "No close gate response loaded",
      missing_event_kinds: [],
      missing_requirement_ids: [],
      missing_requirement_count: 0,
      reason_sentence: "Close-gate verification has not been loaded for this task yet.",
      next_expected_action: "Next expected evidence/action: load the governed timeline and close-gate verification.",
      next_expected_evidence: [],
      blocked: false,
      event_count: 0,
    };
  }
  const missingEventKinds = stable((gate.missing_event_kinds ?? []).map(safeText).filter(Boolean));
  const missingRequirementIds = closeGateMissingRequirementIds(response);
  const missingRequirementCount = missingRequirementIds.length;
  const blocked = response.applicable && (!response.can_close || gate.passed === false || missingEventKinds.length > 0 || missingRequirementCount > 0);
  const nextExpectedEvidence = stable([...missingEventKinds, ...missingRequirementIds]).slice(0, 8);
  return {
    applicable: Boolean(response.applicable),
    can_close: Boolean(response.can_close),
    status: blocked ? "blocked" : gate.passed || response.can_close ? "passed" : "recorded",
    label: response.applicable ? (blocked ? "Close gate blocked" : "Close gate ready") : "Close gate not applicable",
    missing_event_kinds: missingEventKinds,
    missing_requirement_ids: missingRequirementIds,
    missing_requirement_count: missingRequirementCount,
    reason_sentence: closeGateReasonSentence(response, missingEventKinds, missingRequirementIds, blocked),
    next_expected_action: closeGateNextExpectedAction(response, missingEventKinds, missingRequirementIds, blocked),
    next_expected_evidence: nextExpectedEvidence,
    blocked,
    event_count: response.event_count ?? gate.event_count ?? response.events?.length ?? 0,
  };
}

function closeGateMissingRequirementIds(response: BacklogTimelineGateResponse): string[] {
  const gate = response.timeline_gate;
  const contractGate = asRecord(gate?.contract_gate);
  const routeGate = asRecord(gate?.route_context_gate);
  const verification = asRecord(asRecord(response as unknown as Record<string, unknown>).verification);
  const gateRecord = asRecord(gate as unknown as Record<string, unknown>);
  return stable([
    ...stringsFromUnknown(contractGate.missing_requirement_ids),
    ...stringsFromUnknown(routeGate.missing_requirement_ids),
    ...stringsFromUnknown(gateRecord.missing_requirement_ids),
    ...stringsFromUnknown(gateRecord.missing_protected_lanes),
    ...stringsFromUnknown(gateRecord.required_before_protected_evidence),
    ...stringsFromUnknown(verification.missing_requirement_ids),
    ...stringsFromUnknown(verification.missing_protected_lanes),
    ...stringsFromUnknown(verification.required_before_protected_evidence),
    ...stringsFromUnknown(verification.next_expected_event_kind),
  ].map(safeText).filter(Boolean));
}

function closeGateReasonSentence(
  response: BacklogTimelineGateResponse,
  missingEventKinds: string[],
  missingRequirementIds: string[],
  blocked: boolean,
): string {
  if (response.applicable === false) return safeText(response.reason || "Close gate is not applicable to this backlog row.");
  if (!blocked) return "Close gate can pass because required public evidence is present.";
  if (missingEventKinds.length > 0) {
    const label = formatEvidenceList(missingEventKinds);
    const single = missingEventKinds.length === 1;
    return `Blocked because ${label} evidence ${single ? "has" : "have"} not been recorded; the close gate cannot pass until ${single ? "that event exists" : "those events exist"}.`;
  }
  if (missingRequirementIds.length > 0) {
    const label = formatEvidenceList(missingRequirementIds);
    const single = missingRequirementIds.length === 1;
    return `Blocked because ${label} evidence ${single ? "is" : "are"} missing; the close gate cannot pass until ${single ? "that requirement is satisfied" : "those requirements are satisfied"}.`;
  }
  const reason = safeText(response.reason || "");
  if (reason) return `Blocked because ${lowercaseFirst(trimTrailingPunctuation(reason))}.`;
  return "Blocked because close-gate verification has not accepted the current evidence yet.";
}

function closeGateNextExpectedAction(
  response: BacklogTimelineGateResponse,
  missingEventKinds: string[],
  missingRequirementIds: string[],
  blocked: boolean,
): string {
  if (response.applicable === false) return "Next expected evidence/action: no close-gate evidence is required for this row.";
  if (!blocked) return "Next expected evidence/action: review the close-ready evidence and complete the close step.";
  if (missingEventKinds.length > 0) {
    return `Next expected evidence/action: add ${formatEvidenceList(missingEventKinds)} evidence, then rerun close-gate verification.`;
  }
  if (missingRequirementIds.length > 0) {
    return `Next expected evidence/action: satisfy ${formatEvidenceList(missingRequirementIds)}, then rerun close-gate verification.`;
  }
  return "Next expected evidence/action: add the missing public verification evidence, then rerun close-gate verification.";
}

function formatEvidenceList(values: string[]): string {
  const labels = stable(values.map(humanEvidenceName).filter(Boolean));
  if (labels.length === 0) return "required";
  if (labels.length === 1) return labels[0];
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")}, and ${labels[labels.length - 1]}`;
}

function humanEvidenceName(value: string): string {
  const normalized = safeText(value).trim();
  const labels: Record<string, string> = {
    close_ready: "close-ready",
    implementation: "implementation",
    verification: "verification",
    independent_verification_lane: "independent verification lane",
    mf_subagent_startup: "bounded worker startup",
    mf_subagent_read_receipt: "bounded worker read receipt",
    bounded_implementation_worker_dispatch: "bounded worker dispatch",
    route_context: "route context",
    route_action_precheck: "route action precheck",
  };
  return labels[normalized] ?? normalized.replace(/_/g, " ").replace(/\s+/g, " ");
}

function trimTrailingPunctuation(value: string): string {
  return value.replace(/[.!?]+$/g, "").trim();
}

function lowercaseFirst(value: string): string {
  return value ? value.charAt(0).toLowerCase() + value.slice(1) : value;
}

function summarizeFrames(
  frames: TaskPlaybackFrame[],
  gate: TaskPlaybackCloseGateSummary,
  source: TaskPlaybackSource,
): TaskPlaybackStatusSummary {
  const byStatus = Object.fromEntries(FRAME_STATUS_ORDER.map((status) => [status, 0])) as Record<TaskPlaybackFrameStatus, number>;
  for (const frame of frames) byStatus[frame.status] += 1;
  return {
    total_frames: frames.length,
    by_status: byStatus,
    blocked_gate: gate.blocked,
    has_timeline: frames.length > 0,
    has_governed_data: source !== "fallback_sample",
  };
}

function aggregateStatus(statuses: TaskPlaybackFrameStatus[]): TaskPlaybackFrameStatus {
  for (const status of FRAME_STATUS_ORDER) {
    if (statuses.includes(status)) return status;
  }
  return "unknown";
}

function laneLabel(id: string): string {
  if (id === "content_sys") return "content-sys";
  if (id === "gate") return "Close gate";
  if (id === "verification") return "Verification";
  if (id === "worker") return "Bounded worker";
  return "Observer";
}

function laneFamily(id: string): TaskPlaybackLaneFamily {
  if (id === "content_sys") return "content_sys";
  if (id === "gate") return "gate";
  if (id === "verification") return "verification";
  if (id === "worker") return "worker";
  return "observer";
}

function laneSort(id: string): number {
  return ["observer", "worker", "verification", "gate", "content_sys"].indexOf(id);
}

function evidenceFromEvent(event: TaskTimelineEvent, semantic: TaskTimelineSemanticProjection): TaskPlaybackEvidenceRef[] {
  const refs: TaskPlaybackEvidenceRef[] = [
    { kind: "timeline_event", label: "event", value: eventDisplayId(event) },
  ];
  if (event.trace_id) refs.push({ kind: "graph_trace", label: "trace", value: safeText(event.trace_id) });
  if (event.commit_sha) refs.push({ kind: "commit", label: "commit", value: shortCommit(event.commit_sha) });
  const containers = [event.payload, event.verification, event.artifact_refs].map(asRecord);
  refs.push(...collectPublicStrings(containers, ["graph_trace_ids", "graph_query_trace_ids", "trace_ids"]).map((value) => ({
    kind: "graph_trace" as const,
    label: "graph trace",
    value,
  })));
  refs.push(...collectPublicStrings(containers, ["node_id", "node_ids", "target_node_id", "inspected_node_ids"]).map((value) => ({
    kind: "node" as const,
    label: "node",
    value,
  })));
  refs.push(...collectPublicStrings(containers, ["status_cards", "timeline_events", "artifact_refs"]).slice(0, 6).map((value) => ({
    kind: value.toLowerCase().includes("content") ? "content_sys" as const : "artifact" as const,
    label: "evidence",
    value,
  })));
  refs.push(...semantic.evidence.map((chip) => ({
    kind: evidenceKindFromSemanticChip(chip),
    label: chip.label,
    value: chip.value,
  })));
  return stableEvidence(refs.filter((ref) => Boolean(ref.value)));
}

function artifactsFromEvent(event: TaskTimelineEvent, semantic: TaskTimelineSemanticProjection): TaskPlaybackArtifactRef[] {
  const containers = [event.payload, event.verification, event.artifact_refs].map(asRecord);
  const files = collectPublicStrings(containers, ["changed_files", "target_files", "modified_files", "updated_files", "files"]);
  const tests = collectPublicStrings(containers, ["tests_run", "test_commands", "tests_written", "test_files", "commands"]);
  const screenshots = collectPublicStrings(containers, ["screenshot", "screenshots", "browser_screenshot", "browser_screenshots"]);
  const graph = collectPublicStrings(containers, ["graph_trace_ids", "graph_query_trace_ids", "trace_ids"]);
  const artifactRefs = collectPublicStrings(containers, ["artifact_refs", "artifacts", "content_sys_artifacts"]);
  const refs: TaskPlaybackArtifactRef[] = [
    ...files.map((value) => ({ kind: "file" as const, value })),
    ...tests.map((value) => ({ kind: "test" as const, value })),
    ...screenshots.map((value) => ({ kind: "screenshot" as const, value })),
    ...graph.map((value) => ({ kind: "graph" as const, value })),
    ...artifactRefs.map((value) => ({
      kind: value.toLowerCase().includes("content") ? "content_sys" as const : "artifact" as const,
      value,
    })),
  ];
  if (event.commit_sha) refs.push({ kind: "commit", value: shortCommit(event.commit_sha) });
  refs.push(...semantic.artifacts.map((chip) => ({
    kind: artifactKindFromSemanticChip(chip),
    value: chip.value,
  })));
  return stableArtifacts(refs);
}

interface PublicFieldValue {
  value: string;
  path: string;
  source: TaskPlaybackStructuredFact["source"];
}

function pushFact(
  facts: TaskPlaybackStructuredFact[],
  kind: string,
  label: string,
  value: string,
  source: TaskPlaybackStructuredFact["source"],
): void {
  const safe = safeText(value);
  if (!safe || safe === "[private detail redacted]") return;
  facts.push({ kind, label, value: safe, source });
}

function pushFirstFact(facts: TaskPlaybackStructuredFact[], event: TaskTimelineEvent, kind: string, label: string, paths: string[]): void {
  const match = firstPublicValueAtPaths(event, paths);
  if (match) pushFact(facts, kind, label, match.value, match.source);
}

function pushCountFact(
  facts: TaskPlaybackStructuredFact[],
  event: TaskTimelineEvent,
  kind: string,
  label: string,
  singular: string,
  plural: string,
  paths: string[],
): void {
  const count = firstCountAtPaths(event, paths);
  if (count && count.count > 0) pushFact(facts, kind, label, formatCount(count.count, singular, plural), count.source);
}

function pushOutcomeFact(facts: TaskPlaybackStructuredFact[], event: TaskTimelineEvent, kind: string, label: string, paths: string[]): void {
  const values = publicOutcomeValuesAtPaths(event, paths);
  if (values.length > 0) pushFact(facts, kind, label, formatCompactList(values, 6), sourceForPath(values[0].path));
}

function firstPublicValueAtPaths(event: TaskTimelineEvent, paths: string[]): PublicFieldValue | null {
  return publicValuesAtPaths(event, paths)[0] ?? null;
}

function publicValuesAtPaths(event: TaskTimelineEvent, paths: string[]): PublicFieldValue[] {
  const values: PublicFieldValue[] = [];
  for (const path of paths) values.push(...publicValuesAtPath(event, path));
  const seen = new Set<string>();
  return values.filter((item) => {
    const key = `${item.path}:${item.value}`;
    if (!item.value || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function publicValuesAtPath(event: TaskTimelineEvent, path: string): PublicFieldValue[] {
  if (isSensitiveEvidencePath(path)) return [];
  return stringsFromUnknown(valueAtPath(event as unknown as Record<string, unknown>, path))
    .map(safeText)
    .filter((value) => value && value !== "[private detail redacted]")
    .map((value) => ({ value, path, source: sourceForPath(path) }));
}

function publicOutcomeValuesAtPaths(event: TaskTimelineEvent, paths: string[]): PublicFieldValue[] {
  const values: PublicFieldValue[] = [];
  for (const path of paths) values.push(...publicOutcomeValuesAtPath(event, path));
  const seen = new Set<string>();
  return values.filter((item) => {
    const key = `${item.path}:${item.value}`;
    if (!item.value || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function publicOutcomeValuesAtPath(event: TaskTimelineEvent, path: string): PublicFieldValue[] {
  if (isSensitiveEvidencePath(path)) return [];
  return outcomeStringsFromUnknown(valueAtPath(event as unknown as Record<string, unknown>, path))
    .map(safeText)
    .filter((value) => value && value !== "[private detail redacted]")
    .map((value) => ({ value, path, source: sourceForPath(path) }));
}

function firstCountAtPaths(event: TaskTimelineEvent, paths: string[]): { count: number; source: TaskPlaybackStructuredFact["source"]; path: string } | null {
  for (const path of paths) {
    if (isSensitiveEvidencePath(path)) continue;
    const count = countUnknownItems(valueAtPath(event as unknown as Record<string, unknown>, path));
    if (count > 0) return { count, source: sourceForPath(path), path };
  }
  return null;
}

function countUnknownItems(value: unknown): number {
  if (value == null || value === "") return 0;
  if (Array.isArray(value)) return value.filter((item) => safeText(stringFrom(item) || compactUnknown(item))).length;
  if (typeof value === "object") {
    return Object.keys(value as Record<string, unknown>).filter((key) => !isSensitiveEvidencePath(key)).length;
  }
  return safeText(String(value)) ? 1 : 0;
}

function pushEvidenceValues(
  links: TaskPlaybackEvidenceRef[],
  kind: TaskPlaybackEvidenceRef["kind"],
  label: string,
  event: TaskTimelineEvent,
  paths: string[],
  reject?: (value: string) => boolean,
): void {
  for (const item of publicValuesAtPaths(event, paths).slice(0, 8)) {
    if (reject && reject(item.value)) continue;
    links.push({ kind, label, value: item.value });
  }
}

function sourceForPath(path: string): TaskPlaybackStructuredFact["source"] {
  if (path.startsWith("payload.")) return "payload";
  if (path.startsWith("verification.")) return "verification";
  if (path.startsWith("artifact_refs.")) return "artifact_refs";
  return "event";
}

function valueAtPath(root: Record<string, unknown>, path: string): unknown {
  return path.split(".").reduce<unknown>((current, key) => {
    if (current == null) return undefined;
    if (Array.isArray(current)) return current.map((item) => asRecord(item)[key]);
    return asRecord(current)[key];
  }, root);
}

function factValue(facts: TaskPlaybackStructuredFact[], kind: string): string {
  return facts.find((fact) => fact.kind === kind)?.value ?? "";
}

function formatCompactList(values: Array<string | PublicFieldValue>, limit = 8): string {
  const labels = values.map((item) => safeText(typeof item === "string" ? item : item.value)).filter(Boolean);
  const unique = stable(labels);
  const visible = unique.slice(0, limit);
  const suffix = unique.length > visible.length ? ` +${unique.length - visible.length}` : "";
  if (visible.length === 0) return "";
  if (visible.length === 1) return `${visible[0]}${suffix}`;
  return `${visible.join(", ")}${suffix}`;
}

function formatCount(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

function inferredNextLegalAction(diagnosis: TaskPlaybackStructuredFact[], status: TaskPlaybackFrameStatus): string {
  if (factValue(diagnosis, "remaining_acceptance") || factValue(diagnosis, "remaining_open")) {
    return "Finish the remaining acceptance or open backlog scope, then rerun audit and close-gate verification.";
  }
  if (factValue(diagnosis, "mismatched_route_identity")) {
    return "Record matching route-context identity evidence, then retry the gated action.";
  }
  if (factValue(diagnosis, "stale_timeout_reason")) {
    return "Resolve the stale or timed-out attempt, then retry or supersede it with fresh route evidence.";
  }
  if (factValue(diagnosis, "missing_event_kinds") || factValue(diagnosis, "missing_required_evidence")) {
    return "Record the missing public evidence, then rerun close-gate verification.";
  }
  if (status === "failed") return "Repair the failing evidence, rerun verification, then request review again.";
  return "Resolve the listed blocker ids, then retry the governed action.";
}

function hasOutcomeAuditFacts(
  event: TaskTimelineEvent,
  decision: string,
  closedRows: string,
  implementedAndMerged: string,
  remainingAcceptance: string,
  remainingOpen: string,
): boolean {
  if (closedRows || implementedAndMerged || remainingAcceptance || remainingOpen) return true;
  if (!decision) return false;
  const label = [event.event_type, event.event_kind, event.phase].map((value) => safeText(String(value || "")).toLowerCase()).join(" ");
  return /audit|remaining.scope|remaining_scope|postmerge|verification/.test(label);
}

function evidenceKindFromArtifact(ref: TaskPlaybackArtifactRef): TaskPlaybackEvidenceRef["kind"] {
  if (ref.kind === "graph") return "graph_trace";
  if (ref.kind === "content_sys") return "content_sys";
  if (ref.kind === "commit") return "commit";
  if (ref.kind === "file") return "file";
  if (ref.kind === "test") return "test";
  return "artifact";
}

function evidenceKindFromSemanticChip(chip: TaskTimelineSemanticChip): TaskPlaybackEvidenceRef["kind"] {
  if (chip.kind === "graph_trace") return "graph_trace";
  if (chip.kind === "commit") return "commit";
  if (chip.kind === "file") return "file";
  if (chip.kind === "test") return "test";
  if (chip.kind === "node") return "node";
  if (chip.kind === "route" || chip.kind === "worker" || chip.kind === "timeline") return "gate";
  return "artifact";
}

function artifactKindFromSemanticChip(chip: TaskTimelineSemanticChip): TaskPlaybackArtifactRef["kind"] {
  if (chip.kind === "file") return "file";
  if (chip.kind === "test") return "test";
  if (chip.kind === "graph_trace") return "graph";
  if (chip.kind === "commit") return "commit";
  return chip.value.toLowerCase().includes("content") ? "content_sys" : "artifact";
}

function collectPublicStrings(containers: Record<string, unknown>[], keys: string[]): string[] {
  const values: string[] = [];
  for (const container of containers) {
    for (const [key, value] of Object.entries(container)) {
      if (isSensitiveEvidencePath(key)) continue;
      if (!keys.includes(key)) continue;
      values.push(...stringsFromUnknown(value));
    }
  }
  return stable(values.map(safeText).filter((value) => Boolean(value) && value !== "[private detail redacted]")).slice(0, 18);
}

function stringsFromUnknown(value: unknown): string[] {
  if (value == null || value === "") return [];
  if (Array.isArray(value)) return value.flatMap(stringsFromUnknown);
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    return Object.entries(record)
      .filter(([key]) => !isSensitiveEvidencePath(key))
      .slice(0, 4)
      .map(([key, item]) => `${titleize(key)}: ${safeText(stringFrom(item) || compactUnknown(item))}`);
  }
  return [String(value)];
}

function outcomeStringsFromUnknown(value: unknown): string[] {
  if (value == null || value === "") return [];
  if (Array.isArray(value)) return value.flatMap(outcomeStringsFromUnknown);
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const primary = firstStringField(record, ["bug_id", "backlog_id", "id", "row_id", "task_id"]);
    const title = firstStringField(record, ["title", "summary", "label", "name"]);
    const state = firstStringField(record, ["decision", "status", "state", "result", "action"]);
    const detail = firstStringField(record, ["description", "reason", "next_action", "next_legal_action"]);
    const compact = [primary, title, state, detail].filter(Boolean).join(" | ");
    return compact ? [compact] : stringsFromUnknown(value);
  }
  return [String(value)];
}

function firstStringField(record: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = stringFrom(record[key]);
    if (value) return value;
  }
  return "";
}

function compactUnknown(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
  const keys = Object.keys(value as Record<string, unknown>).filter((key) => !isSensitiveEvidencePath(key));
  return keys.length > 0 ? keys.slice(0, 3).join(", ") : "record";
}

function isSensitiveEvidencePath(path: string): boolean {
  const normalized = path.trim().toLowerCase();
  if (!normalized) return false;
  const leaf = normalized.split(".").pop() ?? normalized;
  if (
    leaf === "route_context"
    || leaf === "route_identity"
    || leaf === "prompt_contract"
    || leaf === "visible_injection_manifest"
    || leaf === "visible_injection_manifest_hash"
    || leaf === "judgment_brain_label"
    || leaf === "route_docs"
    || leaf === "source_label"
    || leaf === "body_persisted_status"
    || /(^|_)(hash|id|ids|refs)$/.test(leaf)
    || leaf.endsWith("_hash")
    || leaf.endsWith("_id")
    || leaf.endsWith("_ids")
    || leaf.endsWith("_refs")
  ) {
    return false;
  }
  return PRIVATE_EVIDENCE_KEY.test(normalized);
}

function sanitizeEvidenceString(value: string, path = ""): string {
  const redacted = value
    .replace(ABSOLUTE_HOST_PATH, "$1[local path redacted]")
    .replace(TOKEN_VALUE, "[token redacted]")
    .replace(/\s+/g, " ")
    .trim();
  if (!redacted) return "";
  if (isSensitiveEvidencePath(path) || isSensitiveEvidenceText(redacted, path)) return "[private detail redacted]";
  return redacted;
}

function isSensitiveEvidenceText(value: string, path = ""): boolean {
  const text = value.toLowerCase();
  if (isSensitiveEvidencePath(path)) return true;
  if (/\[fixture private route context body\]|raw private route body|raw private context body|private route context body/.test(text)) return true;
  if (/(system|developer|hidden)[-_\s]?prompt\s*[:=]/i.test(value)) return true;
  if (/(one[-_\s]?time[-_\s]?auth|credential|password|api[-_\s]?key|secret)\s*[:=]/i.test(value)) return true;
  return false;
}

function safeText(value: string): string {
  return sanitizeEvidenceString(value);
}

function stringFrom(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function eventIdentity(event: TaskTimelineEvent, index: number): string {
  return String(event.event_id || event.id || event.trace_id || `${event.event_type}-${event.created_at || index}`);
}

function eventDisplayId(event: TaskTimelineEvent): string {
  if (event.event_id && !isSensitiveEvidenceText(event.event_id, "event_id")) return safeText(event.event_id);
  if (event.id != null) return `#${event.id}`;
  if (event.trace_id && !isSensitiveEvidenceText(event.trace_id, "trace_id")) return safeText(event.trace_id);
  return "recorded";
}

function compareTimelineEvents(a: TaskTimelineEvent, b: TaskTimelineEvent): number {
  const at = Date.parse(a.created_at || "") || 0;
  const bt = Date.parse(b.created_at || "") || 0;
  if (at !== bt) return at - bt;
  return Number(a.id ?? 0) - Number(b.id ?? 0);
}

function titleize(value: string): string {
  return safeText(value.replace(/[_./-]+/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase()));
}

function shortCommit(value: string): string {
  return safeText(value).slice(0, 12);
}

function stable(values: string[]): string[] {
  return Array.from(new Set(values));
}

function stableFacts(facts: TaskPlaybackStructuredFact[]): TaskPlaybackStructuredFact[] {
  const seen = new Set<string>();
  return facts.filter((fact) => {
    const key = `${fact.kind}:${fact.label}:${fact.value}`;
    if (!fact.value || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function stableInspectorRows(rows: TaskTimelineEvidenceInspector["rows"]): TaskTimelineEvidenceInspector["rows"] {
  const seen = new Set<string>();
  return rows.filter((row) => {
    const key = `${row.kind}:${row.label}:${row.value}`;
    if (!row.kind || !row.label || !row.value || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 36);
}

function stableEvidence(refs: TaskPlaybackEvidenceRef[]): TaskPlaybackEvidenceRef[] {
  const seen = new Set<string>();
  return refs.filter((ref) => {
    const key = `${ref.kind}:${ref.label}:${ref.value}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function stableArtifacts(refs: TaskPlaybackArtifactRef[]): TaskPlaybackArtifactRef[] {
  const seen = new Set<string>();
  return refs.filter((ref) => {
    const key = `${ref.kind}:${ref.value}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
