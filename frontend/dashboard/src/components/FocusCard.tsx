import type { EdgeRecord, NodeRecord } from "../types";
import { classifyNode, semStatusDotClass, semStatusLabel, type SemanticStatus } from "../lib/semantic";
import type { Tab as InspectorTab } from "./InspectorDrawer";
import type { ActionKind, ActionTarget } from "./ActionControlPanel";

export interface PinnedEdge {
  src: string;
  dst: string;
  type: string;
  evidence?: string;
  direction?: string;
  confidence?: number;
}

interface Props {
  node: NodeRecord | null;
  edge: PinnedEdge | null;
  byId: Map<string, NodeRecord>;
  byParent: Map<string, NodeRecord[]>;
  edgesBySrc: Map<string, EdgeRecord[]>;
  edgesByDst: Map<string, EdgeRecord[]>;
  onOpenDrawerTab(tab: InspectorTab): void;
  onOpenAction(kind: ActionKind, target: ActionTarget): void;
  onJumpToNode(id: string): void;
  onClearEdge(): void;
}

export default function FocusCard(props: Props) {
  if (props.edge) {
    return <EdgeFocusCard {...props} edge={props.edge} />;
  }
  if (props.node) {
    return <NodeFocusCard {...props} node={props.node} />;
  }
  return (
    <div className="focus-card focus-card-empty">
      <div className="focus-empty-title">Pick a node</div>
      <div className="focus-empty-hint">
        Click any tree row, graph card, or edge to inspect.
        <br />
        Click a status row here to open the right drawer.
      </div>
    </div>
  );
}

// ---------------- Node focus card ----------------

function NodeFocusCard({
  node,
  byParent,
  edgesBySrc,
  edgesByDst,
  onOpenDrawerTab,
  onOpenAction,
}: Props & { node: NodeRecord }) {
  const status = classifyNode(node);
  const tone = statusToTone(status);
  const isContainer = (byParent.get(node.node_id)?.length ?? 0) > 0;
  const meta = nodeMetrics(node, byParent, edgesBySrc, edgesByDst);
  const cta = ctaFor(status);

  return (
    <div className="focus-card">
      <div className="focus-head">
        <span className="focus-kind">{isContainer ? "Container" : node.layer === "L7" ? "Feature" : node.layer === "L4" ? "Asset" : "Node"}</span>
        <span className={`layer-badge layer-${node.layer}`} style={{ marginLeft: "auto" }}>
          {node.layer}
        </span>
      </div>
      <div className="focus-title-block">
        <div className="focus-title" title={node.title}>
          {shortTitle(node.title || node.node_id)}
        </div>
        <div className="focus-mono-line" title={node.node_id}>
          {node.title || ""}
          {node.title && node.title !== node.node_id ? " · " : ""}
          {node.node_id}
        </div>
        <span className={`sem-state-row tone-${tone}`} style={{ marginTop: 8 }}>
          <span className={`sem-dot ${semStatusDotClass(status)}`} />
          <span>{semStatusLabel(status)}</span>
        </span>
      </div>

      <div className="focus-rows">
        {/* Health row is hidden for L4 assets — they're config/state files
            with no scoreable signals. Per-operator decision: no health and
            no asset_binding score (which used to show here as a "—" with
            a binding hint). */}
        {node.layer !== "L4" ? (
          <StatusRow
            label="Health"
            value={meta.health == null ? "—" : `${meta.health}`}
            tone={meta.healthTone}
            hint={meta.healthHint}
            onClick={() => onOpenDrawerTab("problems")}
          />
        ) : null}
        <StatusRow
          label="Semantic"
          value={meta.semanticBadge.value}
          tone={meta.semanticBadge.tone}
          hint={meta.semanticBadge.hint}
          onClick={() => onOpenDrawerTab("overview")}
        />
        <StatusRow
          label="Docs"
          value={meta.docsBadge.value}
          tone={meta.docsBadge.tone}
          hint={meta.docsBadge.hint}
          onClick={() => onOpenDrawerTab("files")}
          actionable={meta.docsBadge.actionable}
        />
        <StatusRow
          label="Tests"
          value={meta.testsBadge.value}
          tone={meta.testsBadge.tone}
          hint={meta.testsBadge.hint}
          onClick={() => onOpenDrawerTab("files")}
          actionable={meta.testsBadge.actionable}
        />
        <StatusRow
          label="Relations"
          value={`${meta.outCount + meta.inCount}`}
          tone="neutral"
          hint={`${meta.inCount} in · ${meta.outCount} out (typed)`}
          onClick={() => onOpenDrawerTab("relations")}
          actionable={meta.outCount + meta.inCount > 0}
        />
        {isContainer ? (
          <StatusRow
            label="Subtree"
            value={`${meta.subtreeCount}`}
            tone="neutral"
            hint={meta.subtreeHint}
            onClick={() => onOpenDrawerTab("overview")}
          />
        ) : null}
      </div>

      <div className="focus-foot">
        <button
          className={`focus-cta cta-${cta.kind}`}
          onClick={() => onOpenAction(cta.kind, { node, forceMode: cta.forceMode })}
          title={cta.hint}
        >
          {cta.label}
        </button>
        <button className="action-btn" onClick={() => onOpenDrawerTab("overview")}>
          Inspector
        </button>
      </div>
    </div>
  );
}

