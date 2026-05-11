import { useState } from "react";

interface Props {
  targetType: string;
  targetId: string;
  feedbackIds: string[];
  priorIssue?: string;
  onCancel: () => void;
  onSubmit: (rationale: string) => Promise<void>;
}

// MF-2026-05-10-016 P2: shared retry modal. Operator types a rationale that
// flows into /semantic-feedback (JSONL store) → next AI run sees it in the
// `review_feedback` payload alongside the rejected proposal in
// `existing_semantic`. The dispatch chain is orchestrated by the caller
// (App.handleFeedbackRetry).
export default function RetryFeedbackModal({
  targetType,
  targetId,
  feedbackIds,
  priorIssue,
  onCancel,
  onSubmit,
}: Props) {
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const trimmed = rationale.trim();
  const canSubmit = trimmed.length >= 4 && !busy;
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal>
        <div className="modal-head">
          <div className="modal-title">Retry semantic enrichment</div>
          <button className="modal-close" onClick={onCancel} aria-label="close">×</button>
        </div>
        <div className="modal-body">
          <div className="kv" style={{ gridTemplateColumns: "100px 1fr" }}>
            <span className="k">target</span>
            <span className="v mono">{targetType} {targetId}</span>
            <span className="k">feedback</span>
            <span className="v mono">{feedbackIds.join(", ")}</span>
            {priorIssue ? (
              <>
                <span className="k">prior</span>
                <span className="v" style={{ fontSize: 11.5 }}>{priorIssue}</span>
              </>
            ) : null}
          </div>
          <div style={{ marginTop: 12 }}>
            <label
              htmlFor="retry-rationale"
              style={{ display: "block", fontWeight: 500, fontSize: 12, marginBottom: 6 }}
            >
              Rationale (what was wrong, what should change)
            </label>
            <textarea
              id="retry-rationale"
              value={rationale}
              onChange={(e) => setRationale(e.target.value)}
              rows={6}
              placeholder="e.g. summary is too generic — call out the EventBus subscriber side-effect explicitly"
              autoFocus
              style={{
                width: "100%",
                padding: 8,
                fontSize: 12,
                fontFamily: "inherit",
                border: "1px solid var(--ink-200)",
                borderRadius: 4,
                boxSizing: "border-box",
              }}
            />
            <div style={{ fontSize: 10.5, color: "var(--ink-400)", marginTop: 4 }}>
              Goes to <span className="mono">/semantic-feedback</span> JSONL → next AI call
              receives it in <span className="mono">review_feedback</span> alongside the prior
              rejected semantic in <span className="mono">existing_semantic</span>.
            </div>
          </div>
        </div>
        <div className="modal-foot">
          <button className="action-btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            className="action-btn action-btn-primary"
            disabled={!canSubmit}
            onClick={async () => {
              setBusy(true);
              try {
                await onSubmit(trimmed);
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Submitting…" : "Reject + re-enqueue"}
          </button>
        </div>
      </div>
    </div>
  );
}
