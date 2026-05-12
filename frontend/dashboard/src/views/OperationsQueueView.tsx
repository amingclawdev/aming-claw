import type { OperationRow, OperationsQueueResponse } from "../types";

interface Props {
  ops: OperationsQueueResponse;
  onCancelOperation?: (opType: string, opId: string, targetId: string) => void;
  onCancelAllByType?: (opType: "node_semantic" | "edge_semantic") => void;
  onClearTerminal?: () => void;
}

// MF-2026-05-10-011: running rows cannot be cancelled (backend returns 409).
const RUNNING_STATUSES = new Set(["running", "ai_running", "claimed", "ai_reviewing"]);
// Terminal statuses that can be drained via /semantic/jobs/clear-terminal.
const TERMINAL_STATUSES = new Set([
  "cancelled",
  "complete",
  "ai_complete",
  "failed",
  "ai_failed",
  "rejected",
  "rule_complete",
]);

// Operation types that have their own dedicated tab/surface and should NOT
// crowd the Operations Queue. feedback_review is shown in the Review Queue
// tab — surfacing it here too creates a duplicate "queued" entry every time
// the dashboard reloads.
const HIDDEN_OP_TYPES = new Set<string>(["feedback_review"]);

export default function OperationsQueueView({
  ops,
  onCancelOperation,
  onCancelAllByType,
  onClearTerminal,
}: Props) {
  const allRows = ops.operations ?? [];
  const rows = allRows.filter((r) => !HIDDEN_OP_TYPES.has(String(r.operation_type || "")));
  const hiddenCount = allRows.length - rows.length;
  const summary = ops.summary;
  // Drop hidden operation types from the by_type card grid as well so the
  // top-of-page summary matches the rows shown below.
  const byType = Object.fromEntries(
    Object.entries(summary?.by_type ?? {}).filter(([k]) => !HIDDEN_OP_TYPES.has(k)),
  );

  const queuedCount = countByBucket(rows, "queued");
  const runningCount = countByBucket(rows, "running");
  const runningRows = rows.filter((r) => statusBucket(r.status) === "running");
  const queuedRows = rows
    .filter((r) => statusBucket(r.status) === "queued")
    .slice()
    .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
  const suggestionRows = rows.filter((r) => r.status === "not_queued");

  return (
    <div className="view">
      <div className="view-head">
        <h2 className="view-title">Operations Queue</h2>
        <span className="view-subtitle">
          source <span className="mono">/operations/queue</span> ·{" "}
          <span className="mono">{ops.snapshot_id}</span> · {rows.length} row{rows.length === 1 ? "" : "s"}
          {hiddenCount > 0 ? (
            <span style={{ color: "var(--ink-400)" }}>
              {" "}
              · {hiddenCount} feedback_review row{hiddenCount === 1 ? "" : "s"} shown in Review Queue
            </span>
          ) : null}
        </span>
      </div>

      {/* Compact KPI strip — four cells for the four numbers the operator
          actually scans for. Bulk-cancel actions live next to the Queued
          section header (not in the strip), per operator feedback. */}
      <div className="ops-kpi-strip">
        {Object.entries(byType).map(([k, v]) => (
          <div className="ops-kpi" key={`type-${k}`}>
            <div className="ops-kpi-label">{labelOpType(k)}</div>
            <div className="ops-kpi-value">{v}</div>
            <div className="ops-kpi-sub">operation_type</div>
          </div>
        ))}
        <div className={`ops-kpi${queuedCount > 0 ? " ops-kpi-amber" : ""}`}>
          <div className="ops-kpi-label">Queued</div>
          <div className="ops-kpi-value">{queuedCount}</div>
          <div className="ops-kpi-sub">waiting to run</div>
        </div>
        <div className={`ops-kpi${runningCount > 0 ? " ops-kpi-blue" : ""}`}>
          <div className="ops-kpi-label">Running</div>
          <div className="ops-kpi-value">{runningCount}</div>
          <div className="ops-kpi-sub">parallel workers</div>
        </div>
      </div>

      {/* Running + Queued always render so the operator can see "0 in flight"
          and "0 queued" at a glance — confirms the worker is idle instead of
          stuck. Empty banner is tiny (one line). */}
      <QueueSection
        title="Running"
        hint="in flight across semantic worker lanes — cancel disabled, will complete or fail on its own"
        rows={runningRows}
        emptyMsg="No tasks running."
        onCancelOperation={onCancelOperation}
      />

      <QueueSection
        title="Queued"
        hint="waiting for an available semantic worker lane"
        rows={queuedRows}
        emptyMsg="No tasks queued."
        onCancelOperation={onCancelOperation}
        headerExtra={
          onCancelAllByType ? (
            <div style={{ display: "flex", gap: 6 }}>
              {(["edge_semantic", "node_semantic"] as const)
                .filter((t) => hasQueuedOrRunning(rows, t))
                .map((t) => (
                  <button
                    key={`cancel-${t}`}
                    className="action-btn action-btn-danger"
                    title={`POST /semantic/jobs/cancel-all (operation_type=${t}, status=queued)`}
                    onClick={() => onCancelAllByType(t)}
                  >
                    cancel queued {t === "edge_semantic" ? "edges" : "nodes"}
                  </button>
                ))}
            </div>
          ) : null
        }
      />

      <QueueSection
        title="Suggestions"
        hint="not yet queued — actionable hints from the snapshot"
        rows={suggestionRows}
        emptyMsg="No suggestions."
        onCancelOperation={onCancelOperation}
        headerExtra={
          onClearTerminal && hasTerminal(rows) ? (
            <button
              className="action-btn"
              title="POST /semantic/jobs/clear-terminal — physically delete cancelled / complete / failed node rows from the audit table"
              onClick={onClearTerminal}
            >
              clear terminal rows
            </button>
          ) : null
        }
      />

      {summary?.semantic_denominators ? (
        <div className="section">
          <div className="section-head">
            Semantic denominators <span className="head-hint">summary.semantic_denominators</span>
          </div>
          <div className="card card-padded">
            <div className="kv" style={{ gridTemplateColumns: "150px 1fr 150px 1fr" }}>
              <span className="k">node_current</span>
              <span className="v">{summary.semantic_denominators.node_current}</span>
              <span className="k">node_stale</span>
              <span className="v">{summary.semantic_denominators.node_stale}</span>
              <span className="k">node_unverified</span>
              <span className="v">{summary.semantic_denominators.node_unverified}</span>
              <span className="k">node_missing</span>
              <span className="v">{summary.semantic_denominators.node_missing}</span>
              <span className="k">edge_eligible</span>
              <span className="v">{summary.semantic_denominators.edge_eligible}</span>
              <span className="k">edge_current</span>
              <span className="v">{summary.semantic_denominators.edge_current}</span>
              <span className="k">edge_missing</span>
              <span className="v">{summary.semantic_denominators.edge_missing}</span>
              <span className="k">edge_requested</span>
              <span className="v">{summary.semantic_denominators.edge_requested}</span>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function QueueSection({
  title,
  hint,
  rows,
  emptyMsg,
  onCancelOperation,
  headerExtra,
}: {
  title: string;
  hint?: string;
  rows: OperationRow[];
  // Optional now — Running/Queued sections skip rendering entirely when empty,
  // so they don't need a message. Suggestions still passes one through.
  emptyMsg?: string;
  onCancelOperation?: (opType: string, opId: string, targetId: string) => void;
  headerExtra?: React.ReactNode;
}) {
  return (
    <div className="section">
      <div className="section-head" style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span>{title}</span>
        <span className="head-hint">{rows.length}</span>
        {hint ? <span className="head-hint" style={{ fontWeight: 400 }}>{hint}</span> : null}
        {headerExtra ? <span style={{ marginLeft: "auto" }}>{headerExtra}</span> : null}
      </div>
      {rows.length === 0 ? (
        emptyMsg ? <div className="empty empty-compact">{emptyMsg}</div> : null
      ) : (
        <div className="card">
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 170 }}>Type</th>
                <th style={{ width: 240 }}>Target</th>
                <th style={{ width: 120 }}>Status</th>
                <th style={{ minWidth: 220 }}>Note</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Row key={row.operation_id} row={row} onCancelOperation={onCancelOperation} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Row({
  row,
  onCancelOperation,
}: {
  row: OperationRow;
  onCancelOperation?: (opType: string, opId: string, targetId: string) => void;
}) {
  return (
    <tr>
      <td>
        <div className="cell-strong">{labelOpType(row.operation_type)}</div>
        <div className="cell-mono-id">{row.operation_id}</div>
      </td>
      <td>
        <div className="mono cell-strong" title={row.target_label}>
          {row.target_label || row.target_id}
        </div>
        <div className="cell-mono-id">{row.target_scope}</div>
      </td>
      <td>
        <span className={statusClass(row.status)}>{row.status || "—"}</span>
      </td>
      <td>
        {row.last_result ? (
          <div style={{ fontSize: 11.5, color: "var(--ink-600)" }}>{row.last_result}</div>
        ) : row.claimed_by ? (
          <div style={{ fontSize: 11.5, color: "var(--ink-600)" }}>
            claimed by <span className="mono">{row.claimed_by}</span>
          </div>
        ) : (
          <span style={{ color: "var(--ink-400)", fontSize: 11 }}>—</span>
        )}
      </td>
      <td>
        {(row.supported_actions ?? []).length === 0 ? (
          <span style={{ color: "var(--ink-400)", fontSize: 10.5 }}>—</span>
        ) : (
          row.supported_actions.map((a) => {
            const isCancel = a === "cancel" && !!onCancelOperation;
            // MF-2026-05-10-011: running rows can't be cancelled (backend 409).
            const isRunning = RUNNING_STATUSES.has(row.status);
            const cancelDisabled = isCancel && isRunning;
            return (
              <button
                key={a}
                className={
                  isCancel
                    ? `action-btn action-btn-danger${cancelDisabled ? " action-btn-disabled" : ""}`
                    : "action-btn"
                }
                disabled={cancelDisabled}
                title={
                  isCancel
                    ? cancelDisabled
                      ? "Running rows cannot be cancelled — wait for completion or use retry."
                      : `POST cancel for ${row.operation_id}`
                    : "P0 surface — wiring deferred until backend action endpoints are documented"
                }
                onClick={() => {
                  if (cancelDisabled) return;
                  if (isCancel) {
                    onCancelOperation!(row.operation_type, row.operation_id, row.target_id);
                  } else {
                    alert(`Action ${a} on ${row.operation_id} not wired in P0`);
                  }
                }}
              >
                {a.replace(/_/g, " ")}
              </button>
            );
          })
        )}
      </td>
    </tr>
  );
}

// Map raw row status into one of: "queued" / "running" / "terminal" / "other".
// Mirrors backend _semantic_cancel_status_bucket so the dashboard's status
// summary reflects the same semantic buckets the cancel/clear endpoints use.
function statusBucket(status: string): "queued" | "running" | "terminal" | "other" {
  const s = (status || "").toLowerCase();
  if (s === "ai_pending" || s === "queued" || s === "pending_ai" || s === "pending") return "queued";
  if (RUNNING_STATUSES.has(s)) return "running";
  if (TERMINAL_STATUSES.has(s)) return "terminal";
  return "other";
}

function countByBucket(rows: OperationRow[], bucket: "queued" | "running"): number {
  return rows.filter((r) => statusBucket(r.status) === bucket).length;
}

function hasQueuedOrRunning(rows: OperationRow[], opType: string): boolean {
  return rows.some(
    (r) =>
      r.operation_type === opType &&
      (r.status === "queued" || r.status === "ai_pending"),
  );
}

function hasTerminal(rows: OperationRow[]): boolean {
  return rows.some((r) => TERMINAL_STATUSES.has(r.status));
}

function statusClass(s: string): string {
  switch (s) {
    case "ai_pending":
    case "queued":
    case "pending":
      return "status-badge status-pending";
    case "ai_running":
    case "running":
      return "status-badge status-running";
    case "complete":
    case "ai_complete":
    case "succeeded":
      return "status-badge status-complete";
    case "failed":
    case "ai_failed":
    case "error":
      return "status-badge status-failed";
    case "not_queued":
      return "status-badge status-not-queued";
    default:
      return "status-badge status-unknown";
  }
}


function labelOpType(t: string): string {
  switch (t) {
    case "node_semantic":
      return "Node semantic";
    case "edge_semantic":
      return "Edge semantic";
    case "scope_reconcile":
      return "Scope reconcile";
    default:
      return t;
  }
}
