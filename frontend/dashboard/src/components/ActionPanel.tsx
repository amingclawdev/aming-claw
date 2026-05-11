import { useEffect, useMemo, useState } from "react";
import type { EnrichPreset } from "./ActionControlPanel";
import { api, ApiError, type BacklogDraft } from "../lib/api";
import type { FeedbackQueueResponse } from "../types";

interface Props {
  open: boolean;
  snapshotId: string | null;
  feedback: FeedbackQueueResponse | null;
  initialTab?: Tab;
  prefillDraft?: Partial<BacklogDraft> | null;
  onClose(): void;
  onOpenPreset(preset: EnrichPreset): void;
  onOpenReviewView(): void;
  onSubmitted(message: string, tone: "success" | "error" | "info"): void;
  onRunReconcile(): void;
}

type Tab = "review" | "backlog";

interface PresetCard {
  id: EnrichPreset;
  group: "semantic" | "review";
  title: string;
  desc: string;
  tone: "blue" | "orange" | "purple";
}

const PRESET_CARDS: PresetCard[] = [
  { id: "missing_nodes", group: "semantic", title: "Semanticize missing nodes", desc: "Target=Nodes · Scope=Missing only", tone: "blue" },
  { id: "missing_edges", group: "semantic", title: "Semanticize missing edges", desc: "Target=Edges · Scope=Missing only", tone: "blue" },
  { id: "retry_stale", group: "semantic", title: "Retry stale semantics", desc: "Target=Both · Scope=Stale only · Mode=Retry", tone: "orange" },
  { id: "subtree", group: "semantic", title: "Semanticize selected subtree", desc: "Target=Both · Scope=Selected subtree", tone: "blue" },
  { id: "full", group: "semantic", title: "Run full semanticization", desc: "Target=Both · Scope=Full graph", tone: "blue" },
  { id: "global_review", group: "semantic", title: "Run global semantic review", desc: "Target=Both · Mode=Review", tone: "purple" },
  { id: "semantic_health_review", group: "review", title: "Run semantic health review", desc: "Scope=Current projection · audit health signals", tone: "purple" },
  { id: "global_arch_review", group: "review", title: "Run global architecture review", desc: "Scope=Current projection · cross-layer insights", tone: "purple" },
  { id: "review_subtree", group: "review", title: "Review selected subtree", desc: "Scope=Selected subtree · focused review", tone: "purple" },
];

const EMPTY_BACKLOG: BacklogDraft = {
  title: "",
  task_type: "dev",
  priority: "P2",
  target_files: [],
  affected_graph_nodes: [],
  graph_gate_mode: "strict",
  branch_mode: "batch_branch",
  acceptance_criteria: [],
  prompt: "",
};

