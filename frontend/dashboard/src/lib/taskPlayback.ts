import type {
  BacklogBug,
  BacklogTimelineGateResponse,
  ContractRuntimeVisualizationNextAction,
  ContractRuntimeVisualizationResponse,
  TaskTimelineEvent,
  TaskTimelineResponse,
} from "../types";
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
export const TASK_COMPACT_LEDGER_SCHEMA = "task_timeline.compact_multi_backlog_ledger.v1";
export const TASK_COMPACT_LEDGER_EVENT_TYPE = "task_timeline.compact_ledger";

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
  audit_close: TaskPlaybackAuditCloseSummary | null;
}

export interface TaskPlaybackAuditCloseSummary {
  present: boolean;
  accepted: boolean;
  qa_passed: boolean;
  normal_close_blocked: boolean;
  evidence_not_reconstructed: boolean;
  status: string;
  reason: string;
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

export interface TaskPlaybackCompactLedgerPayloadRef {
  event_id: string;
  payload_sha256: string;
  payload_bytes: number | null;
}

export interface TaskPlaybackCompactLedgerNextAction {
  id: string;
  action: string;
  stage_id: string;
  line_id: string;
  owner_role: string;
  description: string;
}

export interface TaskPlaybackCompactLedgerBlockerSummary {
  kind: string;
  count: number | null;
  keys: string[];
  summary: string;
  reason: string;
}

export interface TaskPlaybackCompactLedgerRow {
  backlog_id: string;
  title: string;
  priority: string;
  status: string;
  commit: string;
  contract_execution_id: string;
  contract_chain_id: string;
  root_contract_execution_id: string;
  current_contract_execution_id: string;
  current_contract_id: string;
  parent_to_resume_contract_execution_id: string;
  active_child_contract_execution_id: string;
  projection_generation: number | null;
  projection_watermark: number | null;
  projection_hash: string;
  projection_updated_at: string;
  projection_degraded: boolean;
  projection_degraded_flags: Record<string, unknown>;
  contract_chain_current: Record<string, unknown>;
  merge_queue_id: string;
  merge_queue_index: number | null;
  merge_queue_item_id: string;
  merge_queue_task_id: string;
  merge_queue_status: string;
  latest_event_id: string;
  latest_event_kind: string;
  latest_event_type: string;
  latest_status: string;
  latest_payload_ref: TaskPlaybackCompactLedgerPayloadRef;
  next_legal_action: TaskPlaybackCompactLedgerNextAction;
  blocker_summary: TaskPlaybackCompactLedgerBlockerSummary;
  head_commit: string;
  readiness_state: string;
}

export interface TaskPlaybackCompactLedger {
  schema_version: typeof TASK_COMPACT_LEDGER_SCHEMA | string;
  project_id: string;
  row_count: number;
  source_event_count: number;
  rows: TaskPlaybackCompactLedgerRow[];
}

export interface TaskPlaybackCompactLedgerDisplayState {
  blocked: boolean;
  readinessLabel: string;
  readinessTone: string;
  readinessCardTone: "fail" | "pass" | "neutral";
  blockerLabel: string;
  blockerListLabel: "blockers" | "legacy advisory";
  blockerValues: string[];
  blockerTone: "neutral" | "green" | "red";
  legacyAdvisoryValues: string[];
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
  /** Canonical current authority axes. Never projected into playback frames. */
  authority_view: ContractRuntimeAuthorityViewModel | null;
  compact_ledger: TaskPlaybackCompactLedger;
  privacy_boundary: TaskPlaybackPrivacyBoundary;
  close_gate_summary: TaskPlaybackCloseGateSummary;
  close_gate_matrix: GateMatrixProjection;
}

export interface NormalizeTaskPlaybackInput {
  projectId: string;
  backlog: BacklogBug;
  taskTimeline?: TaskTimelineResponse | null;
  gateResponse?: BacklogTimelineGateResponse | null;
  compactLedger?: unknown;
  source?: TaskPlaybackSource;
  generatedAt?: string;
}

export interface RecentTimelineProjectionInput {
  project_id?: string;
  events?: TaskTimelineEvent[];
  compact_ledger?: unknown;
  compactLedger?: unknown;
  contract_runtime_projection_events?: TaskTimelineEvent[];
  generated_at?: string;
}

export type ContractRuntimeAuthorityDisplayStatus =
  | "PASS"
  | "BYPASSED"
  | "WAIVED"
  | "BLOCKED"
  | "FAILED"
  | "RUNNING"
  | "WAITING"
  | "RECORDED"
  | "COMPLETED"
  | "OPEN"
  | "UNKNOWN";

export interface ContractRuntimeAuthorityCacheIdentity {
  backlog_id: string;
  contract_execution_id: string;
  execution_state_revision: number;
  event_id: string;
  key: string;
}

export interface ContractRuntimeAuthorityViewModel {
  schema_version: "contract_runtime.authority_view_model.v1";
  project_id: string;
  backlog_id: string;
  generated_at: string;
  authority_source: "contract_runtime";
  cache_identity: ContractRuntimeAuthorityCacheIdentity;
  contract_execution_progress: Omit<ContractRuntimeVisualizationResponse["contract_execution_progress"], "line_states"> & {
    current_action: ContractRuntimeVisualizationNextAction;
    current_action_source: string;
    display_status: ContractRuntimeAuthorityDisplayStatus;
    line_states: Array<ContractRuntimeVisualizationResponse["contract_execution_progress"]["line_states"][number] & {
      display_status: ContractRuntimeAuthorityDisplayStatus;
    }>;
  };
  backlog_close_readiness: ContractRuntimeVisualizationResponse["backlog_close_readiness"] & {
    display_status: ContractRuntimeAuthorityDisplayStatus;
  };
  historical_diagnostics: {
    timeline_events: TaskTimelineEvent[];
    legacy_advisories: Record<string, unknown>[];
    bypass_records: Record<string, unknown>[];
    projection_conflicts: Record<string, unknown>[];
    current_snapshot_in_playback: false;
    append_only: boolean;
    truncated: boolean;
    next_cursor: string;
  };
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

export function contractRuntimeAuthorityDisplayStatus(
  value: unknown,
  options: { bypassed?: boolean; waived?: boolean } = {},
): ContractRuntimeAuthorityDisplayStatus {
  const status = safeText(String(value ?? "")).toLowerCase();
  if (options.bypassed || status.includes("bypass")) return "BYPASSED";
  if (options.waived || status.includes("waiv")) return "WAIVED";
  if (status.includes("block") || status.includes("missing")) return "BLOCKED";
  if (status.includes("fail") || status.includes("error") || status.includes("reject")) return "FAILED";
  if (status.includes("running") || status.includes("active") || status.includes("progress")) return "RUNNING";
  if (status.includes("waiting") || status.includes("pending") || status.includes("queued")) return "WAITING";
  if (status === "open") return "OPEN";
  if (status.replace(/[-\s]+/g, "_") === "contract_complete") return "COMPLETED";
  if (status.includes("pass") || status.includes("success") || status.includes("complete") || status === "fixed" || status === "closed") return "PASS";
  if (status.includes("record") || status.includes("accept") || status.includes("acknowledg")) return "RECORDED";
  return "UNKNOWN";
}

export function projectContractRuntimeAuthorityViewModel(
  response: ContractRuntimeVisualizationResponse,
): ContractRuntimeAuthorityViewModel {
  const runtimeAction = response.contract_execution_progress.next_legal_action ?? {};
  const chainAction = response.contract_chain.next_legal_action ?? {};
  const runtimeActionPresent = Object.keys(runtimeAction).length > 0;
  const chainActionPresent = Object.keys(chainAction).length > 0;
  const currentAction = runtimeActionPresent ? runtimeAction : chainActionPresent ? chainAction : {};
  const projectedActionSource = runtimeActionPresent
    ? runtimeAction.source
    : chainActionPresent
      ? chainAction.source
      : "";
  const currentActionSource = runtimeActionPresent || chainActionPresent
    ? safeText(projectedActionSource || response.authority.authority_decision_source)
      || (runtimeActionPresent ? "contract_runtime_current" : "backlog_contract_chain_current")
    : "none";
  const contractExecutionId = safeText(
    response.contract_execution_progress.contract_execution_id
      || response.contract_chain.current_contract_execution_id,
  );
  const executionStateRevision = Number(response.contract_execution_progress.execution_state_revision ?? 0) || 0;
  const latestEvent = response.timeline.events[0];
  const eventId = safeText(String(latestEvent?.event_id ?? latestEvent?.id ?? ""));
  const identityParts = [response.backlog_id, contractExecutionId, String(executionStateRevision), eventId];
  const lineStates = response.contract_execution_progress.line_states.map((line) => ({
    ...line,
    display_status: contractRuntimeAuthorityDisplayStatus(line.status, { bypassed: line.bypassed }),
  }));
  const closeAuthorityStatus = contractRuntimeAuthorityDisplayStatus(response.backlog_close_readiness.state, {
    waived: response.backlog_close_readiness.state.toLowerCase().includes("waiv"),
  });
  const backlogRowStatus = contractRuntimeAuthorityDisplayStatus(
    response.backlog.status || response.backlog_close_readiness.backlog_status,
  );
  const backlogCloseDisplayStatus = backlogRowStatus === "WAIVED" || backlogRowStatus === "BYPASSED"
    ? backlogRowStatus
    : closeAuthorityStatus !== "UNKNOWN"
      ? closeAuthorityStatus
      : backlogRowStatus;

  return {
    schema_version: "contract_runtime.authority_view_model.v1",
    project_id: response.project_id,
    backlog_id: response.backlog_id,
    generated_at: response.generated_at,
    authority_source: "contract_runtime",
    cache_identity: {
      backlog_id: response.backlog_id,
      contract_execution_id: contractExecutionId,
      execution_state_revision: executionStateRevision,
      event_id: eventId,
      key: identityParts.map((part) => encodeURIComponent(part)).join(":"),
    },
    contract_execution_progress: {
      ...response.contract_execution_progress,
      next_legal_action: currentAction,
      current_action: currentAction,
      current_action_source: currentActionSource,
      display_status: contractRuntimeAuthorityDisplayStatus(response.contract_execution_progress.readiness_state),
      line_states: lineStates,
    },
    backlog_close_readiness: {
      ...response.backlog_close_readiness,
      display_status: backlogCloseDisplayStatus,
    },
    historical_diagnostics: {
      timeline_events: [...response.timeline.events],
      legacy_advisories: [...response.legacy_advisories],
      bypass_records: [...response.bypass_records],
      projection_conflicts: [...response.projection_conflicts],
      current_snapshot_in_playback: false,
      append_only: response.timeline.append_only,
      truncated: response.timeline.truncated,
      next_cursor: response.timeline.next_cursor,
    },
  };
}

export function normalizeTaskPlaybackTrace(input: NormalizeTaskPlaybackInput): TaskPlaybackTrace {
  const timelineEvents = input.taskTimeline?.events ?? [];
  const authorityView = input.taskTimeline?.contract_runtime_visualization
    ? projectContractRuntimeAuthorityViewModel(input.taskTimeline.contract_runtime_visualization)
    : null;
  const gateEvents = input.gateResponse?.events ?? [];
  const compactLedger = normalizeTaskPlaybackCompactLedger(
    firstCompactLedgerSource(input.compactLedger, input.taskTimeline, input.gateResponse),
    input.projectId,
  );
  const ledgerEvents = taskPlaybackLedgerRowsToTimelineEvents(compactLedger, input.generatedAt);
  const events = mergeTimelineEvents([...timelineEvents, ...ledgerEvents], gateEvents);
  const frames = events.map((event, index) => frameFromEvent(event, index));
  const closeGateSummary = closeGateSummaryFrom(input.gateResponse);
  const lanes = lanesFromFrames(frames, input.backlog, closeGateSummary);
  const closeGateMatrix = projectGateMatrix(timelineGateWithAuditClose(input.gateResponse), closeGateSummary.applicable);
  const source = input.source ?? (input.taskTimeline || input.gateResponse || authorityView || compactLedger.rows.length > 0 ? "governed" : "fallback_sample");
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
    authority_view: authorityView,
    compact_ledger: compactLedger,
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

export function emptyCompactLedger(projectId = ""): TaskPlaybackCompactLedger {
  return {
    schema_version: TASK_COMPACT_LEDGER_SCHEMA,
    project_id: projectId,
    row_count: 0,
    source_event_count: 0,
    rows: [],
  };
}

export function normalizeTaskPlaybackCompactLedger(source: unknown, fallbackProjectId = ""): TaskPlaybackCompactLedger {
  const record = asRecord(findCompactLedgerSource(source) ?? source);
  if (Object.keys(record).length === 0) return emptyCompactLedger(fallbackProjectId);
  const rawRows = Array.isArray(record.rows) ? record.rows : [];
  const rows = rawRows.map(normalizeCompactLedgerRow).filter((row) => row.backlog_id || row.contract_execution_id || row.latest_event_id);
  return {
    schema_version: firstStringField(record, ["schema_version"]) || TASK_COMPACT_LEDGER_SCHEMA,
    project_id: firstStringField(record, ["project_id"]) || fallbackProjectId,
    row_count: numberFrom(record.row_count) ?? rows.length,
    source_event_count: numberFrom(record.source_event_count) ?? 0,
    rows,
  };
}

export function taskPlaybackLedgerRowsToTimelineEvents(
  ledger: TaskPlaybackCompactLedger | null | undefined,
  generatedAt?: string,
): TaskTimelineEvent[] {
  if (!ledger?.rows.length) return [];
  const at = generatedAt ?? new Date().toISOString();
  return ledger.rows.map((row, index) => compactLedgerEventFromRow(row, ledger, index, at));
}

export function recentTimelineEventKey(event: TaskTimelineEvent, index = 0): string {
  return eventIdentity(event, index);
}

export function compareRecentTimelineEvents(a: TaskTimelineEvent, b: TaskTimelineEvent): number {
  return -compareTimelineEvents(a, b);
}

export function mergeRecentTimelineEvents(
  events: TaskTimelineEvent[],
  limit?: number,
): TaskTimelineEvent[] {
  const seen = new Set<string>();
  const merged: TaskTimelineEvent[] = [];
  events.forEach((event, index) => {
    const key = recentTimelineEventKey(event, index);
    if (seen.has(key)) return;
    seen.add(key);
    merged.push(event);
  });
  merged.sort(compareRecentTimelineEvents);
  return typeof limit === "number" && limit > 0 ? merged.slice(0, limit) : merged;
}

export function projectRecentTimelineEvents(
  response: RecentTimelineProjectionInput | null | undefined,
): TaskTimelineEvent[] {
  const record = response ?? {};
  const sourceEvents = Array.isArray(record.events) ? record.events : [];
  const projectionEvents = Array.isArray(record.contract_runtime_projection_events)
    ? record.contract_runtime_projection_events
    : [];
  const compactLedger = normalizeTaskPlaybackCompactLedger(
    firstCompactLedgerSource(record.compact_ledger, record.compactLedger, record),
    record.project_id ?? "",
  );
  const ledgerEvents = taskPlaybackLedgerRowsToTimelineEvents(
    compactLedger,
    record.generated_at,
  );
  return mergeRecentTimelineEvents([
    ...sourceEvents,
    ...projectionEvents,
    ...ledgerEvents,
  ]);
}

export function taskPlaybackLedgerRowRefs(row: TaskPlaybackCompactLedgerRow): TaskPlaybackEvidenceRef[] {
  const payload = row.latest_payload_ref;
  const refs: TaskPlaybackEvidenceRef[] = [];
  if (row.latest_event_id) refs.push({ kind: "source_event", label: "latest event", value: row.latest_event_id });
  if (row.latest_event_kind) refs.push({ kind: "source_event", label: "latest event kind", value: row.latest_event_kind });
  if (payload.event_id) refs.push({ kind: "source_event", label: "payload event", value: payload.event_id });
  if (payload.payload_sha256) refs.push({ kind: "artifact", label: "payload sha", value: payload.payload_sha256 });
  if (payload.payload_bytes != null) refs.push({ kind: "artifact", label: "payload bytes", value: String(payload.payload_bytes) });
  if (row.contract_execution_id) refs.push({ kind: "artifact", label: "contract execution", value: row.contract_execution_id });
  if (row.merge_queue_id) refs.push({ kind: "artifact", label: "merge queue", value: row.merge_queue_id });
  if (row.merge_queue_item_id) refs.push({ kind: "artifact", label: "merge queue item", value: row.merge_queue_item_id });
  if (row.merge_queue_task_id) refs.push({ kind: "artifact", label: "merge queue task", value: row.merge_queue_task_id });
  if (row.contract_chain_id) refs.push({ kind: "artifact", label: "contract chain", value: row.contract_chain_id });
  if (row.root_contract_execution_id) refs.push({ kind: "artifact", label: "root contract", value: row.root_contract_execution_id });
  if (row.current_contract_execution_id) refs.push({ kind: "artifact", label: "current contract execution", value: row.current_contract_execution_id });
  if (row.current_contract_id) refs.push({ kind: "artifact", label: "current contract", value: row.current_contract_id });
  if (row.parent_to_resume_contract_execution_id) refs.push({ kind: "artifact", label: "parent resume", value: row.parent_to_resume_contract_execution_id });
  if (row.active_child_contract_execution_id) refs.push({ kind: "artifact", label: "active child", value: row.active_child_contract_execution_id });
  if (row.projection_watermark != null) refs.push({ kind: "artifact", label: "projection watermark", value: String(row.projection_watermark) });
  if (row.projection_hash) refs.push({ kind: "artifact", label: "projection hash", value: row.projection_hash });
  if (row.head_commit || row.commit) refs.push({ kind: "commit", label: "head commit", value: shortCommit(row.head_commit || row.commit) });
  return stableEvidence(refs);
}

export function taskPlaybackCompactLedgerRowForBacklog(
  ledger: TaskPlaybackCompactLedger | null | undefined,
  backlogId: string,
): TaskPlaybackCompactLedgerRow | null {
  if (!ledger?.rows.length) return null;
  return ledger.rows.find((row) => row.backlog_id === backlogId) ?? ledger.rows[0] ?? null;
}

export function taskPlaybackCompactLedgerNextActionLabel(
  action: TaskPlaybackCompactLedgerNextAction | null | undefined,
): string {
  if (!action) return "";
  const primary = action.action || action.id;
  const meta = [
    action.stage_id ? `stage ${action.stage_id}` : "",
    action.line_id ? `line ${action.line_id}` : "",
    action.owner_role ? `owner ${action.owner_role}` : "",
  ].filter(Boolean);
  const suffix = meta.length > 0 ? ` (${meta.join("; ")})` : "";
  const detail = action.description ? ` - ${action.description}` : "";
  return `${primary}${suffix}${detail}`.trim();
}

export function taskPlaybackCompactLedgerBlockerLabel(
  blocker: TaskPlaybackCompactLedgerBlockerSummary | null | undefined,
): string {
  const filtered = contractRuntimeBlockingBlockerSummary(blocker);
  if (!filtered) return "";
  const count = filtered.count != null ? `${filtered.count} blocker${filtered.count === 1 ? "" : "s"}` : "";
  return [
    filtered.kind,
    count,
    filtered.keys.length > 0 ? formatCompactList(filtered.keys) : "",
    filtered.summary,
    filtered.reason,
  ].filter(Boolean).join(" - ");
}

export function taskPlaybackCompactLedgerBlockingLabel(
  row: TaskPlaybackCompactLedgerRow | null | undefined,
): string {
  if (!row) return "";
  return compactLedgerAuthorityBlockingLabel(row) || taskPlaybackCompactLedgerBlockerLabel(row.blocker_summary);
}

export function taskPlaybackCompactLedgerDisplayState(
  row: TaskPlaybackCompactLedgerRow | null | undefined,
): TaskPlaybackCompactLedgerDisplayState {
  if (!row) {
    return {
      blocked: false,
      readinessLabel: "not loaded",
      readinessTone: "status-unknown",
      readinessCardTone: "neutral",
      blockerLabel: "",
      blockerListLabel: "blockers",
      blockerValues: [],
      blockerTone: "green",
      legacyAdvisoryValues: [],
    };
  }
  const blockerLabel = taskPlaybackCompactLedgerBlockingLabel(row);
  const projectedStatus = compactLedgerStatus(row);
  const blocked = projectedStatus === "blocked" || Boolean(blockerLabel);
  const legacyAdvisoryValues = legacyPrecheckAdvisoryValues(row.blocker_summary.keys);
  const rawBlockish = [
    row.readiness_state,
    row.latest_status,
    row.blocker_summary.kind,
    row.blocker_summary.summary,
    row.blocker_summary.reason,
  ].join(" ").toLowerCase();
  const legacyOnlyAdvisory = !blocked && legacyAdvisoryValues.length > 0 && /(block|fail|missing)/.test(rawBlockish);
  const readinessLabel = blocked
    ? "blocked"
    : legacyOnlyAdvisory
      ? "advisory/recorded"
      : row.readiness_state || projectedStatus || "recorded";
  const readinessTone = blocked
    ? "status-failed"
    : row.readiness_state === "close_ready" || row.readiness_state === "verified" || projectedStatus === "passed"
      ? "status-complete"
      : row.readiness_state === "implemented" || row.readiness_state === "planned" || projectedStatus === "running" || projectedStatus === "waiting"
        ? "status-running"
        : legacyOnlyAdvisory ? "status-unknown" : "status-unknown";
  return {
    blocked,
    readinessLabel,
    readinessTone,
    readinessCardTone: blocked ? "fail" : row.readiness_state === "close_ready" || row.readiness_state === "verified" || projectedStatus === "passed" ? "pass" : "neutral",
    blockerLabel,
    blockerListLabel: blockerLabel ? "blockers" : legacyAdvisoryValues.length > 0 ? "legacy advisory" : "blockers",
    blockerValues: blockerLabel ? [blockerLabel] : legacyAdvisoryValues,
    blockerTone: blockerLabel ? "red" : legacyAdvisoryValues.length > 0 ? "neutral" : "green",
    legacyAdvisoryValues,
  };
}

const LEGACY_PRECHECK_ADVISORY_IDS = new Set([
  "route_action_precheck",
  "route.action_precheck",
  "mf_timeline_precheck",
]);

const GENERIC_BLOCKER_WORDS = new Set(["blocker", "blockers", "blocker_evidence", "missing", "missing_evidence"]);

function isLegacyPrecheckAdvisoryText(value: string): boolean {
  const normalized = safeText(value).toLowerCase().replace(/[-\s.]+/g, "_");
  if (!normalized) return false;
  if (LEGACY_PRECHECK_ADVISORY_IDS.has(normalized)) return true;
  return normalized.includes("route_action_precheck") || normalized.includes("mf_timeline_precheck");
}

function isContractRuntimeEvent(event: TaskTimelineEvent): boolean {
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  const payloadKeys = Object.keys(payload);
  const verificationKeys = Object.keys(verification);
  return event.actor === "ContractRuntime"
    || event.event_type === TASK_COMPACT_LEDGER_EVENT_TYPE
    || event.event_kind === "contract_runtime_compact_ledger"
    || event.event_type.startsWith("contract_runtime.")
    || booleanFrom(payload.contract_runtime_projection)
    || booleanFrom(verification.contract_runtime_projection)
    || payloadKeys.some((key) => key === "contract_chain_current" || key.startsWith("contract_runtime_"))
    || verificationKeys.some((key) => key === "contract_chain_current" || key.startsWith("contract_runtime_"));
}

function filterLegacyPrecheckAdvisoryValues(values: PublicFieldValue[]): PublicFieldValue[] {
  return values.filter((item) => !isLegacyPrecheckAdvisoryText(item.value));
}

function legacyPrecheckAdvisoryValues(values: string[]): string[] {
  return stable(values.filter(isLegacyPrecheckAdvisoryText));
}

function contractRuntimeBlockingBlockerSummary(
  blocker: TaskPlaybackCompactLedgerBlockerSummary | null | undefined,
): TaskPlaybackCompactLedgerBlockerSummary | null {
  if (!blocker) return null;
  const rawValues = [blocker.kind, blocker.summary, blocker.reason, ...blocker.keys].filter(Boolean);
  const mentionsLegacyPrecheck = rawValues.some(isLegacyPrecheckAdvisoryText);
  const keys = blocker.keys.filter((key) => !isLegacyPrecheckAdvisoryText(key));
  const onlyLegacyPrecheckKeys = blocker.keys.length > 0 && keys.length === 0 && blocker.keys.some(isLegacyPrecheckAdvisoryText);
  const summary = isLegacyPrecheckAdvisoryText(blocker.summary) || (onlyLegacyPrecheckKeys && /legacy|precheck|route|timeline|missing/i.test(blocker.summary)) ? "" : blocker.summary;
  const reason = isLegacyPrecheckAdvisoryText(blocker.reason) || (onlyLegacyPrecheckKeys && /legacy|precheck|route|timeline|missing/i.test(blocker.reason)) ? "" : blocker.reason;
  const kind = isLegacyPrecheckAdvisoryText(blocker.kind) || GENERIC_BLOCKER_WORDS.has(blocker.kind.toLowerCase()) ? "" : blocker.kind;
  const genericCountOnly = (blocker.count ?? 0) > 0 && !mentionsLegacyPrecheck;
  const hasBlockingContent = keys.length > 0 || Boolean(summary) || Boolean(reason) || Boolean(kind) || genericCountOnly;
  if (!hasBlockingContent) return null;
  return {
    kind,
    count: keys.length > 0 ? keys.length : blocker.count,
    keys,
    summary,
    reason,
  };
}

const CONTRACT_RUNTIME_AUTHORITY_MISSING_PATHS = [
  "contract_runtime_mf_parallel_close_authority_gate.missing_requirement_ids",
  "contract_runtime_direct_fix_close_authority_gate.missing_requirement_ids",
  "contract_runtime_close_authority_projection.mf_parallel_close_authority_gate.missing_requirement_ids",
  "contract_runtime_close_authority_projection.direct_fix_close_authority_gate.missing_requirement_ids",
  "mf_parallel_close_authority_gate.missing_requirement_ids",
  "direct_fix_close_authority_gate.missing_requirement_ids",
  "next_required_evidence",
  "missing_evidence",
  "contract_chain_current.contract_runtime_mf_parallel_close_authority_gate.missing_requirement_ids",
  "contract_chain_current.contract_runtime_direct_fix_close_authority_gate.missing_requirement_ids",
  "contract_chain_current.contract_runtime_close_authority_projection.mf_parallel_close_authority_gate.missing_requirement_ids",
  "contract_chain_current.contract_runtime_close_authority_projection.direct_fix_close_authority_gate.missing_requirement_ids",
  "contract_chain_current.next_required_evidence",
  "contract_chain_current.missing_evidence",
];

const CONTRACT_RUNTIME_AUTHORITY_PAYLOAD_PATHS = [
  "contract_runtime_mf_parallel_close_authority_gate",
  "contract_runtime_direct_fix_close_authority_gate",
  "contract_runtime_close_authority_projection",
  "mf_parallel_close_authority_gate",
  "direct_fix_close_authority_gate",
  "contract_chain_current.contract_runtime_mf_parallel_close_authority_gate",
  "contract_chain_current.contract_runtime_direct_fix_close_authority_gate",
  "contract_chain_current.contract_runtime_close_authority_projection",
];

const CONTRACT_RUNTIME_AUTHORITY_ACTION_PATHS = [
  "contract_runtime_mf_parallel_close_authority_gate.next_action",
  "contract_runtime_direct_fix_close_authority_gate.next_action",
  "contract_runtime_close_authority_projection.next_action",
  "next_legal_action.description",
  "next_legal_action.action",
  "next_legal_action.id",
  "contract_chain_current.next_legal_action.description",
  "contract_chain_current.next_legal_action.action",
  "contract_chain_current.next_legal_action.id",
];

function contractRuntimeAuthorityValues(root: Record<string, unknown>, paths: string[]): string[] {
  return stable(paths.flatMap((path) => stringsFromUnknown(valueAtPath(root, path))).map(safeText).filter(Boolean))
    .filter((value) => !isLegacyPrecheckAdvisoryText(value));
}

function hasContractRuntimeAuthorityPayload(root: Record<string, unknown>, paths = CONTRACT_RUNTIME_AUTHORITY_PAYLOAD_PATHS): boolean {
  return paths.some((path) => Object.keys(asRecord(valueAtPath(root, path))).length > 0);
}

function compactLedgerAuthorityBlockingLabel(row: TaskPlaybackCompactLedgerRow): string {
  const root = row as unknown as Record<string, unknown>;
  const missing = contractRuntimeAuthorityValues(root, CONTRACT_RUNTIME_AUTHORITY_MISSING_PATHS);
  if (missing.length > 0) return `ContractRuntime authority missing ${formatCompactList(missing)}`;
  if (!hasContractRuntimeAuthorityPayload(root)) return "";
  const blockedish = [
    row.readiness_state,
    row.latest_status,
    row.blocker_summary.kind,
    row.blocker_summary.summary,
    row.blocker_summary.reason,
    row.blocker_summary.keys.join(" "),
  ].join(" ").toLowerCase();
  if (!/(block|fail|missing)/.test(blockedish)) return "";
  const actions = contractRuntimeAuthorityValues(root, CONTRACT_RUNTIME_AUTHORITY_ACTION_PATHS);
  if (actions.length > 0) return `ContractRuntime next legal action ${actions[0]}`;
  return "";
}

function firstCompactLedgerSource(...sources: unknown[]): unknown {
  for (const source of sources) {
    const found = findCompactLedgerSource(source);
    if (found) return found;
  }
  return null;
}

function findCompactLedgerSource(source: unknown): unknown {
  const record = asRecord(source);
  if (Object.keys(record).length === 0) return null;
  if (Array.isArray(record.rows)) return record;
  for (const key of ["compact_ledger", "compactLedger"]) {
    const candidate = asRecord(record[key]);
    if (Array.isArray(candidate.rows)) return candidate;
  }
  for (const key of ["timeline_gate", "payload", "verification", "artifact_refs"]) {
    const nested = findCompactLedgerSource(record[key]);
    if (nested) return nested;
  }
  return null;
}

function normalizeCompactLedgerRow(value: unknown): TaskPlaybackCompactLedgerRow {
  const record = asRecord(value);
  const contractChainCurrent = safePublicRecord(record.contract_chain_current, "contract_chain_current");
  const projectionDegradedFlags = safePublicRecord(
    firstUnknownField(record, ["projection_degraded_flags", "projection_degraded_reason"]) || contractChainCurrent.degraded_flags,
    "projection_degraded_flags",
  );
  const latestPayloadRef = normalizeLedgerPayloadRef(record.latest_payload_ref);
  return {
    backlog_id: firstStringField(record, ["backlog_id", "bug_id"]),
    title: firstStringField(record, ["title"]),
    priority: firstStringField(record, ["priority"]),
    status: firstStringField(record, ["status"]),
    commit: firstStringField(record, ["commit"]),
    contract_execution_id: firstStringField(record, ["contract_execution_id", "cex_id"]),
    contract_chain_id: firstStringField(record, ["contract_chain_id"]) || firstStringField(contractChainCurrent, ["contract_chain_id"]),
    root_contract_execution_id: firstStringField(record, ["root_contract_execution_id"]) || firstStringField(contractChainCurrent, ["root_contract_execution_id"]),
    current_contract_execution_id: firstStringField(record, ["current_contract_execution_id"]) || firstStringField(contractChainCurrent, ["current_contract_execution_id"]),
    current_contract_id: firstStringField(record, ["current_contract_id", "contract_id"]) || firstStringField(contractChainCurrent, ["current_contract_id", "contract_id"]),
    parent_to_resume_contract_execution_id: firstStringField(record, ["parent_to_resume_contract_execution_id"]) || firstStringField(contractChainCurrent, ["parent_to_resume_contract_execution_id"]),
    active_child_contract_execution_id: firstStringField(record, ["active_child_contract_execution_id"]) || firstStringField(contractChainCurrent, ["active_child_contract_execution_id"]),
    projection_generation: numberFrom(record.projection_generation) ?? numberFrom(contractChainCurrent.projection_generation),
    projection_watermark: numberFrom(record.projection_watermark) ?? numberFrom(contractChainCurrent.projection_watermark),
    projection_hash: firstStringField(record, ["projection_hash"]) || firstStringField(contractChainCurrent, ["projection_hash"]),
    projection_updated_at: firstStringField(record, ["projection_updated_at", "updated_at"]) || firstStringField(contractChainCurrent, ["updated_at", "projection_updated_at"]),
    projection_degraded: booleanFrom(record.projection_degraded) || booleanFrom(contractChainCurrent.projection_degraded) || booleanFrom(contractChainCurrent.degraded),
    projection_degraded_flags: projectionDegradedFlags,
    contract_chain_current: contractChainCurrent,
    merge_queue_id: firstStringField(record, ["merge_queue_id"]),
    merge_queue_index: numberFrom(record.merge_queue_index),
    merge_queue_item_id: firstStringField(record, ["merge_queue_item_id"]),
    merge_queue_task_id: firstStringField(record, ["merge_queue_task_id"]),
    merge_queue_status: firstStringField(record, ["merge_queue_status"]),
    latest_event_id: firstStringField(record, ["latest_event_id"]),
    latest_event_kind: firstStringField(record, ["latest_event_kind"]),
    latest_event_type: firstStringField(record, ["latest_event_type"]),
    latest_status: firstStringField(record, ["latest_status"]),
    latest_payload_ref: latestPayloadRef,
    next_legal_action: normalizeLedgerNextAction(record.next_legal_action),
    blocker_summary: normalizeLedgerBlockerSummary(record.blocker_summary),
    head_commit: firstStringField(record, ["head_commit", "head", "commit"]),
    readiness_state: firstStringField(record, ["readiness_state", "readiness", "close_readiness"]),
  };
}

function normalizeLedgerPayloadRef(value: unknown): TaskPlaybackCompactLedgerPayloadRef {
  const record = asRecord(value);
  return {
    event_id: firstStringField(record, ["event_id", "latest_event_id"]),
    payload_sha256: firstStringField(record, ["payload_sha256", "sha256", "payload_hash"]),
    payload_bytes: numberFrom(record.payload_bytes),
  };
}

function normalizeLedgerNextAction(value: unknown): TaskPlaybackCompactLedgerNextAction {
  const record = asRecord(value);
  const textValue = typeof value === "string" ? safeText(value) : "";
  return {
    id: firstStringField(record, ["id"]) || textValue,
    action: firstStringField(record, ["action", "tool", "command"]) || textValue,
    stage_id: firstStringField(record, ["stage_id", "stage"]),
    line_id: firstStringField(record, ["line_id"]),
    owner_role: firstStringField(record, ["owner_role", "role", "worker_role"]),
    description: firstStringField(record, ["description", "detail", "reason"]),
  };
}

function normalizeLedgerBlockerSummary(value: unknown): TaskPlaybackCompactLedgerBlockerSummary {
  const record = asRecord(value);
  const keys = stringsFromUnknown(record.keys ?? record.blocker_ids ?? record.blockers)
    .map(safeText)
    .filter((item) => item && item !== "[private detail redacted]");
  return {
    kind: firstStringField(record, ["kind", "type"]),
    count: numberFrom(record.count),
    keys: stable(keys),
    summary: firstStringField(record, ["summary", "description"]),
    reason: firstStringField(record, ["reason"]),
  };
}

function compactLedgerEventFromRow(
  row: TaskPlaybackCompactLedgerRow,
  ledger: TaskPlaybackCompactLedger,
  index: number,
  generatedAt: string,
): TaskTimelineEvent {
  const refs = taskPlaybackLedgerRowRefs(row);
  const nextAction = row.next_legal_action;
  const projectionFields = compactLedgerProjectionFields(row);
  const payload = {
    schema_version: ledger.schema_version,
    lane: "gate",
    backlog_id: row.backlog_id,
    title: row.title,
    priority: row.priority,
    row_status: row.status,
    contract_execution_id: row.contract_execution_id,
    ...projectionFields,
    merge_queue_id: row.merge_queue_id,
    merge_queue_index: row.merge_queue_index,
    merge_queue_item_id: row.merge_queue_item_id,
    merge_queue_task_id: row.merge_queue_task_id,
    merge_queue_status: row.merge_queue_status,
    latest_event_id: row.latest_event_id,
    latest_event_kind: row.latest_event_kind,
    latest_event_type: row.latest_event_type,
    latest_status: row.latest_status,
    latest_payload_ref: row.latest_payload_ref,
    next_legal_action: nextAction,
    blocker_summary: row.blocker_summary,
    head_commit: row.head_commit,
    readiness_state: row.readiness_state,
    ledger_refs: refs.map((ref) => `${ref.label}: ${ref.value}`),
    source_event_count: ledger.source_event_count,
    ledger_row_count: ledger.row_count,
  };
  return {
    event_id: `ledger:${row.backlog_id || row.contract_execution_id || index + 1}`,
    project_id: ledger.project_id,
    backlog_id: row.backlog_id,
    task_id: row.merge_queue_task_id || row.contract_execution_id,
    event_type: TASK_COMPACT_LEDGER_EVENT_TYPE,
    event_kind: "contract_runtime_compact_ledger",
    phase: "contract_runtime",
    actor: "ContractRuntime",
    status: compactLedgerStatus(row),
    commit_sha: row.head_commit || row.commit || undefined,
    payload,
    verification: {
      contract_execution_id: row.contract_execution_id,
      ...projectionFields,
      readiness_state: row.readiness_state,
      close_ready: row.readiness_state === "close_ready",
      blocked: compactLedgerStatus(row) === "blocked",
      latest_event_id: row.latest_event_id,
      latest_event_kind: row.latest_event_kind,
      latest_event_type: row.latest_event_type,
      latest_status: row.latest_status,
      latest_payload_ref: row.latest_payload_ref,
      head_commit: row.head_commit,
      next_legal_action: nextAction,
      blocker_summary: row.blocker_summary,
    },
    artifact_refs: {
      compact_ledger_schema: ledger.schema_version,
      contract_execution_id: row.contract_execution_id,
      ...projectionFields,
      latest_payload_ref: row.latest_payload_ref,
      merge_queue_id: row.merge_queue_id,
      merge_queue_index: row.merge_queue_index,
      merge_queue_item_id: row.merge_queue_item_id,
      merge_queue_task_id: row.merge_queue_task_id,
      merge_queue_status: row.merge_queue_status,
      ledger_refs: refs.map((ref) => `${ref.label}: ${ref.value}`),
      source_event_id: row.latest_event_id,
      source_event_refs: refs.filter((ref) => ref.kind === "source_event").map((ref) => ref.value),
    },
    created_at: row.projection_updated_at || generatedAt,
  };
}

function compactLedgerProjectionFields(row: TaskPlaybackCompactLedgerRow): Record<string, unknown> {
  return {
    contract_chain_id: row.contract_chain_id,
    root_contract_execution_id: row.root_contract_execution_id,
    current_contract_execution_id: row.current_contract_execution_id,
    current_contract_id: row.current_contract_id,
    parent_to_resume_contract_execution_id: row.parent_to_resume_contract_execution_id,
    active_child_contract_execution_id: row.active_child_contract_execution_id,
    projection_generation: row.projection_generation,
    projection_watermark: row.projection_watermark,
    projection_hash: row.projection_hash,
    projection_updated_at: row.projection_updated_at,
    projection_degraded: row.projection_degraded,
    projection_degraded_flags: row.projection_degraded_flags,
    contract_chain_current: row.contract_chain_current,
  };
}

function compactLedgerStatus(row: TaskPlaybackCompactLedgerRow): TaskPlaybackFrameStatus {
  const readiness = row.readiness_state.toLowerCase();
  const latestStatus = row.latest_status.toLowerCase();
  const blockingBlocker = contractRuntimeBlockingBlockerSummary(row.blocker_summary);
  const blockerText = blockingBlocker
    ? [
      blockingBlocker.kind,
      blockingBlocker.summary,
      blockingBlocker.reason,
      blockingBlocker.keys.join(" "),
    ].join(" ").toLowerCase()
    : "";
  const authorityBlocker = compactLedgerAuthorityBlockingLabel(row);
  if (readiness.includes("block") || latestStatus.includes("block") || blockerText.includes("block")) {
    if (!blockerText && !authorityBlocker) return "recorded";
    return "blocked";
  }
  if (readiness.includes("fail") || latestStatus.includes("fail")) {
    if (!blockerText && !authorityBlocker) return "recorded";
    return "failed";
  }
  if (readiness.includes("close_ready") || readiness.includes("verified") || readiness.includes("implemented")) return "passed";
  if (readiness.includes("planned") || readiness.includes("pending") || latestStatus.includes("pending")) return "waiting";
  return "recorded";
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

interface LineageBridgeActionRecord {
  action: string;
  parent_row_id: string;
  child_task_ids: string[];
  merge_queue_id: string;
  source: TaskPlaybackStructuredFact["source"];
}

function uniqueStrings(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const text = safeText(value);
    if (!text || seen.has(text)) continue;
    seen.add(text);
    result.push(text);
  }
  return result;
}

function lineageBridgeActionsFromEvent(event: TaskTimelineEvent): LineageBridgeActionRecord[] {
  const roots: Array<{ source: TaskPlaybackStructuredFact["source"]; value: unknown }> = [
    { source: "payload", value: event.payload },
    { source: "verification", value: event.verification },
    { source: "artifact_refs", value: event.artifact_refs },
  ];
  const actions: LineageBridgeActionRecord[] = [];
  const seen = new Set<string>();
  const visit = (value: unknown, source: TaskPlaybackStructuredFact["source"], depth: number): void => {
    if (depth > 5 || value == null) return;
    if (Array.isArray(value)) {
      for (const item of value) visit(item, source, depth + 1);
      return;
    }
    if (typeof value !== "object") return;
    const record = value as Record<string, unknown>;
    const text = stringsFromUnknown(record.action ?? record.next_legal_action ?? record.event_kind ?? record.id).join(" ").toLowerCase();
    const childTaskIds = uniqueStrings([
      ...stringsFromUnknown(record.child_task_ids),
      ...stringsFromUnknown(record.attempt_task_ids),
      ...stringsFromUnknown(record.task_ids),
    ]);
    const bridgeShaped = /(^|[\s._-])(lineage_bridge|cross_ref_lineage_bridge|record_cross_ref_lineage_bridge)([\s._-]|$)/.test(text)
      || childTaskIds.length > 0
      || Array.isArray(record.bridged_identities);
    if (bridgeShaped) {
      const bridgedIdentities = Array.isArray(record.bridged_identities) ? record.bridged_identities : [];
      for (const identity of bridgedIdentities) {
        const identityRecord = asRecord(identity);
        childTaskIds.push(...stringsFromUnknown(identityRecord.task_id));
      }
      const action = firstStringField(record, ["action", "next_legal_action", "event_kind"]) || "record_cross_ref_lineage_bridge";
      const normalizedChildTaskIds = uniqueStrings(childTaskIds).filter((item) => item !== "[private detail redacted]");
      const parentRowId = firstStringField(record, ["parent_row_id", "parent_backlog_id", "backlog_id", "bug_id"]);
      const mergeQueueId = firstStringField(record, ["merge_queue_id"]);
      if (action === "record_cross_ref_lineage_bridge" || normalizedChildTaskIds.length > 0 || text.includes("lineage_bridge")) {
        const key = `${action}|${parentRowId}|${normalizedChildTaskIds.join(",")}|${mergeQueueId}`;
        if (!seen.has(key)) {
          seen.add(key);
          actions.push({
            action,
            parent_row_id: parentRowId,
            child_task_ids: normalizedChildTaskIds,
            merge_queue_id: mergeQueueId,
            source,
          });
        }
      }
    }
    for (const key of ["lineage_bridge_action", "bridge_action", "next_legal_action", "deterministic_actions", "repair_summary"]) {
      if (key in record) visit(record[key], source, depth + 1);
    }
  };
  for (const root of roots) visit(root.value, root.source, 0);
  return actions;
}

function formatLineageBridgeAction(action: LineageBridgeActionRecord): string {
  const details = [
    action.parent_row_id ? `parent row ${action.parent_row_id}` : "",
    action.child_task_ids.length > 0 ? `child tasks ${formatCompactList(action.child_task_ids)}` : "",
    action.merge_queue_id ? `merge queue ${action.merge_queue_id}` : "",
  ].filter(Boolean);
  return `${action.action || "record_cross_ref_lineage_bridge"}${details.length > 0 ? ` (${details.join("; ")})` : ""}`;
}

function pushLineageBridgeFacts(facts: TaskPlaybackStructuredFact[], event: TaskTimelineEvent): void {
  const bridgeAction = lineageBridgeActionsFromEvent(event)[0];
  if (!bridgeAction) return;
  pushFact(facts, "lineage_bridge_action", "lineage bridge action", bridgeAction.action, bridgeAction.source);
  if (bridgeAction.parent_row_id) {
    pushFact(facts, "lineage_bridge_parent_row", "lineage bridge parent row", bridgeAction.parent_row_id, bridgeAction.source);
  }
  if (bridgeAction.child_task_ids.length > 0) {
    pushFact(facts, "lineage_bridge_child_tasks", "lineage bridge child tasks", formatCompactList(bridgeAction.child_task_ids), bridgeAction.source);
  }
  if (bridgeAction.merge_queue_id) {
    pushFact(facts, "merge_queue_id", "merge queue id", bridgeAction.merge_queue_id, bridgeAction.source);
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
  pushCompactLedgerFacts(facts, event);
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
  pushAuditCloseFacts(facts, event);
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
  pushLineageBridgeFacts(facts, event);
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
  return stableFacts(facts).slice(0, 32);
}

function pushCompactLedgerFacts(facts: TaskPlaybackStructuredFact[], event: TaskTimelineEvent): void {
  pushFirstFact(facts, event, "contract_execution_id", "contract execution id", [
    "payload.contract_execution_id",
    "verification.contract_execution_id",
    "artifact_refs.contract_execution_id",
  ]);
  pushFirstFact(facts, event, "contract_chain_id", "contract chain id", [
    "payload.contract_chain_id",
    "verification.contract_chain_id",
    "artifact_refs.contract_chain_id",
  ]);
  pushFirstFact(facts, event, "root_contract_execution_id", "root contract execution id", [
    "payload.root_contract_execution_id",
    "verification.root_contract_execution_id",
    "artifact_refs.root_contract_execution_id",
  ]);
  pushFirstFact(facts, event, "current_contract_execution_id", "current contract execution id", [
    "payload.current_contract_execution_id",
    "verification.current_contract_execution_id",
    "artifact_refs.current_contract_execution_id",
  ]);
  pushFirstFact(facts, event, "current_contract_id", "current contract id", [
    "payload.current_contract_id",
    "verification.current_contract_id",
    "artifact_refs.current_contract_id",
  ]);
  pushFirstFact(facts, event, "parent_to_resume_contract_execution_id", "parent resume execution id", [
    "payload.parent_to_resume_contract_execution_id",
    "verification.parent_to_resume_contract_execution_id",
    "artifact_refs.parent_to_resume_contract_execution_id",
  ]);
  pushFirstFact(facts, event, "active_child_contract_execution_id", "active child execution id", [
    "payload.active_child_contract_execution_id",
    "verification.active_child_contract_execution_id",
    "artifact_refs.active_child_contract_execution_id",
  ]);
  pushFirstFact(facts, event, "projection_generation", "projection generation", [
    "payload.projection_generation",
    "verification.projection_generation",
    "artifact_refs.projection_generation",
  ]);
  pushFirstFact(facts, event, "projection_watermark", "projection watermark", [
    "payload.projection_watermark",
    "verification.projection_watermark",
    "artifact_refs.projection_watermark",
  ]);
  pushFirstFact(facts, event, "projection_hash", "projection hash", [
    "payload.projection_hash",
    "verification.projection_hash",
    "artifact_refs.projection_hash",
  ]);
  pushFirstFact(facts, event, "projection_degraded", "projection degraded", [
    "payload.projection_degraded",
    "verification.projection_degraded",
    "artifact_refs.projection_degraded",
  ]);
  pushFirstFact(facts, event, "projection_degraded_flags", "projection degraded flags", [
    "payload.projection_degraded_flags",
    "verification.projection_degraded_flags",
    "artifact_refs.projection_degraded_flags",
  ]);
  pushFirstFact(facts, event, "readiness_state", "readiness", [
    "payload.readiness_state",
    "verification.readiness_state",
    "artifact_refs.readiness_state",
  ]);
  pushFirstFact(facts, event, "head_commit", "head commit", [
    "payload.head_commit",
    "verification.head_commit",
    "artifact_refs.head_commit",
  ]);
  pushFirstFact(facts, event, "latest_event_id", "latest event id", [
    "payload.latest_event_id",
    "verification.latest_event_id",
    "artifact_refs.latest_event_id",
    "artifact_refs.latest_payload_ref.event_id",
  ]);
  pushFirstFact(facts, event, "latest_event_kind", "latest event kind", [
    "payload.latest_event_kind",
    "verification.latest_event_kind",
    "artifact_refs.latest_event_kind",
  ]);
  pushFirstFact(facts, event, "latest_event_type", "latest event type", [
    "payload.latest_event_type",
    "verification.latest_event_type",
    "artifact_refs.latest_event_type",
  ]);
  pushFirstFact(facts, event, "latest_status", "latest status", [
    "payload.latest_status",
    "verification.latest_status",
    "artifact_refs.latest_status",
  ]);
  pushFirstFact(facts, event, "latest_payload_sha", "latest payload sha", [
    "payload.latest_payload_ref.payload_sha256",
    "verification.latest_payload_ref.payload_sha256",
    "artifact_refs.latest_payload_ref.payload_sha256",
  ]);
  pushFirstFact(facts, event, "latest_payload_bytes", "latest payload bytes", [
    "payload.latest_payload_ref.payload_bytes",
    "verification.latest_payload_ref.payload_bytes",
    "artifact_refs.latest_payload_ref.payload_bytes",
  ]);
  pushFirstFact(facts, event, "merge_queue_id", "merge queue id", [
    "payload.merge_queue_id",
    "verification.merge_queue_id",
    "artifact_refs.merge_queue_id",
  ]);
  pushFirstFact(facts, event, "merge_queue_index", "merge queue index", [
    "payload.merge_queue_index",
    "verification.merge_queue_index",
    "artifact_refs.merge_queue_index",
  ]);
  pushFirstFact(facts, event, "merge_queue_item_id", "merge queue item id", [
    "payload.merge_queue_item_id",
    "verification.merge_queue_item_id",
    "artifact_refs.merge_queue_item_id",
  ]);
  pushFirstFact(facts, event, "merge_queue_task_id", "merge queue task id", [
    "payload.merge_queue_task_id",
    "verification.merge_queue_task_id",
    "artifact_refs.merge_queue_task_id",
  ]);
  pushFirstFact(facts, event, "merge_queue_status", "merge queue status", [
    "payload.merge_queue_status",
    "verification.merge_queue_status",
    "artifact_refs.merge_queue_status",
  ]);

  const nextAction = firstLedgerNextAction(event);
  if (nextAction) {
    pushFact(facts, "next_legal_action", "next legal action", nextAction.value, nextAction.source);
  }
  const blocker = firstLedgerBlockerSummary(event);
  if (blocker) {
    pushFact(facts, "blocker_summary", "blocker summary", blocker.value, blocker.source);
  }
  const ledgerRefs = publicValuesAtPaths(event, [
    "payload.ledger_refs",
    "artifact_refs.ledger_refs",
  ]);
  if (ledgerRefs.length > 0) {
    pushFact(facts, "ledger_refs", "ledger refs", formatCompactList(ledgerRefs), sourceForPath(ledgerRefs[0].path));
  }
}

function firstLedgerNextAction(event: TaskTimelineEvent): PublicFieldValue | null {
  const paths = [
    "payload.next_legal_action",
    "verification.next_legal_action",
    "artifact_refs.next_legal_action",
  ];
  for (const path of paths) {
    if (isSensitiveEvidencePath(path)) continue;
    const value = valueAtPath(event as unknown as Record<string, unknown>, path);
    const record = asRecord(value);
    const label = Object.keys(record).length > 0
      ? taskPlaybackCompactLedgerNextActionLabel(normalizeLedgerNextAction(record))
      : safeText(stringFrom(value));
    if (label && label !== "[private detail redacted]") return { value: label, path, source: sourceForPath(path) };
  }
  return null;
}

function firstLedgerBlockerSummary(event: TaskTimelineEvent): PublicFieldValue | null {
  const authorityBlocker = firstContractRuntimeAuthorityBlocker(event);
  if (authorityBlocker) return authorityBlocker;
  const paths = [
    "payload.blocker_summary",
    "verification.blocker_summary",
    "artifact_refs.blocker_summary",
  ];
  for (const path of paths) {
    if (isSensitiveEvidencePath(path)) continue;
    const value = valueAtPath(event as unknown as Record<string, unknown>, path);
    const record = asRecord(value);
    const label = Object.keys(record).length > 0
      ? taskPlaybackCompactLedgerBlockerLabel(normalizeLedgerBlockerSummary(record))
      : safeText(stringFrom(value));
    if (label && label !== "[private detail redacted]") return { value: label, path, source: sourceForPath(path) };
  }
  return null;
}

function contractRuntimeAuthorityPublicValues(event: TaskTimelineEvent, paths: string[]): PublicFieldValue[] {
  if (!isContractRuntimeEvent(event)) return [];
  const prefixed = ["payload", "verification", "artifact_refs"].flatMap((prefix) => paths.map((path) => `${prefix}.${path}`));
  return filterLegacyPrecheckAdvisoryValues(publicValuesAtPaths(event, prefixed));
}

function firstContractRuntimeAuthorityBlocker(event: TaskTimelineEvent): PublicFieldValue | null {
  if (!isContractRuntimeEvent(event)) return null;
  const missing = contractRuntimeAuthorityPublicValues(event, CONTRACT_RUNTIME_AUTHORITY_MISSING_PATHS);
  if (missing.length > 0) {
    return {
      value: `ContractRuntime authority missing ${formatCompactList(missing)}`,
      path: missing[0].path,
      source: sourceForPath(missing[0].path),
    };
  }
  const blockedish = [event.status, event.event_type, event.event_kind, event.phase].join(" ").toLowerCase();
  if (!/(block|fail|missing)/.test(blockedish)) return null;
  const prefixedAuthorityPaths = ["payload", "verification", "artifact_refs"].flatMap((prefix) => CONTRACT_RUNTIME_AUTHORITY_PAYLOAD_PATHS.map((path) => `${prefix}.${path}`));
  if (!hasContractRuntimeAuthorityPayload(event as unknown as Record<string, unknown>, prefixedAuthorityPaths)) return null;
  const actions = contractRuntimeAuthorityPublicValues(event, CONTRACT_RUNTIME_AUTHORITY_ACTION_PATHS);
  if (actions.length > 0) {
    return {
      value: `ContractRuntime next legal action ${actions[0].value}`,
      path: actions[0].path,
      source: sourceForPath(actions[0].path),
    };
  }
  return null;
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

function pushAuditCloseFacts(facts: TaskPlaybackStructuredFact[], event: TaskTimelineEvent): void {
  const auditStatus = firstPublicValueAtPaths(event, [
    "payload.audit_archive.status",
    "payload.audit_close_gate.status",
    "payload.audit_close_gate.decision",
    "verification.audit_archive.status",
    "verification.audit_close_gate.status",
  ]);
  if (auditStatus && /audit|archive|accept|pass|waiv/i.test(auditStatus.value)) {
    pushFact(facts, "audit_close_gate", "audit close gate", auditStatus.value, auditStatus.source);
  }

  const normalClose = firstPublicValueAtPaths(event, [
    "payload.audit_archive.normal_close_gate.normal_close_gate_passed",
    "payload.audit_archive.normal_close_gate.can_close",
    "payload.normal_close_gate.normal_close_gate_passed",
    "payload.normal_close_gate.can_close",
    "verification.normal_close_gate.normal_close_gate_passed",
    "verification.normal_close_gate.can_close",
  ]);
  if (normalClose) {
    const value = /^(false|no|0)$/i.test(normalClose.value)
      ? "blocked - normal MF close remains false"
      : normalClose.value;
    pushFact(facts, "normal_close_gate", "normal close gate", value, normalClose.source);
  }

  const qaAcceptance = firstPublicValueAtPaths(event, [
    "payload.qa_acceptance.status",
    "payload.qa_acceptance.decision",
    "payload.audit_archive.qa_acceptance.status",
    "payload.audit_archive.evidence.verification.status",
    "payload.audit_archive.evidence.verification.passed",
    "verification.qa_acceptance.status",
    "verification.status",
    "verification.passed",
  ]);
  if (qaAcceptance) {
    const value = /^(true|yes|1)$/i.test(qaAcceptance.value) ? "passed" : qaAcceptance.value;
    if (/pass|accept|approve|ok|true/i.test(value)) pushFact(facts, "qa_acceptance", "QA acceptance", value, qaAcceptance.source);
  }

  const nonReconstructable = firstPublicValueAtPaths(event, [
    "payload.audit_archive.non_reconstructable_evidence_reason",
    "payload.non_reconstructable_evidence_reason",
    "verification.non_reconstructable_evidence_reason",
  ]);
  const reconstructed = firstPublicValueAtPaths(event, [
    "payload.audit_archive.evidence.reconstructed",
    "payload.audit_archive.reconstructed",
    "payload.reconstructed",
    "verification.reconstructed",
  ]);
  if (nonReconstructable || reconstructed?.value.toLowerCase() === "false") {
    pushFact(
      facts,
      "evidence_reconstruction",
      "historical evidence reconstruction",
      nonReconstructable?.value || "not reconstructed",
      nonReconstructable?.source ?? reconstructed?.source ?? "semantic",
    );
  }
}

function failureDiagnosisFromEvent(event: TaskTimelineEvent, status: TaskPlaybackFrameStatus): TaskPlaybackStructuredFact[] {
  const diagnosis: TaskPlaybackStructuredFact[] = [];
  const contractRuntimeEvent = isContractRuntimeEvent(event);
  const authorityMissingRequirements = contractRuntimeAuthorityPublicValues(event, CONTRACT_RUNTIME_AUTHORITY_MISSING_PATHS);
  if (authorityMissingRequirements.length > 0) {
    pushFact(
      diagnosis,
      "missing_required_evidence",
      "missing ContractRuntime authority",
      formatCompactList(authorityMissingRequirements),
      sourceForPath(authorityMissingRequirements[0].path),
    );
  }
  const blockerIds = (contractRuntimeEvent ? filterLegacyPrecheckAdvisoryValues : (items: PublicFieldValue[]) => items)(publicValuesAtPaths(event, [
    "payload.blocker_ids",
    "payload.blockers",
    "payload.blocker_summary",
    "payload.blocker_summary.keys",
    "payload.blocker_summary.kind",
    "payload.failed_request_ids",
    "verification.blocker_ids",
    "verification.blockers",
    "verification.blocker_summary",
    "verification.blocker_summary.keys",
    "verification.blocker_summary.kind",
    "artifact_refs.blocker_ids",
    "artifact_refs.blocker_summary",
  ]));
  if (blockerIds.length > 0) {
    pushFact(diagnosis, "blocker_ids", "blocker ids", formatCompactList(blockerIds), sourceForPath(blockerIds[0].path));
  }
  const missingEventKinds = (contractRuntimeEvent ? filterLegacyPrecheckAdvisoryValues : (items: PublicFieldValue[]) => items)(publicValuesAtPaths(event, [
    "payload.missing_event_kinds",
    "payload.blocked_event_kinds",
    "payload.blocked_protected_event_kinds",
    "payload.required_before_protected_evidence",
    "verification.missing_event_kinds",
    "verification.blocked_event_kinds",
  ]));
  if (missingEventKinds.length > 0) {
    pushFact(diagnosis, "missing_event_kinds", "missing event kinds", formatCompactList(missingEventKinds), sourceForPath(missingEventKinds[0].path));
  }
  const missingRequirements = (contractRuntimeEvent ? filterLegacyPrecheckAdvisoryValues : (items: PublicFieldValue[]) => items)(publicValuesAtPaths(event, [
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
  ]));
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
  const bridgeAction = lineageBridgeActionsFromEvent(event)[0];
  const nextAction = bridgeAction ? null : firstPublicValueAtPaths(event, [
    "payload.next_legal_action.description",
    "payload.next_legal_action.action",
    "payload.next_legal_action.id",
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
    "verification.next_legal_action.id",
    "verification.remaining_scope.next_legal_action.description",
    "verification.remaining_scope.next_legal_action.action",
    "verification.next_legal_action",
    "verification.next_action",
    "verification.next_expected_action",
    "verification.legal_next_action",
    "artifact_refs.next_legal_action.description",
    "artifact_refs.next_legal_action.action",
    "artifact_refs.next_legal_action.id",
    "artifact_refs.remaining_scope.next_legal_action.description",
    "artifact_refs.remaining_scope.next_legal_action.action",
    "artifact_refs.next_legal_action",
  ]);
  if (bridgeAction) {
    pushFact(diagnosis, "next_legal_action", "next legal action", formatLineageBridgeAction(bridgeAction), bridgeAction.source);
  } else if (nextAction) {
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
  const contractRuntimeEvent = isContractRuntimeEvent(event);
  const contractRuntimeMissingFilter = contractRuntimeEvent
    ? (item: PublicFieldValue) => !isLegacyPrecheckAdvisoryText(item.value)
    : undefined;

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
  ], typedPaths, unmetStatus, contractRuntimeMissingFilter);
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
  ], typedPaths, undefined, contractRuntimeMissingFilter);
  if (contractRuntimeEvent) {
    const advisoryValues = legacyPrecheckAdvisoryValues(publicValuesAtPaths(event, [
      "payload.missing_event_kinds",
      "payload.blocked_event_kinds",
      "payload.blocked_protected_event_kinds",
      "payload.missing_required_evidence",
      "payload.missing_requirement_ids",
      "payload.route_context_gate.missing_requirement_ids",
      "payload.route_context_gate.missing_required_evidence",
      "verification.missing_event_kinds",
      "verification.blocked_event_kinds",
      "verification.missing_required_evidence",
      "verification.missing_requirement_ids",
      "verification.route_context_gate.missing_requirement_ids",
      "verification.route_context_gate.missing_required_evidence",
    ]).map((item) => item.value));
    if (advisoryValues.length > 0) {
      pushChecklistItem(
        items,
        "legacy_precheck_advisory",
        "Legacy advisory",
        `${formatCompactList(advisoryValues)} is historical; ContractRuntime authority controls the blocking gate.`,
        "recorded",
        "semantic",
      );
    }
  }
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
    "audit_close_gate",
    "normal_close_gate",
    "qa_acceptance",
    "evidence_reconstruction",
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
  { path: "payload.audit_archive", label: "Audit archive" },
  { path: "payload.audit_close_gate", label: "Audit close gate" },
  { path: "payload.qa_acceptance", label: "QA acceptance" },
  { path: "payload.fixed_close_waiver_alert", label: "Fixed close waiver alert" },
  { path: "payload.contract_evidence", label: "Contract evidence" },
  { path: "payload.matrix", label: "Matrix row" },
  { path: "payload.test_results", label: "Test result" },
  { path: "verification", label: "Verification" },
  { path: "verification.checklist", label: "Verification checklist" },
  { path: "verification.checks", label: "Verification checks" },
  { path: "verification.gate", label: "Verification gate" },
  { path: "verification.audit_archive", label: "Audit archive" },
  { path: "verification.audit_close_gate", label: "Audit close gate" },
  { path: "verification.qa_acceptance", label: "QA acceptance" },
  { path: "verification.fixed_close_waiver_alert", label: "Fixed close waiver alert" },
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
  const workerHandoffEvent = isWorkerImplementationOrHandoffEvent(event);
  if (workerHandoffEvent) {
    pushFirstChecklistValue(items, typedPaths, event, "worker_final_state", "Worker final state", [
      "payload.final_state",
      "payload.handoff_state",
      "payload.stop_state",
      "payload.review_state",
      "payload.status",
      "verification.final_state",
      "verification.handoff_state",
      "verification.stop_state",
      "verification.review_state",
      "verification.status",
    ], "recorded", false);
  }
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
  if (workerHandoffEvent) {
    pushChecklistValues(items, typedPaths, event, "test_run", "Test run", [
      "payload.tests_run",
      "payload.test_commands",
      "payload.verification.tests_run",
      "verification.tests_run",
      "verification.test_commands",
      "artifact_refs.tests_run",
      "artifact_refs.test_commands",
    ], "present", false);
    pushChecklistValues(items, typedPaths, event, "worker_graph_trace", "Worker graph trace", [
      "payload.graph_query_trace_ids",
      "payload.graph_trace_ids",
      "payload.query_trace_ids",
      "payload.worker_graph_trace_ids",
      "verification.graph_query_trace_ids",
      "verification.graph_trace_ids",
      "artifact_refs.graph_query_trace_ids",
    ], "present", false);
    pushFirstChecklistValue(items, typedPaths, event, "generated_assets_policy", "Generated assets policy", [
      "payload.generated_assets_policy",
      "payload.generated_assets",
      "verification.generated_assets_policy",
      "artifact_refs.generated_assets_policy",
    ], "recorded", false);
  }
}

function isWorkerImplementationOrHandoffEvent(event: TaskTimelineEvent): boolean {
  const text = [
    event.event_type,
    event.event_kind,
    event.phase,
    event.actor,
  ].map((item) => safeText(String(item || "")).toLowerCase()).join(" ");
  return /(^|[\s._-])(implementation|review_ready|review-ready|waiting_merge|waiting-merge|finish_gate|finish-gate|handoff|checkpoint)([\s._-]|$)/.test(text)
    || (/mf_subagent|worker|parallel_branch/.test(text) && /review|ready|finish|handoff|implementation|checkpoint/.test(text));
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
  includeItem?: (item: PublicFieldValue) => boolean,
): void {
  for (const item of publicValuesAtPaths(event, paths)) {
    typedPaths?.add(item.path);
    if (includeItem && !includeItem(item)) continue;
    pushChecklistItem(items, kind, label, item.value, overrideStatus ?? status, item.source);
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
  if (event.event_type === TASK_COMPACT_LEDGER_EVENT_TYPE) {
    const contractExecutionId = factValue(facts, "contract_execution_id");
    const readiness = factValue(facts, "readiness_state");
    const latestEvent = factValue(facts, "latest_event_id");
    const latestKind = factValue(facts, "latest_event_kind");
    const latestStatus = factValue(facts, "latest_status");
    const headCommit = factValue(facts, "head_commit");
    const nextAction = factValue(facts, "next_legal_action") || factValue(diagnosis, "next_legal_action");
    const blockerSummary = factValue(facts, "blocker_summary") || factValue(diagnosis, "blocker_ids");
    const latest = [latestEvent ? `latest event ${latestEvent}` : "", latestKind, latestStatus].filter(Boolean).join(" / ");
    const identity = [
      contractExecutionId ? `contract execution ${contractExecutionId}` : "",
      backlog ? `backlog ${backlog}` : "",
      readiness ? `readiness ${readiness}` : "",
      headCommit ? `head ${headCommit}` : "",
    ].filter(Boolean);
    return `ContractRuntime compact ledger recorded ${identity.length > 0 ? identity.join(", ") : "runtime ledger state"}${latest ? `; ${latest}` : ""}${blockerSummary ? `; blockers ${blockerSummary}` : ""}${nextAction ? `; next legal action ${nextAction}` : ""}.`;
  }
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
  const bridgeAction = factValue(facts, "lineage_bridge_action");
  if (bridgeAction) {
    const parent = factValue(facts, "lineage_bridge_parent_row");
    const children = factValue(facts, "lineage_bridge_child_tasks");
    const mergeQueue = factValue(facts, "merge_queue_id");
    const details = [
      parent ? `parent row ${parent}` : "",
      children ? `child tasks ${children}` : "",
      mergeQueue ? `merge queue ${mergeQueue}` : "",
    ].filter(Boolean);
    return `Observer recorded ${bridgeAction}${details.length > 0 ? ` for ${details.join("; ")}` : ""}.`;
  }
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
  return /^(closed|fixed|done|complete|completed|resolved|merged|waived|audit_archived|archived|cancelled)$/i.test(safeText(status ?? ""));
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
      audit_close: null,
    };
  }
  const missingEventKinds = stable((gate.missing_event_kinds ?? []).map(safeText).filter(Boolean));
  const missingRequirementIds = closeGateMissingRequirementIds(response);
  const missingRequirementCount = missingRequirementIds.length;
  const blocked = response.applicable && (!response.can_close || gate.passed === false || missingEventKinds.length > 0 || missingRequirementCount > 0);
  const nextExpectedEvidence = stable([...missingEventKinds, ...missingRequirementIds]).slice(0, 8);
  const notApplicable = response.applicable === false;
  const auditClose = auditCloseSummaryFromResponse(response);
  return {
    applicable: Boolean(response.applicable),
    can_close: Boolean(response.can_close),
    status: notApplicable ? "recorded" : blocked ? "blocked" : gate.passed || response.can_close ? "passed" : "recorded",
    label: response.applicable
      ? auditClose?.accepted && blocked
        ? "Normal close blocked; audit close accepted"
        : (blocked ? "Close gate blocked" : "Close gate ready")
      : "Close gate not applicable",
    missing_event_kinds: missingEventKinds,
    missing_requirement_ids: missingRequirementIds,
    missing_requirement_count: missingRequirementCount,
    reason_sentence: closeGateReasonSentence(response, missingEventKinds, missingRequirementIds, blocked),
    next_expected_action: closeGateNextExpectedAction(response, missingEventKinds, missingRequirementIds, blocked),
    next_expected_evidence: nextExpectedEvidence,
    blocked,
    event_count: response.event_count ?? gate.event_count ?? response.events?.length ?? 0,
    audit_close: auditClose,
  };
}

function timelineGateWithAuditClose(response?: BacklogTimelineGateResponse | null): BacklogTimelineGateResponse["timeline_gate"] | undefined {
  const gate = response?.timeline_gate;
  if (!gate) return undefined;
  return {
    ...gate,
    audit_archive: gate.audit_archive ?? response?.audit_archive,
    audit_close_gate: gate.audit_close_gate ?? response?.audit_close_gate,
    qa_acceptance: gate.qa_acceptance ?? response?.qa_acceptance,
    fixed_close_waiver_alert: gate.fixed_close_waiver_alert ?? response?.fixed_close_waiver_alert,
  };
}

function auditCloseSummaryFromResponse(response: BacklogTimelineGateResponse): TaskPlaybackAuditCloseSummary | null {
  const gate = response.timeline_gate;
  const auditArchive = firstRecord(gate.audit_archive, response.audit_archive);
  const auditGate = firstRecord(gate.audit_close_gate, response.audit_close_gate, auditArchive.audit_close_gate, auditArchive);
  const archiveEvidence = asRecord(auditArchive.evidence);
  const qaAcceptance = firstRecord(gate.qa_acceptance, response.qa_acceptance, auditArchive.qa_acceptance, archiveEvidence.verification);
  const normalGate = firstRecord(gate.normal_close_gate, auditArchive.normal_close_gate);
  const failureAudit = firstRecord(auditArchive.failure_audit, archiveEvidence.failure_audit);
  const present =
    Object.keys(auditArchive).length > 0
    || Object.keys(auditGate).length > 0
    || Object.keys(qaAcceptance).length > 0
    || Object.keys(normalGate).length > 0;
  if (!present) return null;
  const accepted = acceptedLike(auditGate) || stringFrom(auditArchive.status) === "audit_archived" || stringFrom(auditArchive.row_status).toUpperCase() === "WAIVED";
  const qaPassed = acceptedLike(qaAcceptance);
  const timelinePrecheck = asRecord(archiveEvidence.timeline_precheck_failure_summary);
  const normalCloseBlocked =
    response.can_close === false
    || gate.passed === false
    || normalGate.can_close === false
    || normalGate.normal_close_gate_passed === false
    || timelinePrecheck.can_close === false;
  const evidenceNotReconstructed =
    archiveEvidence.reconstructed === false
    || auditArchive.reconstructed === false
    || auditArchive.historical_evidence_reconstructed === false
    || failureAudit.historical_evidence_reconstructed === false
    || Boolean(stringFrom(auditArchive.non_reconstructable_evidence_reason));
  return {
    present,
    accepted,
    qa_passed: qaPassed,
    normal_close_blocked: normalCloseBlocked,
    evidence_not_reconstructed: evidenceNotReconstructed,
    status: stringFrom(auditGate.status) || stringFrom(auditArchive.status) || (accepted ? "accepted" : "recorded"),
    reason: stringFrom(auditArchive.reason) || stringFrom(auditGate.reason) || stringFrom(qaAcceptance.reason),
  };
}

function closeGateMissingRequirementIds(response: BacklogTimelineGateResponse): string[] {
  const gate = response.timeline_gate;
  const contractGate = asRecord(gate?.contract_gate);
  const routeGate = asRecord(gate?.route_context_gate);
  const runtimeMfParallelAuthorityGate = asRecord(asRecord(gate as unknown as Record<string, unknown>).contract_runtime_mf_parallel_close_authority_gate);
  const runtimeDirectFixAuthorityGate = asRecord(asRecord(gate as unknown as Record<string, unknown>).contract_runtime_direct_fix_close_authority_gate);
  const runtimeAuthorityProjection = asRecord(asRecord(gate as unknown as Record<string, unknown>).contract_runtime_close_authority_projection);
  const runtimeAuthorityProjectionMfParallelGate = asRecord(runtimeAuthorityProjection.mf_parallel_close_authority_gate);
  const runtimeAuthorityProjectionDirectFixGate = asRecord(runtimeAuthorityProjection.direct_fix_close_authority_gate);
  const verification = asRecord(asRecord(response as unknown as Record<string, unknown>).verification);
  const gateRecord = asRecord(gate as unknown as Record<string, unknown>);
  const hasRuntimeAuthority = hasContractRuntimeCloseAuthority(gateRecord);
  const values = [
    ...stringsFromUnknown(runtimeMfParallelAuthorityGate.missing_requirement_ids),
    ...stringsFromUnknown(runtimeDirectFixAuthorityGate.missing_requirement_ids),
    ...stringsFromUnknown(runtimeAuthorityProjectionMfParallelGate.missing_requirement_ids),
    ...stringsFromUnknown(runtimeAuthorityProjectionDirectFixGate.missing_requirement_ids),
    ...stringsFromUnknown(contractGate.missing_requirement_ids),
    ...stringsFromUnknown(routeGate.missing_requirement_ids),
    ...stringsFromUnknown(gateRecord.missing_requirement_ids),
    ...stringsFromUnknown(gateRecord.missing_protected_lanes),
    ...stringsFromUnknown(gateRecord.required_before_protected_evidence),
    ...stringsFromUnknown(verification.missing_requirement_ids),
    ...stringsFromUnknown(verification.missing_protected_lanes),
    ...stringsFromUnknown(verification.required_before_protected_evidence),
    ...stringsFromUnknown(verification.next_expected_event_kind),
  ].map(safeText).filter(Boolean);
  return stable(hasRuntimeAuthority ? values.filter((value) => !isLegacyPrecheckAdvisoryText(value)) : values);
}

function hasContractRuntimeCloseAuthority(gate: Record<string, unknown>): boolean {
  const closeAuthority = asRecord(gate.close_authority);
  const sourceOfAuthority = stringFrom(gate.source_of_authority).toLowerCase();
  const closeAuthoritySource = stringFrom(closeAuthority.source_of_authority).toLowerCase();
  return sourceOfAuthority === "contract_runtime"
    || closeAuthoritySource === "contract_runtime"
    || booleanFrom(gate.runtime_projection_authority_failed)
    || (booleanFrom(closeAuthority.authoritative) && closeAuthoritySource === "contract_runtime")
    || Object.keys(asRecord(gate.contract_runtime_close_authority_projection)).length > 0
    || Object.keys(asRecord(gate.contract_runtime_mf_parallel_close_authority_gate)).length > 0
    || Object.keys(asRecord(gate.contract_runtime_direct_fix_close_authority_gate)).length > 0;
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
  refs.push(...collectPublicStrings(containers, ["contract_execution_id"]).map((value) => ({
    kind: "artifact" as const,
    label: "contract execution",
    value,
  })));
  refs.push(...collectPublicStrings(containers, ["latest_payload_ref", "ledger_refs"]).map((value) => ({
    kind: "artifact" as const,
    label: "ledger ref",
    value,
  })));
  refs.push(...collectPublicStrings(containers, ["latest_event_id", "source_event_id", "source_event_refs"]).map((value) => ({
    kind: "source_event" as const,
    label: "source event",
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
  const files = collectPublicStrings(containers, ["changed_files", "target_files", "owned_files", "modified_files", "updated_files", "files"]);
  const tests = collectPublicStrings(containers, ["tests_run", "test_commands", "tests_written", "test_files", "commands"]);
  const screenshots = collectPublicStrings(containers, ["screenshot", "screenshots", "browser_screenshot", "browser_screenshots"]);
  const graph = collectPublicStrings(containers, ["graph_trace_ids", "graph_query_trace_ids", "trace_ids"]);
  const artifactRefs = collectPublicStrings(containers, [
    "artifact_refs",
    "artifacts",
    "content_sys_artifacts",
    "contract_execution_id",
    "latest_payload_ref",
    "ledger_refs",
    "merge_queue_id",
    "merge_queue_item_id",
    "merge_queue_task_id",
  ]);
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

function firstRecord(...values: unknown[]): Record<string, unknown> {
  for (const value of values) {
    const record = asRecord(value);
    if (Object.keys(record).length > 0) return record;
  }
  return {};
}

function acceptedLike(record: Record<string, unknown>): boolean {
  if (record.accepted === true || record.passed === true || record.approved === true) return true;
  const text = [
    stringFrom(record.status),
    stringFrom(record.decision),
    stringFrom(record.result),
  ].join(" ").toLowerCase();
  return /\b(accepted|passed|approved|ok|success|audit_archived)\b/.test(text);
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

function numberFrom(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function booleanFrom(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    return normalized === "true" || normalized === "1" || normalized === "yes" || normalized === "degraded";
  }
  return false;
}

function firstUnknownField(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    const value = record[key];
    if (value != null && value !== "") return value;
  }
  return undefined;
}

function safePublicRecord(value: unknown, path: string): Record<string, unknown> {
  const record = asRecord(value);
  if (Object.keys(record).length === 0) return {};
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(record)) {
    const nextPath = `${path}.${key}`;
    out[key] = isSensitiveEvidencePath(nextPath) ? "[private detail redacted]" : safePublicValue(item, nextPath);
  }
  return out;
}

function safePublicValue(value: unknown, path: string): unknown {
  if (value == null) return value;
  if (Array.isArray(value)) return value.map((item, index) => safePublicValue(item, `${path}.${index}`));
  if (typeof value === "object") return safePublicRecord(value, path);
  if (typeof value === "string") return sanitizeEvidenceString(value, path);
  return value;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function eventIdentity(event: TaskTimelineEvent, index: number): string {
  return String(event.event_id || event.id || event.trace_id || `${event.event_type}-${event.created_at || index}`);
}

function eventDisplayId(event: TaskTimelineEvent): string {
  const rawId = (event as { id?: unknown }).id;
  if (event.event_id && !isSensitiveEvidenceText(event.event_id, "event_id")) return safeText(event.event_id);
  if (typeof rawId === "number") return `#${rawId}`;
  if (rawId != null) return safeText(String(rawId));
  if (event.trace_id && !isSensitiveEvidenceText(event.trace_id, "trace_id")) return safeText(event.trace_id);
  return "recorded";
}

function compareTimelineEvents(a: TaskTimelineEvent, b: TaskTimelineEvent): number {
  const at = Date.parse(a.created_at || "") || 0;
  const bt = Date.parse(b.created_at || "") || 0;
  if (at !== bt) return at - bt;
  const an = eventNumericSortValue(a);
  const bn = eventNumericSortValue(b);
  if (an != null && bn != null && an !== bn) return an - bn;
  return eventIdentity(a, 0).localeCompare(eventIdentity(b, 0));
}

function eventNumericSortValue(event: TaskTimelineEvent): number | null {
  const rawId = (event as { id?: unknown }).id;
  if (typeof rawId === "number" && Number.isFinite(rawId)) return rawId;
  if (typeof rawId === "string" && /^\d+$/.test(rawId.trim())) return Number(rawId);
  return null;
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
  const rawId = (publicEvent as { id?: unknown }).id;
  const id = publicEvent.event_id && !isSensitiveEvidenceText(publicEvent.event_id, "event_id")
    ? publicEvent.event_id
    : typeof rawId === "number" && Number.isFinite(rawId)
      ? rawId
      : typeof rawId === "string"
        ? rawId
        : rawId != null
          ? String(rawId)
      : eventIdentity(publicEvent, 0);
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
