import { useMemo, useState } from "react";
import type {
  EdgeRecord,
  FeedbackQueueGroup,
  FeedbackQueueResponse,
  Layer,
  NodeRecord,
} from "../types";
import {
  aggregateNode,
  classifyNode,
  isPackageMarker,
  newSubtreeAggregate,
  semStatusDotClass,
  semStatusLabel,
  type SemanticStatus,
  type SubtreeAggregate,
} from "../lib/semantic";
import FileLink from "./FileLink";
import { editorConfigured, editorScheme, editorUrl } from "../lib/editor";
import type { PinnedEdge } from "./FocusCard";
import type { ActionKind, ActionTarget } from "./ActionControlPanel";
import RetryFeedbackModal from "./RetryFeedbackModal";
import CandidateSemanticBlock from "./CandidateSemanticBlock";
import { healthHex, healthTone } from "../lib/health";

interface Props {
  node: NodeRecord | null;
  allNodes: NodeRecord[];
  edges: EdgeRecord[];
  pinnedEdge?: PinnedEdge | null;
  feedback?: FeedbackQueueResponse | null;
  snapshotId?: string | null;
  edgeSemantics?: Record<string, unknown> | null;
  onSelectNode(id: string): void;
  onClose(): void;
  onClearEdge?(): void;
  onOpenAction?(kind: ActionKind, target: ActionTarget): void;
  onOpenBacklog?(target: ActionTarget): void;
  onDecide?(feedbackIds: string[], action: string, summaryHint?: string): void;
  onRetry?(feedbackIds: string[], nodeId: string, rationale: string): Promise<void> | void;
  tab?: Tab;
  onTabChange?(tab: Tab): void;
}

interface EdgeProps {
  edge: PinnedEdge;
  byId: Map<string, NodeRecord>;
  edges: EdgeRecord[];
  feedback: FeedbackQueueResponse | null;
  snapshotId?: string | null;
  edgeSemantics?: Record<string, unknown> | null;
  onSelectNode(id: string): void;
  onClose(): void;
  onOpenAction?(kind: ActionKind, target: ActionTarget): void;
  onOpenBacklog?(target: ActionTarget): void;
  onDecide?(feedbackIds: string[], action: string, summaryHint?: string): void;
  onRetry?(feedbackIds: string[], nodeId: string, rationale: string): Promise<void> | void;
  tab: Tab;
  onTabChange(tab: Tab): void;
}

export type Tab = "overview" | "files" | "relations" | "feedback" | "backlog";

// MF-2026-05-10-016 P2: surface needs_observer_decision feedback for the
// inspected node so the drawer can show Accept / Retry / Reject inline.
function findPendingReviewGroup(
  feedback: FeedbackQueueResponse | null | undefined,
  nodeId: string,
): FeedbackQueueGroup | null {
  if (!feedback || !nodeId) return null;
  const groups = feedback.groups ?? [];
  for (const g of groups) {
    if (g.target_type !== "node") continue;
    if (g.target_id === nodeId) return g;
  }
  return null;
}

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "files", label: "Files" },
  { id: "relations", label: "Relations" },
  { id: "feedback", label: "Feedback" },
  { id: "backlog", label: "Backlog" },
];

export default function InspectorDrawer({
  node,
  allNodes,
  edges,
  pinnedEdge,
  feedback,
  snapshotId,
  edgeSemantics,
  onSelectNode,
  onClose,
  onClearEdge,
  onOpenAction,
  onOpenBacklog,
  onDecide,
  onRetry,
  tab: controlledTab,
  onTabChange,
}: Props) {
  // All hooks must run unconditionally — keep this block above any early return.
  const [internalTab, setInternalTab] = useState<Tab>("overview");
  const tab = controlledTab ?? internalTab;
  const setTab = (t: Tab) => {
    if (onTabChange) onTabChange(t);
    if (controlledTab == null) setInternalTab(t);
  };
  const byId = useMemo(() => new Map(allNodes.map((n) => [n.node_id, n])), [allNodes]);
  const byParent = useMemo(() => {
    const m = new Map<string, NodeRecord[]>();
    allNodes.forEach((n) => {
      const p = n.metadata?.hierarchy_parent;
      if (!p) return;
      const arr = m.get(p) ?? [];
      arr.push(n);
      m.set(p, arr);
    });
    return m;
  }, [allNodes]);
  const aggregate = useMemo(
    () => (node ? walkAggregate(node.node_id, byId, byParent) : newSubtreeAggregate()),
    [node, byId, byParent],
  );
  const childrenByLayer = useMemo(
    () => (node ? groupChildrenByLayer(node.node_id, byParent) : {}),
    [node, byParent],
  );
  const importantChildren = useMemo(
    () => (node ? importantChildrenOf(node.node_id, byParent, byId) : []),
    [node, byParent, byId],
  );

  // Edge takes precedence — when an edge is pinned, the drawer flips to edge mode
  // until the user explicitly clears it (close button or by selecting a node).
  if (pinnedEdge) {
    return (
      <EdgeInspector
        edge={pinnedEdge}
        byId={byId}
        edges={edges}
        feedback={feedback ?? null}
        snapshotId={snapshotId ?? null}
        edgeSemantics={edgeSemantics ?? null}
        onSelectNode={onSelectNode}
        onClose={onClearEdge ?? onClose}
        onOpenAction={onOpenAction}
        onOpenBacklog={onOpenBacklog}
        onDecide={onDecide}
        onRetry={onRetry}
        tab={tab}
        onTabChange={setTab}
      />
    );
  }
  if (!node) {
    return (
      <aside className="inspector-drawer">
        <div className="empty" style={{ margin: 16 }}>
          Pick a node from the tree or graph.
        </div>
      </aside>
    );
  }
  const status = classifyNode(node);
  const tone = statusTone(status);
  const isContainer = (byParent.get(node.node_id)?.length ?? 0) > 0;
  const drawerCta = drawerCtaFor(status);

  return (
    <aside className="inspector-drawer">
      <header className="inspector-head">
        <div className="inspector-row">
          <span className={`layer-badge layer-${node.layer}`}>{node.layer}</span>
          <span className="pill pill-mono">{nodeTypeLabel(node, isContainer)}</span>
          <span className="pill pill-mono" title={node.node_id}>
            {node.node_id}
          </span>
          {node.metadata?.function_count != null && node.metadata.function_count > 0 ? (
            <span className="pill pill-mono">{node.metadata.function_count} fn</span>
          ) : null}
          <button className="btn-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="inspector-title">{node.title || node.node_id}</div>
        {node.metadata?.module ? <div className="inspector-mono-line">{node.metadata.module}</div> : null}
        <div className="inspector-head-state-row">
          <div className={`sem-state-row tone-${tone}`} style={{ flex: 1 }}>
            <span className={`sem-dot ${semStatusDotClass(status)}`} />
            <span>{semStatusLabel(status)}</span>
          </div>
          {onOpenAction ? (
            <button
              className={`focus-cta cta-${drawerCta.kind}`}
              onClick={() => onOpenAction(drawerCta.kind, { node })}
              title={drawerCta.hint}
            >
              {drawerCta.label}
            </button>
          ) : null}
        </div>
      </header>

      <nav className="inspector-tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`inspector-tab${tab === t.id ? " active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div className="inspector-body scrollbar-thin">
        {tab === "overview" ? (
          <OverviewTab
            node={node}
            isContainer={isContainer}
            aggregate={aggregate}
            childrenByLayer={childrenByLayer}
            importantChildren={importantChildren}
            onSelectNode={onSelectNode}
            pendingReview={findPendingReviewGroup(feedback, node.node_id)}
            onDecide={onDecide}
            onRetry={onRetry}
            snapshotId={snapshotId ?? null}
          />
        ) : null}
        {tab === "files" ? <FilesTab node={node} /> : null}
        {tab === "relations" ? (
          <RelationsTab node={node} edges={edges} byId={byId} onSelectNode={onSelectNode} />
        ) : null}
        {tab === "feedback" ? (
          <FeedbackTab
            target={{ node }}
            targetIds={[node.node_id]}
            feedback={feedback ?? null}
            onOpenAction={onOpenAction}
            onSelectNode={onSelectNode}
          />
        ) : null}
        {tab === "backlog" ? (
          <BacklogTab
            target={{ node }}
            targetIds={[node.node_id]}
            feedback={feedback ?? null}
            onOpenBacklog={onOpenBacklog}
          />
        ) : null}
      </div>
    </aside>
  );
}