export default function ActionPanel({
  open,
  snapshotId,
  feedback,
  initialTab,
  prefillDraft,
  onClose,
  onOpenPreset,
  onOpenReviewView,
  onSubmitted,
  onRunReconcile,
}: Props) {
  const [tab, setTab] = useState<Tab>("review");
  const [draft, setDraft] = useState<BacklogDraft>({ ...EMPTY_BACKLOG });
  const [busy, setBusy] = useState(false);
  const [showPayload, setShowPayload] = useState(false);

  // When (re-)opened with a prefilled draft (drawer's "File backlog for this
  // node" path), seed the form with the node-bound context and switch to the
  // backlog tab so the operator lands directly on the form.
  useEffect(() => {
    if (!open) return;
    if (initialTab) setTab(initialTab);
    if (prefillDraft) {
      setDraft({ ...EMPTY_BACKLOG, ...prefillDraft });
    }
  }, [open, initialTab, prefillDraft]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const semanticPresets = PRESET_CARDS.filter((p) => p.group === "semantic");
  const reviewPresets = PRESET_CARDS.filter((p) => p.group === "review");

  const backlogPayload = useMemo(
    () => ({
      backlog: {
        ...draft,
        target_files: splitLines(draft.target_files as unknown as string),
        affected_graph_nodes: splitCsv(draft.affected_graph_nodes as unknown as string),
        acceptance_criteria: splitLines(draft.acceptance_criteria as unknown as string),
      },
      graph_context: {
        affected_nodes: splitCsv(draft.affected_graph_nodes as unknown as string),
        snapshot_id: snapshotId,
      },
      semantic_context: { source: "manual" },
    }),
    [draft, snapshotId],
  );

  if (!open) return null;

  async function fileBacklog(startChain: boolean) {
    if (!snapshotId) {
      onSubmitted("No active snapshot — refresh first.", "error");
      return;
    }
    if (!draft.title.trim()) {
      onSubmitted("Title is required.", "error");
      return;
    }
    setBusy(true);
    try {
      const flatBacklog = backlogPayload.backlog;
      // Step 1: create the backlog_candidate_requested proposed_event so the
      // file-backlog endpoint has an event to attach to. Mirrors the
      // prototype's two-step fileBacklogConfirm flow (lines 5161-5186).
      const evRes = await api.submitProposedEvent(snapshotId, {
        event_kind: "proposed_event",
        event_type: "backlog_candidate_requested",
        target_type: "node",
        target_id: flatBacklog.affected_graph_nodes[0] ?? "",
        source: "dashboard_user",
        user_text: flatBacklog.title,
        payload: { backlog_draft: flatBacklog },
        precondition: { snapshot_id: snapshotId },
        status: "proposed",
      });
      const eventId = evRes.event?.event_id ?? evRes.event_id;
      if (!eventId) {
        onSubmitted(
          "Backlog event created but the backend didn't return an event_id; cannot continue with file-backlog.",
          "error",
        );
        return;
      }
      const fileRes = await api.fileBacklogFromEvent(snapshotId, eventId, {
        backlog: flatBacklog,
        start_chain: startChain,
      });
      const bugId =
        fileRes.bug_id ??
        fileRes.event?.backlog_bug_id ??
        fileRes.backlog_task_id ??
        fileRes.task_id ??
        "(no bug_id returned)";
      onSubmitted(
        `Backlog filed${startChain ? " + chain started" : ""} · event_id=${eventId} · bug_id=${bugId}`,
        "success",
      );
      setDraft({ ...EMPTY_BACKLOG });
      onClose();
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.message} · ${err.body.slice(0, 200)}`
          : (err as Error).message;
      onSubmitted(`File backlog failed: ${msg}`, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="action-panel-backdrop" onClick={onClose}>
      <div
        className="action-panel action-panel-wide"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="action-panel-head">
          <span className="action-panel-kind kind-feedback" style={{ background: "#f1f5f9", color: "#334155", borderColor: "#e2e8f0" }}>
            ACTIONS
          </span>
          <span className="action-panel-title">Action Panel</span>
          <button className="btn-close" onClick={onClose} aria-label="Close" style={{ marginLeft: "auto" }}>
            ×
          </button>
        </header>
        <nav className="action-panel-tabs" role="tablist">
          <button
            role="tab"
            aria-selected={tab === "review"}
            className={`action-panel-tab${tab === "review" ? " active" : ""}`}
            onClick={() => setTab("review")}
          >
            Review &amp; approve
          </button>
          <button
            role="tab"
            aria-selected={tab === "backlog"}
            className={`action-panel-tab${tab === "backlog" ? " active" : ""}`}
            onClick={() => setTab("backlog")}
          >
            File backlog
          </button>
        </nav>

        {tab === "review" ? (
          <div className="action-panel-body action-panel-body-wide scrollbar-thin">
            <section className="action-panel-section">
              <div className="action-panel-section-head">
                <h3>Semantic Jobs</h3>
                <span className="action-panel-section-hint">Queue AI semanticization &amp; review jobs</span>
              </div>
              <div className="action-preset-grid">
                {semanticPresets.map((p) => (
                  <PresetCardButton key={p.id} card={p} onClick={() => onOpenPreset(p.id)} />
                ))}
              </div>
              <div className="action-panel-section-foot">
                Semantic jobs are queued through backend operator APIs (
                <span className="mono">POST /semantic/jobs</span>). The browser does not call AI directly.
              </div>
            </section>

            <section className="action-panel-section">
              <div className="action-panel-section-head">
                <h3>Reconcile Jobs</h3>
                <span className="action-panel-section-hint">Re-scan structure graph from working tree</span>
              </div>
              <div className="action-preset-grid">
                <button
                  className="action-preset-card tone-orange"
                  onClick={() => {
                    onRunReconcile();
                  }}
                >
                  <div className="action-preset-title">Run scope reconcile</div>
                  <div className="action-preset-desc">Dry-run preview · structure + queue enrichment</div>
                </button>
                <button
                  className="action-preset-card tone-green"
                  onClick={() => setTab("backlog")}
                >
                  <div className="action-preset-title">File backlog</div>
                  <div className="action-preset-desc">Open backlog filing tab</div>
                </button>
                <button
                  className="action-preset-card tone-neutral"
                  onClick={() => {
                    onOpenReviewView();
                    onClose();
                  }}
                >
                  <div className="action-preset-title">Open AI review queue</div>
                  <div className="action-preset-desc">Scroll to lane summary &amp; candidates</div>
                </button>
              </div>
              <div className="action-panel-section-foot">
                Reconcile generates a new snapshot via{" "}
                <span className="mono">POST /reconcile/scope</span>. Frontend never mutates structure
                directly.
              </div>
            </section>

            <section className="action-panel-section">
              <div className="action-panel-section-head">
                <h3>Review Jobs</h3>
                <span className="action-panel-section-hint">AI review on top of current semantic projection</span>
              </div>
              <div className="action-preset-grid">
                {reviewPresets.map((p) => (
                  <PresetCardButton key={p.id} card={p} onClick={() => onOpenPreset(p.id)} />
                ))}
              </div>
              <div className="action-panel-section-foot">
                Review jobs do not rewrite code. They review the current semantic projection and
                produce health signals, graph correction candidates, and backlog candidates.
              </div>
            </section>

            <section className="action-panel-section">
              <div className="action-panel-section-head">
                <h3>Review queue</h3>
                <span className="action-panel-section-hint">From <span className="mono">/feedback/queue</span></span>
              </div>
              <div className="action-panel-queue-summary">
                <SummaryTile label="raw count" value={feedback?.summary.raw_count ?? 0} />
                <SummaryTile label="visible groups" value={feedback?.summary.visible_group_count ?? 0} />
                <SummaryTile label="visible items" value={feedback?.summary.visible_item_count ?? 0} />
                <SummaryTile label="hidden status" value={feedback?.summary.hidden_status_observation_count ?? 0} />
                <SummaryTile label="hidden resolved" value={feedback?.summary.hidden_resolved_count ?? 0} />
                <SummaryTile label="hidden claimed" value={feedback?.summary.hidden_claimed_count ?? 0} />
              </div>
            </section>
          </div>
        ) : (
          <div className="action-panel-body action-panel-body-wide scrollbar-thin">
            <div className="action-panel-section">
              <div className="action-panel-banner">
                Filing creates a <span className="mono">backlog_candidate_requested</span>{" "}
                proposed_event, then POSTs to{" "}
                <span className="mono">/events/{"{id}"}/file-backlog</span>. Frontend never mutates
                graph topology.
              </div>

              <fieldset className="action-fieldset action-fieldset-source">
                <legend>Source evidence</legend>
                <div className="action-form-hint">
                  Standalone backlog filing — not bound to a review candidate. Use this for manual
                  backlog rows or general TODOs you want governed.
                </div>
              </fieldset>

              <fieldset className="action-fieldset">
                <legend>Backlog draft</legend>
                <FormText
                  label="title"
                  value={draft.title}
                  onChange={(v) => setDraft({ ...draft, title: v })}
                />
                <div className="action-form-grid-3">
                  <FormSelect
                    label="task_type"
                    value={draft.task_type}
                    options={["pm", "dev", "test", "qa", "task", "reconcile", "mf"]}
                    onChange={(v) => setDraft({ ...draft, task_type: v as BacklogDraft["task_type"] })}
                  />
                  <FormSelect
                    label="priority"
                    value={draft.priority}
                    options={["P0", "P1", "P2", "P3"]}
                    onChange={(v) => setDraft({ ...draft, priority: v as BacklogDraft["priority"] })}
                  />
                  <FormSelect
                    label="graph_gate_mode"
                    value={draft.graph_gate_mode}
                    options={["strict", "advisory", "raw"]}
                    onChange={(v) => setDraft({ ...draft, graph_gate_mode: v as BacklogDraft["graph_gate_mode"] })}
                  />
                </div>
                <FormSelect
                  label="branch_mode"
                  value={draft.branch_mode}
                  options={["main", "batch_branch", "reconcile_branch"]}
                  onChange={(v) => setDraft({ ...draft, branch_mode: v as BacklogDraft["branch_mode"] })}
                />
                <FormTextArea
                  label="target_files (one per line)"
                  value={(draft.target_files as unknown as string) || ""}
                  rows={3}
                  onChange={(v) => setDraft({ ...draft, target_files: v as unknown as string[] })}
                  mono
                />
                <FormText
                  label="affected_graph_nodes (comma-separated)"
                  value={(draft.affected_graph_nodes as unknown as string) || ""}
                  onChange={(v) => setDraft({ ...draft, affected_graph_nodes: v as unknown as string[] })}
                  mono
                />
                <FormTextArea
                  label="acceptance_criteria (one per line)"
                  value={(draft.acceptance_criteria as unknown as string) || ""}
                  rows={3}
                  onChange={(v) => setDraft({ ...draft, acceptance_criteria: v as unknown as string[] })}
                />
                <FormTextArea
                  label="prompt / instructions"
                  value={draft.prompt}
                  rows={3}
                  onChange={(v) => setDraft({ ...draft, prompt: v })}
                />
              </fieldset>

              <details
                className="action-payload-preview"
                open={showPayload}
                onToggle={(e) => setShowPayload((e.target as HTMLDetailsElement).open)}
              >
                <summary>Preview backlog payload (JSON)</summary>
                <pre className="mono">{JSON.stringify(backlogPayload, null, 2)}</pre>
              </details>
            </div>

            <footer className="action-panel-foot">
              <button
                className="action-btn"
                onClick={() => setDraft({ ...EMPTY_BACKLOG })}
                disabled={busy}
              >
                Reset draft
              </button>
              <button
                className="action-btn"
                onClick={() =>
                  onSubmitted("Draft saved locally (browser only — not persisted yet).", "info")
                }
                disabled={busy}
              >
                Save draft
              </button>
              <button
                className="action-btn-primary kind-feedback"
                onClick={() => fileBacklog(false)}
                disabled={busy || !draft.title.trim()}
              >
                {busy ? "Filing…" : "File backlog"}
              </button>
              <button
                className="action-btn-primary kind-enrich"
                onClick={() => fileBacklog(true)}
                disabled={busy || !draft.title.trim()}
              >
                {busy ? "Filing…" : "File &amp; start chain"}
              </button>
            </footer>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------- Inline form helpers ----------------

function FormText({
  label,
  value,
  onChange,
  mono,
}: {
  label: string;
  value: string;
  onChange(v: string): void;
  mono?: boolean;
}) {
  return (
    <label className="action-form-row">
      <span>{label}</span>
      <input
        type="text"
        className={`action-input${mono ? " mono" : ""}`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function FormSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange(v: string): void;
}) {
  return (
    <label className="action-form-row">
      <span>{label}</span>
      <select className="action-input" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

function FormTextArea({
  label,
  value,
  rows,
  onChange,
  mono,
}: {
  label: string;
  value: string;
  rows: number;
  onChange(v: string): void;
  mono?: boolean;
}) {
  return (
    <label className="action-form-row">
      <span>{label}</span>
      <textarea
        rows={rows}
        className={`action-input${mono ? " mono" : ""}`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function PresetCardButton({ card, onClick }: { card: PresetCard; onClick(): void }) {
  return (
    <button className={`action-preset-card tone-${card.tone}`} onClick={onClick}>
      <div className="action-preset-title">{card.title}</div>
      <div className="action-preset-desc">{card.desc}</div>
    </button>
  );
}

function SummaryTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="action-summary-tile">
      <div className="action-summary-tile-label">{label}</div>
      <div className="action-summary-tile-value">{value}</div>
    </div>
  );
}

// Helpers for textarea/list field round-tripping. The form stores raw strings
// in BacklogDraft fields typed as string[]; we convert at the API boundary.
function splitLines(s: string): string[] {
  if (!s || typeof s !== "string") return [];
  return s.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
}
function splitCsv(s: string): string[] {
  if (!s || typeof s !== "string") return [];
  return s.split(",").map((l) => l.trim()).filter(Boolean);
}
