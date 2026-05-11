import type { HealthResponse, StatusResponse } from "../types";

interface Props {
  health?: HealthResponse;
  status?: StatusResponse;
  busy: boolean;
  onQueueReconcile(): void;
}

// active snapshot is commit-anchored. If the running governance service is on
// a newer commit than the active snapshot, the graph is behind runtime — show
// a banner and offer manual reconcile. Working-tree dirtiness is NOT inferred
// here; without an explicit `dirty` flag from the API we don't pretend the
// scope is stale.
export default function StaleGraphBanner({ health, status, busy, onQueueReconcile }: Props) {
  if (!health || !status) return null;
  const serviceVersion = (health.version || "").trim();
  const snapshotCommit = (status.graph_snapshot_commit || "").trim();
  if (!serviceVersion || !snapshotCommit) return null;
  if (commitsMatch(serviceVersion, snapshotCommit)) return null;

  return (
    <div className="banner-stale-graph" role="alert">
      <span className="banner-icon" aria-hidden="true">!</span>
      <div className="banner-body">
        <span className="banner-title">graph snapshot behind runtime</span>{" "}
        — service is on{" "}
        <span className="mono">{serviceVersion.slice(0, 7)}</span>, active snapshot was built from{" "}
        <span className="mono">{snapshotCommit.slice(0, 7)}</span>. Tree counts and semantic scores reflect the
        snapshot, not the running code.
      </div>
      <button
        className="banner-secondary"
        onClick={() => navigator.clipboard?.writeText?.(snapshotCommit)}
        title="Copy snapshot commit"
      >
        Copy commit
      </button>
      <button onClick={onQueueReconcile} disabled={busy} title="POST /reconcile/scope (dry-run)">
        {busy ? "Queueing…" : "Queue scope reconcile"}
      </button>
    </div>
  );
}

function commitsMatch(a: string, b: string): boolean {
  // Service version is typically a short SHA (e.g. "ca47b2e"); snapshot
  // commit is full-length. Prefix-match defensively in either direction.
  const x = a.toLowerCase();
  const y = b.toLowerCase();
  if (x === y) return true;
  if (x.startsWith(y) || y.startsWith(x)) return true;
  if (x.length >= 7 && y.startsWith(x)) return true;
  if (y.length >= 7 && x.startsWith(y)) return true;
  return false;
}
