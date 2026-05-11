import type { NodeRecord, NodeSemantic, NodeValidity, ProjectionNodeEntry } from "../types";

export type SemanticStatus =
  | "semantic_complete"
  | "semantic_stale"
  | "semantic_hash_unverified"
  | "semantic_pending"
  | "semantic_running"
  | "semantic_failed"
  | "review_pending"
  | "reviewed"
  | "structure_only";

// Mirrors the prototype's vStatus classifier (docs/dev/dashboard-prototype.html
// circa line 5597). The user spec is the contract:
//   - validity.status == semantic_current OR semantic_carried_forward_current → current
//   - validity.hash_validation == file_hash_matched OR hash_source_unavailable → current
//   - validity.file_hash_status == match AND validity.valid == true → current
//   - validity.status contains 'stale' OR file_hash_status in {stale,drifted,changed} → stale
//   - actual unverified/mismatch/invalid signals → hash-unverified
// Treat hash_source_unavailable as current, NOT as drift.
// Containers (L1/L2/L3/L4) carry no semantic of their own — projection sends
// validity.status='semantic_missing' for them. That is the "no payload" path,
// NOT the "drift" path.
export function classifyNode(node: NodeRecord): SemanticStatus {
  const sem: NodeSemantic = node.semantic ?? {};
  const v: NodeValidity = sem.validity ?? {};
  const vStatus = (v.status || v.hash_state || "").toLowerCase();
  const vFileStat = (v.file_hash_status || "").toLowerCase();
  const vHashVal = (v.hash_validation || "").toLowerCase();

  // No semantic payload at all (containers + freshly-discovered L7) → structure_only.
  // This includes cases where the projection only contains a "semantic_missing"
  // stub validity but no actual semantic body.
  const noPayload =
    sem.has_semantic_payload === false ||
    vStatus === "semantic_missing" ||
    vHashVal === "missing_semantic_event";
  if (noPayload && !hasMeaningfulSemanticBody(sem)) return "structure_only";

  const currentByValidity =
    !vStatus.includes("stale") &&
    (
      ["semantic_current", "semantic_carried_forward_current", "current", "valid"].includes(vStatus) ||
      (v.valid === true &&
        ["matched_current", "matched_carried_forward", "file_hash_matched", "hash_source_unavailable"].includes(
          vHashVal,
        )) ||
      (v.valid === true && vFileStat === "match")
    );

  const status = (sem.status || sem.node_status || "").toLowerCase();
  const jobStatus = (sem.job_status || "").toLowerCase();

  if (currentByValidity) {
    if (status === "reviewed" || status === "semantic_reviewed") return "reviewed";
    return "semantic_complete";
  }

  if (
    sem.hash_state === "stale" ||
    vStatus === "stale" ||
    vStatus.includes("stale") ||
    ["stale", "drifted", "changed"].includes(vFileStat) ||
    vHashVal === "stale"
  ) {
    return "semantic_stale";
  }

  if (
    sem.hash_state === "unverified" ||
    sem.hash_state === "hash_unverified" ||
    ["unverified", "hash_unverified", "mismatch", "invalid"].includes(vStatus) ||
    v.valid === false ||
    v.feature_hash_match === false ||
    v.file_hash_match === false ||
    ["unverified", "mismatch"].includes(vFileStat) ||
    ["unverified", "mismatch", "failed"].includes(vHashVal) ||
    v.hash_verified === false
  ) {
    return "semantic_hash_unverified";
  }

  if (status === "ai_complete" || status === "complete") return "semantic_complete";
  if (status === "reviewed" || status === "semantic_reviewed") return "reviewed";
  if (status === "review_pending") return "review_pending";
  if (["queued", "running", "pending", "ai_pending"].includes(status)) return "semantic_pending";
  if (status === "ai_running") return "semantic_running";
  if (["failed", "ai_failed", "error"].includes(status)) return "semantic_failed";
  if (["queued", "pending", "ai_pending"].includes(jobStatus)) return "semantic_pending";
  if (["running", "ai_running"].includes(jobStatus)) return "semantic_running";
  if (["failed", "ai_failed", "error"].includes(jobStatus)) return "semantic_failed";

  return "structure_only";
}

function hasMeaningfulSemanticBody(sem: NodeSemantic): boolean {
  // True when the node has actual AI-derived semantic content — feature_name,
  // summary, or a status that says the AI ran. A `validity.status` alone does
  // NOT count as a payload (containers carry validity stubs without a body).
  return Boolean(
    sem.feature_name ||
      sem.semantic_summary ||
      sem.intent ||
      sem.domain_label ||
      (sem.feature_hash && sem.feature_hash !== "") ||
      (sem.status && sem.status !== "" && sem.status !== "structure_only" && sem.status !== "missing"),
  );
}

// L7 governed feature filter, mirrors prototype's isPackageMarker + isGovernableFeature.
export function isPackageMarker(n: NodeRecord): boolean {
  if (!n) return false;
  const meta = n.metadata ?? {};
  return Boolean(
    n.exclude_as_feature ||
      meta.exclude_as_feature ||
      meta.feature_metadata?.exclude_as_feature ||
      (meta.module && /\.__init__$/.test(meta.module)) ||
      (n.title && n.title.endsWith(".__init__")),
  );
}