function OverviewTab({
  node,
  isContainer,
  aggregate,
  childrenByLayer,
  importantChildren,
  onSelectNode,
  pendingReview,
  onDecide,
  onRetry,
  snapshotId,
}: {
  node: NodeRecord;
  isContainer: boolean;
  aggregate: SubtreeAggregate;
  childrenByLayer: Record<string, number>;
  importantChildren: NodeRecord[];
  onSelectNode(id: string): void;
  pendingReview?: FeedbackQueueGroup | null;
  onDecide?(feedbackIds: string[], action: string, summaryHint?: string): void;
  onRetry?(feedbackIds: string[], nodeId: string, rationale: string): Promise<void> | void;
  snapshotId?: string | null;
}) {
  const sem = node.semantic ?? {};
  const v = sem.validity ?? {};
  const signals = isContainer ? containerSignals(node, aggregate) : leafSignals(node);
  const [showRetry, setShowRetry] = useState(false);
  const [reviewBusy, setReviewBusy] = useState(false);

  const dispatchDecide = async (action: string) => {
    if (!onDecide || !pendingReview) return;
    setReviewBusy(true);
    try {
      await onDecide(
        pendingReview.feedback_ids,
        action,
        `node ${pendingReview.target_id}`,
      );
    } finally {
      setReviewBusy(false);
    }
  };

  return (
    <>
      {pendingReview ? (
        <section className="drawer-review-banner">
          <div className="drawer-review-banner-head">
            <span className="drawer-review-banner-title">
              ⚖ Pending semantic review · {pendingReview.target_id}
            </span>
            <span className="mono" style={{ fontSize: 10.5, color: "var(--ink-400)" }}>
              {pendingReview.representative_feedback_id}
              {pendingReview.feedback_ids.length > 1 ? ` +${pendingReview.feedback_ids.length - 1}` : ""}
            </span>
          </div>
          <div className="drawer-review-banner-body">{pendingReview.representative_issue}</div>
          <CandidateSemanticBlock
            snapshotId={snapshotId ?? null}
            targetType="node"
            targetId={pendingReview.target_id}
          />
          <div className="drawer-review-banner-actions">
            <button
              className="action-btn"
              disabled={reviewBusy || !onDecide}
              title="POST /feedback/decision action=accept_semantic_enrichment"
              onClick={() => dispatchDecide("accept_semantic_enrichment")}
            >
              {reviewBusy ? "…" : "Accept"}
            </button>
            <button
              className="action-btn"
              disabled={reviewBusy || !onRetry}
              title="Reject + re-enqueue with rationale (next AI run sees the prior proposal + your reason)"
              onClick={() => setShowRetry(true)}
            >
              Retry
            </button>
            <button
              className="action-btn action-btn-danger"
              disabled={reviewBusy || !onDecide}
              title="POST /feedback/decision action=reject_false_positive"
              onClick={() => dispatchDecide("reject_false_positive")}
            >
              {reviewBusy ? "…" : "Reject"}
            </button>
          </div>
        </section>
      ) : null}

      {showRetry && pendingReview && onRetry ? (
        <RetryFeedbackModal
          targetType={pendingReview.target_type}
          targetId={pendingReview.target_id}
          feedbackIds={pendingReview.feedback_ids}
          priorIssue={pendingReview.representative_issue}
          onCancel={() => setShowRetry(false)}
          onSubmit={async (rationale) => {
            setReviewBusy(true);
            try {
              await onRetry(pendingReview.feedback_ids, pendingReview.target_id, rationale);
              setShowRetry(false);
            } finally {
              setReviewBusy(false);
            }
          }}
        />
      ) : null}

      {sem.semantic_summary || sem.intent || sem.feature_name || sem.domain_label ? (
        <section className="inspector-section">
          <div className="inspector-section-title">About</div>
          {sem.feature_name ? (
            <div className="inspector-summary-title">{sem.feature_name}</div>
          ) : null}
          {sem.domain_label ? <div className="inspector-mono-line">{sem.domain_label}</div> : null}
          {sem.semantic_summary ? (
            <p className="inspector-paragraph">{sem.semantic_summary}</p>
          ) : null}
          {sem.intent ? (
            <p className="inspector-paragraph">
              <strong>Intent:</strong> {sem.intent}
            </p>
          ) : null}
        </section>
      ) : null}

      <section className="inspector-section">
        <div className="inspector-section-title">
          Feature health{" "}
          <span style={{ fontWeight: 400, color: "var(--ink-400)", fontSize: 11 }}>
            {node._health != null
              ? "leaf score = 35 src + 30 tests + 20 fns + 10 docs + 5 parent"
              : node._asset_binding != null
                ? "L4 asset · separate binding score"
                : "no scoreable descendants"}
          </span>
        </div>
        <div
          className="kv"
          style={{ gridTemplateColumns: "100px 1fr", alignItems: "center" }}
        >
          <span className="k">health</span>
          <span className="v">
            {node._health != null ? (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span className="pdot" style={{ background: healthHex(node._health) }} />
                <span
                  className="mono"
                  style={{
                    fontWeight: 700,
                    color: healthHex(node._health),
                    fontSize: 13,
                  }}
                >
                  {node._health}
                </span>
                <span style={{ fontSize: 11, color: "var(--ink-400)" }}>
                  / 100 · {healthTone(node._health)}
                </span>
              </span>
            ) : (
              <span className="mono" style={{ color: "var(--ink-400)" }}>—</span>
            )}
          </span>
          {node._asset_binding != null ? (
            <>
              <span className="k">asset binding</span>
              <span className="v">
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <span
                    className="pdot"
                    style={{ background: healthHex(node._asset_binding) }}
                  />
                  <span
                    className="mono"
                    style={{
                      fontWeight: 600,
                      color: healthHex(node._asset_binding),
                    }}
                  >
                    {node._asset_binding}
                  </span>
                  <span style={{ fontSize: 11, color: "var(--ink-400)" }}>/ 100</span>
                </span>
              </span>
            </>
          ) : null}
        </div>
      </section>

      <section className="inspector-section">
        <div className="inspector-section-title">Coverage signals</div>
        {signals.map((s) => (
          <div key={s.label} className="signal-row">
            <span className="signal-label">{s.label}</span>
            <span className={`status-badge status-${s.tone}`}>{s.value}</span>
          </div>
        ))}
      </section>

      {isContainer ? (
        <section className="inspector-section">
          <div className="inspector-section-title">Subtree rollup</div>
          <div className="kv">
            <span className="k">feature units</span>
            <span className="v">{aggregate.total}</span>
            <span className="k">current</span>
            <span className="v">{aggregate.complete + aggregate.reviewed}</span>
            <span className="k">stale</span>
            <span className="v">{aggregate.stale}</span>
            <span className="k">hash-unverified</span>
            <span className="v">{aggregate.hash_unverified}</span>
            <span className="k">missing</span>
            <span className="v">{aggregate.struct}</span>
            <span className="k">pending</span>
            <span className="v">{aggregate.pending}</span>
          </div>
        </section>
      ) : null}

      {isContainer && Object.keys(childrenByLayer).length > 0 ? (
        <section className="inspector-section">
          <div className="inspector-section-title">Children by layer</div>
          <div className="layer-counts">
            {(["L1", "L2", "L3", "L4", "L7"] as Layer[]).map((l) =>
              childrenByLayer[l] ? (
                <span key={l} className={`layer-count layer-${l}`}>
                  <span className="layer-badge">{l}</span>
                  <span className="layer-count-num">{childrenByLayer[l]}</span>
                </span>
              ) : null,
            )}
          </div>
        </section>
      ) : null}

      {importantChildren.length > 0 ? (
        <section className="inspector-section">
          <div className="inspector-section-title">
            Important children
            <span className="head-hint">— sorted by health</span>
          </div>
          <ul className="link-list">
            {importantChildren.map((c) => {
              const cs = classifyNode(c);
              return (
                <li key={c.node_id}>
                  <button className="link-row" onClick={() => onSelectNode(c.node_id)}>
                    <span className={`sem-dot ${semStatusDotClass(cs)}`} />
                    <span className={`layer-badge layer-${c.layer}`}>{c.layer}</span>
                    <span className="link-name">{shortTitle(c.title || c.node_id)}</span>
                    <span className="link-meta">{semStatusLabel(cs)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      <section className="inspector-section">
        <div className="inspector-section-title">Validity</div>
        <div className="kv">
          <span className="k">status</span>
          <span className="v">{nonEmpty(sem.status, sem.node_status)}</span>
          <span className="k">job_status</span>
          <span className="v">{nonEmpty(sem.job_status)}</span>
          <span className="k">hash_state</span>
          <span className="v">{nonEmpty(sem.hash_state)}</span>
          <span className="k">validity.valid</span>
          <span className="v">{v.valid == null ? "—" : String(v.valid)}</span>
          <span className="k">validity.status</span>
          <span className="v">{nonEmpty(v.status)}</span>
          <span className="k">hash_validation</span>
          <span className="v">{nonEmpty(v.hash_validation)}</span>
          <span className="k">file_hash_status</span>
          <span className="v">{nonEmpty(v.file_hash_status)}</span>
          <span className="k">feature_hash</span>
          <span className="v">
            {sem.feature_hash ? (
              <span className="hash-chip" title={sem.feature_hash}>
                {truncateHash(sem.feature_hash)}
              </span>
            ) : (
              "—"
            )}
          </span>
        </div>
      </section>

      {node.metadata?.graph_metrics ? (
        <section className="inspector-section">
          <div className="inspector-section-title">Graph metrics</div>
          <div className="kv">
            <span className="k">fan_in</span>
            <span className="v">{node.metadata.graph_metrics.fan_in ?? 0}</span>
            <span className="k">fan_out</span>
            <span className="v">{node.metadata.graph_metrics.fan_out ?? 0}</span>
            <span className="k">hierarchy_in</span>
            <span className="v">{node.metadata.graph_metrics.hierarchy_in ?? 0}</span>
            <span className="k">hierarchy_out</span>
            <span className="v">{node.metadata.graph_metrics.hierarchy_out ?? 0}</span>
          </div>
        </section>
      ) : null}

      {(node.metadata?.functions?.length ?? 0) > 0 ? (
        <FunctionsSection node={node} />
      ) : null}
    </>
  );
}

// ===========================================================================
// Edge inspector — shown when an edge is pinned. Mirrors the 5-tab layout of
// the node drawer (Overview / Files / Relations / Feedback / Backlog) so the
// operator's muscle memory carries between the two modes.
// ===========================================================================
function EdgeInspector({
  edge,
  byId,
  edges,
  feedback,
  snapshotId,
  edgeSemantics,
  onSelectNode,
  onClose,
  onOpenAction,
  onOpenBacklog,
  onDecide,
  onRetry,
  tab,
  onTabChange,
}: EdgeProps) {
  const src = byId.get(edge.src) ?? null;
  const dst = byId.get(edge.dst) ?? null;
  const desc = EDGE_TYPE_DESC[edge.type] ?? "structure-derived typed relation";
  // Find the pending review feedback group for THIS edge — match either of
  // the two target_id formats (`a|b|type` from ActionControlPanel, `a->b:type`
  // from the worker).
  const edgePending = useMemo<FeedbackQueueGroup | null>(() => {
    const groups = feedback?.groups ?? [];
    const pipe = `${edge.src}|${edge.dst}|${edge.type}`;
    const arrow = `${edge.src}->${edge.dst}:${edge.type}`;
    return (
      groups.find(
        (g) =>
          g.target_type === "edge" && (g.target_id === pipe || g.target_id === arrow),
      ) ?? null
    );
  }, [edge.src, edge.dst, edge.type, feedback]);
  // Look up the projection's edge_semantics entry for this edge — populated
  // after Accept flips the PROPOSED event to ACCEPTED and the projection
  // rebuilds. The key is the canonical `<src>-><dst>:<type>` form.
  const edgeSemEntry = useMemo<Record<string, unknown> | null>(() => {
    if (!edgeSemantics) return null;
    const key = `${edge.src}->${edge.dst}:${edge.type}`;
    const entry = (edgeSemantics as Record<string, unknown>)[key];
    if (!entry || typeof entry !== "object") return null;
    const semantic = (entry as { semantic?: unknown }).semantic;
    if (!semantic || typeof semantic !== "object") return null;
    const sem = semantic as Record<string, unknown>;
    // Empty stub entries have no AI fields — skip them so the section
    // doesn't render with all dashes.
    const hasContent = Object.keys(sem).some(
      (k) => !k.startsWith("_") && sem[k] != null && sem[k] !== "",
    );
    return hasContent ? sem : null;
  }, [edgeSemantics, edge.src, edge.dst, edge.type]);
  const [edgeReviewBusy, setEdgeReviewBusy] = useState(false);
  const [edgeShowRetry, setEdgeShowRetry] = useState(false);
  const dispatchEdgeDecide = async (action: string) => {
    if (!onDecide || !edgePending) return;
    setEdgeReviewBusy(true);
    try {
      await onDecide(
        edgePending.feedback_ids,
        action,
        `edge ${edgePending.target_id}`,
      );
    } finally {
      setEdgeReviewBusy(false);
    }
  };

  return (
    <aside className="inspector-drawer">
      <header className="inspector-head">
        <div className="inspector-row">
          <span className="layer-badge layer-edge" style={{ background: "#ede9fe", color: "#6d28d9" }}>
            EDGE
          </span>
          <span className="pill pill-mono">{edge.type}</span>
          {edge.direction ? <span className="pill pill-mono">{edge.direction}</span> : null}
          <button className="btn-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="inspector-title">
          {shortTitle(src?.title || edge.src)} → {shortTitle(dst?.title || edge.dst)}
        </div>
        <div className="inspector-mono-line">{desc}</div>
        {onOpenAction ? (
          <div className="inspector-head-state-row" style={{ marginTop: 8 }}>
            <span className="sem-state-row tone-purple" style={{ flex: 1 }}>
              <span className="sem-dot unverified" />
              <span>edge semantic pending</span>
            </span>
            <button
              className="focus-cta cta-enrich"
              onClick={() => onOpenAction("enrich", { edge })}
              title="Enrich this edge with AI semantic"
            >
              ⚡ AI enrich edge
            </button>
          </div>
        ) : null}
      </header>

      <nav className="inspector-tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`inspector-tab${tab === t.id ? " active" : ""}`}
            onClick={() => onTabChange(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div className="inspector-body scrollbar-thin">
        {tab === "overview" && edgePending ? (
          <section className="drawer-review-banner">
            <div className="drawer-review-banner-head">
              <span className="drawer-review-banner-title">
                ⚖ Pending semantic review · {edgePending.target_id}
              </span>
              <span className="mono" style={{ fontSize: 10.5, color: "var(--ink-400)" }}>
                {edgePending.representative_feedback_id}
                {edgePending.feedback_ids.length > 1
                  ? ` +${edgePending.feedback_ids.length - 1}`
                  : ""}
              </span>
            </div>
            <div className="drawer-review-banner-body">{edgePending.representative_issue}</div>
            <CandidateSemanticBlock
              snapshotId={snapshotId ?? null}
              targetType="edge"
              targetId={edgePending.target_id}
            />
            <div className="drawer-review-banner-actions">
              <button
                className="action-btn"
                disabled={edgeReviewBusy || !onDecide}
                title="POST /feedback/decision action=accept_semantic_enrichment"
                onClick={() => dispatchEdgeDecide("accept_semantic_enrichment")}
              >
                {edgeReviewBusy ? "…" : "Accept"}
              </button>
              <button
                className="action-btn"
                disabled={edgeReviewBusy || !onRetry}
                title="Reject + re-enqueue with rationale"
                onClick={() => setEdgeShowRetry(true)}
              >
                Retry
              </button>
              <button
                className="action-btn action-btn-danger"
                disabled={edgeReviewBusy || !onDecide}
                title="POST /feedback/decision action=reject_false_positive"
                onClick={() => dispatchEdgeDecide("reject_false_positive")}
              >
                {edgeReviewBusy ? "…" : "Reject"}
              </button>
            </div>
          </section>
        ) : null}

        {edgeShowRetry && edgePending && onRetry ? (
          <RetryFeedbackModal
            targetType={edgePending.target_type}
            targetId={edgePending.target_id}
            feedbackIds={edgePending.feedback_ids}
            priorIssue={edgePending.representative_issue}
            onCancel={() => setEdgeShowRetry(false)}
            onSubmit={async (rationale) => {
              setEdgeReviewBusy(true);
              try {
                await onRetry(edgePending.feedback_ids, edgePending.target_id, rationale);
                setEdgeShowRetry(false);
              } finally {
                setEdgeReviewBusy(false);
              }
            }}
          />
        ) : null}

        {tab === "overview" ? (
          <EdgeOverviewTab
            edge={edge}
            src={src}
            dst={dst}
            onSelectNode={onSelectNode}
            edgeSemantic={edgeSemEntry}
          />
        ) : null}
        {tab === "files" ? <EdgeFilesTab src={src} dst={dst} /> : null}
        {tab === "relations" ? (
          <EdgeRelationsTab edge={edge} edges={edges} byId={byId} onSelectNode={onSelectNode} />
        ) : null}
        {tab === "feedback" ? (
          <FeedbackTab
            target={{ edge }}
            targetIds={[edge.src, edge.dst]}
            feedback={feedback}
            onOpenAction={onOpenAction}
            onSelectNode={onSelectNode}
          />
        ) : null}
        {tab === "backlog" ? (
          <BacklogTab
            target={{ edge }}
            targetIds={[edge.src, edge.dst]}
            feedback={feedback}
            onOpenBacklog={onOpenBacklog}
          />
        ) : null}
      </div>
    </aside>
  );
}

function EdgeOverviewTab({
  edge,
  src,
  dst,
  onSelectNode,
  edgeSemantic,
}: {
  edge: PinnedEdge;
  src: NodeRecord | null;
  dst: NodeRecord | null;
  onSelectNode(id: string): void;
  edgeSemantic?: Record<string, unknown> | null;
}) {
  return (
    <>
      {edgeSemantic ? <EdgeSemanticSection sem={edgeSemantic} /> : null}

      <section className="inspector-section">
        <div className="inspector-section-title">Edge attributes</div>
        <div className="kv">
          <span className="k">type</span>
          <span className="v">{edge.type}</span>
          <span className="k">direction</span>
          <span className="v">{edge.direction || "—"}</span>
          <span className="k">confidence</span>
          <span className="v">
            {typeof edge.confidence === "number" ? edge.confidence.toFixed(2) : "—"}
          </span>
          <span className="k">evidence</span>
          <span className="v" style={{ whiteSpace: "normal", lineHeight: 1.5 }}>
            {edge.evidence || "—"}
          </span>
        </div>
      </section>

      <EdgeEndpointSection
        label="Source"
        node={src}
        fallbackId={edge.src}
        onSelectNode={onSelectNode}
      />
      <EdgeEndpointSection
        label="Target"
        node={dst}
        fallbackId={edge.dst}
        onSelectNode={onSelectNode}
      />
    </>
  );
}

// MF-016/017: read-only render of the accepted edge semantic (the AI's
// proposal after the operator clicked Accept). Sources fields from
// projection.edge_semantics[edge_id].semantic. Same fields the
// CandidateSemanticBlock renders, just labelled as the current accepted
// state instead of a pending proposal.
function EdgeSemanticSection({ sem }: { sem: Record<string, unknown> }) {
  const str = (k: string): string => {
    const v = sem[k];
    return typeof v === "string" ? v : v == null ? "" : String(v);
  };
  const relationPurpose = str("relation_purpose");
  const semanticLabel = str("semantic_label");
  const directionality = str("directionality");
  const risk = str("risk");
  const confidence = typeof sem.confidence === "number" ? (sem.confidence as number) : null;
  const evidence = sem.evidence as Record<string, unknown> | undefined;
  const openIssues = Array.isArray(sem.open_issues) ? (sem.open_issues as unknown[]) : [];
  if (!relationPurpose && !semanticLabel && !directionality && !risk && !evidence) {
    return null;
  }
  return (
    <section className="inspector-section">
      <div className="inspector-section-title">
        Edge semantic{" "}
        <span style={{ fontWeight: 400, color: "var(--ink-400)", fontSize: 11 }}>
          accepted
          {confidence != null ? ` · conf=${confidence.toFixed(2)}` : ""}
        </span>
      </div>
      <div className="candidate-block" style={{ marginTop: 0, background: "#f0fdf4", borderColor: "#bbf7d0" }}>
        {semanticLabel ? (
          <div className="candidate-block-row">
            <span className="candidate-block-key">label</span>
            <span className="candidate-block-val mono">{semanticLabel}</span>
          </div>
        ) : null}
        {relationPurpose ? (
          <div className="candidate-block-row">
            <span className="candidate-block-key">purpose</span>
            <span className="candidate-block-val">{relationPurpose}</span>
          </div>
        ) : null}
        {directionality ? (
          <div className="candidate-block-row">
            <span className="candidate-block-key">direction</span>
            <span className="candidate-block-val">{directionality}</span>
          </div>
        ) : null}
        {risk ? (
          <div className="candidate-block-row">
            <span className="candidate-block-key">risk</span>
            <span className="candidate-block-val">{risk}</span>
          </div>
        ) : null}
        {evidence && typeof evidence === "object" ? (
          <div className="candidate-block-row">
            <span className="candidate-block-key">evidence</span>
            <span className="candidate-block-val">
              {typeof (evidence as { basis?: unknown }).basis === "string"
                ? ((evidence as { basis: string }).basis)
                : JSON.stringify(evidence).slice(0, 240)}
            </span>
          </div>
        ) : null}
        {openIssues.length > 0 ? (
          <div className="candidate-block-row">
            <span className="candidate-block-key">open issues</span>
            <div>
              <ul className="candidate-block-issues">
                {openIssues.slice(0, 3).map((it, i) => (
                  <li key={i}>{typeof it === "string" ? it : JSON.stringify(it).slice(0, 240)}</li>
                ))}
              </ul>
              {openIssues.length > 3 ? (
                <div className="candidate-block-issues-more">+{openIssues.length - 3} more</div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}

function EdgeFilesTab({ src, dst }: { src: NodeRecord | null; dst: NodeRecord | null }) {
  const groups: { label: string; node: NodeRecord | null }[] = [
    { label: "Source", node: src },
    { label: "Target", node: dst },
  ];
  const totalFiles = groups.reduce(
    (acc, g) =>
      acc +
      (g.node?.primary_files?.length ?? 0) +
      (g.node?.secondary_files?.length ?? 0) +
      (g.node?.test_files?.length ?? 0) +
      (g.node?.config_files?.length ?? 0),
    0,
  );
  if (totalFiles === 0) {
    return <div className="empty">Neither endpoint has files bound.</div>;
  }
  return (
    <>
      {!editorConfigured ? (
        <div className="inspector-banner">
          <strong>Editor jump disabled.</strong> Set <span className="mono">VITE_WORKSPACE_ROOT</span>{" "}
          in <span className="mono">.env.local</span> to enable click-to-open.
        </div>
      ) : null}
      {groups.map((g) =>
        g.node ? <EdgeFilesGroup key={g.label} label={g.label} node={g.node} /> : null,
      )}
    </>
  );
}

function EdgeFilesGroup({ label, node }: { label: string; node: NodeRecord }) {
  const sections: { key: string; label: string; files?: string[] }[] = [
    { key: "primary", label: "Primary", files: node.primary_files },
    { key: "secondary", label: "Secondary", files: node.secondary_files },
    { key: "tests", label: "Tests", files: node.test_files },
    { key: "config", label: "Config", files: node.config_files },
  ];
  const total = sections.reduce((acc, s) => acc + (s.files?.length ?? 0), 0);
  if (total === 0) return null;
  return (
    <section className="inspector-section">
      <div className="inspector-section-title">
        {label}
        <span className="head-hint">{shortTitle(node.title || node.node_id)}</span>
        <span className="head-hint" style={{ marginLeft: "auto" }}>
          {total} file{total === 1 ? "" : "s"}
        </span>
      </div>
      {sections.map((s) =>
        s.files && s.files.length > 0 ? (
          <div key={s.key} style={{ marginBottom: 8 }}>
            <div
              className="inspector-section-title"
              style={{ fontSize: 9, marginBottom: 4, opacity: 0.8 }}
            >
              {s.label}
              <span className="head-hint">{s.files.length}</span>
            </div>
            <ul className="file-list">
              {s.files.map((f) => (
                <li key={f}>
                  <FileLink path={f} />
                </li>
              ))}
            </ul>
          </div>
        ) : null,
      )}
    </section>
  );
}

function EdgeRelationsTab({
  edge,
  edges,
  byId,
  onSelectNode,
}: {
  edge: PinnedEdge;
  edges: EdgeRecord[];
  byId: Map<string, NodeRecord>;
  onSelectNode(id: string): void;
}) {
  // Show every typed (non-contains) edge that touches either endpoint of the
  // pinned edge, except the pinned edge itself. Helps the operator see "what
  // else does the source talk to" or "who else reads this state".
  const focusKey = `${edge.src}|${edge.dst}|${edge.type}`;
  const related = edges
    .map((e) => {
      const t = (e.edge_type || e.type || "default") as string;
      if (t === "contains") return null;
      if (`${e.src}|${e.dst}|${t}` === focusKey) return null;
      const touchesSrc = e.src === edge.src || e.dst === edge.src;
      const touchesDst = e.src === edge.dst || e.dst === edge.dst;
      if (!touchesSrc && !touchesDst) return null;
      return { edge: e, type: t, touchesSrc, touchesDst };
    })
    .filter((x): x is NonNullable<typeof x> => x != null);

  if (related.length === 0) {
    return <div className="empty">No other typed edges touch either endpoint.</div>;
  }

  // Group by which endpoint they touch + by edge type, for readability.
  const fromSrc = related.filter((r) => r.touchesSrc).slice(0, 60);
  const fromDst = related.filter((r) => r.touchesDst && !r.touchesSrc).slice(0, 60);

  return (
    <>
      {fromSrc.length > 0 ? (
        <RelatedEdgeGroup
          label={`Around source · ${shortTitle(byId.get(edge.src)?.title || edge.src)}`}
          rows={fromSrc}
          anchor={edge.src}
          byId={byId}
          onSelectNode={onSelectNode}
        />
      ) : null}
      {fromDst.length > 0 ? (
        <RelatedEdgeGroup
          label={`Around target · ${shortTitle(byId.get(edge.dst)?.title || edge.dst)}`}
          rows={fromDst}
          anchor={edge.dst}
          byId={byId}
          onSelectNode={onSelectNode}
        />
      ) : null}
    </>
  );
}

function RelatedEdgeGroup({
  label,
  rows,
  anchor,
  byId,
  onSelectNode,
}: {
  label: string;
  rows: { edge: EdgeRecord; type: string; touchesSrc: boolean; touchesDst: boolean }[];
  anchor: string;
  byId: Map<string, NodeRecord>;
  onSelectNode(id: string): void;
}) {
  // Group by edge type for compact display.
  const byType = new Map<string, typeof rows>();
  rows.forEach((r) => {
    const arr = byType.get(r.type) ?? [];
    arr.push(r);
    byType.set(r.type, arr);
  });
  const groups = Array.from(byType.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  return (
    <section className="inspector-section">
      <div className="inspector-section-title">
        {label}
        <span className="head-hint">{rows.length}</span>
      </div>
      {groups.map(([type, items]) => (
        <div key={type} style={{ marginBottom: 8 }}>
          <div className="inspector-section-title" style={{ fontSize: 9, marginBottom: 4, opacity: 0.85 }}>
            {type} <span className="head-hint">{items.length}</span>
          </div>
          <ul className="link-list">
            {items.map((r, i) => {
              const peerId = r.edge.src === anchor ? r.edge.dst : r.edge.src;
              const direction: "out" | "in" = r.edge.src === anchor ? "out" : "in";
              const peer = byId.get(peerId);
              if (!peer) return null;
              const ps = classifyNode(peer);
              return (
                <li key={`${peerId}-${type}-${i}`}>
                  <button className="link-row" onClick={() => onSelectNode(peerId)}>
                    <span className="edge-arrow" title={direction}>
                      {direction === "out" ? "→" : "←"}
                    </span>
                    <span className={`layer-badge layer-${peer.layer}`}>{peer.layer}</span>
                    <span className="link-name">{shortTitle(peer.title || peer.node_id)}</span>
                    <span className={`sem-dot ${semStatusDotClass(ps)}`} title={semStatusLabel(ps)} />
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </section>
  );
}

function EdgeEndpointSection({
  label,
  node,
  fallbackId,
  onSelectNode,
}: {
  label: string;
  node: NodeRecord | null;
  fallbackId: string;
  onSelectNode(id: string): void;
}) {
  if (!node) {
    return (
      <section className="inspector-section">
        <div className="inspector-section-title">{label}</div>
        <div className="empty" style={{ padding: "12px 14px" }}>
          Node <span className="mono">{fallbackId}</span> is not in the active snapshot.
        </div>
      </section>
    );
  }
  const status = classifyNode(node);
  const tone = statusTone(status);
  const fnCount = node.metadata?.functions?.length ?? 0;
  return (
    <section className="inspector-section">
      <div className="inspector-section-title">{label}</div>
      <div className="endpoint-card">
        <div className="endpoint-card-head">
          <span className={`layer-badge layer-${node.layer}`}>{node.layer}</span>
          <span className="endpoint-card-title">{shortTitle(node.title || node.node_id)}</span>
          <span className="mono endpoint-card-id">{node.node_id}</span>
        </div>
        <div className={`sem-state-row tone-${tone}`} style={{ marginTop: 6 }}>
          <span className={`sem-dot ${semStatusDotClass(status)}`} />
          <span>{semStatusLabel(status)}</span>
        </div>
        {node.primary_files && node.primary_files.length > 0 ? (
          <div className="endpoint-card-files">
            {node.primary_files.map((f) => (
              <FileLink key={f} path={f} showCopy={false} />
            ))}
          </div>
        ) : null}
        <div className="endpoint-card-actions">
          <button className="action-btn" onClick={() => onSelectNode(node.node_id)}>
            Open in inspector
          </button>
        </div>
      </div>
      {fnCount > 0 ? <FunctionsSection node={node} /> : null}
    </section>
  );
}

const EDGE_TYPE_DESC: Record<string, string> = {
  contains: "parent contains child (hierarchy)",
  depends_on: "feature uses another feature's API",
  reads_state: "reads from a state / data store",
  writes_state: "writes to a state / data store",
  owns_state: "owns / produces a state asset",
  covered_by_test: "target is covered by tests",
  documented_by: "target has docs",
  configured_by: "configured by",
  configures_role: "configures a role",
  configures_runtime: "configures runtime behavior",
  configures_analyzer: "configures an analyzer",
  configures_model_routing: "configures AI model routing",
  semantic_related: "AI-inferred relation (lower confidence)",
  creates_task: "creates a governance task",
  uses_task_metadata: "reads task metadata",
  emits_event: "emits an event onto the bus",
  consumes_event: "subscribes to an event from the bus",
  http_route: "exposes an HTTP route",
  reads_artifact: "reads from a build / generated artifact",
  writes_artifact: "writes a build / generated artifact",
};

function FunctionsSection({ node }: { node: NodeRecord }) {
  // Function lines come from the graph itself — backend persists
  // `metadata.function_lines: { "ShortName": [start, end] }` (graph adapter
  // since commit 59c9fbc). The dashboard reads them directly; no source fetch.
  const lineMap = useMemo(() => {
    const fl = node.metadata?.function_lines;
    if (!fl) return null;
    const m = new Map<string, number>();
    for (const [name, range] of Object.entries(fl)) {
      const start = Array.isArray(range) ? range[0] : Number(range);
      if (Number.isFinite(start) && start > 0) m.set(name, start);
    }
    return m;
  }, [node.metadata?.function_lines]);

  const resolvedCount = lineMap?.size ?? 0;

  return (
    <section className="inspector-section">
      <div className="inspector-section-title">
        Functions <span className="head-hint">{node.metadata?.functions?.length}</span>
        {editorConfigured ? (
          <span className="head-hint" style={{ marginLeft: "auto" }}>
            {resolvedCount > 0
              ? `${resolvedCount} mapped to ${editorScheme}`
              : "jump to module file"}
          </span>
        ) : null}
      </div>
      <ul className="fn-list">
        {(node.metadata?.functions ?? []).map((fn) => (
          <li key={fn}>
            <FunctionLink symbol={fn} primaryFiles={node.primary_files} lineMap={lineMap} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function drawerCtaFor(status: SemanticStatus): {
  kind: ActionKind;
  label: string;
  hint: string;
} {
  switch (status) {
    case "semantic_complete":
    case "reviewed":
      return { kind: "feedback", label: "Submit feedback", hint: "AI semantic exists — challenge or accept it" };
    case "semantic_pending":
    case "semantic_running":
      return { kind: "feedback", label: "Track in queue", hint: "AI semantic in flight" };
    default:
      return {
        kind: "enrich",
        label: "⚡ AI enrich",
        hint:
          status === "semantic_stale"
            ? "Source drifted — re-enrich"
            : status === "semantic_hash_unverified"
              ? "Hash mismatch — re-enrich to verify"
              : "No AI semantic yet — enrich",
      };
  }
}

function FunctionLink({
  symbol,
  primaryFiles,
  lineMap,
}: {
  symbol: string;
  primaryFiles?: string[];
  lineMap: Map<string, number> | null;
}) {
  const file = primaryFiles?.[0];
  const shortName = symbol.split("::").slice(-1)[0];
  const moduleName = symbol.split("::").slice(0, -1).join("::");
  // Graph's function_lines keys are qualified — `ClassName.method` for methods,
  // bare name for module-level functions. Try the qualified key first; fall back
  // to the last `.` segment for older snapshots indexed by leaf name only.
  const line =
    lineMap?.get(shortName) ??
    lineMap?.get(shortName.split(".").slice(-1)[0]) ??
    null;
  const url = file ? editorUrl(file, line ?? undefined) : null;
  const tooltip = `${symbol}${file ? `\n→ ${file}${line ? `:${line}` : ""}` : ""}`;
  return (
    <span className="fn-link" title={tooltip}>
      {url ? (
        <a className="fn-link-anchor" href={url}>
          <span className="fn-link-name">{shortName}</span>
          {moduleName ? <span className="fn-link-module">{moduleName}</span> : null}
          {line != null ? <span className="fn-link-line mono">:{line}</span> : null}
          <span className="fn-link-cta">↗</span>
        </a>
      ) : (
        <span className="fn-link-anchor fn-link-anchor-disabled">
          <span className="fn-link-name">{shortName}</span>
          {moduleName ? <span className="fn-link-module">{moduleName}</span> : null}
        </span>
      )}
    </span>
  );
}

function FilesTab({ node }: { node: NodeRecord }) {
  const sections: { key: string; label: string; files?: string[] }[] = [
    { key: "primary", label: "Primary", files: node.primary_files },
    { key: "secondary", label: "Secondary", files: node.secondary_files },
    { key: "tests", label: "Tests", files: node.test_files },
    { key: "config", label: "Config", files: node.config_files },
  ];
  const total = sections.reduce((acc, s) => acc + (s.files?.length ?? 0), 0);
  if (total === 0) {
    return <div className="empty">No files bound to this node.</div>;
  }
  return (
    <>
      {!editorConfigured ? (
        <div className="inspector-banner">
          <strong>Editor jump disabled.</strong> Set <span className="mono">VITE_WORKSPACE_ROOT</span>{" "}
          in <span className="mono">.env.local</span> to enable click-to-open.
        </div>
      ) : null}
      {sections.map((s) =>
        s.files && s.files.length > 0 ? (
          <section key={s.key} className="inspector-section">
            <div className="inspector-section-title">
              {s.label} <span className="head-hint">{s.files.length}</span>
              {editorConfigured ? (
                <span className="head-hint" style={{ marginLeft: "auto" }}>
                  open in {editorScheme}
                </span>
              ) : null}
            </div>
            <ul className="file-list">
              {s.files.map((f) => (
                <li key={f}>
                  <FileLink path={f} />
                </li>
              ))}
            </ul>
          </section>
        ) : null,
      )}
    </>
  );
}

function RelationsTab({
  node,
  edges,
  byId,
  onSelectNode,
}: {
  node: NodeRecord;
  edges: EdgeRecord[];
  byId: Map<string, NodeRecord>;
  onSelectNode(id: string): void;
}) {
  const out: { peer: NodeRecord; type: string; direction: "in" | "out" }[] = [];
  edges.forEach((e) => {
    const t = (e.edge_type || e.type || "default") as string;
    if (t === "contains") return;
    if (e.src === node.node_id) {
      const peer = byId.get(e.dst);
      if (peer) out.push({ peer, type: t, direction: "out" });
    } else if (e.dst === node.node_id) {
      const peer = byId.get(e.src);
      if (peer) out.push({ peer, type: t, direction: "in" });
    }
  });
  if (out.length === 0) {
    return <div className="empty">No typed relations attached.</div>;
  }
  // Group by type
  const byType = new Map<string, typeof out>();
  out.forEach((row) => {
    const arr = byType.get(row.type) ?? [];
    arr.push(row);
    byType.set(row.type, arr);
  });
  const groups = Array.from(byType.entries()).sort();
  return (
    <>
      {groups.map(([type, rows]) => (
        <section key={type} className="inspector-section">
          <div className="inspector-section-title">
            {type} <span className="head-hint">{rows.length}</span>
          </div>
          <ul className="link-list">
            {rows.map((r, i) => {
              const ps = classifyNode(r.peer);
              return (
                <li key={`${r.peer.node_id}-${r.direction}-${i}`}>
                  <button className="link-row" onClick={() => onSelectNode(r.peer.node_id)}>
                    <span className="edge-arrow" title={r.direction}>
                      {r.direction === "out" ? "→" : "←"}
                    </span>
                    <span className={`layer-badge layer-${r.peer.layer}`}>{r.peer.layer}</span>
                    <span className="link-name">{shortTitle(r.peer.title || r.peer.node_id)}</span>
                    <span className={`sem-dot ${semStatusDotClass(ps)}`} />
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </>
  );
}

// ===========================================================================
// Feedback / Backlog tabs (shared between node and edge inspector). Both pull
// existing items from the live feedback queue + render a primary CTA that
// opens the action modal pre-filled with this target.
// ===========================================================================

interface QueueGroup {
  id?: string;
  lane?: string;
  priority?: string;
  target_type?: string;
  target_id?: string;
  event_type?: string;
  issue_type?: string;
  requires_human_signoff?: boolean;
  semantic_review_ready?: boolean;
  representative_issue?: string;
  source_node_ids?: string[];
  item_count?: number;
  suppressed_count?: number;
  confidence?: number;
}

function selectQueueGroups(
  feedback: FeedbackQueueResponse | null | undefined,
  targetIds: string[],
): QueueGroup[] {
  if (!feedback || !Array.isArray(feedback.groups)) return [];
  const idSet = new Set(targetIds);
  return (feedback.groups as QueueGroup[]).filter((g) => {
    if (g.target_id && idSet.has(g.target_id)) return true;
    if (g.source_node_ids && g.source_node_ids.some((n) => idSet.has(n))) return true;
    return false;
  });
}

function FeedbackTab({
  target,
  targetIds,
  feedback,
  onOpenAction,
  onSelectNode,
}: {
  target: ActionTarget;
  targetIds: string[];
  feedback: FeedbackQueueResponse | null;
  onOpenAction?(kind: ActionKind, target: ActionTarget): void;
  onSelectNode(id: string): void;
}) {
  const matched = selectQueueGroups(feedback, targetIds);
  const summary = feedback?.summary;
  return (
    <>
      <section className="inspector-section">
        <div className="inspector-section-title">Submit feedback</div>
        <div className="action-form-hint" style={{ marginBottom: 8 }}>
          Sends to <span className="mono">POST /feedback</span> with this target attached. Use{" "}
          <strong>graph_correction</strong> for structural issues; the backend auto-creates a graph
          event for that kind.
        </div>
        <button
          className="focus-cta cta-feedback"
          style={{ width: "100%" }}
          onClick={() => onOpenAction?.("feedback", target)}
          disabled={!onOpenAction}
        >
          Submit feedback for this {target.edge ? "edge" : "node"}
        </button>
      </section>

      <section className="inspector-section">
        <div className="inspector-section-title">
          Items targeting this {target.edge ? "edge" : "node"}{" "}
          <span className="head-hint">{matched.length}</span>
        </div>
        {matched.length === 0 ? (
          <div className="empty">
            No feedback items targeting this {target.edge ? "edge endpoints" : "node"} yet.
            <div className="empty-hint">
              Live queue holds {summary?.visible_group_count ?? 0} visible · {summary?.raw_count ?? 0} raw
              items snapshot-wide.
            </div>
          </div>
        ) : (
          <ul className="link-list">
            {matched.map((g, i) => (
              <li key={g.id ?? `${g.target_id}-${i}`}>
                <QueueGroupRow group={g} onSelectNode={onSelectNode} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function BacklogTab({
  target,
  targetIds,
  feedback,
  onOpenBacklog,
}: {
  target: ActionTarget;
  targetIds: string[];
  feedback: FeedbackQueueResponse | null;
  onOpenBacklog?(target: ActionTarget): void;
}) {
  const matched = selectQueueGroups(feedback, targetIds);
  const candidateBacklog = matched.filter((g) => g.lane === "candidate_backlog");
  return (
    <>
      <section className="inspector-section">
        <div className="inspector-section-title">File backlog</div>
        <div className="action-form-hint" style={{ marginBottom: 8 }}>
          Pre-fills the backlog draft with this target's primary file(s) and node id, then opens
          the global Action Panel on the File backlog tab. Two-step flow: <span className="mono">POST /events</span>{" "}
          → <span className="mono">POST /events/{"{id}"}/file-backlog</span>.
        </div>
        <button
          className="focus-cta cta-enrich"
          style={{ width: "100%" }}
          onClick={() => onOpenBacklog?.(target)}
          disabled={!onOpenBacklog}
        >
          File backlog for this {target.edge ? "edge" : "node"}
        </button>
      </section>

      {candidateBacklog.length > 0 ? (
        <section className="inspector-section">
          <div className="inspector-section-title">
            Existing backlog candidates <span className="head-hint">{candidateBacklog.length}</span>
          </div>
          <ul className="link-list">
            {candidateBacklog.map((g, i) => (
              <li key={g.id ?? `${g.target_id}-${i}`}>
                <QueueGroupRow group={g} onSelectNode={() => {}} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="inspector-section">
        <div className="inspector-section-title">Other items in queue lanes</div>
        {matched.length - candidateBacklog.length === 0 ? (
          <div className="empty">No related queue items in other lanes.</div>
        ) : (
          <ul className="link-list">
            {matched
              .filter((g) => g.lane !== "candidate_backlog")
              .map((g, i) => (
                <li key={g.id ?? `${g.target_id}-${i}`}>
                  <QueueGroupRow group={g} onSelectNode={() => {}} />
                </li>
              ))}
          </ul>
        )}
      </section>
    </>
  );
}

function QueueGroupRow({
  group,
  onSelectNode,
}: {
  group: QueueGroup;
  onSelectNode(id: string): void;
}) {
  const conf = typeof group.confidence === "number" ? group.confidence.toFixed(2) : "—";
  const peer = group.target_id ?? "";
  return (
    <button
      className="link-row"
      onClick={() => peer && onSelectNode(peer)}
      style={{ alignItems: "flex-start", flexDirection: "column", gap: 3 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", width: "100%" }}>
        {group.lane ? <span className={`status-badge status-${laneToTone(group.lane)}`}>{group.lane}</span> : null}
        {group.priority ? <span className="pill pill-mono">{group.priority}</span> : null}
        {group.event_type ? <span className="pill pill-mono">{group.event_type}</span> : null}
        {group.issue_type ? <span className="pill pill-mono">{group.issue_type}</span> : null}
        {group.requires_human_signoff ? (
          <span className="status-badge status-failed">requires signoff</span>
        ) : null}
        <span style={{ marginLeft: "auto", fontSize: 10, color: "var(--ink-500)" }}>
          conf <strong>{conf}</strong> · items <strong>{group.item_count ?? 1}</strong>
        </span>
      </div>
      {group.representative_issue ? (
        <div style={{ fontSize: 11.5, color: "var(--ink-800)", fontWeight: 500, whiteSpace: "normal" }}>
          {group.representative_issue}
        </div>
      ) : null}
      {group.target_id ? (
        <div className="mono" style={{ fontSize: 9.5, color: "var(--ink-500)" }}>
          {group.target_type}: {group.target_id}
        </div>
      ) : null}
    </button>
  );
}

function laneToTone(lane: string): string {
  switch (lane) {
    case "graph_patch_candidate":
      return "failed";
    case "review_required":
      return "pending";
    case "candidate_backlog":
      return "pending";
    case "status_only":
      return "unknown";
    case "resolved":
      return "complete";
    case "stale":
      return "not-queued";
    default:
      return "unknown";
  }
}

// ---------------- Helpers ----------------

interface Signal {
  label: string;
  value: "yes" | "no" | "partial" | "pending";
  tone: "complete" | "failed" | "pending" | "not-queued";
}

function leafSignals(n: NodeRecord): Signal[] {
  const sem = n.semantic ?? {};
  const has = (arr?: string[]) => (arr?.length ?? 0) > 0;
  return [
    { label: "source present", value: has(n.primary_files) ? "yes" : "no", tone: has(n.primary_files) ? "complete" : "failed" },
    { label: "tests present", value: has(n.test_files) ? "yes" : "no", tone: has(n.test_files) ? "complete" : "failed" },
    { label: "docs present", value: has(n.secondary_files) ? "yes" : "no", tone: has(n.secondary_files) ? "complete" : "pending" },
    {
      label: "functions captured",
      value: (n.metadata?.function_count ?? 0) > 0 ? "yes" : "no",
      tone: (n.metadata?.function_count ?? 0) > 0 ? "complete" : "pending",
    },
    {
      label: "semantic reviewed",
      value: semReviewSignal(sem.review_status, sem.status, sem.has_semantic_payload),
      tone: semReviewTone(sem.review_status, sem.status, sem.has_semantic_payload),
    },
  ];
}

function containerSignals(_n: NodeRecord, agg: SubtreeAggregate): Signal[] {
  const total = Math.max(1, agg.total);
  const currentRatio = (agg.complete + agg.reviewed) / total;
  return [
    {
      label: "subtree feature units",
      value: agg.total > 0 ? "yes" : "no",
      tone: agg.total > 0 ? "complete" : "failed",
    },
    {
      label: "subtree current",
      value: currentRatio >= 0.95 ? "yes" : currentRatio > 0 ? "partial" : "no",
      tone: currentRatio >= 0.95 ? "complete" : currentRatio > 0 ? "pending" : "failed",
    },
    {
      label: "subtree stale",
      value: agg.stale === 0 ? "yes" : "partial",
      tone: agg.stale === 0 ? "complete" : "pending",
    },
    {
      label: "subtree hash-unverified",
      value: agg.hash_unverified === 0 ? "yes" : "no",
      tone: agg.hash_unverified === 0 ? "complete" : "failed",
    },
    {
      label: "subtree missing",
      value: agg.struct === 0 ? "yes" : "no",
      tone: agg.struct === 0 ? "complete" : "failed",
    },
  ];
}

function semReviewSignal(
  reviewStatus: string | undefined,
  status: string | undefined,
  hasPayload: boolean | undefined,
): "yes" | "no" | "pending" {
  if (hasPayload === false) return "no";
  if (reviewStatus === "complete" || status === "reviewed" || status === "semantic_reviewed") return "yes";
  if (status === "ai_complete" || status === "complete") return "pending";
  return "no";
}

function semReviewTone(
  reviewStatus: string | undefined,
  status: string | undefined,
  hasPayload: boolean | undefined,
): "complete" | "failed" | "pending" {
  const v = semReviewSignal(reviewStatus, status, hasPayload);
  if (v === "yes") return "complete";
  if (v === "pending") return "pending";
  return "failed";
}

function walkAggregate(
  rootId: string,
  byId: Map<string, NodeRecord>,
  byParent: Map<string, NodeRecord[]>,
): SubtreeAggregate {
  const out = newSubtreeAggregate();
  function walk(id: string) {
    const node = byId.get(id);
    if (!node) return;
    const kids = byParent.get(id) ?? [];
    if (kids.length === 0) {
      if (node.layer === "L7" && !isPackageMarker(node)) {
        aggregateNode(out, node);
      }
    } else {
      kids.forEach((k) => walk(k.node_id));
    }
  }
  walk(rootId);
  return out;
}

function groupChildrenByLayer(parentId: string, byParent: Map<string, NodeRecord[]>): Record<string, number> {
  const counts: Record<string, number> = {};
  function walk(id: string) {
    const kids = byParent.get(id) ?? [];
    kids.forEach((k) => {
      counts[k.layer] = (counts[k.layer] ?? 0) + 1;
      walk(k.node_id);
    });
  }
  walk(parentId);
  return counts;
}

function importantChildrenOf(
  parentId: string,
  byParent: Map<string, NodeRecord[]>,
  _byId: Map<string, NodeRecord>,
): NodeRecord[] {
  const direct = byParent.get(parentId) ?? [];
  if (direct.length === 0) return [];
  // Sort: stale > hash-unverified > pending > complete (so the worst surface to the top)
  const score = (n: NodeRecord) => {
    const s = classifyNode(n);
    switch (s) {
      case "semantic_failed":
        return 0;
      case "semantic_stale":
        return 1;
      case "semantic_hash_unverified":
        return 2;
      case "semantic_pending":
      case "semantic_running":
        return 3;
      case "review_pending":
        return 4;
      case "structure_only":
        return 5;
      default:
        return 6;
    }
  };
  return direct.slice().sort((a, b) => score(a) - score(b)).slice(0, 6);
}

function nodeTypeLabel(node: NodeRecord, isContainer: boolean): string {
  if (!isContainer) return node.layer === "L7" ? "Feature" : node.layer === "L4" ? "Asset" : "Leaf";
  switch (node.layer) {
    case "L1":
      return "Project";
    case "L2":
      return "Domain";
    case "L3":
      return "Module";
    case "L4":
      return "Asset group";
    default:
      return "Container";
  }
}

function shortTitle(t: string): string {
  const parts = t.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : t;
}

function nonEmpty(...values: (string | undefined)[]): string {
  for (const x of values) if (x) return x;
  return "—";
}

function truncateHash(h: string | undefined): string {
  if (!h) return "—";
  return h.length > 28 ? `${h.slice(0, 22)}…` : h;
}

function statusTone(s: SemanticStatus): "green" | "amber" | "red" | "purple" | "blue" | "neutral" {
  switch (s) {
    case "semantic_complete":
    case "reviewed":
      return "green";
    case "semantic_stale":
    case "semantic_pending":
      return "amber";
    case "semantic_hash_unverified":
    case "review_pending":
      return "purple";
    case "semantic_running":
      return "blue";
    case "semantic_failed":
      return "red";
    case "structure_only":
    default:
      return "neutral";
  }
}
