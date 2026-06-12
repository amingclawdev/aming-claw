import type { BacklogBug, BacklogTimelineGateResponse, TaskTimelineEvent, TaskTimelineResponse } from "../types";
import {
  projectGateMatrix,
  isPrivateTimelineText,
  projectTaskTimelineEvent,
  timelineStatusFromEvent,
  type GateMatrixProjection,
  type TaskTimelineEvidenceInspector,
  type TaskTimelineSemanticChip,
  type TaskTimelineSemanticNarrative,
  type TaskTimelineSemanticProjection,
  type TaskTimelineSemanticRelation,
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

export type TaskPlaybackChecklistItemStatus =
  | "passed"
  | "satisfied"
  | "present"
  | "missing"
  | "blocked"
  | "failed"
  | "required"
  | "pending"
  | "recorded"
  | "unknown";

export interface TaskPlaybackChecklistItem {
  id: string;
  label: string;
  value: string;
  status: TaskPlaybackChecklistItemStatus;
  source: TaskPlaybackStructuredFact["source"];
}

export interface TaskPlaybackChecklistCategory {
  id: "unmet" | "passed" | "required" | "recorded";
  label: string;
  status: TaskPlaybackChecklistItemStatus;
  items: TaskPlaybackChecklistItem[];
}

export interface TaskPlaybackEventChecklist {
  categories: TaskPlaybackChecklistCategory[];
  item_count: number;
  hidden_count: number;
  blocked_count: number;
  passed_count: number;
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
  /** Role-action headline: WHO did WHAT in sentence form. Leads the detail pane. */
  headline: string;
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
  event_checklist: TaskPlaybackEventChecklist;
  evidence_links: TaskPlaybackEvidenceRef[];
  /** Structured cross-reference links: related events + parent/child backlog rows, each clickable. */
  relation_links: TaskTimelineSemanticRelation[];
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
  driving_frame_id: string;
  reason_sentence: string;
  next_expected_action: string;
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
  close_gate_matrix: GateMatrixProjection;
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
  /(^|[._\s-])(raw_prompt|raw_private_prompt_text|private_prompt|prompt_text|prompt_body|prompt_payload|hidden_prompt|hidden_context|system_prompt|developer_prompt|secret|credential|credentials|password|api_key|access_token|refresh_token|auth_token|one_time_auth|filesystem|cwd|worktree_path|host_path|host_paths|host_home|raw_private_context|private_context|private_context_body|raw_private_route_body|private_route_context_body|private_body|observer_only_context|unmanifested_prompt_text)([._\s-]|$)|(^|[._\s-])token([._\s-]|$)(?!hash)/i;
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
  const closeGateSummary = closeGateSummaryFrom(input.gateResponse);
  const lanes = lanesFromFrames(frames, input.backlog, closeGateSummary);
  const closeGateMatrix = projectGateMatrix(input.gateResponse?.timeline_gate, closeGateSummary.applicable);
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
    close_gate_matrix: closeGateMatrix,
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
  const eventChecklist = eventChecklistFromEvent(publicEvent, status, specificFacts, failureDiagnosis);
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
    headline: semantic.headline,
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
    event_checklist: eventChecklist,
    evidence_links: evidenceLinks,
    relation_links: semantic.relations,
    detail_inspector: playbackInspectorFromEvent(publicEvent, semantic),
    evidence_refs: evidenceRefs,
    artifact_refs: artifactRefs,
    has_structured_detail: specificFacts.length > 0 || failureDiagnosis.length > 0 || eventChecklist.item_count > 0 || evidenceLinks.length > 1,
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

const EVENT_CHECKLIST_MAX_ITEMS = 24;
const EVENT_CHECKLIST_MAX_ITEMS_PER_CATEGORY = 12;

function eventChecklistFromEvent(
  event: TaskTimelineEvent,
  frameStatus: TaskPlaybackFrameStatus,
  specificFacts: TaskPlaybackStructuredFact[],
  failureDiagnosis: TaskPlaybackStructuredFact[],
): TaskPlaybackEventChecklist {
  const items: TaskPlaybackChecklistItem[] = [];
  const typedPaths = new Set<string>();
  const finalBlockingVerdict = isFinalBlockingChecklistVerdict(event, frameStatus);

  for (const fact of failureDiagnosis) {
    const status = checklistStatusFromFact(fact, frameStatus);
    pushChecklistItem(items, fact.kind, fact.label, fact.value, status, fact.source);
  }

  const unmetStatus: TaskPlaybackChecklistItemStatus = finalBlockingVerdict ? "missing" : "pending";
  pushChecklistPathItems(items, event, "missing_event_kinds", "Missing event kind", "missing", [
    "payload.missing_event_kinds",
    "payload.blocked_event_kinds",
    "payload.blocked_protected_event_kinds",
    "verification.missing_event_kinds",
    "verification.blocked_event_kinds",
  ], typedPaths, unmetStatus);
  pushChecklistPathItems(items, event, "missing_requirements", finalBlockingVerdict ? "Missing requirement" : "Pending requirement", unmetStatus, [
    "payload.missing_required_evidence",
    "payload.missing_requirement_ids",
    "payload.missing_protected_lanes",
    "payload.required_missing_evidence",
    "payload.route_context_gate.missing_requirement_ids",
    "payload.route_context_gate.missing_required_evidence",
    "payload.contract_gate.missing_requirement_ids",
    "payload.contract_gate.missing_required_evidence",
    "verification.missing_required_evidence",
    "verification.missing_requirement_ids",
    "verification.required_missing_evidence",
    "verification.route_context_gate.missing_requirement_ids",
    "verification.route_context_gate.missing_required_evidence",
    "verification.contract_gate.missing_requirement_ids",
    "verification.contract_gate.missing_required_evidence",
  ], typedPaths);
  pushChecklistPathItems(items, event, "present_event_kinds", "Present event kind", "present", [
    "payload.present_event_kinds",
    "payload.recorded_event_kinds",
    "payload.satisfied_event_kinds",
    "verification.present_event_kinds",
    "verification.recorded_event_kinds",
    "verification.satisfied_event_kinds",
  ]);
  pushChecklistPathItems(items, event, "satisfied_requirements", "Satisfied requirement", "satisfied", [
    "payload.present_requirement_ids",
    "payload.satisfied_requirement_ids",
    "payload.passed_requirement_ids",
    "payload.present_required_evidence",
    "payload.satisfied_required_evidence",
    "verification.present_requirement_ids",
    "verification.satisfied_requirement_ids",
    "verification.passed_requirement_ids",
    "verification.present_required_evidence",
    "verification.satisfied_required_evidence",
  ], typedPaths);
  pushChecklistPathItems(items, event, "required_event_kinds", "Required event kind", "required", [
    "payload.required_event_kinds",
    "payload.required_before_protected_evidence",
    "verification.required_event_kinds",
    "verification.required_before_protected_evidence",
  ], typedPaths);
  pushChecklistPathItems(items, event, "required_requirements", "Required requirement", "required", [
    "payload.required_evidence",
    "payload.evidence_required",
    "payload.required_requirement_ids",
    "payload.prompt_contract.required_evidence",
    "payload.prompt_contract.evidence_required",
    "verification.required_evidence",
    "verification.evidence_required",
    "verification.required_requirement_ids",
  ], typedPaths);

  const verificationPassed = firstPublicValueAtPaths(event, ["verification.passed", "payload.verification.passed", "payload.passed"]);
  if (verificationPassed) {
    pushChecklistItem(
      items,
      "verification_passed",
      "Verification result",
      verificationPassed.value,
      checklistStatusFromPathValue(verificationPassed.path, verificationPassed.value, finalBlockingVerdict ? "failed" : "recorded", finalBlockingVerdict),
      verificationPassed.source,
    );
    typedPaths.add(verificationPassed.path);
  }

  const factAllowList = new Set([
    "required_evidence",
    "required_lanes_evidence",
    "allowed_actions",
    "blocked_actions",
    "blocker_resolution_gate",
    "cross_ref_gate",
    "stale_route_evidence_gate",
    "surrogate_close_satisfying",
  ]);
  for (const fact of specificFacts) {
    if (!factAllowList.has(fact.kind)) continue;
    pushChecklistItem(items, fact.kind, fact.label, fact.value, checklistStatusFromFact(fact, frameStatus), fact.source);
  }

  pushRouteTokenGateChecklistItems(items, typedPaths, event, finalBlockingVerdict);
  pushMfSubagentStartupGateChecklistItems(items, typedPaths, event, finalBlockingVerdict);
  pushReadReceiptChecklistItems(items, typedPaths, event, finalBlockingVerdict);
  pushImplementationEvidenceChecklistItems(items, typedPaths, event);

  for (const root of CHECKLIST_STRUCTURED_ROOTS) {
    collectChecklistLikeItems(items, valueAtPath(event as unknown as Record<string, unknown>, root.path), root.path, root.label, root.status, finalBlockingVerdict, typedPaths);
  }

  return buildEventChecklist(items);
}

const CHECKLIST_STRUCTURED_ROOTS: Array<{ path: string; label: string; status?: TaskPlaybackChecklistItemStatus }> = [
  { path: "payload.checklist", label: "Checklist" },
  { path: "payload.checks", label: "Checks" },
  { path: "payload.gate", label: "Gate" },
  { path: "payload.route_token_gate", label: "Route token gate" },
  { path: "payload.mf_subagent_startup_gate", label: "MF subagent startup gate" },
  { path: "payload.route_action_gate", label: "Route action gate" },
  { path: "payload.route_context_gate", label: "Route context gate" },
  { path: "payload.contract_gate", label: "Contract gate" },
  { path: "payload.close_gate", label: "Close gate" },
  { path: "payload.contract_evidence", label: "Contract evidence" },
  { path: "payload.matrix", label: "Matrix row" },
  { path: "payload.test_results", label: "Test result" },
  { path: "verification", label: "Verification" },
  { path: "verification.checklist", label: "Verification checklist" },
  { path: "verification.checks", label: "Verification checks" },
  { path: "verification.gate", label: "Verification gate" },
  { path: "verification.contract_evidence", label: "Contract evidence" },
  { path: "verification.matrix", label: "Verification matrix" },
  { path: "verification.test_results", label: "Test result" },
  { path: "artifact_refs.contract_evidence", label: "Contract evidence" },
];

function pushRouteTokenGateChecklistItems(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
  finalBlockingVerdict: boolean,
): void {
  pushFirstChecklistValue(items, typedPaths, event, "route_gate_decision", "Route gate decision", [
    "payload.route_token_gate.decision",
    "verification.route_token_gate.decision",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "route_gate_action", "Route gate action", [
    "payload.route_token_gate.action",
    "verification.route_token_gate.action",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "route_gate_status", "Route gate status", [
    "payload.route_token_gate.status",
    "payload.route_token_gate.result",
    "verification.route_token_gate.status",
    "verification.route_token_gate.result",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "route_gate_binding_source", "Binding source", [
    "payload.route_token_gate.binding_source",
    "payload.route_token_gate.binding.source",
    "verification.route_token_gate.binding_source",
    "verification.route_token_gate.binding.source",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "route_token_ref", "Route token ref", [
    "payload.route_token_gate.route_token_ref",
    "payload.route_token_gate.binding.route_token_ref",
    "payload.route_token_gate.server_binding.route_token_ref",
    "verification.route_token_gate.route_token_ref",
  ], "present", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "route_token_hash", "Route token hash", [
    "payload.route_token_gate.route_token_hash",
    "payload.route_token_gate.binding.route_token_hash",
    "verification.route_token_gate.route_token_hash",
  ], "present", finalBlockingVerdict);
  pushRoutePromptHashChecklistItems(items, typedPaths, event, "payload.route_token_gate", finalBlockingVerdict);
  pushRoutePromptHashChecklistItems(items, typedPaths, event, "verification.route_token_gate", finalBlockingVerdict);
  pushChecklistValues(items, typedPaths, event, "route_gate_server_binding", "Server binding", [
    "payload.route_token_gate.server_binding_ref",
    "payload.route_token_gate.server_binding_id",
    "payload.route_token_gate.server_binding.ref",
    "payload.route_token_gate.server_binding.id",
    "verification.route_token_gate.server_binding_ref",
    "verification.route_token_gate.server_binding_id",
  ], "present", finalBlockingVerdict);
  pushChecklistValues(items, typedPaths, event, "route_gate_hash_verification", "Hash verification", [
    "payload.route_token_gate.route_context_hash_verified",
    "payload.route_token_gate.prompt_contract_hash_verified",
    "payload.route_token_gate.visible_injection_manifest_hash_verified",
    "payload.route_token_gate.route_context_hash_matches",
    "payload.route_token_gate.prompt_contract_hash_matches",
    "payload.route_token_gate.visible_injection_manifest_hash_matches",
    "verification.route_token_gate.route_context_hash_verified",
    "verification.route_token_gate.prompt_contract_hash_verified",
  ], "passed", finalBlockingVerdict);
}

function pushMfSubagentStartupGateChecklistItems(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
  finalBlockingVerdict: boolean,
): void {
  pushChecklistValues(items, typedPaths, event, "startup_gate_state", "Startup gate state", [
    "payload.mf_subagent_startup_gate.startup_complete",
    "payload.mf_subagent_startup_gate.actual_startup_recorded",
    "payload.mf_subagent_startup_gate.passed",
    "verification.mf_subagent_startup_gate.startup_complete",
    "verification.mf_subagent_startup_gate.actual_startup_recorded",
  ], "passed", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "startup_source", "Startup source", [
    "payload.mf_subagent_startup_gate.startup_source",
    "payload.mf_subagent_startup_gate.source",
    "verification.mf_subagent_startup_gate.startup_source",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "worker_role", "Worker role", [
    "payload.mf_subagent_startup_gate.worker_role",
    "payload.worker_role",
    "verification.mf_subagent_startup_gate.worker_role",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "worker_id", "Worker id", [
    "payload.mf_subagent_startup_gate.worker_id",
    "payload.worker_id",
    "verification.mf_subagent_startup_gate.worker_id",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "agent_id_match_mode", "Agent-id match mode", [
    "payload.mf_subagent_startup_gate.agent_id_match_mode",
    "payload.identity_join.agent_id_match_mode",
    "verification.mf_subagent_startup_gate.agent_id_match_mode",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "session_token_evidence_type", "Session token evidence type", [
    "payload.mf_subagent_startup_gate.session_token_evidence_type",
    "payload.session_token_evidence_type",
    "verification.mf_subagent_startup_gate.session_token_evidence_type",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "close_satisfying", "Close satisfying", [
    "payload.mf_subagent_startup_gate.close_satisfying",
    "payload.close_satisfying",
    "verification.mf_subagent_startup_gate.close_satisfying",
  ], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "branch_ref", "Branch ref", [
    "payload.mf_subagent_startup_gate.branch_ref",
    "payload.branch_ref",
    "verification.mf_subagent_startup_gate.branch_ref",
  ], "present", finalBlockingVerdict);
  pushRoutePromptHashChecklistItems(items, typedPaths, event, "payload.mf_subagent_startup_gate", finalBlockingVerdict);
  pushRoutePromptHashChecklistItems(items, typedPaths, event, "verification.mf_subagent_startup_gate", finalBlockingVerdict);
  pushReadReceiptBindingChecklistItems(items, typedPaths, event, "payload.mf_subagent_startup_gate", finalBlockingVerdict);
  pushReadReceiptBindingChecklistItems(items, typedPaths, event, "verification.mf_subagent_startup_gate", finalBlockingVerdict);
  pushWorkerScopeChecklistItems(items, typedPaths, event, "payload.mf_subagent_startup_gate", finalBlockingVerdict);
  pushWorkerScopeChecklistItems(items, typedPaths, event, "verification.mf_subagent_startup_gate", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "startup_server_binding", "Server binding", [
    "payload.mf_subagent_startup_gate.server_binding_ref",
    "payload.mf_subagent_startup_gate.server_binding_id",
    "payload.mf_subagent_startup_gate.server_binding.ref",
    "payload.mf_subagent_startup_gate.server_binding.id",
  ], "present", finalBlockingVerdict);
}

function pushReadReceiptChecklistItems(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
  finalBlockingVerdict: boolean,
): void {
  pushRoutePromptHashChecklistItems(items, typedPaths, event, "payload", finalBlockingVerdict);
  pushRoutePromptHashChecklistItems(items, typedPaths, event, "verification", finalBlockingVerdict);
  pushReadReceiptBindingChecklistItems(items, typedPaths, event, "payload", finalBlockingVerdict);
  pushReadReceiptBindingChecklistItems(items, typedPaths, event, "verification", finalBlockingVerdict);
  pushWorkerScopeChecklistItems(items, typedPaths, event, "payload", finalBlockingVerdict);
  pushWorkerScopeChecklistItems(items, typedPaths, event, "verification", finalBlockingVerdict);
  pushChecklistValues(items, typedPaths, event, "read_receipt_ordering", "Read ordering", [
    "payload.read_before",
    "payload.read_before_startup",
    "payload.read_before_dispatch",
    "payload.read_before_implementation",
    "payload.read_ordering",
    "payload.read_receipt_ordering",
    "verification.read_before",
    "verification.read_before_startup",
    "verification.read_ordering",
  ], "recorded", finalBlockingVerdict);
  pushChecklistValues(items, typedPaths, event, "read_receipt_ack", "Read receipt acknowledgement", [
    "payload.acknowledged_stop_state",
    "payload.acknowledged_forbidden_actions",
    "payload.acknowledged_owned_files",
    "verification.acknowledged_stop_state",
    "verification.acknowledged_forbidden_actions",
  ], "recorded", finalBlockingVerdict);
}

function pushImplementationEvidenceChecklistItems(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
): void {
  pushChecklistValues(items, typedPaths, event, "changed_file", "Changed file", [
    "payload.changed_files",
    "payload.modified_files",
    "payload.updated_files",
    "verification.changed_files",
    "artifact_refs.changed_files",
  ], "present", false);
  pushFirstChecklistValue(items, typedPaths, event, "commit_sha", "Commit SHA", [
    "commit_sha",
    "payload.commit_sha",
    "payload.commit",
    "verification.commit_sha",
    "artifact_refs.commit_sha",
  ], "present", false);
  pushFirstChecklistValue(items, typedPaths, event, "worker_precommit_trace", "Worker precommit trace", [
    "payload.worker_reported_precommit_trace",
    "payload.worker_reported_precommit_trace_id",
    "payload.worker_precommit_trace",
    "payload.worker_precommit_trace_id",
    "payload.precommit_trace_id",
    "verification.worker_reported_precommit_trace",
    "artifact_refs.worker_reported_precommit_trace",
  ], "present", false);
}

function pushRoutePromptHashChecklistItems(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
  root: string,
  finalBlockingVerdict: boolean,
): void {
  pushFirstChecklistValue(items, typedPaths, event, "route_id", "Route id", [`${root}.route_id`, `${root}.route_identity.route_id`], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "route_context_hash", "Route context hash", [`${root}.route_context_hash`, `${root}.route_identity.route_context_hash`], "present", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "prompt_contract_id", "Prompt contract id", [`${root}.prompt_contract_id`, `${root}.route_identity.prompt_contract_id`], "recorded", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "prompt_contract_hash", "Prompt contract hash", [`${root}.prompt_contract_hash`, `${root}.route_identity.prompt_contract_hash`], "present", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "visible_injection_manifest_hash", "Visible injection manifest", [`${root}.visible_injection_manifest_hash`, `${root}.route_identity.visible_injection_manifest_hash`], "present", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "launch_text_hash", "Launch text hash", [`${root}.launch_text_hash`, `${root}.route_identity.launch_text_hash`], "present", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "canonical_visible_contract_text_hash", "Visible contract text hash", [`${root}.canonical_visible_contract_text_hash`, `${root}.visible_contract_text_hash`], "present", finalBlockingVerdict);
}

function pushReadReceiptBindingChecklistItems(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
  root: string,
  finalBlockingVerdict: boolean,
): void {
  pushFirstChecklistValue(items, typedPaths, event, "read_receipt_event_id", "Read receipt event", [
    `${root}.read_receipt_event_id`,
    `${root}.read_receipt_event_ref`,
  ], "present", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "read_receipt_hash", "Read receipt hash", [`${root}.read_receipt_hash`], "present", finalBlockingVerdict);
}

function pushWorkerScopeChecklistItems(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
  root: string,
  finalBlockingVerdict: boolean,
): void {
  pushChecklistValues(items, typedPaths, event, "owned_file", "Owned file", [`${root}.owned_files`, `${root}.target_files`], "present", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "base_commit", "Base commit", [`${root}.base_commit`, `${root}.target_head_commit`], "present", finalBlockingVerdict);
  pushFirstChecklistValue(items, typedPaths, event, "head_commit", "Head commit", [`${root}.head_commit`, `${root}.branch_head`], "present", finalBlockingVerdict);
}

function pushFirstChecklistValue(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
  kind: string,
  label: string,
  paths: string[],
  fallbackStatus: TaskPlaybackChecklistItemStatus,
  finalBlockingVerdict: boolean,
): void {
  const value = firstPublicValueAtPaths(event, paths);
  if (!value) return;
  pushChecklistItem(
    items,
    kind,
    label,
    value.value,
    checklistStatusFromPathValue(value.path, value.value, fallbackStatus, finalBlockingVerdict),
    value.source,
  );
  typedPaths.add(value.path);
}

function pushChecklistValues(
  items: TaskPlaybackChecklistItem[],
  typedPaths: Set<string>,
  event: TaskTimelineEvent,
  kind: string,
  label: string,
  paths: string[],
  fallbackStatus: TaskPlaybackChecklistItemStatus,
  finalBlockingVerdict: boolean,
): void {
  for (const value of publicValuesAtPaths(event, paths).slice(0, 12)) {
    pushChecklistItem(
      items,
      kind,
      label,
      value.value,
      checklistStatusFromPathValue(value.path, value.value, fallbackStatus, finalBlockingVerdict),
      value.source,
    );
    typedPaths.add(value.path);
  }
}

function pushChecklistPathItems(
  items: TaskPlaybackChecklistItem[],
  event: TaskTimelineEvent,
  kind: string,
  label: string,
  status: TaskPlaybackChecklistItemStatus,
  paths: string[],
  typedPaths?: Set<string>,
  overrideStatus?: TaskPlaybackChecklistItemStatus,
): void {
  for (const item of publicValuesAtPaths(event, paths)) {
    pushChecklistItem(items, kind, label, item.value, overrideStatus ?? status, item.source);
    typedPaths?.add(item.path);
  }
}

function collectChecklistLikeItems(
  items: TaskPlaybackChecklistItem[],
  value: unknown,
  path: string,
  fallbackLabel: string,
  inheritedStatus: TaskPlaybackChecklistItemStatus = "recorded",
  finalBlockingVerdict = false,
  skipPaths: Set<string> = new Set(),
  depth = 0,
): void {
  if (items.length >= EVENT_CHECKLIST_MAX_ITEMS * 2 || depth > 4 || value == null || value === "") return;
  if (shouldSkipChecklistPath(path, skipPaths)) return;
  if (isSensitiveEvidencePath(path)) return;
  const pathStatus = checklistStatusFromPath(path, inheritedStatus);
  if (Array.isArray(value)) {
    value.slice(0, 12).forEach((item, index) => {
      collectChecklistLikeItems(items, item, `${path}.${index}`, fallbackLabel, pathStatus, finalBlockingVerdict, skipPaths, depth + 1);
    });
    return;
  }
  if (typeof value !== "object") {
    const text = safeText(String(value));
    if (text) pushChecklistItem(items, path, fallbackLabel, text, checklistStatusFromPathValue(path, text, pathStatus, finalBlockingVerdict), sourceForPath(path));
    return;
  }

  const record = value as Record<string, unknown>;
  const recordItem = checklistItemFromRecord(record, path, fallbackLabel, pathStatus, finalBlockingVerdict);
  if (recordItem) items.push(recordItem);

  for (const [key, item] of Object.entries(record).slice(0, 32)) {
    const childPath = `${path}.${key}`;
    if (shouldSkipChecklistPath(childPath, skipPaths)) continue;
    if (isSensitiveEvidencePath(childPath)) continue;
    const childStatus = checklistStatusFromPath(childPath, pathStatus);
    if (Array.isArray(item)) {
      if (isChecklistSignalKey(key)) {
        for (const valueItem of item.slice(0, 10)) {
          if (valueItem && typeof valueItem === "object") {
            collectChecklistLikeItems(items, valueItem, childPath, titleize(key), childStatus, finalBlockingVerdict, skipPaths, depth + 1);
          } else {
            const text = safeText(String(valueItem ?? ""));
            if (text) pushChecklistItem(items, childPath, titleize(key), text, checklistStatusFromPathValue(childPath, text, childStatus, finalBlockingVerdict), sourceForPath(childPath));
          }
        }
      }
      continue;
    }
    if (item && typeof item === "object") {
      if (isChecklistSignalKey(key)) collectChecklistLikeItems(items, item, childPath, titleize(key), childStatus, finalBlockingVerdict, skipPaths, depth + 1);
      continue;
    }
    if (!isChecklistSignalKey(key)) continue;
    const text = safeText(String(item ?? ""));
    if (text) pushChecklistItem(items, childPath, titleize(key), text, checklistStatusFromPathValue(childPath, text, childStatus, finalBlockingVerdict), sourceForPath(childPath));
  }
}

function checklistItemFromRecord(
  record: Record<string, unknown>,
  path: string,
  fallbackLabel: string,
  fallbackStatus: TaskPlaybackChecklistItemStatus,
  finalBlockingVerdict: boolean,
): TaskPlaybackChecklistItem | null {
  const label =
    firstStringField(record, ["label", "name", "title", "requirement_id", "requirement", "event_kind", "kind", "check", "test", "id"])
    || fallbackLabel;
  const statusValue = firstStringField(record, ["status", "result", "decision", "state", "outcome"])
    || stringFrom(record.passed)
    || stringFrom(record.satisfied)
    || stringFrom(record.present)
    || stringFrom(record.required);
  const detail =
    firstStringField(record, ["value", "reason", "summary", "message", "next_action", "next_expected_action", "evidence", "event_id"])
    || statusValue
    || compactUnknown(record);
  const safeLabel = safeText(label);
  const safeDetail = safeText(detail);
  if (!safeLabel || !safeDetail || safeDetail === "record") return null;
  return {
    id: checklistItemId(path, safeLabel, safeDetail),
    label: safeLabel,
    value: safeDetail,
    status: checklistStatusFromPathValue(path, statusValue || safeDetail, checklistStatusFromPath(path, fallbackStatus), finalBlockingVerdict),
    source: sourceForPath(path),
  };
}

function shouldSkipChecklistPath(path: string, skipPaths: Set<string>): boolean {
  if (skipPaths.size === 0) return false;
  return Array.from(skipPaths).some((skipPath) => path === skipPath || path.startsWith(`${skipPath}.`));
}

function isChecklistSignalKey(key: string): boolean {
  return /(checklist|checks?|gate|verification|contract_evidence|evidence|requirements?|event_kinds?|missing|blocked|failed|passed|satisfied|present|required|status|decision|result|tests?|matrix|rows?)/i.test(key);
}

function checklistStatusFromFact(fact: TaskPlaybackStructuredFact, frameStatus: TaskPlaybackFrameStatus): TaskPlaybackChecklistItemStatus {
  if (isNeutralChecklistDeclarationKey(fact.kind) || isNeutralChecklistDeclarationKey(fact.label)) return "recorded";
  return checklistStatusFromValue(`${fact.kind} ${fact.label} ${fact.value}`, checklistStatusFromFrameStatus(frameStatus));
}

function checklistStatusFromFrameStatus(status: TaskPlaybackFrameStatus): TaskPlaybackChecklistItemStatus {
  if (status === "passed") return "passed";
  if (status === "blocked" || status === "failed" || status === "missing") return status;
  if (status === "recorded" || status === "running" || status === "waiting") return "recorded";
  return "unknown";
}

function checklistStatusFromPath(path: string, fallback: TaskPlaybackChecklistItemStatus): TaskPlaybackChecklistItemStatus {
  const text = path.toLowerCase();
  if (isNeutralChecklistDeclarationPath(text)) return "recorded";
  if (/missing|required_missing|unmet/.test(text)) return "missing";
  if (/blocked|forbidden/.test(text)) return "blocked";
  if (/failed|failure|error/.test(text)) return "failed";
  if (/passed|satisfied|complete|completed|accepted|allowed/.test(text)) return "passed";
  if (/present|recorded/.test(text)) return "present";
  if (/pending|not_yet|not-yet/.test(text)) return "pending";
  if (/required/.test(text)) return "required";
  return fallback;
}

function checklistStatusFromValue(value: string, fallback: TaskPlaybackChecklistItemStatus): TaskPlaybackChecklistItemStatus {
  return checklistStatusFromPathValue("", value, fallback, false);
}

function checklistStatusFromPathValue(
  path: string,
  value: string,
  fallback: TaskPlaybackChecklistItemStatus,
  finalBlockingVerdict: boolean,
): TaskPlaybackChecklistItemStatus {
  const normalizedPath = path.toLowerCase();
  if (isNeutralChecklistDeclarationPath(normalizedPath)) return "recorded";
  const text = safeText(value).toLowerCase();
  if (/pending|not yet|not-yet|not_yet|not due|awaiting|queued/.test(text)) return "pending";
  if (/missing|required-but-unmet|required_but_unmet|unmet|not recorded|not been recorded|not present/.test(text)) return finalBlockingVerdict ? "missing" : "pending";
  if (/blocked|forbidden|not allowed|refused|rejected/.test(text)) return finalBlockingVerdict ? "blocked" : fallback;
  if (/failed|failure|error/.test(text)) return finalBlockingVerdict ? "failed" : fallback;
  if (/passed|satisfied|accepted|allowed|complete|completed|success|true|yes\b|ok\b/.test(text)) return "passed";
  if (/present|recorded|exists/.test(text)) return "present";
  if (!finalBlockingVerdict && isStageRelativeRequirementPath(normalizedPath) && ["missing", "blocked", "failed"].includes(fallback)) return "pending";
  if (/required|must|expected/.test(text)) return "required";
  if (/\bfalse\b|\bno\b/.test(text)) return fallback === "passed" ? "recorded" : fallback;
  return fallback;
}

function isFinalBlockingChecklistVerdict(event: TaskTimelineEvent, frameStatus: TaskPlaybackFrameStatus): boolean {
  if (!["blocked", "failed", "missing"].includes(frameStatus)) return false;
  const text = [
    event.status,
    event.event_type,
    event.event_kind,
    event.phase,
    stringFrom(valueAtPath(event as unknown as Record<string, unknown>, "payload.decision")),
    stringFrom(valueAtPath(event as unknown as Record<string, unknown>, "payload.route_token_gate.decision")),
    stringFrom(valueAtPath(event as unknown as Record<string, unknown>, "payload.route_action_gate.decision")),
    stringFrom(valueAtPath(event as unknown as Record<string, unknown>, "payload.close_gate.status")),
    stringFrom(valueAtPath(event as unknown as Record<string, unknown>, "verification.decision")),
    stringFrom(valueAtPath(event as unknown as Record<string, unknown>, "verification.status")),
  ].map((item) => safeText(String(item || "")).toLowerCase()).join(" ");
  if (/blocked|failed|failure|missing|refused|rejected|denied|not allowed/.test(text)) return true;
  return ["blocked", "failed", "missing"].includes(frameStatus);
}

function isNeutralChecklistDeclarationPath(path: string): boolean {
  const leaf = path.split(".").pop() ?? path;
  return isNeutralChecklistDeclarationKey(leaf)
    || /(^|[._-])(allowed_actions|blocked_actions|forbidden_actions|acknowledged_forbidden_actions)([._-]|$)/.test(path);
}

function isNeutralChecklistDeclarationKey(key: string): boolean {
  const normalized = key.toLowerCase().replace(/\s+/g, "_");
  return /(^|_)(counts_as_close_evidence|close_satisfying|surrogate_close_satisfying|session_token_evidence_type|session_token_type|session_token_surrogate|surrogate_startup|is_surrogate|agent_id_match_mode|allowed_actions|blocked_actions|forbidden_actions|acknowledged_forbidden_actions)$/.test(normalized)
    || /(^|_)(surrogate|session)_/.test(normalized);
}

function isStageRelativeRequirementPath(path: string): boolean {
  return /(missing|required_missing|unmet|required_before|blocked_event_kinds|missing_requirement_ids|missing_event_kinds|required_lanes|missing_protected_lanes)/.test(path);
}

function pushChecklistItem(
  items: TaskPlaybackChecklistItem[],
  kind: string,
  label: string,
  value: string,
  status: TaskPlaybackChecklistItemStatus,
  source: TaskPlaybackStructuredFact["source"],
): void {
  const safeLabel = safeText(label);
  const safeValue = safeText(value);
  if (!safeLabel || !safeValue || safeValue === "[private detail redacted]") return;
  items.push({
    id: checklistItemId(kind, safeLabel, safeValue),
    label: safeLabel,
    value: safeValue,
    status,
    source,
  });
}

function buildEventChecklist(items: TaskPlaybackChecklistItem[]): TaskPlaybackEventChecklist {
  const stableItems = stableChecklistItems(items);
  const groups: Record<TaskPlaybackChecklistCategory["id"], TaskPlaybackChecklistItem[]> = {
    unmet: [],
    passed: [],
    required: [],
    recorded: [],
  };
  for (const item of stableItems) {
    groups[checklistCategoryId(item.status)].push(item);
  }

  const categorySpecs: Array<{ id: TaskPlaybackChecklistCategory["id"]; label: string; status: TaskPlaybackChecklistItemStatus }> = [
    { id: "unmet", label: "Missing / blocked / failed", status: "blocked" },
    { id: "passed", label: "Passed / satisfied", status: "passed" },
    { id: "required", label: "Required / pending", status: "required" },
    { id: "recorded", label: "Recorded checks", status: "recorded" },
  ];
  let visibleCount = 0;
  const categories: TaskPlaybackChecklistCategory[] = [];
  for (const spec of categorySpecs) {
    const remainingBudget = Math.max(0, EVENT_CHECKLIST_MAX_ITEMS - visibleCount);
    const categoryItems = groups[spec.id].slice(0, Math.min(EVENT_CHECKLIST_MAX_ITEMS_PER_CATEGORY, remainingBudget));
    if (categoryItems.length === 0) continue;
    visibleCount += categoryItems.length;
    categories.push({ ...spec, items: categoryItems });
  }
  const blockedCount = stableItems.filter((item) => ["missing", "blocked", "failed"].includes(item.status)).length;
  const passedCount = stableItems.filter((item) => ["passed", "satisfied", "present"].includes(item.status)).length;
  return {
    categories,
    item_count: stableItems.length,
    hidden_count: Math.max(0, stableItems.length - visibleCount),
    blocked_count: blockedCount,
    passed_count: passedCount,
  };
}

function checklistCategoryId(status: TaskPlaybackChecklistItemStatus): TaskPlaybackChecklistCategory["id"] {
  if (status === "missing" || status === "blocked" || status === "failed") return "unmet";
  if (status === "passed" || status === "satisfied" || status === "present") return "passed";
  if (status === "required" || status === "pending") return "required";
  return "recorded";
}

function stableChecklistItems(items: TaskPlaybackChecklistItem[]): TaskPlaybackChecklistItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = `${item.label}:${item.value}:${item.status}`;
    if (!item.value || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function checklistItemId(kind: string, label: string, value: string): string {
  return safeText(`${kind}:${label}:${value}`).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 96);
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

function lanesFromFrames(
  frames: TaskPlaybackFrame[],
  backlog: BacklogBug,
  closeGateSummary: TaskPlaybackCloseGateSummary,
): TaskPlaybackLane[] {
  const grouped = new Map<string, TaskPlaybackFrame[]>();
  for (const frame of frames) grouped.set(frame.lane_id, [...(grouped.get(frame.lane_id) ?? []), frame]);
  return Array.from(grouped.entries())
    .map(([id, laneFrames]) => {
      const latest = laneFrames[laneFrames.length - 1];
      const status = normalizeLaneSummaryStatus(
        id,
        laneCurrentStatus(laneFrames),
        backlog,
        closeGateSummary,
      );
      const drivingFrame = drivingFrameForLane(laneFrames, status) ?? latest;
      return {
        id,
        label: laneLabel(id),
        family: laneFamily(id),
        status,
        frame_count: laneFrames.length,
        latest_at: latest?.at || "",
        driving_frame_id: drivingFrame?.id || "",
        reason_sentence: laneReasonSentence(drivingFrame, status),
        next_expected_action: laneNextExpectedAction(drivingFrame, status),
      };
    })
    .sort((a, b) => laneSort(a.id) - laneSort(b.id) || a.id.localeCompare(b.id));
}

function laneCurrentStatus(frames: TaskPlaybackFrame[]): TaskPlaybackFrameStatus {
  const latestTerminalIndex = findLastFrameIndex(frames, (frame) => isTerminalFrameStatus(frame.status));
  const latestBlockerIndex = findLastFrameIndex(frames, (frame) => isBlockingFrameStatus(frame.status));
  if (latestBlockerIndex >= 0 && latestBlockerIndex > latestTerminalIndex) return frames[latestBlockerIndex].status;
  const latestMeaningful = findLastFrame(frames, (frame) => frame.status !== "unknown");
  return latestMeaningful?.status ?? aggregateStatus(frames.map((frame) => frame.status));
}

function normalizeLaneSummaryStatus(
  laneId: string,
  status: TaskPlaybackFrameStatus,
  backlog: BacklogBug,
  closeGateSummary: TaskPlaybackCloseGateSummary,
): TaskPlaybackFrameStatus {
  if (laneId === "gate" && closeGateSummary.applicable === false) return "recorded";
  if (isTerminalBacklogStatus(backlog.status) && (status === "running" || status === "waiting")) return "recorded";
  return status;
}

function drivingFrameForLane(frames: TaskPlaybackFrame[], status: TaskPlaybackFrameStatus): TaskPlaybackFrame | undefined {
  if (isBlockingFrameStatus(status)) {
    return findLastFrame(frames, (frame) => frame.status === status && hasDrivingReason(frame))
      ?? findLastFrame(frames, (frame) => isBlockingFrameStatus(frame.status) && hasDrivingReason(frame))
      ?? findLastFrame(frames, (frame) => frame.status === status)
      ?? findLastFrame(frames, (frame) => isBlockingFrameStatus(frame.status));
  }
  return findLastFrame(frames, (frame) => frame.status === status)
    ?? findLastFrame(frames, (frame) => frame.status !== "unknown")
    ?? frames[frames.length - 1];
}

function laneReasonSentence(frame: TaskPlaybackFrame | undefined, status: TaskPlaybackFrameStatus): string {
  if (!frame || !["blocked", "failed", "missing"].includes(status)) return "";
  const diagnosis =
    factValue(frame.failure_diagnosis, "stale_timeout_reason")
    || factValue(frame.failure_diagnosis, "missing_required_evidence")
    || factValue(frame.failure_diagnosis, "missing_event_kinds")
    || factValue(frame.failure_diagnosis, "blocker_ids")
    || factValue(frame.failure_diagnosis, "mismatched_route_identity")
    || factValue(frame.failure_diagnosis, "remaining_acceptance")
    || factValue(frame.failure_diagnosis, "remaining_open");
  if (diagnosis) return diagnosis;
  return frame.summary || `${frame.title} recorded ${status}.`;
}

function laneNextExpectedAction(frame: TaskPlaybackFrame | undefined, status: TaskPlaybackFrameStatus): string {
  if (!frame || !["blocked", "failed", "missing"].includes(status)) return "";
  return factValue(frame.failure_diagnosis, "next_legal_action") || frame.narrative.outcome || "";
}

function hasDrivingReason(frame: TaskPlaybackFrame): boolean {
  return Boolean(laneReasonSentence(frame, frame.status) || laneNextExpectedAction(frame, frame.status));
}

function isBlockingFrameStatus(status: TaskPlaybackFrameStatus): boolean {
  return status === "blocked" || status === "failed" || status === "missing";
}

function isTerminalFrameStatus(status: TaskPlaybackFrameStatus): boolean {
  return status === "passed" || status === "recorded";
}

function isTerminalBacklogStatus(status?: string): boolean {
  return /^(closed|fixed|done|complete|completed|resolved|merged)$/i.test(safeText(status ?? ""));
}

function findLastFrame(frames: TaskPlaybackFrame[], predicate: (frame: TaskPlaybackFrame) => boolean): TaskPlaybackFrame | undefined {
  const index = findLastFrameIndex(frames, predicate);
  return index >= 0 ? frames[index] : undefined;
}

function findLastFrameIndex(frames: TaskPlaybackFrame[], predicate: (frame: TaskPlaybackFrame) => boolean): number {
  for (let index = frames.length - 1; index >= 0; index -= 1) {
    if (predicate(frames[index])) return index;
  }
  return -1;
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
  const notApplicable = response.applicable === false;
  return {
    applicable: Boolean(response.applicable),
    can_close: Boolean(response.can_close),
    status: notApplicable ? "recorded" : blocked ? "blocked" : gate.passed || response.can_close ? "passed" : "recorded",
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
  if (path === "payload") return "payload";
  if (path === "verification") return "verification";
  if (path === "artifact_refs") return "artifact_refs";
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
  if (leaf === "session_token_evidence_type") return false;
  if (leaf === "route_token_gate") return false;
  if (normalized.includes("route_token_gate.") && PUBLIC_ROUTE_TOKEN_GATE_LEAVES.has(leaf)) return false;
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

const PUBLIC_ROUTE_TOKEN_GATE_LEAVES = new Set([
  "action",
  "allowed",
  "binding_source",
  "decision",
  "passed",
  "prompt_contract_hash_matches",
  "prompt_contract_hash_verified",
  "result",
  "route_context_hash_matches",
  "route_context_hash_verified",
  "route_token_ref",
  "server_binding_id",
  "server_binding_ref",
  "status",
  "visible_injection_manifest_hash_matches",
  "visible_injection_manifest_hash_verified",
]);

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

// ---------------------------------------------------------------------------
// Pure display helpers — extracted from TaskPlaybackPanel so they are
// unit-testable without React (AC-PLAYBACK-SEMANTICS-TEST-COVERAGE-20260610).
// The component still owns all UI state; these are the pure, side-effect-free
// computations that the component delegates to.
// ---------------------------------------------------------------------------

/** Lightweight nav-stack entry used by pushPlaybackNavStack / popPlaybackNavStack. */
export interface PlaybackNavEntry {
  frameId: string;
  label: string;
}

/**
 * Return the frames in display order.
 *
 * In newestFirst mode the underlying `frames` array is oldest-first (ascending
 * created_at order), so the display list is reversed so the newest event
 * appears at the top.  This is a pure function — it does not mutate the input.
 */
export function displayPlaybackFrames(
  frames: TaskPlaybackFrame[],
  newestFirst: boolean,
): TaskPlaybackFrame[] {
  return newestFirst ? [...frames].reverse() : frames;
}

/**
 * Return the id of the latest (newest) frame, i.e. `frames[frames.length - 1].id`,
 * or "" when the array is empty.
 *
 * In the underlying data model frames are always stored oldest-first, so the
 * newest event is the last element regardless of the newestFirst display flag.
 */
export function latestPlaybackFrameId(frames: TaskPlaybackFrame[]): string {
  return frames.length > 0 ? frames[frames.length - 1].id : "";
}

/**
 * Push a new entry onto the nav stack, keeping at most 10 entries total.
 *
 * Returns a new array (pure, does not mutate the input).
 */
export function pushPlaybackNavStack(
  stack: PlaybackNavEntry[],
  entry: PlaybackNavEntry,
): PlaybackNavEntry[] {
  // Keep the 9 most-recent existing entries + the new one = 10 total.
  return [...stack.slice(-9), entry];
}

/**
 * Pop the top entry from the nav stack.
 *
 * Returns `{ entry, stack }` where `entry` is the popped value (or null when
 * the stack was empty) and `stack` is the new array without that entry.
 */
export function popPlaybackNavStack(
  stack: PlaybackNavEntry[],
): { entry: PlaybackNavEntry | null; stack: PlaybackNavEntry[] } {
  if (stack.length === 0) return { entry: null, stack: [] };
  return {
    entry: stack[stack.length - 1],
    stack: stack.slice(0, -1),
  };
}

// ---------------------------------------------------------------------------
// Activity event card helpers (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES)
// ---------------------------------------------------------------------------

/** One card in the Current-tab event card list. */
export interface ActivityEventCard {
  id: number | string;
  at: string;
  event_kind: string;
  event_type: string;
  status: string;
  actor: string;
  backlog_id: string;
  task_id: string;
  /** One-line semantic headline. */
  headline: string;
  /** Count of evidence_links in the projected frame. */
  evidence_count: number;
  /** Evidence kind types present (deduplicated). */
  evidence_types: string[];
}

/**
 * Project a raw TaskTimelineEvent to an ActivityEventCard for the Current tab
 * event card list.
 */
export function projectEventToCard(event: TaskTimelineEvent): ActivityEventCard {
  const publicEvent = hydrateTimelineEventJson(event);
  const id = typeof publicEvent.id === "number" ? publicEvent.id : String(publicEvent.id ?? "");
  const at = (publicEvent.created_at ?? "").trim();
  const event_kind = (publicEvent.event_kind ?? publicEvent.event_type ?? "event").trim();
  const event_type = (publicEvent.event_type ?? "").trim();
  const status = (publicEvent.status ?? "unknown").trim();
  const actor = String(publicEvent.actor ?? "").trim();
  const backlog_id = firstPublicValueAtPaths(publicEvent, [
    "backlog_id",
    "payload.backlog_id",
    "payload.bug_id",
    "payload.root_backlog_id",
    "payload.root_backlog_ids",
    "payload.backlog_ids",
    "verification.backlog_id",
    "artifact_refs.backlog_id",
  ])?.value ?? "";
  const task_id = firstPublicValueAtPaths(publicEvent, [
    "task_id",
    "payload.task_id",
    "payload.stage_task_id",
    "verification.task_id",
    "artifact_refs.task_id",
  ])?.value ?? "";
  // Build a headline from the semantic projection helper; fall back to compact
  // event_kind / status text when the projection is unavailable.
  let headline = "";
  try {
    const projection = projectTaskTimelineEvent(publicEvent, 0);
    headline = projection.headline || projection.title || "";
  } catch {
    headline = `${event_kind}${status ? ` — ${status}` : ""}`;
  }
  if (!headline) headline = `${event_kind}${status ? ` — ${status}` : ""}`;
  // Collect evidence types from the projected frame when available.
  // F1 (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611):
  // The card's evidence_count must NOT include the mandatory self-link
  // (kind="timeline_event" whose value is the event's own display id —
  // e.g. "#9001" or "recorded").  Showing "1 evidence" for a minimal
  // event that carries no real references is misleading.  We count only
  // non-self references (where value ≠ eventDisplayId of this event).
  // The self-link stays in frame.evidence_links for the panel inspector
  // so the raw event id is always clickable; it is just excluded from
  // the card count so "0 refs" means "no references beyond itself".
  let evidence_count = 0;
  const evidence_types: string[] = [];
  try {
    const frame = frameFromEventPublic(publicEvent, 0);
    // Exclude any evidence link whose value equals the event's own display id
    // (self-references: kind=timeline_event + kind=gate both get value=source_event_id
    // on minimal events via the semantic evidence pipeline).  Self-reference links
    // are always inspectable in the panel; they must not inflate the card count.
    const selfId = frame.source_event_id; // e.g. "#9001" or "recorded"
    const nonSelfLinks = frame.evidence_links.filter(
      (ref) => ref.value !== selfId,
    );
    evidence_count = nonSelfLinks.length;
    const seen = new Set<string>();
    for (const ref of nonSelfLinks) {
      if (!seen.has(ref.kind)) { seen.add(ref.kind); evidence_types.push(ref.kind); }
    }
  } catch { /* no-op */ }
  return { id, at, event_kind, event_type, status, actor, backlog_id, task_id, headline, evidence_count, evidence_types };
}

/**
 * Slice a page out of an event array (zero-indexed page, size is items-per-page).
 * Returns `{ items, totalPages, page }`.
 */
export function sliceEventPage<T>(
  items: T[],
  page: number,
  pageSize: number,
): { items: T[]; totalPages: number; page: number } {
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const safePage = Math.max(0, Math.min(page, totalPages - 1));
  const start = safePage * pageSize;
  return { items: items.slice(start, start + pageSize), totalPages, page: safePage };
}

/**
 * Truncate a sha256/sha-prefixed hash to the form `sha256:xxxx…yyyy` for
 * compact display. Non-hash strings are returned unchanged.
 *
 * Format: prefix + first 4 hex chars + `…` + last 4 hex chars.
 * Full value should be shown only in click-to-copy / Advanced raw data.
 */
export function truncateHash(value: string): string {
  if (!value) return value;
  const trimmed = value.trim();
  const match = /^(sha256:|sha512:|sha1:)?([0-9a-f]{8,})$/i.exec(trimmed);
  if (!match) return trimmed;
  const prefix = match[1] ?? "sha256:";
  const hex = match[2];
  if (hex.length <= 12) return trimmed;
  return `${prefix}${hex.slice(0, 4)}…${hex.slice(-4)}`;
}

/**
 * Reference category mapping for the merged "References & Evidence" section.
 * Maps an evidence ref `kind` to one of 6 typed categories.
 */
export type ReferenceCategory =
  | "timeline_events"
  | "backlog_and_task"
  | "route_and_prompt"
  | "gate_and_verification"
  | "commit_and_artifact"
  | "graph_and_trace";

export function categorizeEvidenceRef(kind: TaskPlaybackEvidenceRef["kind"]): ReferenceCategory {
  switch (kind) {
    case "timeline_event":
    case "source_event":
      return "timeline_events";
    case "gate":
    case "precheck":
      return "gate_and_verification";
    case "route_context":
    case "read_receipt":
    case "prompt_contract":
      return "route_and_prompt";
    case "commit":
    case "file":
    case "test":
    case "artifact":
      return "commit_and_artifact";
    case "graph_trace":
    case "node":
      return "graph_and_trace";
    case "content_sys":
    default:
      // Backlog/task refs are detected by value pattern (AC-…/task-…).
      return "backlog_and_task";
  }
}

const PLAYBACK_BACKLOG_REF_VALUE = /^(AC-|task-|cmd-|mq-)/i;
const PLAYBACK_HASH_REF_VALUE = /^(sha256:|sha512:|sha1:)?[0-9a-f]{24,}$/i;
const PLAYBACK_EVENTISH_REF_VALUE = /^(#?\d+|evt[-_:]|event[-_:])/i;

export function isPlaybackBacklogRefValue(value: string): boolean {
  return PLAYBACK_BACKLOG_REF_VALUE.test(value.trim());
}

export function isPlaybackEventEvidenceRef(ref: Pick<TaskPlaybackEvidenceRef, "kind" | "label" | "value">): boolean {
  const value = (ref.value ?? "").trim();
  if (!value || isPlaybackBacklogRefValue(value) || PLAYBACK_HASH_REF_VALUE.test(value)) return false;
  if (ref.kind === "timeline_event" || ref.kind === "source_event") return true;
  if (ref.kind === "read_receipt") return PLAYBACK_EVENTISH_REF_VALUE.test(value) || /\bevent\b/i.test(ref.label);
  return false;
}

/**
 * Group evidence refs into the 6 typed categories.
 * Refs with values matching backlog/task id patterns are promoted to
 * `backlog_and_task` regardless of their `kind`.
 */
export function groupEvidenceRefsByCategory(
  refs: TaskPlaybackEvidenceRef[],
): Record<ReferenceCategory, TaskPlaybackEvidenceRef[]> {
  const result: Record<ReferenceCategory, TaskPlaybackEvidenceRef[]> = {
    timeline_events: [],
    backlog_and_task: [],
    route_and_prompt: [],
    gate_and_verification: [],
    commit_and_artifact: [],
    graph_and_trace: [],
  };
  for (const ref of refs) {
    const value = ref.value ?? "";
    // Backlog rows (AC-…) and task ids (task-…/cmd-…) → backlog_and_task
    if (isPlaybackBacklogRefValue(value)) {
      result.backlog_and_task.push(ref);
    } else {
      result[categorizeEvidenceRef(ref.kind)].push(ref);
    }
  }
  return result;
}

// Helper: expose frameFromEvent for card projection (internal, not exported from module boundary).
function frameFromEventPublic(event: TaskTimelineEvent, index: number): TaskPlaybackFrame {
  // Re-use the internal frameFromEvent path via normalizeTaskPlaybackTrace on a
  // single-event trace (avoids duplicating projection logic).
  const trace = normalizeTaskPlaybackTrace({
    projectId: "",
    backlog: { bug_id: event.backlog_id ?? "card", title: "", status: "", priority: "P3" },
    taskTimeline: { project_id: "", backlog_id: event.backlog_id ?? "card", events: [event], count: 1 },
    gateResponse: null,
    source: "governed",
  });
  return trace.frames[index] ?? trace.frames[0];
}

// projectTaskTimelineEvent is already imported at the top of this module.

// ---------------------------------------------------------------------------
// Playback deep-link URL helpers (B1 / B2 — UE blockers AC-ACTIVITY-PLAYBACK-IA-UE-BLOCKERS-20260611)
// ---------------------------------------------------------------------------

/**
 * Canonical URL params used by the Playback deep-link contract.
 *
 * Canonical route (B2):
 *   /dashboard?project_id=<pid>&view=activity&activity_tab=history
 *             &playback_backlog=<backlog_id>[&playback_event=<event_id>]
 *
 * The old `view=playback` form is normalised to `view=activity` by App.tsx
 * but does NOT preserve `playback_backlog` / `playback_event` / `activity_tab`.
 * Always use the canonical form above when constructing shareable links.
 */
export const PLAYBACK_URL_PARAMS = {
  /** view=activity (canonical; view=playback is a legacy alias) */
  view: "view",
  /** activity_tab=history to land on the Playback history tab */
  activity_tab: "activity_tab",
  /** playback_backlog=<backlog_id> */
  playback_backlog: "playback_backlog",
  /** playback_event=<event_id> — selects a specific frame by event id */
  playback_event: "playback_event",
} as const;

/**
 * Build the canonical deep-link URL for a playback row + optionally a specific
 * event frame.  This is the contract URL referenced in B2 (smoke paths, docs,
 * e2e script).
 *
 * @param projectId  - governance project id
 * @param backlogId  - backlog row id (playback_backlog param)
 * @param eventId    - optional event id to pre-select a frame (playback_event param)
 * @param base       - base URL; defaults to window.location.href when in browser
 */
export function buildPlaybackUrl(
  projectId: string,
  backlogId: string,
  eventId?: string | number | null,
  base?: string,
): string {
  const href = base ?? (typeof window !== "undefined" ? window.location.href : "http://localhost/dashboard");
  const url = new URL(href);
  url.searchParams.set("project_id", projectId);
  url.searchParams.set(PLAYBACK_URL_PARAMS.view, "activity");
  url.searchParams.set(PLAYBACK_URL_PARAMS.activity_tab, "history");
  url.searchParams.set(PLAYBACK_URL_PARAMS.playback_backlog, backlogId);
  if (eventId != null && eventId !== "") {
    url.searchParams.set(PLAYBACK_URL_PARAMS.playback_event, String(eventId));
  } else {
    url.searchParams.delete(PLAYBACK_URL_PARAMS.playback_event);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

/**
 * Read the playback_event URL param.  Returns "" when absent.
 */
export function readPlaybackEventParam(): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(PLAYBACK_URL_PARAMS.playback_event)?.trim() || "";
}

/**
 * Given a set of playback frames and a raw event-id param value (string/number
 * coming from the URL), find the matching frame id.
 *
 * Matching strategy (order):
 *  1. Exact frame.id match (includes both numeric "#101" and string ids).
 *  2. source_event_id match (e.g. "#101" stored as display id).
 *  3. Numeric id coercion: if param is a plain number string, also try "#N".
 *
 * Returns "" when no frame matches (caller falls back to default frame selection).
 */
export function findFrameIdByEventParam(
  frames: TaskPlaybackFrame[],
  eventParam: string,
): string {
  if (!eventParam || frames.length === 0) return "";
  const param = eventParam.trim();
  // 1. Exact frame.id match.
  const byId = frames.find((f) => f.id === param);
  if (byId) return byId.id;
  // 2. source_event_id match.
  const bySourceId = frames.find((f) => f.source_event_id === param);
  if (bySourceId) return bySourceId.id;
  // 3. Numeric: try both plain number and "#N" form.
  if (/^\d+$/.test(param)) {
    const hashForm = `#${param}`;
    const byHash = frames.find((f) => f.id === hashForm || f.source_event_id === hashForm);
    if (byHash) return byHash.id;
  }
  // 4. Hash-prefixed: try stripping "#" to get plain number.
  if (param.startsWith("#")) {
    const plain = param.slice(1);
    const byPlain = frames.find((f) => f.id === plain || f.source_event_id === plain);
    if (byPlain) return byPlain.id;
  }
  return "";
}

export interface PlaybackEventParamResolution {
  frameId: string;
  matched: boolean;
}

/**
 * Resolve a playback_event param against an already-available frame list.
 *
 * The warm-cache path in TaskPlaybackView calls this whenever the URL/event
 * param changes, even if the trace did not reload.  A missing param match keeps
 * the current valid frame selected instead of forcing a silent frame-1 fallback.
 */
export function resolveSelectedFrameIdForEventParam(
  frames: TaskPlaybackFrame[],
  eventParam: string,
  currentFrameId = "",
): PlaybackEventParamResolution {
  const frameId = findFrameIdByEventParam(frames, eventParam);
  if (frameId) return { frameId, matched: true };
  const currentExists = Boolean(currentFrameId && frames.some((frame) => frame.id === currentFrameId));
  return { frameId: currentExists ? currentFrameId : "", matched: false };
}

export function resolveInitialPlaybackFrameId(
  frames: TaskPlaybackFrame[],
  eventParam: string,
  currentFrameId = "",
): string {
  if (currentFrameId && frames.some((frame) => frame.id === currentFrameId)) return currentFrameId;
  const resolution = resolveSelectedFrameIdForEventParam(frames, eventParam, "");
  if (resolution.matched) return resolution.frameId;
  return frames[0]?.id || "";
}