export function isGovernableFeature(n: NodeRecord): boolean {
  return n.layer === "L7" && !isPackageMarker(n);
}

// Merge per-node validity from /snapshots/active/semantic/projection into
// records returned by /snapshots/{id}/nodes. The /nodes endpoint omits
// validity.* by default; the projection endpoint is authoritative for it.
export function mergeProjection(
  nodes: NodeRecord[],
  nodeSemantics: Record<string, ProjectionNodeEntry> | undefined,
): NodeRecord[] {
  if (!nodeSemantics) return nodes;
  return nodes.map((n) => {
    const entry = nodeSemantics[n.node_id];
    if (!entry) return n;
    const projSem = entry.semantic ?? {};
    const validity = entry.validity ?? projSem.validity ?? {};
    const projHasBody = hasMeaningfulSemanticBody(projSem);
    const merged: NodeSemantic = {
      ...(n.semantic ?? {}),
      // Projection wins on validity-derived fields. Existing /nodes data
      // (status, hash_state, etc.) wins on identifying fields.
      validity,
      intent: projSem.intent ?? n.semantic?.intent,
      semantic_summary: projSem.semantic_summary ?? n.semantic?.semantic_summary,
      domain_label: projSem.domain_label ?? n.semantic?.domain_label,
      feature_name: projSem.feature_name ?? n.semantic?.feature_name,
      feature_hash: projSem.feature_hash ?? n.semantic?.feature_hash,
      doc_status: projSem.doc_status ?? n.semantic?.doc_status,
      test_status: projSem.test_status ?? n.semantic?.test_status,
      config_status: projSem.config_status ?? n.semantic?.config_status,
      feedback_round: projSem.feedback_round ?? n.semantic?.feedback_round,
      open_issues: projSem.open_issues ?? n.semantic?.open_issues,
      observer_decision: projSem.observer_decision ?? n.semantic?.observer_decision,
      review_status: projSem.review_status ?? n.semantic?.review_status,
      ai_route: projSem.ai_route ?? n.semantic?.ai_route,
      carried_forward_from_snapshot_id:
        projSem.carried_forward_from_snapshot_id ?? n.semantic?.carried_forward_from_snapshot_id,
      carried_forward_at: projSem.carried_forward_at ?? n.semantic?.carried_forward_at,
      has_semantic_payload:
        projSem.has_semantic_payload ??
        n.semantic?.has_semantic_payload ??
        projHasBody,
      status: n.semantic?.status ?? projSem.status,
      node_status: n.semantic?.node_status ?? projSem.node_status,
      hash_state: n.semantic?.hash_state ?? projSem.hash_state,
    };
    return { ...n, semantic: merged };
  });
}

export interface SubtreeAggregate {
  total: number;
  complete: number;
  reviewed: number;
  hash_unverified: number;
  pending: number;
  running: number;
  stale: number;
  failed: number;
  review: number;
  struct: number;
}

const EMPTY_AGG: SubtreeAggregate = {
  total: 0,
  complete: 0,
  reviewed: 0,
  hash_unverified: 0,
  pending: 0,
  running: 0,
  stale: 0,
  failed: 0,
  review: 0,
  struct: 0,
};

export function newSubtreeAggregate(): SubtreeAggregate {
  return { ...EMPTY_AGG };
}

export function aggregateNode(agg: SubtreeAggregate, node: NodeRecord): SubtreeAggregate {
  if (!isGovernableFeature(node)) return agg;
  agg.total++;
  const s = classifyNode(node);
  switch (s) {
    case "semantic_complete":
      agg.complete++;
      break;
    case "reviewed":
      agg.reviewed++;
      break;
    case "semantic_hash_unverified":
      agg.hash_unverified++;
      break;
    case "semantic_pending":
      agg.pending++;
      break;
    case "semantic_running":
      agg.running++;
      break;
    case "semantic_stale":
      agg.stale++;
      break;
    case "semantic_failed":
      agg.failed++;
      break;
    case "review_pending":
      agg.review++;
      break;
    case "structure_only":
    default:
      agg.struct++;
      break;
  }
  return agg;
}

export function semStatusLabel(s: SemanticStatus): string {
  switch (s) {
    case "semantic_complete":
      return "current";
    case "reviewed":
      return "reviewed";
    case "semantic_stale":
      return "stale";
    case "semantic_hash_unverified":
      return "hash-unverified";
    case "semantic_pending":
      return "pending";
    case "semantic_running":
      return "running";
    case "semantic_failed":
      return "failed";
    case "review_pending":
      return "review pending";
    case "structure_only":
    default:
      return "structure only";
  }
}

export function semStatusDotClass(s: SemanticStatus): string {
  switch (s) {
    case "semantic_complete":
      return "complete";
    case "reviewed":
      return "reviewed";
    case "semantic_stale":
      return "stale";
    case "semantic_hash_unverified":
      return "unverified";
    case "semantic_pending":
      return "pending";
    case "semantic_running":
      return "running";
    case "semantic_failed":
      return "failed";
    case "review_pending":
      return "review";
    case "structure_only":
    default:
      return "struct";
  }
}
