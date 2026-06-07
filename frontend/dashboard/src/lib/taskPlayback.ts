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
  kind: "timeline_event" | "graph_trace" | "artifact" | "commit" | "test" | "file" | "node" | "gate" | "content_sys";
  label: string;
  value: string;
}

export interface TaskPlaybackArtifactRef {
  kind: "file" | "test" | "screenshot" | "graph" | "commit" | "content_sys" | "artifact";
  value: string;
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
  status: TaskPlaybackFrameStatus;
  actor: string;
  narrative: TaskTimelineSemanticNarrative;
  semantic_entry_id: string;
  semantic_chips: TaskTimelineSemanticChip[];
  detail_inspector: TaskTimelineEvidenceInspector;
  evidence_refs: TaskPlaybackEvidenceRef[];
  artifact_refs: TaskPlaybackArtifactRef[];
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
  const semantic = projectTaskTimelineEvent(event, index);
  const status = timelineStatusFromEvent(event);
  const artifactRefs = artifactsFromEvent(event, semantic);
  return {
    id: eventIdentity(event, index),
    sequence: index + 1,
    at: event.created_at || "",
    lane_id: semantic.lane_id,
    source_event_id: eventDisplayId(event),
    event_type: semantic.event_type_label,
    event_kind: semantic.event_kind_label,
    phase: semantic.phase_label,
    title: semantic.title,
    detail: semantic.detail,
    status,
    actor: semantic.actor_label,
    narrative: semantic.narrative,
    semantic_entry_id: semantic.catalog_entry_id,
    semantic_chips: semantic.chips,
    detail_inspector: semantic.inspector,
    evidence_refs: evidenceFromEvent(event, semantic),
    artifact_refs: artifactRefs,
  };
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
    const plural = missingEventKinds.length === 1;
    return `Blocked because ${label} evidence has not been recorded; the close gate cannot pass until ${plural ? "that event exists" : "those events exist"}.`;
  }
  if (missingRequirementIds.length > 0) {
    const label = formatEvidenceList(missingRequirementIds);
    const plural = missingRequirementIds.length === 1;
    return `Blocked because ${label} evidence is missing; the close gate cannot pass until ${plural ? "that requirement is satisfied" : "those requirements are satisfied"}.`;
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
    return `Next expected evidence/action: record ${formatEvidenceList(missingEventKinds)} evidence, then rerun close-gate verification.`;
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