// ---------------- Edge focus card ----------------

function EdgeFocusCard({
  edge,
  byId,
  onJumpToNode,
  onClearEdge,
  onOpenAction,
}: Props & { edge: PinnedEdge }) {
  const src = byId.get(edge.src);
  const dst = byId.get(edge.dst);
  const desc = EDGE_TYPE_DESC[edge.type] ?? "structure-derived typed relation";
  // Edges currently have no per-edge semantic state in the snapshot, so the
  // primary CTA is always 'enrich' until the backend surfaces edge_semantic.
  const cta: { kind: ActionKind; label: string } = {
    kind: "enrich",
    label: "⚡ AI enrich edge",
  };
  return (
    <div className="focus-card focus-edge">
      <div className="focus-head">
        <span className="focus-kind" style={{ color: "var(--purple-fg)" }}>Relation</span>
        <button className="btn-close" onClick={onClearEdge} aria-label="Clear edge selection">
          ×
        </button>
      </div>
      <div className="focus-title-block">
        <div className="focus-title">
          {shortTitle(src?.title || edge.src)} → {shortTitle(dst?.title || edge.dst)}
        </div>
        <span
          className="sem-state-row tone-purple"
          style={{ marginTop: 8 }}
          title={desc}
        >
          <span className="sem-dot unverified" />
          <span className="mono">{edge.type}</span>
        </span>
      </div>
      <div className="focus-rows" style={{ paddingTop: 4 }}>
        <div className="focus-section-label">Meaning</div>
        <div className="focus-paragraph">{desc}</div>
      </div>
      <div className="focus-rows">
        <div className="focus-section-label">Endpoints</div>
        <button className="endpoint-link" onClick={() => onJumpToNode(edge.src)}>
          <span className={`layer-badge layer-${src?.layer ?? ""}`}>{src?.layer ?? "?"}</span>
          <span className="endpoint-name">{shortTitle(src?.title || edge.src)}</span>
          <span className="endpoint-id mono">{edge.src}</span>
        </button>
        <div className="endpoint-arrow">↓</div>
        <button className="endpoint-link" onClick={() => onJumpToNode(edge.dst)}>
          <span className={`layer-badge layer-${dst?.layer ?? ""}`}>{dst?.layer ?? "?"}</span>
          <span className="endpoint-name">{shortTitle(dst?.title || edge.dst)}</span>
          <span className="endpoint-id mono">{edge.dst}</span>
        </button>
      </div>
      <div className="focus-foot focus-foot-edge">
        <button
          className={`focus-cta cta-${cta.kind}`}
          onClick={() => onOpenAction(cta.kind, { edge, forceMode: cta.forceMode })}
        >
          {cta.label}
        </button>
        <button className="action-btn" onClick={() => onJumpToNode(edge.src)}>
          Go to source
        </button>
        <button className="action-btn" onClick={() => onJumpToNode(edge.dst)}>
          Go to target
        </button>
      </div>
    </div>
  );
}

// ---------------- CTA classifier ----------------

