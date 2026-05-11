import type { HealthResponse, StatusResponse } from "../types";

export type ReconcilePhase =
  | "idle"
  | "queueing"
  | "materializing"
  | "rebuilding"
  | "done"
  | "error";

interface Props {
  health?: HealthResponse;
  status?: StatusResponse;
  busy: boolean;
  phase?: ReconcilePhase;
  phaseDetail?: string;
  onQueueReconcile(): void;
}

// active snapshot is commit-anchored. If the running governance service is on
// a newer commit than the active snapshot, the graph is behind runtime — show
// a banner and offer manual reconcile. Working-tree dirtiness is NOT inferred
// here; without an explicit `dirty` flag from the API we don't pretend the
// scope is stale.
//
// MF-016 banner P3: banner has two display modes —
// 1. **stale**: HEAD ≠ snapshot. Default state; shows the queue button.
// 2. **in-progress / just done**: while handleQueueReconcile runs, replace the
//    button with phase chips + progress bar so the operator can read off what
//    is happening (queue → materialize → projection rebuild → done).
//    Banner auto-hides once the stale condition clears (governed by the
//    snapshot/service commit comparison below).
export default function StaleGraphBanner({
  health,
  status,
  busy,
  phase = "idle",
  phaseDetail = "",
  onQueueReconcile,
}: Props) {
  if (!health || !status) return null;
  const serviceVersion = (health.version || "").trim();
  const snapshotCommit = (status.graph_snapshot_commit || "").trim();
  if (!serviceVersion || !snapshotCommit) return null;

  const stale = !commitsMatch(serviceVersion, snapshotCommit);
  const inProgress = phase !== "idle";
  // Keep banner visible briefly after success so the operator can see "done"
  // before it disappears on the next refresh tick.
  if (!stale && !inProgress) return null;

  return (
    <div
      className={`banner-stale-graph${inProgress ? " banner-stale-graph-busy" : ""}`}
      role="alert"
    >
      <span className="banner-icon" aria-hidden="true">
        {phase === "done" ? "✓" : phase === "error" ? "×" : "!"}
      </span>
      <div className="banner-body">
        {inProgress ? (
          <ReconcileProgress phase={phase} detail={phaseDetail} />
        ) : (
          <>
            <span className="banner-title">graph snapshot behind runtime</span>{" "}
            — service is on <span className="mono">{serviceVersion.slice(0, 7)}</span>,
            active snapshot was built from{" "}
            <span className="mono">{snapshotCommit.slice(0, 7)}</span>. Tree counts
            and semantic scores reflect the snapshot, not the running code.
          </>
        )}
      </div>
      {!inProgress ? (
        <>
          <button
            className="banner-secondary"
            onClick={() => navigator.clipboard?.writeText?.(snapshotCommit)}
            title="Copy snapshot commit"
          >
            Copy commit
          </button>
          <button
            onClick={onQueueReconcile}
            disabled={busy}
            title="Queue + materialize + rebuild projection — runs inline"
          >
            {busy ? "Reconcile…" : "Queue scope reconcile"}
          </button>
        </>
      ) : null}
    </div>
  );
}

const PHASE_STEPS: { id: ReconcilePhase; label: string }[] = [
  { id: "queueing", label: "Queueing" },
  { id: "materializing", label: "Build snapshot" },
  { id: "rebuilding", label: "Rebuild projection" },
  { id: "done", label: "Done" },
];

function ReconcileProgress({
  phase,
  detail,
}: {
  phase: ReconcilePhase;
  detail: string;
}) {
  const isError = phase === "error";
  const currentIdx = isError
    ? -1
    : PHASE_STEPS.findIndex((s) => s.id === phase);
  const progress = isError
    ? 100
    : currentIdx >= 0
    ? ((currentIdx + 1) / PHASE_STEPS.length) * 100
    : 0;
  const phaseLabel = isError
    ? "Failed"
    : phase === "done"
    ? "Done"
    : PHASE_STEPS[currentIdx]?.label ?? "Running";

  return (
    <div className="banner-reconcile">
      <div className="banner-reconcile-head">
        <span className="banner-reconcile-status">
          {phase === "done" ? "Reconcile complete" : isError ? "Reconcile failed" : "Reconcile in progress…"}
        </span>
        <span className="banner-reconcile-phase mono">
          {phaseLabel}
        </span>
        {detail ? (
          <span className="banner-reconcile-detail">{detail}</span>
        ) : null}
      </div>
      <div
        className={`banner-reconcile-bar${isError ? " err" : ""}${phase === "done" ? " done" : ""}`}
        role="progressbar"
        aria-valuenow={Math.round(progress)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="banner-reconcile-bar-fill" style={{ width: `${progress}%` }} />
      </div>
      <div className="banner-reconcile-steps">
        {PHASE_STEPS.map((s, i) => {
          const reached = !isError && currentIdx >= i;
          const active = !isError && currentIdx === i && phase !== "done";
          return (
            <span
              key={s.id}
              className={`banner-reconcile-step${reached ? " reached" : ""}${active ? " active" : ""}`}
            >
              <span className="banner-reconcile-step-dot" aria-hidden />
              <span className="banner-reconcile-step-label">{s.label}</span>
            </span>
          );
        })}
      </div>
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
