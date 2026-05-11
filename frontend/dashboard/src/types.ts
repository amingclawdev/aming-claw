// Type definitions mirror live shapes returned by the governance HTTP API
// (http://localhost:40000). They are intentionally narrow to what the P0
// dashboard needs — additional fields are tolerated.

export interface HealthResponse {
  status: string;
  service: string;
  port: number;
  version: string;
  pid: number;
  request_id: string;
}

export interface StatusResponse {
  ok: boolean;
  project_id: string;
  active_snapshot_id: string;
  graph_snapshot_commit: string;
  materialized_graph_baseline_commit: string;
  scan_baseline_commit: string;
  scan_baseline_id: number;
  pending_scope_reconcile_count: number;
  pending_scope_reconcile: unknown[];
  current_state?: {
    snapshot_id?: string;
    graph_stale?: {
      is_stale: boolean;
      active_graph_commit: string;
      head_commit: string;
      changed_files?: string[];
      changed_file_count?: number;
    };
  };
}

export interface ActiveSummaryResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  commit_sha: string;
  snapshot_kind: string;
  snapshot_status: string;
  created_at: string;
  graph_sha256: string;
  inventory_sha256: string;
  drift_sha256: string;
  counts: SummaryCounts;
  health: SummaryHealth;
}

export interface SummaryCounts {
  nodes: number;
  nodes_by_layer: Record<string, number>;
  edges: number;
  edges_by_type: Record<string, number>;
  features: number;
  files: number;
  orphan_files: number;
  pending_decision_files: number;
  cleanup_candidates: number;
  ai_review_feedback: number;
}

export interface SummaryHealth {
  project_health_score: number;
  raw_project_health_score: number;
  file_hygiene_score: number;
  artifact_binding_score: number;
  governance_observability_score: number;
  doc_coverage_ratio: number;
  test_coverage_ratio: number;
  semantic_coverage_ratio: number;
  structure_health_score: number;
  semantic_health_score: number;
  project_insight_health_score: number;
  semantic_health: SemanticHealthBlock;
  structure_health?: { feature_count?: number; governed_feature_count?: number };
}

export interface SemanticHealthBlock {
  score: number;
  feature_count: number;
  semantic_current_count: number;
  semantic_missing_count: number;
  semantic_stale_count: number;
  semantic_unverified_hash_count: number;
  semantic_current_ratio: number;
  edge_semantic_eligible_count: number;
  edge_semantic_current_count: number;
  edge_semantic_requested_count: number;
  edge_semantic_missing_count: number;
}

export type Layer = "L1" | "L2" | "L3" | "L4" | "L7";

export interface NodeRecord {
  node_id: string;
  layer: Layer | string;
  title: string;
  kind?: string;
  primary_files?: string[];
  secondary_files?: string[];
  test_files?: string[];
  config_files?: string[];
  metadata?: NodeMetadata;
  semantic?: NodeSemantic;
  exclude_as_feature?: boolean;
  // MF-016/017 follow-up: per-node feature-health score (prototype algorithm).
  // null for L4 asset leaves and empty containers. Populated by lib/health.ts
  // after the dashboard data bundle loads. Asset-side binding score lives
  // separately so it doesn't get mixed into feature-health rollups.
  _health?: number | null;
  _asset_binding?: number | null;
}

export interface NodeMetadata {
  hierarchy_parent?: string;
  children?: string[];
  module?: string;
  file_role?: string;
  feature_hash?: string;
  function_count?: number;
  functions?: string[];
  // Per-function line metadata persisted by the graph adapter (since 59c9fbc).
  // Map key is the short symbol name (`DecisionValidator.__init__`), value is
  // a `[start_line, end_line]` 1-based pair.
  function_lines?: Record<string, [number, number]>;
  graph_metrics?: {
    fan_in?: number;
    fan_out?: number;
    hierarchy_in?: number;
    hierarchy_out?: number;
  };
  exclude_as_feature?: boolean;
  feature_metadata?: { exclude_as_feature?: boolean };
  quality_flags?: string[];
}

export interface NodeSemantic {
  status?: string;
  node_status?: string;
  job_status?: string;
  feature_hash?: string;
  hash_state?: string;
  has_semantic_payload?: boolean;
  feature_name?: string;
  domain_label?: string;
  intent?: string;
  semantic_summary?: string;
  doc_status?: string;
  test_status?: string;
  config_status?: string;
  feedback_round?: number;
  open_issues?: unknown[];
  observer_decision?: string;
  review_status?: string;
  validity?: NodeValidity;
  carried_forward_from_snapshot_id?: string;
  carried_forward_at?: string;
  ai_route?: { provider?: string; model?: string };
}

