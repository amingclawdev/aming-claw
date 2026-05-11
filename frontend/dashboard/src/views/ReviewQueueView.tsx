import { useState } from "react";
import type { FeedbackQueueGroup, FeedbackQueueResponse } from "../types";

interface Props {
  feedback: FeedbackQueueResponse;
  onDecide?: (feedbackIds: string[], action: string, summaryHint?: string) => void;
}

// MF-2026-05-10-016 P1: per-item review surface for needs_observer_decision
// items emitted by the event-driven semantic worker. Each row maps 1:1 to a
// feedback row in graph_feedback_items; the Accept button dispatches the
// `accept_semantic_enrichment` verb (flips graph_semantic_nodes.status
// pending_review→ai_complete + graph_events PROPOSED→ACCEPTED + rebuilds
// projection), Reject dispatches `reject_false_positive`.
export default function ReviewQueueView({ feedback, onDecide }: Props) {
  const s = feedback.summary;
  const groups = feedback.groups ?? [];
  const empty = groups.length === 0 && s.raw_count === 0;
  const [busyId, setBusyId] = useState<string | null>(null);

  const dispatch = async (group: FeedbackQueueGroup, action: string) => {
    if (!onDecide) return;
    setBusyId(group.queue_id);
    try {
      await onDecide(
        group.feedback_ids,
        action,
        `${group.target_type} ${group.target_id}`,
      );
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="view">
      <div className="view-head">
        <h2 className="view-title">Review Queue</h2>
        <span className="view-subtitle">
          source <span className="mono">/feedback/queue?require_current_semantic=false</span>
        </span>
      </div>

      <div className="section">
        <div className="section-head">Summary</div>
        <div className="score-grid">
          <Card label="Raw count" v={s.raw_count} />
          <Card label="Visible groups" v={s.visible_group_count} />
          <Card label="Visible items" v={s.visible_item_count} />
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          Items{" "}
          <span style={{ fontWeight: 400, color: "var(--ink-400)", fontSize: 11 }}>
            — click Accept to promote AI semantic into the projection, Reject to discard
          </span>
        </div>
        {empty ? (
          <div className="empty">Review queue is empty.</div>
        ) : groups.length === 0 ? (
          <div className="empty">
            All items hidden by current filter (raw_count={s.raw_count}).
          </div>
        ) : (
          <div className="card">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 110 }}>Lane</th>
                  <th style={{ width: 60 }}>Type</th>
                  <th style={{ width: 130 }}>Target</th>
                  <th>Issue</th>
                  <th style={{ width: 130 }}>Semantic gate</th>
                  <th style={{ width: 70 }}>Priority</th>
                  <th style={{ width: 220 }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => {
                  const busy = busyId === g.queue_id;
                  const gate = g.semantic_review_gate;
                  const gateReason = gate?.reason ?? "—";
                  const gateReady = gate?.ready;
                  return (
                    <tr key={g.queue_id}>
                      <td>
                        <span className="mono">{g.lane}</span>
                      </td>
                      <td>
                        <span className="mono">{g.target_type}</span>
                      </td>
                      <td>
                        <span className="mono">{g.target_id}</span>
                      </td>
                      <td>
                        <div>{g.representative_issue}</div>
                        <div style={{ fontSize: 10.5, color: "var(--ink-400)", marginTop: 2 }}>
                          <span className="mono">{g.representative_feedback_id}</span>
                          {g.feedback_ids.length > 1 ? ` +${g.feedback_ids.length - 1} more` : ""}
                          {g.confidence != null ? ` · conf=${g.confidence.toFixed(2)}` : ""}
                          {g.created_at ? ` · ${shortDate(g.created_at)}` : ""}
                        </div>
                      </td>
                      <td>
                        <span
                          className="mono"
                          style={{
                            color: gateReady ? "var(--ink-700)" : "var(--ink-400)",
                            fontSize: 10.5,
                          }}
                          title={gateReady ? "ready for accept" : "underlying semantic not current"}
                        >
                          {gateReady ? "✓ " : "○ "}
                          {gateReason}
                        </span>
                      </td>
                      <td>
                        <span className="mono">{g.priority ?? "—"}</span>
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                          <button
                            className="action-btn"
                            disabled={busy || !onDecide}
                            title="POST /feedback/decision action=accept_semantic_enrichment"
                            onClick={() => dispatch(g, "accept_semantic_enrichment")}
                          >
                            {busy ? "…" : "Accept"}
                          </button>
                          <button
                            className="action-btn action-btn-danger"
                            disabled={busy || !onDecide}
                            title="POST /feedback/decision action=reject_false_positive"
                            onClick={() => dispatch(g, "reject_false_positive")}
                          >
                            {busy ? "…" : "Reject"}
                          </button>
                          <button
                            className="action-btn"
                            disabled={busy || !onDecide}
                            title="POST /feedback/decision action=needs_human_signoff"
                            onClick={() => dispatch(g, "needs_human_signoff")}
                          >
                            Defer
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="section">
        <div className="section-head">Lanes (visible groups)</div>
        <div className="card">
          <table className="table">
            <thead>
              <tr>
                <th>Lane</th>
                <th style={{ width: 90 }}>Visible</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(s.by_lane_visible_groups ?? {}).map(([lane, n]) => (
                <tr key={lane}>
                  <td>{lane}</td>
                  <td>
                    <span className="mono">{String(n)}</span>
                  </td>
                </tr>
              ))}
              {Object.keys(s.by_lane_visible_groups ?? {}).length === 0 ? (
                <tr>
                  <td colSpan={2} className="empty" style={{ padding: 12 }}>
                    No lanes.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>

      <div className="section">
        <div className="section-head">Hidden / dropped</div>
        <div className="card card-padded">
          <div className="kv" style={{ gridTemplateColumns: "200px 1fr 200px 1fr" }}>
            <span className="k">hidden_status_observation</span>
            <span className="v">{s.hidden_status_observation_count ?? 0}</span>
            <span className="k">hidden_resolved</span>
            <span className="v">{s.hidden_resolved_count ?? 0}</span>
            <span className="k">hidden_claimed</span>
            <span className="v">{s.hidden_claimed_count ?? 0}</span>
            <span className="k">hidden_semantic_pending</span>
            <span className="v">{s.hidden_semantic_pending_count ?? 0}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function Card({ label, v }: { label: string; v: number }) {
  return (
    <div className="score-card">
      <div className="lbl">{label}</div>
      <div className="val">{v}</div>
    </div>
  );
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  return d.toISOString().slice(5, 16).replace("T", " ");
}
