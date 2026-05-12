import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  projectId as DEFAULT_PROJECT_ID,
  setProjectId as setApiProjectId,
} from "./lib/api";
import { mergeProjection } from "./lib/semantic";
import { computeNodeHealth } from "./lib/health";
import { useEventStream } from "./lib/sse";
import type {
  ActiveSummaryResponse,
  BacklogResponse,
  EdgeRecord,
  FeedbackQueueResponse,
  HealthResponse,
  NodeRecord,
  OperationsQueueResponse,
  ProjectionResponse,
  StatusResponse,
} from "./types";
import Header from "./components/Header";
import StaleGraphBanner from "./components/StaleGraphBanner";
import TreePanel from "./components/TreePanel";
import InspectorDrawer, { type Tab as InspectorTabName } from "./components/InspectorDrawer";
import type { PinnedEdge } from "./components/FocusCard";
import ActionControlPanel, { type ActionKind, type ActionTarget, type EnrichPreset } from "./components/ActionControlPanel";
import ActionPanel from "./components/ActionPanel";
import type { BacklogDraft } from "./lib/api";
import type { AiConfigResponse, ProjectListItem } from "./lib/api";
import OverviewView from "./views/OverviewView";
import OperationsQueueView from "./views/OperationsQueueView";
import ReviewQueueView from "./views/ReviewQueueView";
import GraphView from "./views/GraphView";
import BacklogView from "./views/BacklogView";
import ProjectConsoleView from "./views/ProjectConsoleView";

export type ViewName = "projects" | "overview" | "graph" | "operations" | "review" | "backlog";

interface DataBundle {
  health: HealthResponse;
  status: StatusResponse;
  summary: ActiveSummaryResponse;
  projection: ProjectionResponse;
  nodes: NodeRecord[];
  edges: EdgeRecord[];
  ops: OperationsQueueResponse;
  feedback: FeedbackQueueResponse;
  backlog: BacklogResponse;
  loadedAt: string;
}

interface Toast {
  kind: "info" | "error" | "success";
  msg: string;
}

const CLOSED_BACKLOG_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED"]);

