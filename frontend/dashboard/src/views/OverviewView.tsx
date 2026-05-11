import type {
  ActiveSummaryResponse,
  FeedbackQueueResponse,
  OperationsQueueResponse,
  ProjectionResponse,
  NodeRecord,
  OperationRow,
} from "../types";

interface Props {
  data: {
    summary: ActiveSummaryResponse;
    ops: OperationsQueueResponse;
    feedback: FeedbackQueueResponse;
    projection: ProjectionResponse;
    nodes: NodeRecord[];
  };
  onSelectNode(id: string): void;
}

type Tone = "green" | "amber" | "red" | "purple" | "blue" | "neutral";

export default function OverviewView({ data, onSelectNode }: Props) {
  const { summary, ops, feedback } = data;
  const h = summary.health;
  const sem = h.semantic_health;

  return (
    <div className="view">
      <div className="view-head">
        <h2 className="view-title">Overview</h2>
        <span className="view-subtitle">
          snapshot <span className="mono">{summary.snapshot_id}</span> · commit{" "}
          <span className="mono">{summary.commit_sha.slice(0, 7)}</span> · {summary.snapshot_kind}/
          {summary.snapshot_status}
        </span>
      </div>

      <div className="section">
        <div className="section-head">
          Scores <span className="head-hint">/snapshots/active/summary · health</span>
        </div>
        <div className="score-grid">
          <ScoreCard label="Project health" value={h.project_health_score} sub={`raw ${fmt(h.raw_project_health_score)}`} />
          <ScoreCard label="Structure" value={h.structure_health_score} />
          <ScoreCard label="Semantic" value={h.semantic_health_score} />
          <ScoreCard label="File hygiene" value={h.file_hygiene_score} />
          <ScoreCard label="Artifact binding" value={h.artifact_binding_score} />
          <ScoreCard
            label="Doc coverage"
            value={pct(h.doc_coverage_ratio)}
            suffix="%"
          />
          <ScoreCard
            label="Test coverage"
            value={pct(h.test_coverage_ratio)}
            suffix="%"
          />
          <ScoreCard
            label="Semantic coverage"
            value={pct(h.semantic_coverage_ratio)}
            suffix="%"
          />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          Node semantic <span className="head-hint">governed L7 = {sem.feature_count}</span>
        </div>
        <div className="score-grid">
          <CountCard label="Current" count={sem.semantic_current_count} total={sem.feature_count} tone="green" />
          <CountCard label="Stale" count={sem.semantic_stale_count} total={sem.feature_count} tone="amber" />
          <CountCard label="Hash-unverified" count={sem.semantic_unverified_hash_count} total={sem.feature_count} tone="purple" />
          <CountCard label="Missing" count={sem.semantic_missing_count} total={sem.feature_count} tone="red" />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          Edge semantic <span className="head-hint">eligible {sem.edge_semantic_eligible_count}</span>
        </div>
        <div className="score-grid">
          <CountCard label="Current" count={sem.edge_semantic_current_count} total={sem.edge_semantic_eligible_count} tone="green" />
          <CountCard label="Missing" count={sem.edge_semantic_missing_count} total={sem.edge_semantic_eligible_count} tone="red" />
          <CountCard label="Requested" count={sem.edge_semantic_requested_count} total={sem.edge_semantic_eligible_count} tone="amber" />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          Operations queue <span className="head-hint">top {Math.min(ops.count, 5)} of {ops.count}</span>
        </div>
        {ops.operations.length === 0 ? (
          <div className="empty">No queued operations.</div>
        ) : (
          <div className="card">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 160 }}>Type</th>
                  <th style={{ width: 220 }}>Target</th>
                  <th style={{ width: 110 }}>Status</th>
                  <th>Progress</th>
                </tr>
              </thead>
              <tbody>
                {ops.operations.slice(0, 5).map((o) => (
                  <OverviewOpsRow key={o.operation_id} row={o} onSelectNode={onSelectNode} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="section">
        <div className="section-head">
          Feedback queue <span className="head-hint">/feedback/queue?require_current_semantic=true</span>
        </div>
        <div className="card card-padded">
          <div className="kv" style={{ gridTemplateColumns: "150px 1fr 150px 1fr" }}>
            <span className="k">raw_count</span>
            <span className="v">{feedback.summary.raw_count}</span>
            <span className="k">visible_groups</span>
            <span className="v">{feedback.summary.visible_group_count}</span>
            <span className="k">visible_items</span>
            <span className="v">{feedback.summary.visible_item_count}</span>
            <span className="k">require_current</span>
            <span className="v">{String(feedback.summary.require_current_semantic)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function OverviewOpsRow({ row, onSelectNode }: { row: OperationRow; onSelectNode(id: string): void }) {
  const total = row.progress?.total ?? 0;
  const done = row.progress?.done ?? 0;
  const pctV = total > 0 ? Math.round((done / total) * 100) : 0;
  const isNode = row.target_scope === "node";
  return (
    <tr>
      <td>
        <div className="cell-strong">{labelOpType(row.operation_type)}</div>
        <div className="cell-mono-id">{row.operation_id}</div>
      </td>
      <td>
        {isNode ? (
          <a
            href="#"
            className="mono"
            onClick={(e) => {
              e.preventDefault();
              onSelectNode(row.target_id);
            }}
            title="Open node in inspector"
          >
            {row.target_label || row.target_id}
          </a>
        ) : (
          <span className="mono">{row.target_label || row.target_id}</span>
        )}
        <div className="cell-mono-id">{row.target_scope}</div>
      </td>
      <td>
        <span className={statusClass(row.status)}>{row.status || "—"}</span>
      </td>
      <td>
        <div className="progress">
          <div className={`progress-bar tone-${progressTone(row.status, total, done)}`}>
            <span style={{ width: total > 0 ? `${pctV}%` : "0%" }} />
          </div>
          <span className="progress-text">
            {done}/{total}
          </span>
        </div>
      </td>
    </tr>
  );
}

function ScoreCard({
  label,
  value,
  sub,
  suffix,
}: {
  label: string;
  value: number | undefined;
  sub?: string;
  suffix?: string;
}) {
  const tone = scoreTone(value);
  const v = typeof value === "number" ? value.toFixed(2) : "—";
  const ratio = typeof value === "number" ? Math.max(0, Math.min(1, value / 100)) : 0;
  return (
    <div className={`score-card tone-${tone}`}>
      <span className="accent-bar" />
      <div className="lbl">{label}</div>
      <div className="val">
        {v}
        {suffix ? <span className="val-suffix">{suffix}</span> : null}
      </div>
      <div className="mini-bar">
        <span style={{ width: `${ratio * 100}%` }} />
      </div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  );
}

function CountCard({
  label,
  count,
  total,
  tone,
}: {
  label: string;
  count: number;
  total: number;
  tone: Tone;
}) {
  const ratio = total > 0 ? count / total : 0;
  const cardTone: Tone = count === 0 ? "neutral" : tone;
  return (
    <div className={`score-card count-card tone-${cardTone}`}>
      <div className="lbl">{label}</div>
      <div className="val">
        {count}
        <span className="val-frac">/ {total}</span>
      </div>
      <div className="mini-bar">
        <span style={{ width: `${ratio * 100}%` }} />
      </div>
      <div className="sub">{Math.round(ratio * 100)}%</div>
    </div>
  );
}

function scoreTone(v: number | undefined): Tone {
  if (v == null) return "neutral";
  if (v >= 85) return "green";
  if (v >= 70) return "amber";
  return "red";
}

function fmt(v: number | undefined): string {
  return typeof v === "number" ? v.toFixed(2) : "—";
}

function pct(r: number | undefined): number | undefined {
  return typeof r === "number" ? r * 100 : undefined;
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

function progressTone(status: string, total: number, done: number): "amber" | "blue" | "green" | "red" | "empty" {
  if (status === "not_queued") return "empty";
  if (total === 0) return "empty";
  if (done === total) return "green";
  if (status === "failed" || status === "ai_failed" || status === "error") return "red";
  if (status === "ai_running" || status === "running") return "blue";
  return "amber";
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
