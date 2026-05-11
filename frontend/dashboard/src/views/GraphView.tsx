import { useEffect, useMemo, useRef, useState } from "react";
import { select, type Selection } from "d3-selection";
import { zoom as d3Zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import type { EdgeRecord, Layer, NodeRecord } from "../types";
import {
  aggregateNode,
  classifyNode,
  newSubtreeAggregate,
  type SemanticStatus,
  type SubtreeAggregate,
} from "../lib/semantic";
import { healthHex } from "../lib/health";
import FocusCard, { type PinnedEdge } from "../components/FocusCard";
import type { Tab as InspectorTab } from "../components/InspectorDrawer";
import type { ActionKind, ActionTarget } from "../components/ActionControlPanel";

interface Props {
  nodes: NodeRecord[];
  edges: EdgeRecord[];
  selectedNodeId: string | null;
  pinnedEdge: PinnedEdge | null;
  onPinEdge(edge: PinnedEdge | null): void;
  onSelectNode(id: string): void;
  onOpenDrawerTab(tab: InspectorTab): void;
  onOpenAction(kind: ActionKind, target: ActionTarget): void;
  // Multi-select mode — when set, the graph paints selected nodes/edges with
  // a "✓" highlight and clicks toggle the bucket via onSelectNode /
  // onPinEdge instead of navigating.
  multiSelectMode?: boolean;
  multiSelectIds?: Set<string>;
}

type GraphMode = "hierarchy" | "relations";

interface EdgeInfo {
  key: string;
  type: string;
  srcId: string;
  dstId: string;
  srcTitle: string;
  dstTitle: string;
  srcLayer: string;
  dstLayer: string;
  x: number;
  y: number;
}

function edgeKey(e: { src: string; dst: string; type: string }): string {
  return `${e.src}|${e.dst}|${e.type}`;
}

const EDGE_TYPE_INFO: { k: string; color: string; dash: string }[] = [
  { k: "contains", color: "#cbd5e1", dash: "" },
  { k: "depends_on", color: "#6366f1", dash: "" },
  { k: "reads_state", color: "#10b981", dash: "5 3" },
  { k: "writes_state", color: "#f59e0b", dash: "5 3" },
  { k: "owns_state", color: "#0ea5e9", dash: "" },
  { k: "covered_by_test", color: "#eab308", dash: "2 3" },
  { k: "documented_by", color: "#06b6d4", dash: "2 3" },
  { k: "configured_by", color: "#8b5cf6", dash: "2 3" },
  { k: "semantic_related", color: "#a855f7", dash: "3 4" },
  { k: "creates_task", color: "#0ea5e9", dash: "" },
  { k: "uses_task_metadata", color: "#0284c7", dash: "" },
  { k: "emits_event", color: "#ec4899", dash: "" },
  { k: "consumes_event", color: "#db2777", dash: "" },
  { k: "http_route", color: "#3b82f6", dash: "" },
  { k: "reads_artifact", color: "#f97316", dash: "" },
  { k: "writes_artifact", color: "#ea580c", dash: "" },
];

function edgeStyle(type: string): { color: string; dash: string } {
  const found = EDGE_TYPE_INFO.find((t) => t.k === type);
  if (found) return { color: found.color, dash: found.dash };
  return { color: "#94a3b8", dash: "" };
}

const RELATIONS_DEFAULT_TYPES = new Set([
  "depends_on",
  "reads_state",
  "writes_state",
  "owns_state",
  "creates_task",
  "uses_task_metadata",
  "emits_event",
  "consumes_event",
  "http_route",
  "reads_artifact",
  "writes_artifact",
  "covered_by_test",
  "documented_by",
  "configured_by",
  "semantic_related",
]);

interface Index {
  byId: Map<string, NodeRecord>;
  byParent: Map<string, NodeRecord[]>;
  edgesBySrc: Map<string, EdgeRecord[]>;
  edgesByDst: Map<string, EdgeRecord[]>;
  agg: Map<string, SubtreeAggregate>;
}

interface PlacedNode {
  node: NodeRecord;
  x: number;
  y: number;
  role: "chain" | "focus" | "sibling" | "child" | "out" | "in" | "context" | "more" | "empty";
  isFocus: boolean;
  isLeaf: boolean;
  status: SemanticStatus;
  subtreeCount: number;
  edgeMeta?: string; // optional one-line summary shown under context/edge nodes
}

interface PlacedEdge {
  src: string;
  dst: string;
  type: string;
  s: PlacedNode;
  t: PlacedNode;
}

function buildIndex(nodes: NodeRecord[], edges: EdgeRecord[]): Index {
  const byId = new Map<string, NodeRecord>();
  const byParent = new Map<string, NodeRecord[]>();
  const edgesBySrc = new Map<string, EdgeRecord[]>();
  const edgesByDst = new Map<string, EdgeRecord[]>();
  nodes.forEach((n) => byId.set(n.node_id, n));
  nodes.forEach((n) => {
    const p = n.metadata?.hierarchy_parent;
    if (p && byId.has(p)) {
      const arr = byParent.get(p) ?? [];
      arr.push(n);
      byParent.set(p, arr);
    }
  });
  edges.forEach((e) => {
    if (!byId.has(e.src) || !byId.has(e.dst)) return;
    const srcArr = edgesBySrc.get(e.src) ?? [];
    srcArr.push(e);
    edgesBySrc.set(e.src, srcArr);
    const dstArr = edgesByDst.get(e.dst) ?? [];
    dstArr.push(e);
    edgesByDst.set(e.dst, dstArr);
  });

  const agg = new Map<string, SubtreeAggregate>();
  function walk(id: string): SubtreeAggregate {
    const cached = agg.get(id);
    if (cached) return cached;
    const node = byId.get(id);
    const out = newSubtreeAggregate();
    if (!node) {
      agg.set(id, out);
      return out;
    }
    const kids = byParent.get(id) ?? [];
    if (kids.length === 0) {
      aggregateNode(out, node);
    } else {
      kids.forEach((k) => {
        const sub = walk(k.node_id);
        out.total += sub.total;
        out.complete += sub.complete;
        out.reviewed += sub.reviewed;
        out.hash_unverified += sub.hash_unverified;
        out.pending += sub.pending;
        out.running += sub.running;
        out.stale += sub.stale;
        out.failed += sub.failed;
        out.review += sub.review;
        out.struct += sub.struct;
      });
    }
    agg.set(id, out);
    return out;
  }
  nodes.forEach((n) => walk(n.node_id));

  return { byId, byParent, edgesBySrc, edgesByDst, agg };
}

function isContainerNode(idx: Index, id: string): boolean {
  return (idx.byParent.get(id)?.length ?? 0) > 0;
}

function subtreeCount(idx: Index, id: string): number {
  // Count of descendants (matches prototype's "NODES" caption — total - 1).
  let n = 0;
  const stack = [...(idx.byParent.get(id) ?? [])];
  while (stack.length) {
    const top = stack.pop()!;
    n++;
    const kids = idx.byParent.get(top.node_id);
    if (kids) stack.push(...kids);
  }
  return n;
}

function rootIds(nodes: NodeRecord[], byId: Map<string, NodeRecord>): string[] {
  return nodes
    .filter((n) => {
      const p = n.metadata?.hierarchy_parent;
      return !p || !byId.has(p);
    })
    .map((n) => n.node_id);
}

function chainOf(node: NodeRecord, byId: Map<string, NodeRecord>): NodeRecord[] {
  const chain: NodeRecord[] = [];
  let cur: NodeRecord | undefined = node;
  let safety = 8;
  while (cur && safety-- > 0) {
    chain.unshift(cur);
    const parent: string | undefined = cur.metadata?.hierarchy_parent;
    cur = parent ? byId.get(parent) : undefined;
  }
  return chain;
}

export default function GraphView({
  nodes,
  edges,
  selectedNodeId,
  pinnedEdge,
  onPinEdge,
  onSelectNode,
  onOpenDrawerTab,
  onOpenAction,
  multiSelectMode = false,
  multiSelectIds,
}: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<GraphMode>("hierarchy");
  const [edgeFilter, setEdgeFilter] = useState<Set<string>>(() => new Set(["contains", ...RELATIONS_DEFAULT_TYPES]));
  const pinnedRef = useRef<PinnedEdge | null>(null);
  pinnedRef.current = pinnedEdge;

  const idx = useMemo(() => buildIndex(nodes, edges), [nodes, edges]);

  // Pick a focus node: explicit selection > first L1 root > first node.
  const focusNode = useMemo<NodeRecord | null>(() => {
    if (selectedNodeId && idx.byId.has(selectedNodeId)) return idx.byId.get(selectedNodeId)!;
    const roots = rootIds(nodes, idx.byId);
    if (roots[0]) return idx.byId.get(roots[0]) ?? null;
    return nodes[0] ?? null;
  }, [selectedNodeId, idx, nodes]);

  const allEdgeTypes = useMemo(() => {
    const set = new Set<string>();
    edges.forEach((e) => set.add((e.edge_type || e.type || "default") as string));
    return Array.from(set).sort((a, b) => {
      const ai = EDGE_TYPE_INFO.findIndex((t) => t.k === a);
      const bi = EDGE_TYPE_INFO.findIndex((t) => t.k === b);
      return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
    });
  }, [edges]);

  // ---------- Render ----------
  useEffect(() => {
    if (!svgRef.current || !wrapRef.current || !focusNode) return;
    const svgEl = svgRef.current;
    const { width, height } = wrapRef.current.getBoundingClientRect();
    if (width <= 0 || height <= 0) return;

    const svg = select(svgEl);
    svg.selectAll("*").remove();
    const root = svg.append("g").attr("class", "graph-root");
    const linkLayer = root.append("g").attr("class", "graph-links");
    const nodeLayer = root.append("g").attr("class", "graph-nodes");

    const zoomBehavior = d3Zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.2, 4])
      .on("zoom", (event: { transform: ZoomTransform }) => {
        root.attr("transform", event.transform.toString());
      });
    svg.call(zoomBehavior).call(zoomBehavior.transform, zoomIdentity);

    const renderArgs: RenderArgs = {
      focus: focusNode,
      idx,
      edges,
      edgeFilter,
      width,
      height,
      linkLayer,
      nodeLayer,
      onSelectNode,
      onEdgeHover: () => {
        // Hover-only: no UI side-effect now. Reserved for future quick tooltip.
      },
      onEdgeClick: (info) => {
        const cur = pinnedRef.current;
        const same = cur && cur.src === info.srcId && cur.dst === info.dstId && cur.type === info.type;
        if (same) {
          onPinEdge(null);
        } else {
          // Look up the original EdgeRecord to capture evidence/confidence/direction.
          const match = edges.find(
            (e) =>
              e.src === info.srcId &&
              e.dst === info.dstId &&
              ((e.edge_type || e.type || "") as string) === info.type,
          );
          onPinEdge({
            src: info.srcId,
            dst: info.dstId,
            type: info.type,
            evidence: typeof match?.evidence === "string" ? match.evidence : undefined,
            direction: match?.direction,
            confidence: match?.confidence,
          });
        }
      },
      multiSelectMode,
      multiSelectIds: multiSelectIds ?? new Set<string>(),
    };

    if (mode === "hierarchy") {
      renderHierarchy(renderArgs);
    } else {
      renderRelations(renderArgs);
    }

    // Click empty canvas clears any pinned edge selection.
    svg.on("click", (event: MouseEvent) => {
      const target = event.target as Element | null;
      if (target && target.closest(".gnode, .ghit, .gnode-hit")) return;
      onPinEdge(null);
    });

    // Both modes auto-fit so parent chain + focus + neighbors all fit cleanly.
    window.setTimeout(() => fitView(svg, root, zoomBehavior, width, height), 50);
  }, [
    focusNode,
    mode,
    idx,
    edges,
    edgeFilter,
    onSelectNode,
    multiSelectMode,
    // Serialize the Set so changing selection re-runs the d3 render to repaint
    // the ✓ highlight on toggled nodes/edges. Joining keeps the dep array
    // stable when the Set membership is identical.
    multiSelectIds ? [...multiSelectIds].sort().join("|") : "",
  ]);

  // ---------- Toolbar handlers ----------
  const toggleEdge = (t: string) =>
    setEdgeFilter((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });

  const breadcrumb = useMemo(() => (focusNode ? chainOf(focusNode, idx.byId) : []), [focusNode, idx.byId]);

  return (
    <div className="graph-view">
      <div className="graph-toolbar">
        <div className="toolbar-row">
          <div className="mode-tabs">
            <button
              className={`mode-tab${mode === "hierarchy" ? " active" : ""}`}
              onClick={() => setMode("hierarchy")}
              title="Parent chain → focus → children"
            >
              Hierarchy
            </button>
            <button
              className={`mode-tab${mode === "relations" ? " active" : ""}`}
              onClick={() => setMode("relations")}
              title="Force-directed: relations of all visible nodes"
            >
              Relations
            </button>
          </div>
          <div className="breadcrumb breadcrumb-toolbar">
            {breadcrumb.length === 0 ? (
              <span className="text-muted">no focus</span>
            ) : (
              breadcrumb.map((b, i) => (
                <span key={b.node_id} className="breadcrumb-item">
                  {i > 0 ? <span className="breadcrumb-sep">›</span> : null}
                  <span className={`layer-badge layer-${b.layer}`}>{b.layer}</span>
                  {b.node_id === focusNode?.node_id ? (
                    <span className="breadcrumb-self">{b.title}</span>
                  ) : (
                    <a
                      href="#"
                      onClick={(e) => {
                        e.preventDefault();
                        onSelectNode(b.node_id);
                      }}
                    >
                      {b.title}
                    </a>
                  )}
                </span>
              ))
            )}
          </div>
          <span className="toolbar-spacer" />
          <span className="toolbar-meta">
            {mode === "hierarchy"
              ? "parent chain → focus → children"
              : "fan-in (left) → focus → fan-out (right)"}
          </span>
        </div>
        <div className="toolbar-row">
          <span className="toolbar-label">Edges</span>
          {allEdgeTypes.map((t) => (
            <button
              key={t}
              className={`chip edge-chip${edgeFilter.has(t) ? " on" : " off"}`}
              onClick={() => toggleEdge(t)}
              title={edgeFilter.has(t) ? `Hide ${t}` : `Show ${t}`}
            >
              <span className="edge-swatch" style={{ background: edgeStyle(t).color }} />
              {t}
            </button>
          ))}
        </div>
      </div>
      <div className="graph-canvas" ref={wrapRef}>
        <svg ref={svgRef} className="graph-svg" />
        <FocusCard
          node={focusNode ?? null}
          edge={pinnedEdge}
          byId={idx.byId}
          byParent={idx.byParent}
          edgesBySrc={idx.edgesBySrc}
          edgesByDst={idx.edgesByDst}
          onOpenDrawerTab={onOpenDrawerTab}
          onOpenAction={onOpenAction}
          onJumpToNode={(id) => {
            onPinEdge(null);
            onSelectNode(id);
          }}
          onClearEdge={() => onPinEdge(null)}
        />
        <div className="graph-legend">
          <div className="legend-title">Mode: {mode}</div>
          <div className="legend-row">
            <span className="sem-dot complete" /> <span>current</span>
          </div>
          <div className="legend-row">
            <span className="sem-dot stale" /> <span>stale</span>
          </div>
          <div className="legend-row">
            <span className="sem-dot unverified" /> <span>hash-unverified</span>
          </div>
          <div className="legend-row">
            <span className="sem-dot struct" /> <span>structure</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------- Hierarchy mode ----------------

interface RenderArgs {
  focus: NodeRecord;
  idx: Index;
  edges: EdgeRecord[];
  edgeFilter: Set<string>;
  width: number;
  height: number;
  linkLayer: ReturnType<typeof select<SVGGElement, unknown>>;
  nodeLayer: ReturnType<typeof select<SVGGElement, unknown>>;
  onSelectNode(id: string): void;
  onEdgeHover(info: EdgeInfo | null): void;
  onEdgeClick(info: EdgeInfo): void;
  multiSelectMode: boolean;
  multiSelectIds: Set<string>;
}

function renderHierarchy(args: RenderArgs) {
  const { focus, idx, width, height, linkLayer, nodeLayer, onSelectNode } = args;

  const chain = chainOf(focus, idx.byId);
  const parent = chain.length > 1 ? chain[chain.length - 2] : null;
  const siblings = parent
    ? (idx.byParent.get(parent.node_id) ?? []).filter((n) => n.node_id !== focus.node_id)
    : [];
  const children = idx.byParent.get(focus.node_id) ?? [];

  const placed = new Map<string, PlacedNode>();
  const cols = chain.length + 1;
  const colW = Math.max(190, Math.min(280, (width - 80) / Math.max(4, cols)));
  const startX = 60;
  const focusY = height * 0.42;

  chain.forEach((c, i) => {
    placed.set(c.node_id, {
      node: c,
      x: startX + i * colW,
      y: focusY,
      role: c.node_id === focus.node_id ? "focus" : "chain",
      isFocus: c.node_id === focus.node_id,
      isLeaf: !isContainerNode(idx, c.node_id),
      status: classifyNode(c),
      subtreeCount: subtreeCount(idx, c.node_id),
    });
  });

  const focusX = placed.get(focus.node_id)!.x;

  // Siblings stacked below focus (max 10)
  const sibCap = 10;
  const sibHasContainer = siblings.slice(0, sibCap).some((s) => isContainerNode(idx, s.node_id));
  const sibPitch = sibHasContainer ? 88 : 60;
  const sibStartOffset = sibHasContainer ? 110 : 80;
  siblings.slice(0, sibCap).forEach((s, i) => {
    placed.set(s.node_id, {
      node: s,
      x: focusX,
      y: focusY + sibStartOffset + i * sibPitch,
      role: "sibling",
      isFocus: false,
      isLeaf: !isContainerNode(idx, s.node_id),
      status: classifyNode(s),
      subtreeCount: subtreeCount(idx, s.node_id),
    });
  });

  // Children fanned right (max 14)
  const childX = focusX + colW;
  const childCap = 14;
  const shownChildren = children.slice(0, childCap);
  const childHasContainer = shownChildren.some((c) => isContainerNode(idx, c.node_id));
  const childPitch = childHasContainer ? 88 : 60;
  shownChildren.forEach((c, i) => {
    placed.set(c.node_id, {
      node: c,
      x: childX,
      y: focusY + (i - (shownChildren.length - 1) / 2) * childPitch,
      role: "child",
      isFocus: false,
      isLeaf: !isContainerNode(idx, c.node_id),
      status: classifyNode(c),
      subtreeCount: subtreeCount(idx, c.node_id),
    });
  });

  if (children.length > childCap) {
    const overflowNode: NodeRecord = {
      node_id: "__more__",
      layer: "L7",
      title: `+${children.length - childCap} more`,
    };
    placed.set("__more__", {
      node: overflowNode,
      x: childX,
      y: focusY + (shownChildren.length / 2 + 0.6) * childPitch,
      role: "more",
      isFocus: false,
      isLeaf: true,
      status: "structure_only",
      subtreeCount: children.length - childCap,
    });
  }

  // Edges: contains only (chain + focus→children + chain edges)
  const placedEdges: PlacedEdge[] = [];
  if (args.edgeFilter.has("contains")) {
    args.edges.forEach((e) => {
      const t = (e.edge_type || e.type || "default") as string;
      if (t !== "contains") return;
      const s = placed.get(e.src);
      const ti = placed.get(e.dst);
      if (!s || !ti) return;
      placedEdges.push({ src: e.src, dst: e.dst, type: t, s, t: ti });
    });
  }

  drawSvg({
    placed,
    edges: placedEdges,
    linkLayer,
    nodeLayer,
    onSelectNode,
    onEdgeHover: args.onEdgeHover,
    onEdgeClick: args.onEdgeClick,
    idx,
    multiSelectMode: args.multiSelectMode,
    multiSelectIds: args.multiSelectIds,
  });
}

// ---------------- Relations mode (deterministic fan-in / fan-out) ----------------
//
// Layout (mirrors prototype renderFocusGraph dependency mode, lines 1854-1900):
//   - parent chain in a tiny breadcrumb row at the top (context-only)
//   - focus node centred
//   - INCOMING edges' sources fanned vertically on the LEFT  (fan-in)
//   - OUTGOING edges' targets fanned vertically on the RIGHT (fan-out)
//   - each neighbour is captioned with the typed edge that connects it to focus
//   - empty state: an inline pill explaining "no typed relations" / "N hidden"

function renderRelations(args: RenderArgs): void {
  const { focus, idx, width, height, edgeFilter, linkLayer, nodeLayer, onSelectNode } = args;

  const placed = new Map<string, PlacedNode>();
  const focusX = width * 0.5;
  const focusY = height * 0.5;

  // 1. Focus at centre
  placed.set(focus.node_id, {
    node: focus,
    x: focusX,
    y: focusY,
    role: "focus",
    isFocus: true,
    isLeaf: !isContainerNode(idx, focus.node_id),
    status: classifyNode(focus),
    subtreeCount: subtreeCount(idx, focus.node_id),
  });

  // 2. Parent chain breadcrumb at top (context, smaller — drawn as muted nodes)
  const chain = chainOf(focus, idx.byId).slice(0, -1); // drop focus itself
  const ctxY = Math.max(70, focusY - 220);
  const ctxStartX = focusX - (chain.length - 1) * 70;
  chain.forEach((c, i) => {
    placed.set(c.node_id, {
      node: c,
      x: ctxStartX + i * 140,
      y: ctxY,
      role: "context",
      isFocus: false,
      isLeaf: !isContainerNode(idx, c.node_id),
      status: classifyNode(c),
      subtreeCount: subtreeCount(idx, c.node_id),
    });
  });

  // 3. Collect typed neighbours.
  // 'contains' OUT (focus → child) is included in fan-out so containers can
  // navigate into their subtree. 'contains' IN (parent → focus) is suppressed
  // because the parent chain already shows it as breadcrumb at the top.
  type PeerEdge = { peer: NodeRecord; type: string };
  const outRaw = idx.edgesBySrc.get(focus.node_id) ?? [];
  const inRaw = idx.edgesByDst.get(focus.node_id) ?? [];
  const out: PeerEdge[] = [];
  const inE: PeerEdge[] = [];
  const seenOut = new Set<string>();
  const seenIn = new Set<string>();
  let hiddenOutCount = 0;
  let hiddenInCount = 0;

  outRaw.forEach((e) => {
    const t = (e.edge_type || e.type || "default") as string;
    if (!edgeFilter.has(t)) {
      hiddenOutCount++;
      return;
    }
    const peer = idx.byId.get(e.dst);
    if (!peer || peer.node_id === focus.node_id) return;
    const key = `${peer.node_id}|${t}`;
    if (seenOut.has(key)) return;
    seenOut.add(key);
    out.push({ peer, type: t });
  });
  inRaw.forEach((e) => {
    const t = (e.edge_type || e.type || "default") as string;
    // contains-IN is the focus's parent — already in the breadcrumb chain.
    if (t === "contains") return;
    if (!edgeFilter.has(t)) {
      hiddenInCount++;
      return;
    }
    const peer = idx.byId.get(e.src);
    if (!peer || peer.node_id === focus.node_id) return;
    const key = `${peer.node_id}|${t}`;
    if (seenIn.has(key)) return;
    seenIn.add(key);
    inE.push({ peer, type: t });
  });

  // 4. Place fan-in (left) and fan-out (right) columns
  const dx = 280;
  const cap = 14;
  const pitch = 64;

  const inShown = inE.slice(0, cap);
  inShown.forEach((entry, i) => {
    if (placed.has(entry.peer.node_id)) return; // already placed (context chain)
    placed.set(entry.peer.node_id, {
      node: entry.peer,
      x: focusX - dx,
      y: focusY + (i - (inShown.length - 1) / 2) * pitch,
      role: "in",
      isFocus: false,
      isLeaf: !isContainerNode(idx, entry.peer.node_id),
      status: classifyNode(entry.peer),
      subtreeCount: subtreeCount(idx, entry.peer.node_id),
      edgeMeta: entry.type,
    });
  });

  const outShown = out.slice(0, cap);
  outShown.forEach((entry, i) => {
    if (placed.has(entry.peer.node_id)) return;
    placed.set(entry.peer.node_id, {
      node: entry.peer,
      x: focusX + dx,
      y: focusY + (i - (outShown.length - 1) / 2) * pitch,
      role: "out",
      isFocus: false,
      isLeaf: !isContainerNode(idx, entry.peer.node_id),
      status: classifyNode(entry.peer),
      subtreeCount: subtreeCount(idx, entry.peer.node_id),
      edgeMeta: entry.type,
    });
  });

  // 5. Overflow indicators
  if (out.length > cap) {
    placed.set("__moreOut__", {
      node: { node_id: "__moreOut__", layer: "L7", title: `+${out.length - cap} more` },
      x: focusX + dx,
      y: focusY + (cap / 2 + 0.6) * pitch,
      role: "more",
      isFocus: false,
      isLeaf: true,
      status: "structure_only",
      subtreeCount: out.length - cap,
    });
  }
  if (inE.length > cap) {
    placed.set("__moreIn__", {
      node: { node_id: "__moreIn__", layer: "L7", title: `+${inE.length - cap} more` },
      x: focusX - dx,
      y: focusY + (cap / 2 + 0.6) * pitch,
      role: "more",
      isFocus: false,
      isLeaf: true,
      status: "structure_only",
      subtreeCount: inE.length - cap,
    });
  }

  // 6. Empty-state pill (distinguish "no typed edges at all" vs "all hidden by filter")
  if (out.length === 0 && inE.length === 0) {
    const totalAround = (idx.edgesBySrc.get(focus.node_id) ?? []).filter((e) => (e.edge_type || e.type) !== "contains").length
      + (idx.edgesByDst.get(focus.node_id) ?? []).filter((e) => (e.edge_type || e.type) !== "contains").length;
    const msg =
      totalAround === 0
        ? "No typed relations to/from this node"
        : `No edges visible — ${totalAround} hidden by filter (toggle types above)`;
    placed.set("__empty__", {
      node: { node_id: "__empty__", layer: "L7", title: msg },
      x: focusX,
      y: focusY + 140,
      role: "empty",
      isFocus: false,
      isLeaf: true,
      status: "structure_only",
      subtreeCount: 0,
    });
  } else if (hiddenInCount > 0 || hiddenOutCount > 0) {
    // Soft hint near focus when partial hidden
    const msg = `${hiddenInCount + hiddenOutCount} more edge${hiddenInCount + hiddenOutCount === 1 ? "" : "s"} hidden by filter`;
    placed.set("__hidden__", {
      node: { node_id: "__hidden__", layer: "L7", title: msg },
      x: focusX,
      y: focusY + 180,
      role: "empty",
      isFocus: false,
      isLeaf: true,
      status: "structure_only",
      subtreeCount: 0,
    });
  }

  // 7. Edges to draw — only edges of the focus node, plus the parent → focus
  // breadcrumb link if 'contains' is enabled.
  const placedEdges: PlacedEdge[] = [];
  inShown.forEach((entry) => {
    const s = placed.get(entry.peer.node_id);
    const t = placed.get(focus.node_id);
    if (s && t) placedEdges.push({ src: entry.peer.node_id, dst: focus.node_id, type: entry.type, s, t });
  });
  outShown.forEach((entry) => {
    const s = placed.get(focus.node_id);
    const t = placed.get(entry.peer.node_id);
    if (s && t) placedEdges.push({ src: focus.node_id, dst: entry.peer.node_id, type: entry.type, s, t });
  });
  // Optional thin contains link from immediate parent → focus for orientation
  if (edgeFilter.has("contains") && chain.length > 0) {
    const parent = chain[chain.length - 1];
    const ps = placed.get(parent.node_id);
    const fs = placed.get(focus.node_id);
    if (ps && fs) placedEdges.push({ src: parent.node_id, dst: focus.node_id, type: "contains", s: ps, t: fs });
  }

  drawSvg({
    placed,
    edges: placedEdges,
    linkLayer,
    nodeLayer,
    onSelectNode,
    onEdgeHover: args.onEdgeHover,
    onEdgeClick: args.onEdgeClick,
    idx,
    multiSelectMode: args.multiSelectMode,
    multiSelectIds: args.multiSelectIds,
  });
}

// ---------------- Drawing ----------------

interface DrawArgs {
  placed: Map<string, PlacedNode>;
  edges: PlacedEdge[];
  linkLayer: ReturnType<typeof select<SVGGElement, unknown>>;
  nodeLayer: ReturnType<typeof select<SVGGElement, unknown>>;
  onSelectNode(id: string): void;
  onEdgeHover(info: EdgeInfo | null): void;
  onEdgeClick(info: EdgeInfo): void;
  idx: Index;
  multiSelectMode: boolean;
  multiSelectIds: Set<string>;
}

function drawSvg(args: DrawArgs) {
  const { placed, edges, linkLayer, nodeLayer, onSelectNode, idx, onEdgeHover, onEdgeClick } = args;
  const placedArr = Array.from(placed.values());

  const linkSel = linkLayer
    .selectAll<SVGPathElement, PlacedEdge>("path.glink")
    .data(edges, (d) => edgeKey(d))
    .join("path")
    .attr("class", "glink")
    .attr("fill", "none")
    .attr("stroke", (d) => edgeStyle(d.type).color)
    .attr("stroke-width", (d) => (d.type === "contains" ? 1 : 1.4))
    .attr("stroke-opacity", (d) => (d.type === "contains" ? 0.6 : 0.85))
    .attr("stroke-dasharray", (d) => edgeStyle(d.type).dash || null);

  linkSel.attr("d", (d) => bezierPath(d));

  // Transparent fat hit-line on top so edges are clickable / hoverable.
  // Drawn last in the link layer so it sits above the visible stroke.
  const hitSel = linkLayer
    .selectAll<SVGPathElement, PlacedEdge>("path.ghit")
    .data(edges, (d) => edgeKey(d))
    .join("path")
    .attr("class", "ghit")
    .attr("fill", "none")
    .attr("stroke", "transparent")
    .attr("stroke-width", 14)
    .attr("pointer-events", "stroke")
    .style("cursor", "pointer");

  hitSel.attr("d", (d) => bezierPath(d));

  function buildEdgeInfo(d: PlacedEdge, event: MouseEvent): EdgeInfo {
    const srcN = idx.byId.get(d.src);
    const dstN = idx.byId.get(d.dst);
    return {
      key: edgeKey(d),
      type: d.type,
      srcId: d.src,
      dstId: d.dst,
      srcTitle: srcN?.title || d.src,
      dstTitle: dstN?.title || d.dst,
      srcLayer: srcN?.layer ?? "",
      dstLayer: dstN?.layer ?? "",
      x: event.clientX,
      y: event.clientY,
    };
  }

  hitSel
    .on("mouseenter", (event: MouseEvent, d) => {
      onEdgeHover(buildEdgeInfo(d, event));
    })
    .on("mousemove", (event: MouseEvent, d) => {
      onEdgeHover(buildEdgeInfo(d, event));
    })
    .on("mouseleave", () => {
      onEdgeHover(null);
    })
    .on("click", (event: MouseEvent, d) => {
      event.stopPropagation();
      onEdgeClick(buildEdgeInfo(d, event));
    });

  const multiOn = args.multiSelectMode;
  const multiSet = args.multiSelectIds;

  // Multi-select edge highlight layer. Keyed by edge id (in worker
  // `<src>-><dst>:<type>` form so it matches the App.tsx bucket). Joined on
  // a filtered subset so unselected edges leave zero DOM nodes here.
  // vector-effect=non-scaling-stroke keeps the overlay at 4px regardless of
  // the parent zoom transform — otherwise it visually disappears once the
  // graph is scaled down ~0.35x.
  const selectedEdges = multiOn
    ? edges.filter((d) => multiSet.has(`edge:${d.src}->${d.dst}:${d.type}`))
    : [];
  linkLayer
    .selectAll<SVGPathElement, PlacedEdge>("path.gselect-edge")
    .data(selectedEdges, (d) => edgeKey(d))
    .join("path")
    .attr("class", "gselect-edge")
    .attr("fill", "none")
    .attr("stroke", "#4f46e5")
    .attr("stroke-width", 4)
    .attr("stroke-opacity", 0.7)
    .attr("stroke-linecap", "round")
    .attr("vector-effect", "non-scaling-stroke")
    .attr("pointer-events", "none")
    .attr("d", (d) => bezierPath(d));

  // Midpoint ✓ badge for each selected edge — uses getPointAtLength so the
  // marker tracks the actual rendered bezier rather than a naive lerp. We
  // also fight the parent zoom by sizing the badge in graph-space and
  // counter-scaling so it stays roughly readable.
  const badgeSel = linkLayer
    .selectAll<SVGGElement, PlacedEdge>("g.gselect-edge-badge")
    .data(selectedEdges, (d) => edgeKey(d))
    .join((enter) => enter.append("g").attr("class", "gselect-edge-badge").attr("pointer-events", "none"));
  badgeSel.selectAll("*").remove();
  badgeSel.each(function (d) {
    const g = select<SVGGElement, PlacedEdge>(this);
    // Resolve the bezier midpoint via the matching glink path. Falls back to
    // the straight-line midpoint if the path isn't in the DOM yet (first paint).
    let mx = (d.s.x + d.t.x) / 2;
    let my = (d.s.y + d.t.y) / 2;
    const pathNode = linkLayer
      .selectAll<SVGPathElement, PlacedEdge>("path.glink")
      .filter((pd) => pd === d)
      .node();
    if (pathNode) {
      const total = pathNode.getTotalLength();
      const pt = pathNode.getPointAtLength(total / 2);
      mx = pt.x;
      my = pt.y;
    }
    g.attr("transform", `translate(${mx},${my})`);
    g.append("circle")
      .attr("r", 14)
      .attr("fill", "#4f46e5")
      .attr("stroke", "#fff")
      .attr("stroke-width", 2.5)
      .attr("vector-effect", "non-scaling-stroke");
    g.append("text")
      .attr("text-anchor", "middle")
      .attr("y", 5)
      .attr("font-size", 16)
      .attr("font-weight", 700)
      .attr("fill", "#fff")
      .text("✓");
  });

  const nodeSel = nodeLayer
    .selectAll<SVGGElement, PlacedNode>("g.gnode")
    .data(placedArr, (d) => d.node.node_id)
    .join((enter) => {
      const g = enter.append("g").attr("class", "gnode").style("cursor", "pointer");
      return g;
    });

  // Wipe + redraw shapes (deterministic, fewer than 100 nodes typically in hierarchy mode).
  nodeSel.selectAll("*").remove();

  nodeSel.each(function (d) {
    const g = select<SVGGElement, PlacedNode>(this);
    if (d.role === "more" || d.role === "empty") {
      drawPillNode(g, d);
      return;
    }
    if (d.role === "context") {
      drawContextNode(g, d);
      g.append("title").text(`${d.node.node_id} ${d.node.title}`);
      return;
    }
    if (d.isLeaf) {
      drawLeaf(g, d);
    } else {
      drawContainer(g, d);
    }
    if (d.edgeMeta) {
      g.append("text")
        .attr("text-anchor", "middle")
        .attr("y", 36)
        .attr("font-size", 9)
        .attr("font-family", "JetBrains Mono, ui-monospace, monospace")
        .attr("fill", "var(--ink-500)")
        .text(d.edgeMeta);
    }
    // Multi-select highlight: ✓ badge top-right when in bucket. Painted
    // last so it sits on top of the layer pill / health ring. Sizes are in
    // graph-space; non-scaling-stroke keeps the white outline crisp at any
    // zoom level (otherwise the 2.5px stroke vanishes under scale 0.35x).
    if (multiOn && multiSet.has(`node:${d.node.node_id}`)) {
      g.append("circle")
        .attr("class", "gselect-badge")
        .attr("cx", 22)
        .attr("cy", -22)
        .attr("r", 14)
        .attr("fill", "#4f46e5")
        .attr("stroke", "#fff")
        .attr("stroke-width", 2.5)
        .attr("vector-effect", "non-scaling-stroke");
      g.append("text")
        .attr("class", "gselect-tick")
        .attr("x", 22)
        .attr("y", -17)
        .attr("text-anchor", "middle")
        .attr("font-size", 16)
        .attr("font-weight", 700)
        .attr("fill", "#fff")
        .text("✓");
    }
    g.append("title").text(`${d.node.node_id} ${d.node.title}${d.edgeMeta ? ` · ${d.edgeMeta}` : ""}`);
  });

  nodeSel.on("click", (event: MouseEvent, d) => {
    event.stopPropagation();
    if (d.role === "more") return;
    onSelectNode(d.node.node_id);
  });

  drawSvgPositions(linkLayer, nodeLayer);
}

function drawSvgPositions(
  linkLayer: ReturnType<typeof select<SVGGElement, unknown>>,
  nodeLayer: ReturnType<typeof select<SVGGElement, unknown>>,
) {
  linkLayer.selectAll<SVGPathElement, PlacedEdge>("path.glink").attr("d", (d) => bezierPath(d));
  linkLayer.selectAll<SVGPathElement, PlacedEdge>("path.ghit").attr("d", (d) => bezierPath(d));
  nodeLayer
    .selectAll<SVGGElement, PlacedNode>("g.gnode")
    .attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);
}

