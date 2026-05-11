import type {
  ActiveSummaryResponse,
  EdgesResponse,
  FeedbackQueueResponse,
  HealthResponse,
  NodesResponse,
  OperationsQueueResponse,
  ProjectionResponse,
  StatusResponse,
} from "../types";

const PROJECT_ID = (import.meta.env.VITE_PROJECT_ID as string | undefined) || "aming-claw";
const DIRECT = (import.meta.env.VITE_DIRECT_API as string | undefined) === "true";
const BACKEND = (import.meta.env.VITE_BACKEND_URL as string | undefined) || "http://localhost:40000";

export const projectId = PROJECT_ID;

function base(): string {
  return DIRECT ? BACKEND : "";
}

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const url = `${base()}${path}`;
  const res = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `GET ${path} → ${res.status}`, text);
  }
  return (await res.json()) as T;
}

async function postJSON<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const url = `${base()}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: body == null ? undefined : JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `POST ${path} → ${res.status}`, text);
  }
  return (await res.json()) as T;
}

export class ApiError extends Error {
  status: number;
  body: string;
  constructor(status: number, message: string, body: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export const api = {
  health(signal?: AbortSignal) {
    return getJSON<HealthResponse>("/api/health", signal);
  },
  status(signal?: AbortSignal) {
    return getJSON<StatusResponse>(`/api/graph-governance/${PROJECT_ID}/status`, signal);
  },
  activeSummary(signal?: AbortSignal) {
    return getJSON<ActiveSummaryResponse>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/active/summary`,
      signal,
    );
  },
  activeProjection(signal?: AbortSignal) {
    return getJSON<ProjectionResponse>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/active/semantic/projection`,
      signal,
    );
  },
  nodes(snapshotId: string, limit = 1000, signal?: AbortSignal) {
    const path =
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/nodes?include_semantic=true&limit=${limit}`;
    return getJSON<NodesResponse>(path, signal);
  },
  edges(snapshotId: string, limit = 4000, signal?: AbortSignal) {
    const path =
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/edges?limit=${limit}`;
    return getJSON<EdgesResponse>(path, signal);
  },
  operationsQueue(signal?: AbortSignal) {
    return getJSON<OperationsQueueResponse>(
      `/api/graph-governance/${PROJECT_ID}/operations/queue`,
      signal,
    );
  },
  feedbackQueue(snapshotId: string, signal?: AbortSignal) {
    // MF-2026-05-10-016 P1: drop require_current_semantic filter so the
    // dashboard surfaces every needs_observer_decision item the operator can
    // act on. The semantic_review_gate.reason on each group still tells the UI
    // whether the underlying semantic is current.
    const path =
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}` +
      `/feedback/queue?require_current_semantic=false`;
    return getJSON<FeedbackQueueResponse>(path, signal);
  },
  decideFeedback(
    snapshotId: string,
    payload: {
      feedback_ids: string[];
      action: string;
      actor?: string;
      rationale?: string;
    },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok?: boolean;
      decided_count?: number;
      error_count?: number;
      semantic_enrichment_accepted?: {
        node_ids_flipped?: string[];
        event_ids_flipped?: string[];
      };
      projection_rebuilt?: boolean;
      projection_rebuild_error?: string;
    }>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/feedback/decision`,
      {
        feedback_ids: payload.feedback_ids,
        action: payload.action,
        actor: payload.actor ?? "dashboard_user",
        rationale: payload.rationale ?? "",
      },
      signal,
    );
  },
  // Reconcile actions are wired but disabled in P0 unless the user opts in;
  // surfaced through the stale-graph banner.
  queueScopeReconcile(opts: { commit_sha: string; parent_commit_sha?: string; actor?: string }, signal?: AbortSignal) {
    return postJSON<{
      ok: boolean;
      pending_scope_reconcile?: {
        commit_sha: string;
        // queued / running / materialized / failed / waived — the upsert
        // preserves materialized & waived so a previously cancelled commit
        // returns its OLD status here, even though POST returned 201.
        status: string;
        retry_count?: number;
        queued_at?: string;
      };
    }>(
      `/api/graph-governance/${PROJECT_ID}/pending-scope`,
      {
        commit_sha: opts.commit_sha,
        parent_commit_sha: opts.parent_commit_sha,
        actor: opts.actor ?? "dashboard_user",
        evidence: { source: "dashboard_stale_banner" },
      },
      signal,
    );
  },
  // MF-2026-05-10-014: incrementally materialize the queued pending-scope
  // row(s) into a candidate snapshot AND activate it in one round-trip.
  // MF-012's hook then auto-rebuilds the projection on activation.
  // dry_run=false here means "really build the snapshot"; AI is opt-in via
  // semantic_use_ai (default false → rule-based + carry-forward only).
  materializeAndActivatePendingScope(
    opts: { target_commit_sha: string; semantic_use_ai?: boolean; actor?: string },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      snapshot_id: string;
      activation?: { snapshot_id?: string; previous_snapshot_id?: string; projection_status?: string };
    }>(
      `/api/graph-governance/${PROJECT_ID}/reconcile/pending-scope`,
      {
        target_commit_sha: opts.target_commit_sha,
        actor: opts.actor ?? "dashboard_user",
        semantic_use_ai: opts.semantic_use_ai ?? false,
        activate: true,
      },
      signal,
    );
  },
  cancelScopeReconcile(
    opts: { commit_sha?: string; operation_id?: string; actor?: string; reason?: string },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      status: "cancelled" | "not_found" | string;
      cancelled_count: number;
      waived_count?: number;
      operation_id?: string;
    }>(
      `/api/graph-governance/${PROJECT_ID}/reconcile/scope/cancel`,
      {
        commit_sha: opts.commit_sha,
        operation_id: opts.operation_id,
        actor: opts.actor ?? "dashboard_user",
        reason: opts.reason ?? "dashboard_cancel",
      },
      signal,
    );
  },
  cancelSemanticJob(snapshotId: string, jobId: string, signal?: AbortSignal) {
    return postJSON<{
      ok: boolean;
      cancelled_count?: number;
      job?: { job_id?: string; status?: string };
    }>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/${encodeURIComponent(jobId)}/cancel`,
      { actor: "dashboard_user" },
      signal,
    );
  },
  cancelAllSemanticJobs(
    snapshotId: string,
    filters: {
      operation_type?: "node_semantic" | "edge_semantic";
      target_scope?: "node" | "edge" | "subtree" | "snapshot";
      before_ts?: string;
      status?: "queued" | "running";
    },
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      cancelled_count: number;
      skipped_terminal: number;
      matched_count?: number;
      cancelled_ops?: Array<{ operation_id: string; target_id: string }>;
    }>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/cancel-all`,
      { ...filters, actor: "dashboard_user" },
      signal,
    );
  },
  clearTerminalSemanticJobs(
    snapshotId: string,
    opts: {
      operation_type?: "node_semantic" | "edge_semantic";
      before_ts?: string;
      statuses?: string[];
    },
    signal?: AbortSignal,
  ) {
    // MF-2026-05-10-011: physically deletes terminal node rows; edge events
    // stay as audit history (edge_audit_matched is informational).
    return postJSON<{
      ok: boolean;
      deleted_count: number;
      edge_audit_matched: number;
      requested_statuses: string[];
    }>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/clear-terminal`,
      { ...opts, actor: "dashboard_user" },
      signal,
    );
  },
  cancelFeedback(snapshotId: string, opts: { feedback_ids?: string[]; limit?: number }, signal?: AbortSignal) {
    return postJSON<{
      ok: boolean;
      status: "soft_cancelled" | string;
      cancelled_count: number;
      feedback_cancel_contract?: "keep_status_observation";
    }>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/feedback/cancel`,
      { ...opts, actor: "dashboard_user" },
      signal,
    );
  },
  submitSemanticJob(snapshotId: string, payload: SemanticJobPayload, signal?: AbortSignal) {
    return postJSON<SemanticJobResponse>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`,
      payload,
      signal,
    );
  },
  submitFeedback(snapshotId: string, payload: FeedbackSubmitPayload, signal?: AbortSignal) {
    return postJSON<FeedbackSubmitResponse>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/feedback`,
      payload,
      signal,
    );
  },
  // POST /semantic-feedback — appends to the JSONL artifact that
  // run_semantic_enrichment reads (and pipes per-node into the AI payload's
  // `review_feedback` array). Separate from graph_feedback_items table.
  // Used by Retry: operator's rationale flows into the next AI call.
  appendSemanticFeedback(
    snapshotId: string,
    items: Array<{
      target_type: "node" | "edge" | "path" | "snapshot";
      target_id?: string;
      issue: string;
      priority?: "P0" | "P1" | "P2" | "P3";
      reason?: string;
      source_node_ids?: string[];
    }>,
    actor?: string,
    signal?: AbortSignal,
  ) {
    return postJSON<{
      ok: boolean;
      added_count?: number;
      feedback_path?: string;
    }>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/semantic-feedback`,
      { feedback_items: items, actor: actor ?? "dashboard_user" },
      signal,
    );
  },
  submitProposedEvent(snapshotId: string, payload: Record<string, unknown>, signal?: AbortSignal) {
    // Backend wraps the event row under `event`; older builds returned a flat
    // `event_id` field. Keep both shapes resilient.
    return postJSON<{
      ok: boolean;
      event?: { event_id?: string; status?: string };
      event_id?: string;
      status?: string;
    }>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/events`,
      payload,
      signal,
    );
  },
  fileBacklogFromEvent(
    snapshotId: string,
    eventId: string,
    payload: { backlog: BacklogDraft; start_chain?: boolean },
    signal?: AbortSignal,
  ) {
    // The endpoint returns `bug_id` + `event.backlog_bug_id` as the canonical
    // identifier. Older builds returned `backlog_task_id` / `task_id`; keep
    // both shapes resilient.
    return postJSON<{
      ok: boolean;
      bug_id?: string;
      event?: { backlog_bug_id?: string };
      backlog_task_id?: string;
      task_id?: string;
    }>(
      `/api/graph-governance/${PROJECT_ID}/snapshots/${encodeURIComponent(snapshotId)}/events/${encodeURIComponent(eventId)}/file-backlog`,
      payload,
      signal,
    );
  },
};

export interface BacklogDraft {
  title: string;
  task_type: "pm" | "dev" | "test" | "qa" | "task" | "reconcile" | "mf";
  priority: "P0" | "P1" | "P2" | "P3";
  target_files: string[];
  affected_graph_nodes: string[];
  graph_gate_mode: "strict" | "advisory" | "raw";
  branch_mode: "main" | "batch_branch" | "reconcile_branch";
  acceptance_criteria: string[];
  prompt: string;
}

export interface SemanticJobPayload {
  job_type: "semantic_enrichment" | "global_review";
  target_scope: "snapshot" | "node" | "subtree" | "edge";
  target_ids: string[];
  options: {
    target?: "nodes" | "edges" | "both";
    include_nodes?: boolean;
    include_edges?: boolean;
    scope?: string;
    mode?: "semanticize" | "retry" | "review";
    dry_run?: boolean;
    skip_current?: boolean;
    retry_stale_failed?: boolean;
    include_package_markers?: boolean;
    // Bulk-edge enrichment knobs (target_scope=edge with no target_ids).
    all_eligible?: boolean;
    include_contains?: boolean;
    edge_types?: string[];
    limit?: number;
  };
  created_by?: string;
}

export interface SemanticJobResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  job_id: string;
  status: string;
  queued_count?: number;
  operator_request?: unknown;
}

export interface FeedbackSubmitPayload {
  feedback_kind: string;
  summary: string;
  source_node_ids?: string[];
  target_id?: string;
  target_type?: "node" | "edge";
  priority?: "P0" | "P1" | "P2" | "P3" | "";
  paths?: string[];
  reason?: string;
  create_graph_event?: boolean;
  actor?: string;
  source_round?: string;
}

export interface FeedbackSubmitResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  // Single record shape (current backend, returns 201).
  feedback?: {
    feedback_id?: string;
    feedback_kind?: string;
    target_id?: string;
    target_type?: string;
    status?: string;
    issue?: string;
    issue_type?: string;
    confidence?: number;
    priority?: string;
  };
  event?: {
    event_id?: string;
    event_kind?: string;
    event_type?: string;
    status?: string;
    risk_level?: string;
  };
  // Legacy list shape — older builds returned `items: [...]`. Kept for resilience.
  items?: Array<{
    feedback_id?: string;
    feedback_kind?: string;
    target_id?: string;
    target_type?: string;
  }>;
  graph_event?: unknown;
}

export type Api = typeof api;
