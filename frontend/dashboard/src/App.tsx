import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "./lib/api";
import { mergeProjection } from "./lib/semantic";
import type {
  ActiveSummaryResponse,
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
import OverviewView from "./views/OverviewView";
import OperationsQueueView from "./views/OperationsQueueView";
import ReviewQueueView from "./views/ReviewQueueView";
import GraphView from "./views/GraphView";

export type ViewName = "overview" | "graph" | "operations" | "review";

interface DataBundle {
  health: HealthResponse;
  status: StatusResponse;
  summary: ActiveSummaryResponse;
  projection: ProjectionResponse;
  nodes: NodeRecord[];
  edges: EdgeRecord[];
  ops: OperationsQueueResponse;
  feedback: FeedbackQueueResponse;
  loadedAt: string;
}

interface Toast {
  kind: "info" | "error" | "success";
  msg: string;
}

export default function App() {
  const [data, setData] = useState<DataBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<ViewName>("overview");
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [pinnedEdge, setPinnedEdge] = useState<PinnedEdge | null>(null);
  const [drawerTab, setDrawerTab] = useState<InspectorTabName>("overview");
  const [actionPanel, setActionPanel] = useState<{ kind: ActionKind; target: ActionTarget } | null>(null);
  const [actionPanelOpen, setActionPanelOpen] = useState(false);
  const [actionPanelInitialTab, setActionPanelInitialTab] = useState<"review" | "backlog">("review");
  const [actionPanelPrefill, setActionPanelPrefill] = useState<Partial<BacklogDraft> | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [reconcileBusy, setReconcileBusy] = useState(false);

  const fetchAll = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const [health, status, summary, projection, ops] = await Promise.all([
        api.health(signal),
        api.status(signal),
        api.activeSummary(signal),
        api.activeProjection(signal),
        api.operationsQueue(signal),
      ]);
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
      setData({
        health,
        status,
        summary,
        projection,
        nodes: merged,
        edges: edgesRes.edges,
        ops,
        feedback,
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
  }, []);

  useEffect(() => {
    const ac = new AbortController();
    fetchAll(ac.signal);
    return () => ac.abort();
  }, [fetchAll]);

  const handleQueueReconcile = useCallback(async () => {
    if (reconcileBusy) return;
    const headCommit = data?.status?.current_state?.graph_stale?.head_commit;
    const snapCommit = data?.status?.graph_snapshot_commit;
    if (!headCommit) {
      setToast({ kind: "error", msg: "Cannot reconcile: HEAD commit unknown (status response missing)." });
      return;
    }
    const ok = window.confirm(
      `Catch the active graph up to HEAD ${headCommit.slice(0, 7)}? This runs ` +
        "an incremental scope reconcile (POST /pending-scope + " +
        "/reconcile/pending-scope with activate=true) and rebuilds the " +
        "semantic projection so the dashboard reflects the new snapshot.",
    );
    if (!ok) return;
    setReconcileBusy(true);
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
      const runRes = await api.materializeAndActivatePendingScope({
        target_commit_sha: headCommit,
        semantic_use_ai: false, // rule-based + carry-forward only; no AI billed
        actor: "dashboard_user",
      });
      const newSid = runRes.snapshot_id || runRes.activation?.snapshot_id || "(unknown)";
      const projection = runRes.activation?.projection_status ?? "(unknown)";
      setToast({
        kind: "success",
        msg:
          `Scope reconcile complete · snapshot=${newSid.slice(0, 24)} · ` +
          `projection=${projection}. Refreshing dashboard…`,
      });
      fetchAll();
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
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
    setSelectedNodeId(id);
    setPinnedEdge(null); // selecting a node implicitly clears the edge pin
    // Re-route any view to the graph so the focus card + drawer line up.
    setView((prev) => (prev === "graph" ? prev : "graph"));
  }, []);

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
        reviewBadge={data?.feedback?.summary?.visible_group_count ?? 0}
        onRefresh={handleRefresh}
        onOpenReview={() => setActionPanelOpen(true)}
      />
      <StaleGraphBanner
        health={data?.health}
        status={data?.status}
        busy={reconcileBusy}
        onQueueReconcile={handleQueueReconcile}
      />
      <div className="app-body">
        <TreePanel
          nodes={data?.nodes ?? []}
          selectedNodeId={selectedNodeId}
          activeView={view}
          opsCount={data?.ops?.count ?? 0}
          reviewCount={data?.feedback?.summary?.visible_group_count ?? 0}
          onSelectNode={handleSelectNode}
          onSelectView={(v) => setView(v)}
          loading={loading}
        />
        <main className="main scrollbar-thin">
          {error && !data ? (
            <div className="view">
              <div className="empty">
                Load failed. Check the governance service is reachable at{" "}
                <span className="mono">/api/*</span>.<br />
                <span className="mono" style={{ color: "var(--ink-700)" }}>{error}</span>
              </div>
            </div>
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
                  onPinEdge={setPinnedEdge}
                  onSelectNode={handleSelectNode}
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
                  onSelectNode={handleSelectNode}
                  onClose={() => {
                    setSelectedNodeId(null);
                    setPinnedEdge(null);
                  }}
                  onClearEdge={() => setPinnedEdge(null)}
                  onOpenAction={handleOpenAction}
                  onOpenBacklog={handleOpenBacklog}
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
            <ReviewQueueView feedback={data.feedback} onDecide={handleFeedbackDecision} />
          ) : null}
          {!data && !error ? (
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