function fitView(
  svg: Selection<SVGSVGElement, unknown, null, undefined>,
  root: Selection<SVGGElement, unknown, null, undefined>,
  zoomBehavior: ZoomBehavior<SVGSVGElement, unknown>,
  width: number,
  height: number,
) {
  const node = root.node();
  if (!node) return;
  const bbox = node.getBBox();
  if (bbox.width === 0 || bbox.height === 0) return;
  const padding = 60;
  const scale = Math.min(
    (width - padding) / bbox.width,
    (height - padding) / bbox.height,
    1.4,
  );
  const tx = width / 2 - (bbox.x + bbox.width / 2) * scale;
  const ty = height / 2 - (bbox.y + bbox.height / 2) * scale;
  svg.call(zoomBehavior.transform, zoomIdentity.translate(tx, ty).scale(scale));
}

function bezierPath(d: PlacedEdge): string {
  const sx = d.s.x;
  const sy = d.s.y;
  const tx = d.t.x;
  const ty = d.t.y;
  const dx = tx - sx;
  if (Math.abs(dx) < 6) {
    // Same-column siblings — gentle right hook
    const cx = sx + 30;
    return `M${sx},${sy}C${cx},${sy} ${cx},${ty} ${tx},${ty}`;
  }
  const c1x = sx + dx * 0.45;
  const c2x = sx + dx * 0.55;
  return `M${sx},${sy}C${c1x},${sy} ${c2x},${ty} ${tx},${ty}`;
}

