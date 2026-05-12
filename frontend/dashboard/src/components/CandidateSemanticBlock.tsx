import { useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { formatSemanticValue } from "../lib/semanticFormat";

interface Props {
  snapshotId: string | null | undefined;
  targetType: "node" | "edge";
  targetId: string;
}

// MF-016/017 review surface: when the operator is reviewing a
// needs_observer_decision feedback row, the AI's proposed semantic_payload
// lives in graph_events (status=proposed) — NOT in the feedback row itself.
// This block fetches the most recent proposed event for the target and
// renders the candidate content (relation_purpose, semantic_label, evidence,
// risk, open_issues, confidence) so the operator can read what the AI
// actually proposed before clicking Accept / Retry / Reject.
export default function CandidateSemanticBlock({ snapshotId, targetType, targetId }: Props) {
  const [state, setState] = useState<
    | { phase: "idle" }
    | { phase: "loading" }
    | { phase: "empty" }
    | { phase: "error"; message: string }
    | {
        phase: "loaded";
        eventId: string;
        eventStatus: string;
        eventConfidence?: number;
        payload: Record<string, unknown>;
      }
  >({ phase: "idle" });

  useEffect(() => {
    if (!snapshotId || !targetId) {
      setState({ phase: "idle" });
      return;
    }
    const ac = new AbortController();
    setState({ phase: "loading" });
    api
      .listProposedEvents(snapshotId, { target_type: targetType, target_id: targetId }, ac.signal)
      .then((res) => {
        const events = res.events ?? [];
        if (events.length === 0) {
          setState({ phase: "empty" });
          return;
        }
        // Most recent first — backend already orders by event_seq desc.
        const latest = events[0];
        const payload =
          (latest.payload?.semantic_payload as Record<string, unknown>) ?? {};
        setState({
          phase: "loaded",
          eventId: latest.event_id,
          eventStatus: latest.status,
          eventConfidence: latest.confidence,
          payload,
        });
      })
      .catch((e) => {
        if ((e as { name?: string }).name === "AbortError") return;
        const message = e instanceof ApiError ? `${e.message} ${e.body}` : (e as Error).message;
        setState({ phase: "error", message });
      });
    return () => ac.abort();
  }, [snapshotId, targetType, targetId]);

  if (state.phase === "idle") return null;
  if (state.phase === "loading") {
    return (
      <div className="candidate-block candidate-loading">
        <span className="spinner" /> Loading candidate semantic…
      </div>
    );
  }
  if (state.phase === "empty") {
    return (
      <div className="candidate-block candidate-empty">
        No PROPOSED graph event for {targetType} <span className="mono">{targetId}</span>. The
        feedback row may have lost its linked event after a snapshot transition.
      </div>
    );
  }
  if (state.phase === "error") {
    return (
      <div className="candidate-block candidate-error">
        Failed to load candidate semantic: {state.message}
      </div>
    );
  }

  const p = state.payload;
  const relationPurpose = formatSemanticValue(p.relation_purpose);
  const semanticLabel = formatSemanticValue(p.semantic_label, 180);
  const directionality = formatSemanticValue(p.directionality);
  const risk = formatSemanticValue(p.risk);
  const semanticSummary = formatSemanticValue(p.semantic_summary);
  const intent = formatSemanticValue(p.intent);
  const featureName = formatSemanticValue(p.feature_name, 180);
  const domainLabel = formatSemanticValue(p.domain_label, 180);
  const evidence = p.evidence as Record<string, unknown> | undefined;
  const openIssues = Array.isArray(p.open_issues) ? (p.open_issues as unknown[]) : [];
  const confidence =
    typeof p.confidence === "number"
      ? p.confidence
      : typeof state.eventConfidence === "number"
        ? state.eventConfidence
        : null;

  return (
    <div className="candidate-block">
      <div className="candidate-block-head">
        <span className="candidate-block-title">⚡ Candidate semantic (AI proposed)</span>
        <span className="mono candidate-block-meta">
          {state.eventId} · {state.eventStatus}
          {confidence != null ? ` · conf=${confidence.toFixed(2)}` : ""}
        </span>
      </div>

      {/* Node-style fields */}
      {featureName ? (
        <div className="candidate-block-row">
          <span className="candidate-block-key">feature</span>
          <span className="candidate-block-val">{featureName}</span>
        </div>
      ) : null}
      {domainLabel ? (
        <div className="candidate-block-row">
          <span className="candidate-block-key">domain</span>
          <span className="candidate-block-val mono">{domainLabel}</span>
        </div>
      ) : null}
      {semanticSummary ? (
        <div className="candidate-block-row">
          <span className="candidate-block-key">summary</span>
          <span className="candidate-block-val">{semanticSummary}</span>
        </div>
      ) : null}
      {intent ? (
        <div className="candidate-block-row">
          <span className="candidate-block-key">intent</span>
          <span className="candidate-block-val">{intent}</span>
        </div>
      ) : null}

      {/* Edge-style fields */}
      {relationPurpose ? (
        <div className="candidate-block-row">
          <span className="candidate-block-key">purpose</span>
          <span className="candidate-block-val">{relationPurpose}</span>
        </div>
      ) : null}
      {semanticLabel ? (
        <div className="candidate-block-row">
          <span className="candidate-block-key">label</span>
          <span className="candidate-block-val mono">{semanticLabel}</span>
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

      {/* Shared: evidence + open_issues */}
      {evidence && typeof evidence === "object" ? (
        <div className="candidate-block-row">
          <span className="candidate-block-key">evidence</span>
          <span className="candidate-block-val">
            {formatSemanticValue(evidence.basis || evidence, 240)}
          </span>
        </div>
      ) : null}
      {openIssues.length > 0 ? (
        <div className="candidate-block-row">
          <span className="candidate-block-key">open issues</span>
          <div>
            <ul className="candidate-block-issues">
              {openIssues.slice(0, 3).map((it, i) => (
                <li key={i}>{formatSemanticValue(it, 240)}</li>
              ))}
            </ul>
            {openIssues.length > 3 ? (
              <div className="candidate-block-issues-more">
                +{openIssues.length - 3} more
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