function ctaFor(status: SemanticStatus): {
  kind: ActionKind;
  label: string;
  hint: string;
  forceMode?: "semanticize" | "retry" | "review";
} {
  // CTA always launches the AI enrich modal — when the AI semantic is already
  // current/reviewed the CTA becomes "Retry AI enrich" (operator wants the
  // model to take another pass, possibly with a note to course-correct).
  // Feedback as a separate verb is deferred (Feedback/Backlog tabs were
  // removed; will be redone later).
  switch (status) {
    case "semantic_complete":
    case "reviewed":
      return {
        kind: "enrich",
        label: "↻ Retry AI enrich",
        hint: "AI semantic exists — retry to refine or course-correct",
        forceMode: "retry",
      };
    case "semantic_pending":
    case "semantic_running":
      return {
        kind: "enrich",
        label: "↻ Retry AI enrich",
        hint: "AI semantic is in flight — queue another pass",
        forceMode: "retry",
      };
    case "semantic_stale":
      return {
        kind: "enrich",
        label: "↻ Retry AI enrich",
        hint: "Source drifted — re-enrich to refresh",
        forceMode: "retry",
      };
    case "semantic_hash_unverified":
      return {
        kind: "enrich",
        label: "↻ Retry AI enrich",
        hint: "Hash mismatch — re-enrich to verify",
        forceMode: "retry",
      };
    default:
      return {
        kind: "enrich",
        label: "⚡ AI enrich",
        hint: "No AI semantic yet — enrich to populate",
      };
  }
}

// ---------------- Helpers ----------------

interface StatusBadge {
  value: string;
  tone: BadgeTone;
  hint: string;
  actionable?: boolean;
}

type BadgeTone = "complete" | "pending" | "failed" | "unknown" | "neutral";