type GroupSel = ReturnType<typeof select<SVGGElement, PlacedNode>>;

function drawPillNode(g: GroupSel, d: PlacedNode) {
  const text = d.node.title || d.node.node_id;
  const w = Math.max(120, Math.min(360, text.length * 6.5 + 24));
  g.append("rect")
    .attr("x", -w / 2)
    .attr("y", -15)
    .attr("width", w)
    .attr("height", 30)
    .attr("rx", 15)
    .attr("ry", 15)
    .attr("fill", d.role === "empty" ? "#fef9c3" : "#f1f5f9")
    .attr("stroke", d.role === "empty" ? "#fcd34d" : "#cbd5e1");
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("dy", "0.35em")
    .attr("font-size", 10.5)
    .attr("fill", d.role === "empty" ? "#854d0e" : "var(--ink-600)")
    .text(text);
}

function drawContextNode(g: GroupSel, d: PlacedNode) {
  const text = (d.node.title || d.node.node_id).split(".").slice(-2).join(".");
  const w = Math.max(96, Math.min(220, text.length * 6.5 + 36));
  g.append("rect")
    .attr("x", -w / 2)
    .attr("y", -13)
    .attr("width", w)
    .attr("height", 26)
    .attr("rx", 13)
    .attr("ry", 13)
    .attr("fill", "#fff")
    .attr("stroke", "var(--ink-200)");
  g.append("rect")
    .attr("x", -w / 2 + 5)
    .attr("y", -8)
    .attr("width", 18)
    .attr("height", 11)
    .attr("rx", 3)
    .attr("fill", layerBg(d.node.layer));
  g.append("text")
    .attr("x", -w / 2 + 14)
    .attr("y", 1)
    .attr("text-anchor", "middle")
    .attr("font-size", 8.5)
    .attr("font-weight", 700)
    .attr("fill", layerFg(d.node.layer))
    .text(d.node.layer);
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("dy", "0.35em")
    .attr("x", 6)
    .attr("font-size", 10.5)
    .attr("fill", "var(--ink-700)")
    .text(text);
}

