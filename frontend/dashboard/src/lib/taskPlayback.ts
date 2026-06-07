import type { BacklogBug, BacklogTimelineGateResponse, TaskTimelineEvent, TaskTimelineResponse } from "../types";
import {
  PRIVATE_TIMELINE_TEXT_KEY,
  isPrivateTimelineText,
  projectTaskTimelineEvent,
  sanitizePublicTimelineText,
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
export const PRIVATE_EVIDENCE_KEY = PRIVATE_TIMELINE_TEXT_KEY;
const ABSOLUTE_HOST_PATH = /(^|\s)(\/Users\/[^\s,;:]+|\/home\/[^\s,;:]+|\/var\/folders\/[^\s,;:]+|[A-Za-z]:\\[^\s,;:]+)/g;

export function isPrivatePlaybackText(value?: string | null): boolean {
  return isPrivateTimelineText(value);
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
    detail_inspector: semantic.inspector,
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
  pushFirstFact(facts, event, "route_id", "route id", [
    "payload.route_id",
    "payload.route_identity.route_id",
    "verification.route_id",
    "verification.route_identity.route_id",
    "artifact_refs.route_id",
  ]);
  pushFirstFact(facts, event, "prompt_contract_id", "prompt contract id", [
    "payload.prompt_contract_id",
    "payload.prompt_contract.prompt_contract_id",
    "payload.route_identity.prompt_contract_id",
    "verification.prompt_contract_id",
    "verification.route_identity.prompt_contract_id",
    "artifact_refs.prompt_contract_id",
  ]);
  pushFirstFact(facts, event, "stage", "stage", [
    "payload.stage",
    "payload.lifecycle_state",
    "payload.failing_stage",
    "verification.stage",
    "phase",
  ]);
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
  return stableFacts(facts).slice(0, 18);
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
    "payload.route_context_gate.missing_requirement_ids",
    "payload.contract_gate.missing_requirement_ids",
    "verification.missing_required_evidence",
    "verification.missing_requirement_ids",
    "verification.required_before_protected_evidence",
    "verification.route_context_gate.missing_requirement_ids",
    "verification.contract_gate.missing_requirement_ids",
  ]);
  if (missingRequirements.length > 0) {
    pushFact(diagnosis, "missing_required_evidence", "missing required evidence", formatCompactList(missingRequirements), sourceForPath(missingRequirements[0].path));
  }
  const routeMismatch = publicValuesAtPaths(event, [
    "payload.route_identity_mismatch",
    "payload.mismatched_route_identity",
    "payload.identity_recovery.route_identity_mismatch",
    "verification.route_identity_mismatch",
    "verification.mismatched_route_identity",
    "verification.identity_recovery.route_identity_mismatch",
  ]);
  const blockerValues = blockerIds.map((item) => item.value.toLowerCase());
  if (routeMismatch.length > 0) {
    pushFact(diagnosis, "mismatched_route_identity", "mismatched route identity", formatCompactList(routeMismatch), sourceForPath(routeMismatch[0].path));
  } else if (blockerValues.some((value) => value.includes("route_identity_mismatch") || value.includes("mismatched_route_identity"))) {
    pushFact(diagnosis, "mismatched_route_identity", "mismatched route identity", "route_identity_mismatch", "payload");
  }
  const staleReasons = publicValuesAtPaths(event, [
    "payload.stale_reason",
    "payload.timeout_reason",
    "payload.pending_scope_timeout",
    "payload.failure_reason",
    "payload.reason",
    "payload.last_error",
    "verification.stale_reason",
    "verification.timeout_reason",
    "verification.reason",
    "verification.errors",
  ]);
  const staleBlockers = blockerIds.filter((item) => /stale|timeout|timed_out|pending_scope/.test(item.value.toLowerCase()));
  if (staleReasons.length > 0) {
    pushFact(diagnosis, "stale_timeout_reason", "stale/timeout reason", formatCompactList(staleReasons), sourceForPath(staleReasons[0].path));
  } else if (staleBlockers.length > 0) {
    pushFact(diagnosis, "stale_timeout_reason", "stale/timeout reason", formatCompactList(staleBlockers), sourceForPath(staleBlockers[0].path));
  }
  const nextAction = firstPublicValueAtPaths(event, [
    "payload.next_legal_action",
    "payload.next_action",
    "payload.next_expected_action",
    "payload.recovery_action",
    "payload.recovery_options",
    "verification.next_legal_action",
    "verification.next_action",
    "verification.next_expected_action",
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
    "payload.route_id",
    "payload.route_identity.route_id",
    "verification.route_id",
    "artifact_refs.route_id",
  ]);
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
    "verification.prompt_contract_id",
    "artifact_refs.prompt_contract_id",
  ]);
  pushEvidenceValues(links, "prompt_contract", "prompt contract hash", event, [
    "payload.prompt_contract_hash",
    "payload.route_identity.prompt_contract_hash",
    "verification.prompt_contract_hash",
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
    "verification.source_event_id",
    "artifact_refs.source_event_id",
    "artifact_refs.source_event_refs",
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
  if (!safe || PRIVATE_EVIDENCE_KEY.test(safe)) return;
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
  if (PRIVATE_EVIDENCE_KEY.test(path)) return [];
  return stringsFromUnknown(valueAtPath(event as unknown as Record<string, unknown>, path))
    .map(safeText)
    .filter((value) => value && value !== "[private detail redacted]" && !PRIVATE_EVIDENCE_KEY.test(value))
    .map((value) => ({ value, path, source: sourceForPath(path) }));
}

function firstCountAtPaths(event: TaskTimelineEvent, paths: string[]): { count: number; source: TaskPlaybackStructuredFact["source"]; path: string } | null {
  for (const path of paths) {
    if (PRIVATE_EVIDENCE_KEY.test(path)) continue;
    const count = countUnknownItems(valueAtPath(event as unknown as Record<string, unknown>, path));
    if (count > 0) return { count, source: sourceForPath(path), path };
  }
  return null;
}

function countUnknownItems(value: unknown): number {
  if (value == null || value === "") return 0;
  if (Array.isArray(value)) return value.filter((item) => safeText(stringFrom(item) || compactUnknown(item))).length;
  if (typeof value === "object") {
    return Object.keys(value as Record<string, unknown>).filter((key) => !PRIVATE_EVIDENCE_KEY.test(key)).length;
  }
  return safeText(String(value)) ? 1 : 0;
}

function pushEvidenceValues(
  links: TaskPlaybackEvidenceRef[],
  kind: TaskPlaybackEvidenceRef["kind"],
  label: string,
  event: TaskTimelineEvent,
  paths: string[],
): void {
  for (const item of publicValuesAtPaths(event, paths).slice(0, 8)) {
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
      if (PRIVATE_EVIDENCE_KEY.test(key)) continue;
      if (!keys.includes(key)) continue;
      values.push(...stringsFromUnknown(value));
    }
  }
  return stable(values.map(safeText).filter((value) => Boolean(value) && !PRIVATE_EVIDENCE_KEY.test(value))).slice(0, 18);
}

function stringsFromUnknown(value: unknown): string[] {
  if (value == null || value === "") return [];
  if (Array.isArray(value)) return value.flatMap(stringsFromUnknown);
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    return Object.entries(record)
      .filter(([key]) => !PRIVATE_EVIDENCE_KEY.test(key))
      .slice(0, 4)
      .map(([key, item]) => `${titleize(key)}: ${safeText(stringFrom(item) || compactUnknown(item))}`);
  }
  return [String(value)];
}

function compactUnknown(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
  const keys = Object.keys(value as Record<string, unknown>).filter((key) => !PRIVATE_EVIDENCE_KEY.test(key));
  return keys.length > 0 ? keys.slice(0, 3).join(", ") : "record";
}

function safeText(value: string): string {
  return sanitizePublicTimelineText(value).replace(ABSOLUTE_HOST_PATH, "$1[local path redacted]").replace(/\s+/g, " ").trim();
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
  if (event.event_id && !PRIVATE_EVIDENCE_KEY.test(event.event_id)) return safeText(event.event_id);
  if (event.id != null) return `#${event.id}`;
  if (event.trace_id && !PRIVATE_EVIDENCE_KEY.test(event.trace_id)) return safeText(event.trace_id);
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