export default function App() {
  const [data, setData] = useState<DataBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<ViewName>("projects");
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [pinnedEdge, setPinnedEdge] = useState<PinnedEdge | null>(null);
  const [drawerTab, setDrawerTab] = useState<InspectorTabName>("overview");
  const [actionPanel, setActionPanel] = useState<{ kind: ActionKind; target: ActionTarget } | null>(null);
  const [actionPanelOpen, setActionPanelOpen] = useState(false);
  const [actionPanelInitialTab, setActionPanelInitialTab] = useState<"review" | "backlog">("review");
  const [actionPanelPrefill, setActionPanelPrefill] = useState<Partial<BacklogDraft> | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [reconcileBusy, setReconcileBusy] = useState(false);
  // MF-016 banner P3: surface reconcile progress inline. Each phase covers a
  // discrete step the user can read off — no more "did the click do anything?"
  const [reconcilePhase, setReconcilePhase] = useState<
    "idle" | "queueing" | "materializing" | "rebuilding" | "done" | "error"
  >("idle");
  const [reconcileDetail, setReconcileDetail] = useState<string>("");
  // Multi-select mode: operator toggles it on to batch-enrich many targets at
  // once. Graph clicks switch from "pin / select" to "add to bucket". IDs are
  // prefixed `node:<id>` / `edge:<id>` so the single Set can hold both.
  const [multiSelectMode, setMultiSelectMode] = useState(false);
  const [multiSelectIds, setMultiSelectIds] = useState<Set<string>>(() => new Set());
  const multiSelectIdsRef = useRef<Set<string>>(new Set());
  const [batchEnrichBusy, setBatchEnrichBusy] = useState(false);
  const [currentProjectId, setCurrentProjectId] = useState(DEFAULT_PROJECT_ID);
  const [projects, setProjects] = useState<ProjectListItem[]>([]);
  const [aiConfig, setAiConfig] = useState<AiConfigResponse | null>(null);
  const [aiConfigOpen, setAiConfigOpen] = useState(false);

  useEffect(() => {
    multiSelectIdsRef.current = multiSelectIds;
  }, [multiSelectIds]);

  const fetchAll = useCallback(async (signal?: AbortSignal) => {
    setApiProjectId(currentProjectId);
    setLoading(true);
    setError(null);
    try {
      const [health, status, summary, projection, ops, backlog, projectList, aiCfg] = await Promise.all([
        api.health(signal),
        api.status(signal),
        api.activeSummary(signal),
        api.activeProjection(signal),
        api.operationsQueue(signal),
        api.backlog(signal),
        api.projects(signal),
        api.aiConfig(signal),
      ]);
      setProjects(projectList.projects ?? []);
      setAiConfig(aiCfg);
      const snapshotId = status.active_snapshot_id || summary.snapshot_id;
      const [nodesRes, edgesRes, feedback] = await Promise.all([
        api.nodes(snapshotId, 1000, signal),
        api.edges(snapshotId, 4000, signal),
        api.feedbackQueue(snapshotId, signal),
      ]);
      // projection.projection is null when the snapshot was just rebuilt and
      // the semantic projection hasn't been computed yet. mergeProjection
      // tolerates an empty map.
      const merged = mergeProjection(nodesRes.nodes, projection?.projection?.node_semantics ?? {});
      // Per-node feature health (prototype algorithm — leafScore for L7 leaves,
      // recursive average for containers; L4 leaves are intentionally unscored).
      const healthMap = computeNodeHealth(merged, edgesRes.edges);
      const mergedWithHealth = merged.map((n) => {
        const h = healthMap.get(n.node_id);
        return h ? { ...n, _health: h._health } : n;
      });
      setData({
        health,
        status,
        summary,
        projection,
        nodes: mergedWithHealth,
        edges: edgesRes.edges,
        ops,
        feedback,
        backlog,
        loadedAt: new Date().toISOString(),
      });
    } catch (e) {
      if ((e as { name?: string }).name === "AbortError") return;
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setError(msg);
      setToast({ kind: "error", msg: `Load failed: ${msg}` });
    } finally {
      setLoading(false);
    }
  }, [currentProjectId]);

  useEffect(() => {
    const ac = new AbortController();
    fetchAll(ac.signal);
    return () => ac.abort();
  }, [fetchAll]);

  // Live sync: SSE pushes a 'dashboard.changed' event whenever any mutating
  // graph-governance endpoint succeeds (server.py:_emit_dashboard_changed),
  // plus pass-through for node.status_changed etc. We debounce a refetch so
  // bursts (e.g. worker draining 20 nodes in 1s) collapse into one call.
  const refetchTimerRef = useRef<number | null>(null);
  const scheduleLiveRefetch = useCallback(() => {
    if (refetchTimerRef.current != null) window.clearTimeout(refetchTimerRef.current);
    refetchTimerRef.current = window.setTimeout(() => {
      refetchTimerRef.current = null;
      // Only re-fetch when not already loading — avoids fetchAll re-entrancy
      // (the in-flight load will pick up the latest server state anyway).
      if (!loading) void fetchAll();
    }, 600);
  }, [fetchAll, loading]);

  useEffect(
    () => () => {
      if (refetchTimerRef.current != null) window.clearTimeout(refetchTimerRef.current);
    },
    [],
  );

  const liveStatus = useEventStream(currentProjectId, {
    enabled: true,
    onEvent: scheduleLiveRefetch,
  });

  const handleQueueReconcile = useCallback(async () => {
    if (reconcileBusy) return;
    const headCommit = data?.status?.current_state?.graph_stale?.head_commit;
    const snapCommit = data?.status?.graph_snapshot_commit;
    if (!headCommit) {
      setToast({ kind: "error", msg: "Cannot reconcile: HEAD commit unknown (status response missing)." });
      return;
    }
    const ok = window.confirm(
      `Catch the active graph up to HEAD ${headCommit.slice(0, 7)}? Runs the ` +
        "scope reconcile inline (materialize+activate → projection rebuild). " +
        "The banner shows live progress.",
    );
    if (!ok) return;
    setReconcileBusy(true);
    // Skip the "queueing" phase chip — the queue API call is ~100ms and the
    // visible step just flashed by. Start at "materializing" which covers
    // the queue + build round-trip from the operator's POV.
    setReconcilePhase("materializing");
    setReconcileDetail(`target ${headCommit.slice(0, 7)}`);
    try {
      // Step 1: enqueue the pending-scope row.
      const queueRes = await api.queueScopeReconcile({
        commit_sha: headCommit,
        parent_commit_sha: snapCommit ?? undefined,
        actor: "dashboard_user",
      });
      const queueStatus = queueRes.pending_scope_reconcile?.status ?? "queued";
      // Store preserves waived/materialized across upserts (graph_snapshot_store
      // .queue_pending_scope_reconcile:2313). Tell the operator clearly that
      // a previously cancelled commit will not re-queue without a new commit.
      if (queueStatus === "waived" || queueStatus === "materialized") {
        setReconcilePhase("error");
        setReconcileDetail(
          `${headCommit.slice(0, 7)} is already ${queueStatus} — make a new commit to re-arm`,
        );
        setToast({
          kind: "info",
          msg:
            `Reconcile NOT queued · ${headCommit.slice(0, 7)} is already ${queueStatus} ` +
            `(this commit was previously ${queueStatus === "waived" ? "cancelled" : "materialized"}). ` +
            `Make a new commit on main to re-arm.`,
        });
        return;
      }
      // Step 2: materialize + activate in one round-trip (MF-2026-05-10-014).
      // MF-2026-05-10-012's activate hook auto-rebuilds the projection.
      setReconcilePhase("materializing");
      setReconcileDetail(`building snapshot for ${headCommit.slice(0, 7)}`);
      const runRes = await api.materializeAndActivatePendingScope({
        target_commit_sha: headCommit,
        semantic_use_ai: false, // rule-based + carry-forward only; no AI billed
        actor: "dashboard_user",
      });
      const newSid = runRes.snapshot_id || runRes.activation?.snapshot_id || "(unknown)";
      const projection = runRes.activation?.projection_status ?? "(unknown)";
      setReconcilePhase("rebuilding");
      setReconcileDetail(`activated ${newSid.slice(0, 20)} · projection ${projection}`);
      setToast({
        kind: "success",
        msg:
          `Scope reconcile complete · snapshot=${newSid.slice(0, 24)} · ` +
          `projection=${projection}. Refreshing dashboard…`,
      });
      await fetchAll();
      setReconcilePhase("done");
      setReconcileDetail(`active snapshot is now ${newSid.slice(0, 20)}`);
      // Drop the banner status after a short visible window so the operator can
      // confirm it succeeded; the banner itself will disappear once the stale
      // condition clears in the refreshed status.
      window.setTimeout(() => {
        setReconcilePhase("idle");
        setReconcileDetail("");
      }, 4000);
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setReconcilePhase("error");
      setReconcileDetail(msg.slice(0, 120));
      setToast({ kind: "error", msg: `Reconcile failed: ${msg}` });
    } finally {
      setReconcileBusy(false);
    }
  }, [reconcileBusy, data, fetchAll]);

  const handleClearTerminal = useCallback(async () => {
    const snapshotId = data?.status?.active_snapshot_id;
    if (!snapshotId) {
      setToast({ kind: "error", msg: "No active snapshot." });
      return;
    }
    const ok = window.confirm(
      "Permanently delete all cancelled / complete / failed node queue rows from this snapshot? Edge audit events are preserved.",
    );
    if (!ok) return;
    try {
      const res = await api.clearTerminalSemanticJobs(snapshotId, {});
      setToast({
        kind: res.ok ? "success" : "error",
        msg: `Clear terminal · deleted_nodes=${res.deleted_count} · edge_audit_matched=${res.edge_audit_matched}`,
      });
      fetchAll();
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setToast({ kind: "error", msg: `Clear terminal failed: ${msg}` });
    }
  }, [data, fetchAll]);

  const handleCancelAllByType = useCallback(
    async (opType: "node_semantic" | "edge_semantic") => {
      const snapshotId = data?.status?.active_snapshot_id;
      if (!snapshotId) {
        setToast({ kind: "error", msg: "No active snapshot." });
        return;
      }
      const ok = window.confirm(
        `Cancel ALL queued ${opType} jobs in this snapshot? Terminal rows are not affected.`,
      );
      if (!ok) return;
      try {
        const res = await api.cancelAllSemanticJobs(snapshotId, {
          operation_type: opType,
          status: "queued",
        });
        setToast({
          kind: res.ok ? "success" : "error",
          msg: `Cancel-all ${opType} · cancelled=${res.cancelled_count} · skipped_terminal=${res.skipped_terminal} · matched=${res.matched_count ?? "?"}`,
        });
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({ kind: "error", msg: `Cancel-all failed: ${msg}` });
      }
    },
    [data, fetchAll],
  );

  const handleFeedbackDecision = useCallback(
    async (feedbackIds: string[], action: string, summaryHint?: string) => {
      const snapshotId = data?.status?.active_snapshot_id;
      if (!snapshotId) {
        setToast({ kind: "error", msg: "No active snapshot." });
        return;
      }
      if (feedbackIds.length === 0) {
        setToast({ kind: "error", msg: "No feedback ids selected." });
        return;
      }
      const idsLabel = feedbackIds.length === 1 ? feedbackIds[0] : `${feedbackIds.length} items`;
      const ok = window.confirm(
        `${action} for ${idsLabel}${summaryHint ? ` (${summaryHint})` : ""}?`,
      );
      if (!ok) return;
      try {
        const res = await api.decideFeedback(snapshotId, {
          feedback_ids: feedbackIds,
          action,
        });
        const accepted = res.semantic_enrichment_accepted;
        const flipped = accepted?.node_ids_flipped?.length ?? 0;
        const proj = res.projection_rebuilt ? "projection rebuilt" : "projection unchanged";
        setToast({
          kind: res.ok === false ? "error" : "success",
          msg:
            `${action} · decided=${res.decided_count ?? 0} · errors=${res.error_count ?? 0}` +
            (action === "accept_semantic_enrichment"
              ? ` · nodes flipped=${flipped} · ${proj}`
              : ""),
        });
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({ kind: "error", msg: `${action} failed: ${msg}` });
      }
    },
    [data, fetchAll],
  );

  const handleFeedbackRetry = useCallback(
    async (feedbackIds: string[], nodeId: string, rationale: string) => {
      const snapshotId = data?.status?.active_snapshot_id;
      if (!snapshotId) {
        setToast({ kind: "error", msg: "No active snapshot." });
        return;
      }
      const reason = rationale.trim();
      if (!reason) {
        setToast({ kind: "error", msg: "Retry needs a rationale." });
        return;
      }
      try {
        // Step 1: close the current feedback row as rejected with the operator
        // rationale so the Review Queue stops showing the stale proposal.
        await api.decideFeedback(snapshotId, {
          feedback_ids: feedbackIds,
          action: "reject_false_positive",
          rationale: reason,
        });
        // Step 2: append the rationale to the JSONL semantic-feedback store
        // that run_semantic_enrichment reads — this is what the next AI run
        // sees in its `review_feedback` array alongside `existing_semantic`.
        await api.appendSemanticFeedback(
          snapshotId,
          [
            {
              target_type: "node",
              target_id: nodeId,
              issue: reason,
              priority: "P2",
              source_node_ids: [nodeId],
              reason,
            },
          ],
          "dashboard_user",
        );
        // Step 3: re-enqueue the node. Worker picks it up via EventBus and the
        // AI call now has the prior rejected semantic + new rationale.
        await api.submitSemanticJob(snapshotId, {
          job_type: "semantic_enrichment",
          target_scope: "node",
          target_ids: [nodeId],
          options: { mode: "retry", target: "nodes" },
          created_by: "dashboard_user",
        });
        setToast({
          kind: "success",
          msg: `Retry queued for ${nodeId} · prior proposal rejected · rationale forwarded to next AI run`,
        });
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({ kind: "error", msg: `Retry failed: ${msg}` });
      }
    },
    [data, fetchAll],
  );

  // Multi-select handlers
  const toggleMultiSelect = useCallback((kind: "node" | "edge", id: string) => {
    const next = new Set(multiSelectIdsRef.current);
    const key = `${kind}:${id}`;
    if (next.has(key)) next.delete(key);
    else next.add(key);
    multiSelectIdsRef.current = next;
    setMultiSelectIds(next);
  }, []);

  const clearMultiSelect = useCallback(() => {
    const next = new Set<string>();
    multiSelectIdsRef.current = next;
    setMultiSelectIds(next);
  }, []);

  const handleBatchEnrich = useCallback(async () => {
    const snapshotId = data?.status?.active_snapshot_id;
    if (!snapshotId) {
      setToast({ kind: "error", msg: "No active snapshot." });
      return;
    }
    const selectedKeys = Array.from(multiSelectIdsRef.current);
    if (selectedKeys.length === 0) {
      setToast({ kind: "error", msg: "Pick at least one node or edge first." });
      return;
    }
    const nodeIds: string[] = [];
    const edgeIds: string[] = [];
    selectedKeys.forEach((k) => {
      const idx = k.indexOf(":");
      const kind = k.slice(0, idx);
      const id = k.slice(idx + 1);
      if (kind === "node") nodeIds.push(id);
      else if (kind === "edge") edgeIds.push(id);
    });
    const ok = window.confirm(
      `Queue AI enrich for ${nodeIds.length} node(s) and ${edgeIds.length} edge(s)?`,
    );
    if (!ok) return;
    setBatchEnrichBusy(true);
    try {
      const summary: string[] = [];
      const partial: string[] = [];
      if (nodeIds.length > 0) {
        const res = await api.submitSemanticJob(snapshotId, {
          job_type: "semantic_enrichment",
          target_scope: "node",
          target_ids: nodeIds,
          options: {
            target: "nodes",
            scope: "selected_nodes",
            mode: "retry",
            dry_run: false,
            include_nodes: true,
            include_edges: false,
            skip_current: false,
            retry_stale_failed: true,
            include_package_markers: false,
          },
          created_by: "dashboard_user",
        });
        const queued = res.queued_count ?? nodeIds.length;
        summary.push(`nodes ${queued}/${nodeIds.length}`);
        if (queued < nodeIds.length) partial.push(`nodes queued ${queued}/${nodeIds.length}`);
      }
      if (edgeIds.length > 0) {
        const res = await api.submitSemanticJob(snapshotId, {
          job_type: "semantic_enrichment",
          target_scope: "edge",
          target_ids: edgeIds,
          options: {
            target: "edges",
            scope: "selected_edges",
            mode: "semanticize",
            dry_run: false,
            include_nodes: false,
            include_edges: true,
          },
          created_by: "dashboard_user",
        });
        const queued = res.queued_count ?? edgeIds.length;
        summary.push(`edges ${queued}/${edgeIds.length}`);
        if (queued < edgeIds.length) partial.push(`edges queued ${queued}/${edgeIds.length}`);
      }
      setToast({
        kind: partial.length > 0 ? "info" : "success",
        msg: `Batch AI enrich queued · ${summary.join(" · ")}${partial.length > 0 ? " · check queue for skipped/current targets" : ""}`,
      });
      const next = new Set<string>();
      multiSelectIdsRef.current = next;
      setMultiSelectIds(next);
      // Auto-exit multi-select mode so subsequent clicks behave normally.
      setMultiSelectMode(false);
      fetchAll();
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
      setToast({ kind: "error", msg: `Batch enrich failed: ${msg}` });
    } finally {
      setBatchEnrichBusy(false);
    }
  }, [data, fetchAll]);

  const handleSelectNodeFromReview = useCallback(
    (id: string) => {
      setSelectedNodeId(id);
      setPinnedEdge(null);
      setView("graph");
      setDrawerTab("overview");
    },
    [],
  );

  // MF-016 P3 follow-up: edge feedback rows use target_id of the form
  // `<src>-><dst>:<type>` (e.g. "L7.1->L4.1:creates_task"). Parse it and pin
  // the matching edge on the graph view so clicking the Review Queue chip
  // jumps the same way node clicks do.
  const handleSelectEdgeFromReview = useCallback(
    (edgeId: string) => {
      // Edge target_id ships in two formats depending on who created the
      // feedback row:
      //   1. `<src>-><dst>:<type>` — worker writes this when it enriches an
      //      edge_semantic_requested event (semantic_worker, server.py edge
      //      POST handler).
      //   2. `<src>|<dst>|<type>` — ActionControlPanel writes this when the
      //      operator files feedback from the graph view.
      // Try both, normalize to {src, dst, type}.
      let src = "", dst = "", type = "";
      if (edgeId.includes("|")) {
        const parts = edgeId.split("|");
        if (parts.length < 2) {
          setToast({ kind: "error", msg: `Cannot parse edge id ${edgeId}` });
          return;
        }
        [src, dst, type = ""] = parts;
      } else {
        const arrowIdx = edgeId.indexOf("->");
        if (arrowIdx < 0) {
          setToast({ kind: "error", msg: `Cannot parse edge id ${edgeId}` });
          return;
        }
        src = edgeId.slice(0, arrowIdx);
        const rest = edgeId.slice(arrowIdx + 2);
        const colonIdx = rest.indexOf(":");
        dst = colonIdx >= 0 ? rest.slice(0, colonIdx) : rest;
        type = colonIdx >= 0 ? rest.slice(colonIdx + 1) : "";
      }
      // Look up the real edge record so confidence/evidence/direction come
      // from the live graph instead of being blanked out on the pin.
      const real = data?.edges.find(
        (e) =>
          e.src === src && e.dst === dst && (e.type === type || e.edge_type === type),
      );
      setSelectedNodeId(null);
      setPinnedEdge({
        src,
        dst,
        type: type || real?.type || real?.edge_type || "",
        evidence: real?.evidence,
        direction: real?.direction,
        confidence: real?.confidence,
      });
      setView("graph");
      setDrawerTab("overview");
    },
    [data],
  );

  const handleCancelOperation = useCallback(
    async (opType: string, opId: string, targetId: string) => {
      const snapshotId = data?.status?.active_snapshot_id;
      try {
        if (opType === "scope_reconcile") {
          const ok = window.confirm(`Cancel scope reconcile for ${targetId.slice(0, 12)}?`);
          if (!ok) return;
          const res = await api.cancelScopeReconcile({ operation_id: opId });
          setToast({
            kind: res.status === "cancelled" ? "success" : "info",
            msg: `Reconcile cancel · ${res.status} · waived=${res.cancelled_count}`,
          });
        } else if (opType === "node_semantic" || opType === "edge_semantic") {
          if (!snapshotId) throw new Error("no active snapshot");
          const ok = window.confirm(`Cancel ${opType} job for ${targetId}?`);
          if (!ok) return;
          const res = await api.cancelSemanticJob(snapshotId, targetId);
          setToast({
            kind: res.ok ? "success" : "error",
            msg: `${opType} cancel · status=${res.job?.status ?? "?"}`,
          });
        } else {
          setToast({ kind: "info", msg: `Cancel for ${opType} not wired yet.` });
          return;
        }
        fetchAll();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setToast({ kind: "error", msg: `Cancel failed: ${msg}` });
      }
    },
    [data, fetchAll],
  );

  const handleRefresh = useCallback(() => {
    fetchAll();
  }, [fetchAll]);

  const handleProjectChange = useCallback((nextProjectId: string) => {
    const next = nextProjectId.trim() || DEFAULT_PROJECT_ID;
    setApiProjectId(next);
    setCurrentProjectId(next);
    setData(null);
    setError(null);
    setSelectedNodeId(null);
    setPinnedEdge(null);
    setMultiSelectIds(new Set());
    multiSelectIdsRef.current = new Set();
  }, []);

  const handleOpenProject = useCallback(
    (nextProjectId: string) => {
      handleProjectChange(nextProjectId);
      setView("overview");
    },
    [handleProjectChange],
  );

  // Auto-dismiss toasts.
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(t);
  }, [toast]);

  const selectedNode = useMemo(() => {
    if (!data || !selectedNodeId) return null;
    return data.nodes.find((n) => n.node_id === selectedNodeId) ?? null;
  }, [data, selectedNodeId]);

  const handleSelectNode = useCallback((id: string) => {
    // Tree / drawer / FocusCard navigation always sets focus, even in
    // multi-select mode — operator needs to be able to position the graph
    // to pick targets from. Graph-internal clicks use handleGraphSelectNode
    // which toggles the bucket instead.
    setSelectedNodeId(id);
    setPinnedEdge(null);
    setView((prev) => (prev === "graph" ? prev : "graph"));
  }, []);

  const handleGraphSelectNode = useCallback(
    (id: string) => {
      if (multiSelectMode) {
        toggleMultiSelect("node", id);
        return;
      }
      setSelectedNodeId(id);
      setPinnedEdge(null);
    },
    [multiSelectMode, toggleMultiSelect],
  );

  const handlePinEdge = useCallback(
    (edge: PinnedEdge | null) => {
      if (multiSelectMode && edge) {
        // Encode the canonical edge id used by the worker / projection
        // (`<src>-><dst>:<type>`).
        toggleMultiSelect("edge", `${edge.src}->${edge.dst}:${edge.type}`);
        return;
      }
      setPinnedEdge(edge);
    },
    [multiSelectMode, toggleMultiSelect],
  );

  const handleOpenDrawerTab = useCallback((tab: InspectorTabName) => {
    setDrawerTab(tab);
  }, []);

  const handleOpenAction = useCallback((kind: ActionKind, target: ActionTarget) => {
    setActionPanel({ kind, target });
  }, []);

  const handleOpenPreset = useCallback((preset: EnrichPreset) => {
    setActionPanelOpen(false);
    setActionPanel({ kind: "enrich", target: { preset } });
  }, []);

  const handleOpenBacklog = useCallback((target: ActionTarget) => {
    // Pre-fill the backlog draft from the target node so the form lands ready
    // to submit. For edges we use the source node's primary files; the backend
    // will store both endpoints in affected_graph_nodes.
    let prefill: Partial<BacklogDraft> = {};
    if (target.node) {
      const n = target.node;
      const lastSeg = (n.title || n.node_id).split(".").slice(-1)[0];
      prefill = {
        title: `Follow-up on ${lastSeg}`,
        target_files: (n.primary_files ?? []).slice(0, 3) as unknown as BacklogDraft["target_files"],
        affected_graph_nodes: [n.node_id] as unknown as BacklogDraft["affected_graph_nodes"],
      };
    } else if (target.edge) {
      const e = target.edge;
      prefill = {
        title: `Follow-up on ${e.type} edge`,
        affected_graph_nodes: [e.src, e.dst] as unknown as BacklogDraft["affected_graph_nodes"],
      };
    }
    // BacklogDraft form fields hold raw textarea/csv strings during editing;
    // convert array → string for the seed.
    if (prefill.target_files) {
      prefill = { ...prefill, target_files: (prefill.target_files as unknown as string[]).join("\n") as unknown as BacklogDraft["target_files"] };
    }
    if (prefill.affected_graph_nodes) {
      prefill = {
        ...prefill,
        affected_graph_nodes: (prefill.affected_graph_nodes as unknown as string[]).join(", ") as unknown as BacklogDraft["affected_graph_nodes"],
      };
    }
    setActionPanelPrefill(prefill);
    setActionPanelInitialTab("backlog");
    setActionPanelOpen(true);
  }, []);

  return (
    <div className="app">
      <Header
        loading={loading}
        summary={data?.summary}
        status={data?.status}
        health={data?.health}
        ops={data?.ops}
        loadedAt={data?.loadedAt}
        projectId={currentProjectId}
        projects={projects}
        aiConfig={aiConfig}
        reviewBadge={data?.feedback?.summary?.visible_group_count ?? 0}
        onRefresh={handleRefresh}
        onProjectChange={handleProjectChange}
        onOpenAiConfig={() => setAiConfigOpen(true)}
        onOpenReview={() => setActionPanelOpen(true)}
        liveStatus={liveStatus}
        multiSelectMode={multiSelectMode}
        multiSelectCount={multiSelectIds.size}
        batchEnrichBusy={batchEnrichBusy}
        onToggleMultiSelect={() => {
          setMultiSelectMode((prev) => !prev);
          if (multiSelectMode) {
            const next = new Set<string>();
            multiSelectIdsRef.current = next;
            setMultiSelectIds(next);
          }
        }}
        onBatchEnrich={handleBatchEnrich}
        onClearMultiSelect={clearMultiSelect}
      />
      <StaleGraphBanner
        health={data?.health}
        status={data?.status}
        busy={reconcileBusy}
        phase={reconcilePhase}
        phaseDetail={reconcileDetail}
        onQueueReconcile={handleQueueReconcile}
      />
      <div className="app-body">
        <TreePanel
          nodes={data?.nodes ?? []}
          selectedNodeId={selectedNodeId}
          activeView={view}
          opsCount={data?.ops?.count ?? 0}
          reviewCount={data?.feedback?.summary?.visible_group_count ?? 0}
          backlogCount={countOpenBacklog(data?.backlog)}
          projectCount={projects.length}
          onSelectNode={handleSelectNode}
          onSelectView={(v) => setView(v)}
          loading={loading}
        />
        <main className="main scrollbar-thin">
          {error && !data && view !== "projects" ? (
            <div className="view">
              <div className="empty">
                Load failed. Check the governance service is reachable at{" "}
                <span className="mono">/api/*</span>.<br />
                <span className="mono" style={{ color: "var(--ink-700)" }}>{error}</span>
              </div>
            </div>
          ) : null}
          {view === "projects" ? (
            <ProjectConsoleView
              projects={projects}
              currentProjectId={currentProjectId}
              loading={loading}
              onOpenProject={handleOpenProject}
              onOpenAiConfig={() => setAiConfigOpen(true)}
            />
          ) : null}
          {view === "overview" && data ? (
            <OverviewView data={data} onSelectNode={handleSelectNode} />
          ) : null}
          {view === "graph" && data ? (
            <div className="graph-with-drawer">
              <div className="graph-with-drawer-main">
                <GraphView
                  nodes={data.nodes}
                  edges={data.edges}
                  selectedNodeId={selectedNodeId}
                  pinnedEdge={pinnedEdge}
                  onPinEdge={handlePinEdge}
                  multiSelectMode={multiSelectMode}
                  multiSelectIds={multiSelectIds}
                  onSelectNode={handleGraphSelectNode}
                  onOpenDrawerTab={handleOpenDrawerTab}
                  onOpenAction={handleOpenAction}
                />
              </div>
              {pinnedEdge || selectedNode ? (
                <InspectorDrawer
                  node={selectedNode}
                  pinnedEdge={pinnedEdge}
                  allNodes={data.nodes}
                  edges={data.edges}
                  feedback={data.feedback}
                  snapshotId={data.status?.active_snapshot_id ?? data.summary?.snapshot_id ?? null}
                  edgeSemantics={
                    (data.projection?.projection?.edge_semantics as
                      | Record<string, unknown>
                      | undefined) ?? null
                  }
                  onSelectNode={handleSelectNode}
                  onClose={() => {
                    setSelectedNodeId(null);
                    setPinnedEdge(null);
                  }}
                  onClearEdge={() => setPinnedEdge(null)}
                  onOpenAction={handleOpenAction}
                  onOpenBacklog={handleOpenBacklog}
                  onDecide={handleFeedbackDecision}
                  onRetry={handleFeedbackRetry}
                  tab={drawerTab}
                  onTabChange={setDrawerTab}
                />
              ) : null}
            </div>
          ) : null}
          {view === "operations" && data ? (
            <OperationsQueueView
              ops={data.ops}
              onCancelOperation={handleCancelOperation}
              onCancelAllByType={handleCancelAllByType}
              onClearTerminal={handleClearTerminal}
            />
          ) : null}
          {view === "review" && data ? (
            <ReviewQueueView
              feedback={data.feedback}
              onDecide={handleFeedbackDecision}
              onRetry={handleFeedbackRetry}
              onOpenNodeInGraph={handleSelectNodeFromReview}
              onOpenEdgeInGraph={handleSelectEdgeFromReview}
            />
          ) : null}
          {view === "backlog" && data ? (
            <BacklogView backlog={data.backlog} projectId={currentProjectId} />
          ) : null}
          {!data && !error && view !== "projects" ? (
            <div className="view">
              <div className="empty">
                <span className="spinner" /> Loading governance snapshot…
              </div>
            </div>
          ) : null}
        </main>
      </div>
      {toast ? (
        <div className={`toast ${toast.kind}`} role="status">
          {toast.msg}
        </div>
      ) : null}
      {aiConfigOpen ? (
        <AiConfigDialog
          config={aiConfig}
          projectId={currentProjectId}
          onClose={() => setAiConfigOpen(false)}
        />
      ) : null}
      <ActionControlPanel
        open={actionPanel != null}
        kind={actionPanel?.kind ?? "enrich"}
        target={actionPanel?.target ?? null}
        snapshotId={data?.status.active_snapshot_id ?? data?.summary.snapshot_id ?? null}
        onClose={() => setActionPanel(null)}
        onSubmitted={(msg, kind) => setToast({ kind, msg })}
      />
      <ActionPanel
        open={actionPanelOpen}
        snapshotId={data?.status.active_snapshot_id ?? data?.summary.snapshot_id ?? null}
        feedback={data?.feedback ?? null}
        initialTab={actionPanelInitialTab}
        prefillDraft={actionPanelPrefill}
        onClose={() => {
          setActionPanelOpen(false);
          setActionPanelPrefill(null);
          setActionPanelInitialTab("review");
        }}
        onOpenPreset={handleOpenPreset}
        onOpenReviewView={() => {
          setActionPanelOpen(false);
          setView("review");
        }}
        onSubmitted={(msg, kind) => setToast({ kind, msg })}
        onRunReconcile={() => {
          setActionPanelOpen(false);
          handleQueueReconcile();
        }}
      />
    </div>
  );
}

function countOpenBacklog(backlog?: BacklogResponse): number {
  return (
    backlog?.bugs?.filter((bug) => {
      const status = String(bug.status || "OPEN").toUpperCase();
      return !CLOSED_BACKLOG_STATUSES.has(status);
    }).length ?? 0
  );
}

function AiConfigDialog({
  config,
  projectId,
  onClose,
}: {
  config: AiConfigResponse | null;
  projectId: string;
  onClose(): void;
}) {
  const projectRouting = config?.project_config?.ai?.routing ?? {};
  const roleRouting = config?.role_routing ?? {};
  const roles = Array.from(
    new Set([
      ...Object.keys(projectRouting),
      ...Object.keys(roleRouting),
      "pm",
      "dev",
      "tester",
      "qa",
      "semantic",
    ]),
  );

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="AI configuration">
      <div className="config-dialog">
        <div className="config-dialog-head">
          <div>
            <div className="config-dialog-title">AI configuration</div>
            <div className="config-dialog-sub mono">{projectId}</div>
          </div>
          <button className="icon-btn" onClick={onClose} title="Close AI configuration">
            ×
          </button>
        </div>
        <div className="config-section">
          <div className="config-section-title">Project routing</div>
          <div className="config-table">
            <div className="config-row config-row-head">
              <span>Role</span>
              <span>Configured</span>
              <span>Effective</span>
            </div>
            {roles.map((role) => {
              const configured = projectRouting[role];
              const effective = role === "semantic" ? config?.semantic : roleRouting[role];
              return (
                <div className="config-row" key={role}>
                  <span className="mono">{role}</span>
                  <span>{fmtRoute(configured)}</span>
                  <span>{fmtRoute(effective)}</span>
                </div>
              );
            })}
          </div>
        </div>
        <div className="config-section">
          <div className="config-section-title">Semantic worker</div>
          <div className="config-kv">
            <span>Analyzer role</span>
            <span className="mono">{config?.semantic?.analyzer_role ?? "—"}</span>
            <span>Chain role</span>
            <span className="mono">{config?.semantic?.chain_role ?? "—"}</span>
            <span>Default AI</span>
            <span>{config?.semantic?.use_ai_default ? "enabled" : "manual"}</span>
          </div>
        </div>
        {config?.pipeline_error || config?.semantic_error || config?.project_config_error ? (
          <div className="config-warning">
            {[config.pipeline_error, config.semantic_error, config.project_config_error]
              .filter(Boolean)
              .join(" · ")}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function fmtRoute(route?: { provider?: string; model?: string } | null): string {
  if (!route) return "—";
  const provider = route.provider || "default";
  const model = route.model || "default";
  return `${provider}/${model}`;
}