function drawContainer(g: GroupSel, d: PlacedNode) {
  const isFocus = d.isFocus;
  const w = isFocus ? 132 : 108;
  const h = isFocus ? 54 : 46;
  const fill = isFocus ? "#eef2ff" : "#ffffff";
  // Container outline color: feature-health rollup if known, falls back to
  // semantic status when no L7 descendants exist.
  const healthValue = d.node._health ?? null;
  const healthColor = healthValue != null ? healthHex(healthValue) : statusStroke(d.status);
  const stroke = isFocus ? "#4f46e5" : healthColor;
  const strokeWidth = isFocus ? 2.5 : 1.5;
  g.append("rect")
    .attr("x", -w / 2)
    .attr("y", -h / 2)
    .attr("width", w)
    .attr("height", h)
    .attr("rx", 8)
    .attr("ry", 8)
    .attr("fill", fill)
    .attr("stroke", stroke)
    .attr("stroke-width", strokeWidth);
  // Title above (split last 2 segments)
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("y", -h / 2 - 22)
    .attr("font-size", 11.5)
    .attr("font-weight", 600)
    .attr("fill", "var(--ink-900)")
    .text(shortTitle(d.node.title || d.node.node_id));
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("y", -h / 2 - 8)
    .attr("font-size", 9.5)
    .attr("fill", "var(--ink-500)")
    .attr("font-family", "JetBrains Mono, ui-monospace, monospace")
    .text(d.node.node_id);
  // Layer pill top-left
  g.append("rect")
    .attr("x", -w / 2 + 6)
    .attr("y", -h / 2 + 5)
    .attr("width", 22)
    .attr("height", 12)
    .attr("rx", 3)
    .attr("fill", layerBg(d.node.layer));
  g.append("text")
    .attr("x", -w / 2 + 17)
    .attr("y", -h / 2 + 14)
    .attr("text-anchor", "middle")
    .attr("font-size", 9)
    .attr("font-weight", 700)
    .attr("fill", layerFg(d.node.layer))
    .text(d.node.layer);
  // Health dot top-right — colored by feature-health rollup so the container
  // matches the tree's color at a glance.
  g.append("circle")
    .attr("cx", w / 2 - 8)
    .attr("cy", -h / 2 + 9)
    .attr("r", 3.5)
    .attr("fill", healthValue != null ? healthColor : statusDotFill(d.status));
  // Big number = subtree node count
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("dy", "0.35em")
    .attr("font-size", isFocus ? 18 : 16)
    .attr("font-weight", 700)
    .attr("fill", "var(--ink-900)")
    .text(d.subtreeCount);
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("y", isFocus ? 14 : 11)
    .attr("font-size", 8)
    .attr("font-weight", 600)
    .attr("letter-spacing", "0.1em")
    .attr("fill", "var(--ink-500)")
    .text("NODES");
  // Container rollup health is conveyed by the box outline color + the small
  // health dot top-right — no numeric score, which was hard to read at this
  // size and competed visually with the bigger "NODES" count at the center.
  // (Per-L7-leaf scores remain visible in drawLeaf.)
}

