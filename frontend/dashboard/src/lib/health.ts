import type { EdgeRecord, NodeRecord } from "../types";

// Per-node "feature health" — port of the docs/dev/dashboard-prototype.html
// algorithm.
//
//   - L4 leaves (state / contract / artifact / config): EXCLUDED from feature
//     health. They get a separate `_asset_binding` score and do NOT
//     participate in their container's feature-health rollup.
//   - L7 leaf: definition score (35 source + 30 tests + 20 fns + 10 docs + 5
//     has parent), capped at 100.
//   - Container: recursive average of L7 descendants only (L4 children skipped).

export function leafScore(n: NodeRecord): number {
  let s = 0;
  if (n.primary_files?.length) s += 35;
  if (n.test_files?.length) s += 30;
  if (n.metadata?.functions?.length) s += 20;
  if (n.secondary_files?.length) s += 10;
  if (n.metadata?.hierarchy_parent !== undefined) s += 5;
  return Math.min(100, s);
}

// L4 asset binding score (0..100). Separate from feature health.
//   - +50 if any non-contains edge consumes this node (incoming)
//   - +20 if any non-contains edge produced by this node (outgoing)
//   - +20 if has hierarchy_parent (owner container)
//   - +10 if has any backing file (config_files / primary_files)
export function assetBindingScore(
  n: NodeRecord,
  edgesBySrc: Map<string, EdgeRecord[]>,
  edgesByDst: Map<string, EdgeRecord[]>,
): number {
  const isContains = (e: EdgeRecord) => (e.type ?? e.edge_type) === "contains";
  const outs = (edgesBySrc.get(n.node_id) ?? []).filter((e) => !isContains(e));
  const ins = (edgesByDst.get(n.node_id) ?? []).filter((e) => !isContains(e));
  let s = 0;
  if (ins.length > 0) s += 50;
  if (outs.length > 0) s += 20;
  if (n.metadata?.hierarchy_parent) s += 20;
  if (
    (n.config_files?.length ?? 0) + (n.primary_files?.length ?? 0) > 0
  ) {
    s += 10;
  }
  return Math.min(100, s);
}

export interface HealthScored {
  node_id: string;
  _health: number | null;
  _asset_binding: number | null;
}

// Compute health for every node and return a map of node_id → {_health, _asset_binding}.
// _health is null for L4 leaves and for empty containers (no L7 descendants).
export function computeNodeHealth(
  nodes: NodeRecord[],
  edges: EdgeRecord[],
): Map<string, HealthScored> {
  const byId = new Map(nodes.map((n) => [n.node_id, n]));
  const byParent = new Map<string, NodeRecord[]>();
  nodes.forEach((n) => {
    const p = n.metadata?.hierarchy_parent;
    if (!p) return;
    const arr = byParent.get(p) ?? [];
    arr.push(n);
    byParent.set(p, arr);
  });
  const edgesBySrc = new Map<string, EdgeRecord[]>();
  const edgesByDst = new Map<string, EdgeRecord[]>();
  edges.forEach((e) => {
    const a = edgesBySrc.get(e.src) ?? [];
    a.push(e);
    edgesBySrc.set(e.src, a);
    const b = edgesByDst.get(e.dst) ?? [];
    b.push(e);
    edgesByDst.set(e.dst, b);
  });

  const memo = new Map<string, HealthScored>();
  function visit(id: string): HealthScored {
    const cached = memo.get(id);
    if (cached) return cached;
    const n = byId.get(id);
    if (!n) {
      const result: HealthScored = { node_id: id, _health: null, _asset_binding: null };
      memo.set(id, result);
      return result;
    }
    const kids = byParent.get(id) ?? [];
    let h: number | null;
    let assetBinding: number | null = null;
    if (kids.length === 0) {
      if (n.layer === "L4") {
        assetBinding = assetBindingScore(n, edgesBySrc, edgesByDst);
        h = null;
      } else {
        h = leafScore(n);
      }
    } else {
      let sum = 0;
      let count = 0;
      let assetSum = 0;
      let assetCount = 0;
      kids.forEach((c) => {
        const grandChildren = byParent.get(c.node_id) ?? [];
        if (grandChildren.length === 0) {
          if (c.layer === "L4") {
            assetSum += assetBindingScore(c, edgesBySrc, edgesByDst);
            assetCount++;
          } else {
            sum += leafScore(c);
            count++;
          }
        } else {
          const childResult = visit(c.node_id);
          if (childResult._health != null) {
            sum += childResult._health;
            count++;
          }
          if (childResult._asset_binding != null) {
            assetSum += childResult._asset_binding;
            assetCount++;
          }
        }
      });
      h = count > 0 ? Math.round(sum / count) : null;
      assetBinding = assetCount > 0 ? Math.round(assetSum / assetCount) : null;
    }
    const result: HealthScored = {
      node_id: id,
      _health: h,
      _asset_binding: assetBinding,
    };
    memo.set(id, result);
    return result;
  }
  nodes.forEach((n) => visit(n.node_id));
  return memo;
}

// Stoplight color for a feature-health score.
export function healthHex(h: number | null | undefined): string {
  if (h == null) return "#cbd5e1"; // neutral gray
  if (h >= 85) return "#10b981"; // green
  if (h >= 70) return "#f59e0b"; // amber
  return "#ef4444"; // red
}

// Coarse tone for CSS classnames (matches existing tone-green/amber/red usage
// elsewhere in the dashboard).
export function healthTone(h: number | null | undefined): "green" | "amber" | "red" | "neutral" {
  if (h == null) return "neutral";
  if (h >= 85) return "green";
  if (h >= 70) return "amber";
  return "red";
}