export interface NodeValidity {
  status?: string;
  hash_validation?: string;
  file_hash_status?: string;
  valid?: boolean;
  feature_hash_match?: boolean;
  file_hash_match?: boolean;
  hash_state?: string;
  hash_verified?: boolean;
  current_feature_hash?: string;
  stored_feature_hash?: string;
  semantic_status?: string;
}

export interface NodesResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  nodes: NodeRecord[];
  count: number;
}

export interface EdgeRecord {
  src: string;
  dst: string;
  type?: string;
  edge_type?: string;
  evidence?: string;
  direction?: string;
  confidence?: number;
}

export interface EdgesResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  edges: EdgeRecord[];
  count: number;
}

export interface ProjectionResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  projection_id?: string;
  event_watermark?: string;
  base_commit?: string;
  // null when the snapshot's semantic projection has not been computed yet
  // (e.g. immediately after a /reconcile/full activation).
  projection: {
    project_id: string;
    snapshot_id: string;
    commit_sha: string;
    schema_version: number;
    node_semantics: Record<string, ProjectionNodeEntry>;
    edge_semantics: Record<string, unknown>;
    health_review: unknown;
  } | null;
  health: Record<string, unknown>;
}

export interface ProjectionNodeEntry {
  node_id: string;
  semantic: NodeSemantic;
  validity: NodeValidity;
  source_event?: { event_id?: string; event_seq?: number; updated_at?: string };
  stable_node_key?: string;
}

export interface OperationsQueueResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  active_snapshot_id: string;
  count: number;
  operations: OperationRow[];
  summary: OperationsSummary;
}

export interface OperationRow {
  operation_id: string;
  operation_type: string;
  target_scope: string;
  target_id: string;
  target_label: string;
  status: string;
  progress: { done: number; total: number };
  created_at: string;
  updated_at: string;
  claimed_by: string;
  worker_id: string;
  lease_expires_at: string;
  last_error: string;
  last_result: string;
  trace_id: string;
  supported_actions: string[];
}

export interface OperationsSummary {
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  pending_scope_reconcile_count: number;
  semantic_denominators?: {
    node_current: number;
    node_unverified: number;
    node_missing: number;
    node_stale: number;
    edge_eligible: number;
    edge_current: number;
    edge_requested: number;
    edge_missing: number;
  };
  feedback_queue?: { raw_count: number; visible_group_count: number; visible_item_count: number };
  graph_correction_patches?: { total: number; proposed_count: number; rejected_count: number };
}

export interface FeedbackQueueResponse {
  ok: boolean;
  project_id: string;
  snapshot_id: string;
  group_count: number;
  count: number;
  groups: FeedbackQueueGroup[];
  summary: FeedbackQueueSummary;
}

export interface FeedbackQueueGroup {
  queue_id: string;
  group_by: string;
  lane: string;
  action_hint?: string;
  priority?: string;
  source_node_ids?: string[];
  target_type: "node" | "edge" | string;
  target_id: string;
  issue_type?: string;
  target_ids?: string[];
  target_count?: number;
  representative_feedback_id: string;
  representative_issue: string;
  feedback_ids: string[];
  item_count: number;
  suppressed_count?: number;
  active_claim_count?: number;
  claim?: Record<string, unknown>;
  semantic_review_ready?: boolean;
  semantic_review_gate?: {
    ready?: boolean;
    reason?: string;
    source_node_ids?: string[];
    statuses?: Record<string, {
      status?: string;
      feature_hash?: string;
      has_file_hashes?: boolean;
      updated_at?: string;
    }>;
    missing_node_ids?: string[];
    pending_node_ids?: string[];
    stale_node_ids?: string[];
  };
  requires_human_signoff?: boolean;
  confidence?: number;
  created_at?: string;
  updated_at?: string;
}

export interface FeedbackQueueSummary {
  raw_count: number;
  visible_group_count: number;
  visible_item_count: number;
  hidden_status_observation_count: number;
  hidden_resolved_count: number;
  hidden_claimed_count: number;
  hidden_semantic_pending_count: number;
  require_current_semantic: boolean;
  by_kind: Record<string, number>;
  by_status: Record<string, number>;
  by_lane_all_items: Record<string, number>;
  by_lane_visible_groups: Record<string, number>;
}