function drawLeaf(g: GroupSel, d: PlacedNode) {
  const isFocus = d.isFocus;
  const r = isFocus ? 26 : 20;
  // Health ring color comes from lib/health.ts feature-health score, matching
  // the tree + drawer + FocusCard. Falls back to semantic-status color when
  // the node has no _health (L4 assets, package markers, empty placeholders).
  const healthValue = d.node._health ?? null;
  const assetBinding = d.node._asset_binding ?? null;
  const ringColor = healthValue != null ? healthHex(healthValue) : statusDotFill(d.status);
  // Health ring
  g.append("circle")
    .attr("r", r + 4)
    .attr("fill", "none")
    .attr("stroke", ringColor)
    .attr("stroke-width", 3)
    .attr("opacity", 0.85);
  // Body
  g.append("circle")
    .attr("r", r)
    .attr("fill", isFocus ? "#eef2ff" : "#ffffff")
    .attr("stroke", isFocus ? "#4f46e5" : "var(--ink-200)")
    .attr("stroke-width", isFocus ? 2.5 : 1.5);
  // Center label: prefer the actual health score number (red/amber/green by
  // ring color). The layer letter (L7 / L4) was visually meaningless — the
  // shape + position + ring already convey layer. Fall back to the layer
  // letter only when no scoreable signal is available (package markers).
  const centerText =
    healthValue != null
      ? String(healthValue)
      : assetBinding != null
        ? String(assetBinding)
        : d.node.layer;
  const centerColor =
    healthValue != null || assetBinding != null ? ringColor : layerFg(d.node.layer);
  const centerFontSize =
    healthValue != null || assetBinding != null
      ? isFocus
        ? 14
        : 12
      : isFocus
        ? 13
        : 11;
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("dy", "0.35em")
    .attr("font-size", centerFontSize)
    .attr("font-weight", 700)
    .attr("fill", centerColor)
    .text(centerText);
  // Title
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("y", -r - 14)
    .attr("font-size", 11)
    .attr("font-weight", 600)
    .attr("fill", "var(--ink-900)")
    .text(shortTitle(d.node.title || d.node.node_id));
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("y", -r - 2)
    .attr("font-size", 9)
    .attr("fill", "var(--ink-500)")
    .attr("font-family", "JetBrains Mono, ui-monospace, monospace")
    .text(d.node.node_id);
}

