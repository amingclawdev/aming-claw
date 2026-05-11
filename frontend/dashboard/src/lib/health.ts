import type { EdgeRecord, NodeRecord } from "../types";

// Per-node "feature health" — port of the docs/dev/dashboard-prototype.html
// algorithm.
//
//   - L4 leaves (state / contract / artifact / config): NOT SCORED. They have
//     no concept of feature health (and no asset_binding either — we used to
//     track that as a separate score but it's been retired per operator
//     feedback: "L4 are config/asset files, don't score them"). They do not
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

export interface HealthScored {
  node_id: string;
  _health: number | null;
}

// Compute health for every node and return a map of node_id → {_health}.
// _health is null for L4 leaves and for empty containers (no L7 descendants).
export function computeNodeHealth(
  nodes: NodeRecord[],
  _edges: EdgeRecord[],
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

  const memo = new Map<string, HealthScored>();
  function visit(id: string): HealthScored {
    const cached = memo.get(id);
    if (cached) return cached;
    const n = byId.get(id);
    if (!n) {
      const result: HealthScored = { node_id: id, _health: null };
      memo.set(id, result);
      return result;
    }
    const kids = byParent.get(id) ?? [];
    let h: number | null;
    if (kids.length === 0) {
      // Leaf. L4 nodes are unscored on purpose; everything else gets the
      // leafScore breakdown.
      h = n.layer === "L4" ? null : leafScore(n);
    } else {
      // Container — recursive average of L7 descendants. L4 children are
      // skipped entirely (no contribution, no separate rollup).
      let sum = 0;
      let count = 0;
      kids.forEach((c) => {
        const grandChildren = byParent.get(c.node_id) ?? [];
        if (grandChildren.length === 0) {
          if (c.layer === "L4") return; // skip L4 leaves
          sum += leafScore(c);
          count++;
        } else {
          const childResult = visit(c.node_id);
          if (childResult._health != null) {
            sum += childResult._health;
            count++;
          }
        }
      });
      h = count > 0 ? Math.round(sum / count) : null;
    }
    const result: HealthScored = { node_id: id, _health: h };
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