interface NodeMetrics {
  health: number | null;
  healthTone: BadgeTone;
  healthHint: string;
  semanticBadge: StatusBadge;
  docsBadge: StatusBadge;
  testsBadge: StatusBadge;
  outCount: number;
  inCount: number;
  subtreeCount: number;
  subtreeHint: string;
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

function nodeMetrics(
  node: NodeRecord,
  byParent: Map<string, NodeRecord[]>,
  edgesBySrc: Map<string, EdgeRecord[]>,
  edgesByDst: Map<string, EdgeRecord[]>,
): NodeMetrics {
  const status = classifyNode(node);
  const sem = node.semantic ?? {};
  const v = sem.validity ?? {};

  // Feature health — single source of truth is node._health computed by
  // lib/health.ts (leafScore = 35 src + 30 tests + 20 fns + 10 docs + 5
  // parent; container = recursive avg of L7 descendants). L4 leaves are
  // intentionally unscored — they're config/asset files and the row is
  // hidden in the FocusCard above; this hint is only used for L7/container.
  const health: number | null = node._health ?? null;
  const healthTone: BadgeTone =
    health == null ? "unknown" : health >= 85 ? "complete" : health >= 70 ? "pending" : "failed";
  const healthHint =
    health == null ? "no scoreable signals — structure only" : `score ${health}/100`;

  // Semantic badge — exposes the classified status
  const semanticBadge: StatusBadge = {
    value: semStatusLabel(status),
    tone: statusToTone(status),
    hint: semanticHint(status, v),
  };

  // Docs row — secondary_files act as binding evidence
  const docsCount = node.secondary_files?.length ?? 0;
  const docDrift = isDocDrift(v, sem.doc_status);
  const docsBadge: StatusBadge = docsCount === 0
    ? { value: "missing", tone: "failed", hint: "no docs bound to this node", actionable: true }
    : docDrift
      ? { value: "drift", tone: "pending", hint: `${docsCount} bound · file hash drifted`, actionable: true }
      : sem.doc_status === "thin" || sem.doc_status === "weak"
        ? { value: sem.doc_status, tone: "pending", hint: `${docsCount} bound · semantic flagged ${sem.doc_status}`, actionable: true }
        : { value: `${docsCount}`, tone: "complete", hint: `${docsCount} bound docs ok`, actionable: docsCount > 0 };

  // Tests row
  const testCount = node.test_files?.length ?? 0;
  const testsBadge: StatusBadge = testCount === 0
    ? { value: "missing", tone: "failed", hint: "no tests bound", actionable: true }
    : sem.test_status === "thin" || sem.test_status === "partial" || sem.test_status === "weak"
      ? { value: sem.test_status, tone: "pending", hint: `${testCount} bound · ${sem.test_status}`, actionable: true }
      : { value: `${testCount}`, tone: "complete", hint: `${testCount} test files`, actionable: true };

  // Relations counts (typed only)
  let outCount = 0;
  let inCount = 0;
  (edgesBySrc.get(node.node_id) ?? []).forEach((e) => {
    const t = (e.edge_type || e.type || "default") as string;
    if (t !== "contains") outCount++;
  });
  (edgesByDst.get(node.node_id) ?? []).forEach((e) => {
    const t = (e.edge_type || e.type || "default") as string;
    if (t !== "contains") inCount++;
  });

  const subtree = walkSubtree(node.node_id, byParent);
  const subtreeHint = subtree.containers + subtree.leaves > 0
    ? `${subtree.leaves} leaves · ${subtree.containers} containers`
    : "leaf node";

  return {
    health,
    healthTone,
    healthHint,
    semanticBadge,
    docsBadge,
    testsBadge,
    outCount,
    inCount,
    subtreeCount: subtree.leaves + subtree.containers,
    subtreeHint,
  };
}

function isDocDrift(
  v: NodeRecord["semantic"] extends infer T ? (T extends { validity?: infer V } ? V : never) : never,
  docStatus?: string,
): boolean {
  if (!v) return false;
  const fhs = (v as { file_hash_status?: string })?.file_hash_status;
  if (fhs === "stale" || fhs === "drifted" || fhs === "changed") return true;
  if (docStatus === "drift") return true;
  return false;
}

function statusToTone(s: SemanticStatus): BadgeTone {
  switch (s) {
    case "semantic_complete":
    case "reviewed":
      return "complete";
    case "semantic_stale":
    case "semantic_pending":
    case "semantic_running":
    case "review_pending":
    case "semantic_hash_unverified":
      return "pending";
    case "semantic_failed":
      return "failed";
    case "structure_only":
    default:
      return "neutral";
  }
}

function semanticHint(s: SemanticStatus, v: { hash_validation?: string; status?: string }): string {
  switch (s) {
    case "semantic_complete":
      return "AI semantic current; hash matches source";
    case "reviewed":
      return "AI semantic reviewed and accepted";
    case "semantic_stale":
      return `validity.${v.status || v.hash_validation || "stale"} — re-enrich needed`;
    case "semantic_hash_unverified":
      return "semantic exists but source hash drifted";
    case "semantic_pending":
      return "AI semantic queued";
    case "semantic_running":
      return "AI semantic running";
    case "review_pending":
      return "AI semantic complete; awaiting review";
    case "semantic_failed":
      return "AI semantic enrichment failed";
    case "structure_only":
    default:
      return "no AI semantic yet — structure only";
  }
}

function walkSubtree(rootId: string, byParent: Map<string, NodeRecord[]>): { leaves: number; containers: number } {
  let leaves = 0;
  let containers = 0;
  const stack = [...(byParent.get(rootId) ?? [])];
  while (stack.length) {
    const top = stack.pop()!;
    const kids = byParent.get(top.node_id);
    if (kids && kids.length) {
      containers++;
      stack.push(...kids);
    } else {
      leaves++;
    }
  }
  return { leaves, containers };
}

function shortTitle(t: string): string {
  const parts = t.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : t;
}

// ---------------- Status row ----------------

function StatusRow({
  label,
  value,
  tone,
  hint,
  onClick,
  actionable,
}: {
  label: string;
  value: string;
  tone: BadgeTone;
  hint: string;
  onClick(): void;
  actionable?: boolean;
}) {
  // Always clickable so the row can route the user to the inspector tab; but
  // problematic states get a stronger affordance.
  const clickable = actionable ?? true;
  return (
    <button
      className={`focus-row${clickable ? " clickable" : ""}`}
      onClick={clickable ? onClick : undefined}
      title={hint}
    >
      <span className="focus-row-label">{label}</span>
      <span className={`focus-row-pill tone-${tone}`}>{value}</span>
      <span className="focus-row-hint">{hint}</span>
      {clickable ? <span className="focus-row-chev">›</span> : null}
    </button>
  );
}
