// Re-runs the dashboard's semantic classifier against the live governance
// service and reports the headline tallies that the acceptance checks cover.
// Run with: node scripts/verify-acceptance.mjs
const BACKEND = process.env.VITE_BACKEND_URL || "http://localhost:40000";
const PROJECT = process.env.VITE_PROJECT_ID || "aming-claw";

async function getJSON(path) {
  const r = await fetch(`${BACKEND}${path}`, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

function isPackageMarker(n) {
  if (!n) return false;
  const meta = n.metadata || {};
  return Boolean(
    n.exclude_as_feature ||
      meta.exclude_as_feature ||
      (meta.feature_metadata && meta.feature_metadata.exclude_as_feature) ||
      (meta.module && /\.__init__$/.test(meta.module)) ||
      (n.title && n.title.endsWith(".__init__")),
  );
}

function hasMeaningfulSemanticBody(sem) {
  return Boolean(
    sem.feature_name ||
      sem.semantic_summary ||
      sem.intent ||
      sem.domain_label ||
      (sem.feature_hash && sem.feature_hash !== "") ||
      (sem.status && sem.status !== "" && sem.status !== "structure_only" && sem.status !== "missing"),
  );
}
function classify(n) {
  const sem = n.semantic || {};
  const v = sem.validity || {};
  const vs = (v.status || v.hash_state || "").toLowerCase();
  const fs = (v.file_hash_status || "").toLowerCase();
  const hv = (v.hash_validation || "").toLowerCase();
  const noPayload =
    sem.has_semantic_payload === false || vs === "semantic_missing" || hv === "missing_semantic_event";
  if (noPayload && !hasMeaningfulSemanticBody(sem)) return "structure_only";
  const currentByValidity =
    !vs.includes("stale") &&
    (
      ["semantic_current", "semantic_carried_forward_current", "current", "valid"].includes(vs) ||
      (v.valid === true && ["matched_current", "matched_carried_forward", "file_hash_matched", "hash_source_unavailable"].includes(hv)) ||
      (v.valid === true && fs === "match")
    );
  const status = (sem.status || sem.node_status || "").toLowerCase();
  if (currentByValidity) return status === "reviewed" || status === "semantic_reviewed" ? "reviewed" : "semantic_complete";
  if (sem.hash_state === "stale" || vs === "stale" || vs.includes("stale") || ["stale", "drifted", "changed"].includes(fs) || hv === "stale") return "semantic_stale";
  if (
    sem.hash_state === "unverified" ||
    sem.hash_state === "hash_unverified" ||
    ["unverified", "hash_unverified", "mismatch", "invalid"].includes(vs) ||
    v.valid === false ||
    v.feature_hash_match === false ||
    v.file_hash_match === false ||
    ["unverified", "mismatch"].includes(fs) ||
    ["unverified", "mismatch", "failed"].includes(hv) ||
    v.hash_verified === false
  ) {
    return "semantic_hash_unverified";
  }
  if (status === "ai_complete" || status === "complete") return "semantic_complete";
  if (status === "reviewed" || status === "semantic_reviewed") return "reviewed";
  if (status === "review_pending") return "review_pending";
  if (["queued", "running", "pending", "ai_pending"].includes(status)) return "semantic_pending";
  if (["failed", "ai_failed", "error"].includes(status)) return "semantic_failed";
  return "structure_only";
}

(async () => {
  const [health, status, summary, projection, ops] = await Promise.all([
    getJSON("/api/health"),
    getJSON(`/api/graph-governance/${PROJECT}/status`),
    getJSON(`/api/graph-governance/${PROJECT}/snapshots/active/summary`),
    getJSON(`/api/graph-governance/${PROJECT}/snapshots/active/semantic/projection`),
    getJSON(`/api/graph-governance/${PROJECT}/operations/queue`),
  ]);
  const snapshotId = status.active_snapshot_id;
  const nodes = (await getJSON(`/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/nodes?include_semantic=true&limit=1000`)).nodes;
  const projSem = projection.projection.node_semantics || {};

  // Merge projection.validity into nodes (mirroring mergeProjection)
  for (const n of nodes) {
    const e = projSem[n.node_id];
    if (!e) continue;
    const projNS = e.semantic || {};
    n.semantic = {
      ...(n.semantic || {}),
      ...projNS,
      validity: e.validity || projNS.validity || {},
    };
  }

  // L7 governed tally (root container = whole project)
  const tally = { total: 0, complete: 0, reviewed: 0, stale: 0, hash_unverified: 0, struct: 0, pending: 0, running: 0, failed: 0, review: 0 };
  let markers = 0;
  for (const n of nodes) {
    if (n.layer !== "L7") continue;
    if (isPackageMarker(n)) {
      markers++;
      continue;
    }
    tally.total++;
    const s = classify(n);
    switch (s) {
      case "semantic_complete": tally.complete++; break;
      case "reviewed": tally.reviewed++; break;
      case "semantic_stale": tally.stale++; break;
      case "semantic_hash_unverified": tally.hash_unverified++; break;
      case "semantic_pending": tally.pending++; break;
      case "semantic_running": tally.running++; break;
      case "semantic_failed": tally.failed++; break;
      case "review_pending": tally.review++; break;
      default: tally.struct++;
    }
  }

  const sh = summary.health.semantic_health;
  const banner = !commitsMatch(health.version, status.graph_snapshot_commit);
  const lines = [
    `service.version              = ${health.version}`,
    `status.graph_snapshot_commit = ${status.graph_snapshot_commit.slice(0, 7)}…`,
    `stale-graph banner shown?    = ${banner}`,
    "",
    `summary.semantic_health      → current=${sh.semantic_current_count} stale=${sh.semantic_stale_count} unverified=${sh.semantic_unverified_hash_count} missing=${sh.semantic_missing_count} of ${sh.feature_count}`,
    `summary.edge_semantic        → current=${sh.edge_semantic_current_count} missing=${sh.edge_semantic_missing_count} of ${sh.edge_semantic_eligible_count}`,
    "",
    `local L7 tally (governed)    → governed=${tally.total} markers=${markers} complete=${tally.complete} reviewed=${tally.reviewed} stale=${tally.stale} unverified=${tally.hash_unverified} struct=${tally.struct} pending=${tally.pending} running=${tally.running} failed=${tally.failed} review=${tally.review}`,
    `local tree-root meta render  → ${formatRootMeta(tally)}`,
    "",
    `operations.queue.count       = ${ops.count}`,
    `operations[0]                = ${ops.operations[0]?.operation_id} ${ops.operations[0]?.status} ${ops.operations[0]?.progress?.done}/${ops.operations[0]?.progress?.total}`,
    `operations[1]                = ${ops.operations[1]?.operation_id} ${ops.operations[1]?.status} ${ops.operations[1]?.progress?.done}/${ops.operations[1]?.progress?.total}`,
  ];
  console.log(lines.join("\n"));

  // Acceptance invariants (independent of how the queue is populated at the moment):
  //   - dashboard's local L7 classifier tally must match backend semantic_health exactly
  //   - operations queue must contain at least the node + edge rows the demo references
  const tallyMatchesBackend =
    tally.total === sh.feature_count &&
    tally.complete + tally.reviewed === sh.semantic_current_count &&
    tally.stale === sh.semantic_stale_count &&
    tally.hash_unverified === sh.semantic_unverified_hash_count;
  const opsHasNode = ops.operations.some((o) => o.operation_type === "node_semantic");
  const opsHasEdge = ops.operations.some((o) => o.operation_type === "edge_semantic");
  const ok = tallyMatchesBackend && opsHasNode && opsHasEdge;
  console.log("");
  console.log(`tally matches backend ?      = ${tallyMatchesBackend}`);
  console.log(`ops has node_semantic row ?  = ${opsHasNode}`);
  console.log(`ops has edge_semantic row ?  = ${opsHasEdge}`);
  console.log(ok ? "ACCEPTANCE OK" : "ACCEPTANCE FAIL");
  if (!ok) process.exit(1);
})().catch((e) => {
  console.error("ERROR:", e);
  process.exit(2);
});

function commitsMatch(a, b) {
  if (!a || !b) return false;
  const x = String(a).toLowerCase();
  const y = String(b).toLowerCase();
  if (x === y) return true;
  if (x.startsWith(y) || y.startsWith(x)) return true;
  return false;
}

function formatRootMeta(t) {
  const cur = t.complete + t.reviewed;
  const parts = [`${cur}/${t.total}`];
  if (t.stale > 0) parts.push(`${t.stale}S`);
  if (t.hash_unverified > 0) parts.push(`${t.hash_unverified}D`);
  if (t.struct > 0) parts.push(`${t.struct}M`);
  const pend = t.pending + t.running;
  if (pend > 0) parts.push(`${pend}P`);
  if (t.review > 0) parts.push(`${t.review}R`);
  return parts.join(" · ");
}