function shortTitle(t: string): string {
  // Last 2 dot-segments (matches prototype)
  const parts = t.split(".");
  return parts.slice(-2).join(".");
}


function statusStroke(s: SemanticStatus): string {
  switch (s) {
    case "semantic_complete":
    case "reviewed":
      return "#10b981";
    case "semantic_stale":
      return "#f59e0b";
    case "semantic_hash_unverified":
      return "#a855f7";
    case "semantic_failed":
      return "#ef4444";
    default:
      return "#cbd5e1";
  }
}

function statusDotFill(s: SemanticStatus): string {
  switch (s) {
    case "semantic_complete":
    case "reviewed":
      return "#10b981";
    case "semantic_stale":
    case "semantic_pending":
      return "#f59e0b";
    case "semantic_hash_unverified":
    case "review_pending":
      return "#a855f7";
    case "semantic_running":
      return "#0ea5e9";
    case "semantic_failed":
      return "#ef4444";
    default:
      return "#cbd5e1";
  }
}

function layerBg(layer: Layer | string): string {
  switch (layer) {
    case "L1":
      return "#f1f5f9";
    case "L2":
      return "#e0e7ff";
    case "L3":
      return "#dbeafe";
    case "L4":
      return "#cffafe";
    case "L7":
      return "#fef3c7";
    default:
      return "#f1f5f9";
  }
}

function layerFg(layer: Layer | string): string {
  switch (layer) {
    case "L1":
      return "#0f172a";
    case "L2":
      return "#3730a3";
    case "L3":
      return "#1e40af";
    case "L4":
      return "#155e75";
    case "L7":
      return "#92400e";
    default:
      return "#0f172a";
  }
}

