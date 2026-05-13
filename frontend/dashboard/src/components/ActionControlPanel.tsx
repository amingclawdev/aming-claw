import { useEffect, useMemo, useRef, useState } from "react";
import type { NodeRecord } from "../types";
import type { PinnedEdge } from "./FocusCard";
import { classifyNode, semStatusLabel, type SemanticStatus } from "../lib/semantic";
import { api, ApiError, type AiConfigResponse, type FeedbackSubmitPayload, type SemanticJobPayload } from "../lib/api";

export type ActionKind = "enrich" | "feedback";

// Preset triggered from the global Action Panel (vs. per-node CTA from the
// FocusCard / drawer head). Each preset seeds the form with prototype-style
// (target, scope, mode) defaults.
export type EnrichPreset =
  | "missing_nodes"
  | "missing_edges"
  | "retry_stale"
  | "subtree"
  | "full"
  | "global_review"
  | "semantic_health_review"
  | "global_arch_review"
  | "review_subtree";

export interface ActionTarget {
  // Exactly one of node / edge is set; preset can be used alone for
  // snapshot-level (target_ids=[]) operations.
  node?: NodeRecord;
  edge?: PinnedEdge;
  preset?: EnrichPreset;
  // Optional explicit mode override — e.g. the "Retry AI enrich" CTA passes
  // forceMode="retry" so the modal lands directly on retry without showing
  // the mode selector.
  forceMode?: "semanticize" | "retry" | "review";
}

interface Props {
  open: boolean;
  kind: ActionKind;
  target: ActionTarget | null;
  snapshotId: string | null;
  aiConfig?: AiConfigResponse | null;
  onClose(): void;
  onSubmitted(message: string, tone: "success" | "error" | "info"): void;
}

// Mirrors the prototype's openSemJobModal radio groups (lines 4857-4948).
// Backend contract: POST /api/graph-governance/{pid}/snapshots/{sid}/semantic/jobs
type EnrichTarget = "nodes" | "edges" | "both";
type EnrichScope = "missing" | "stale" | "selected_node" | "selected_subtree" | "full";
type EnrichMode = "semanticize" | "retry" | "review";
type EnrichExec = "dry_run" | "apply";

const ENRICH_TARGETS: { id: EnrichTarget; label: string; desc: string }[] = [
  { id: "nodes", label: "Nodes", desc: "Feature-level (L7) and asset (L4) AI semantic" },
  { id: "edges", label: "Edges", desc: "Typed relation semantic" },
  { id: "both", label: "Both", desc: "Nodes + edges in one job" },
];

const ENRICH_SCOPES: { id: EnrichScope; label: string; desc: string }[] = [
  { id: "missing", label: "Missing only", desc: "Targets without any AI semantic yet" },
  { id: "stale", label: "Stale only", desc: "Source hash drifted since last enrichment" },
  { id: "selected_node", label: "Selected node / edge", desc: "Just the focused target" },
  { id: "selected_subtree", label: "Selected subtree", desc: "Focus + every descendant" },
  { id: "full", label: "Full graph", desc: "Whole snapshot — slow, prefer scoped runs" },
];

const ENRICH_MODES: { id: EnrichMode; label: string; desc: string }[] = [
  { id: "semanticize", label: "Semanticize", desc: "Generate AI semantic for matching targets" },
  { id: "retry", label: "Retry", desc: "Re-enrich stale / failed targets, ignore current" },
  { id: "review", label: "Global review", desc: "Architecture + cross-cutting AI review pass" },
];

const ENRICH_EXEC: { id: EnrichExec; label: string; desc: string }[] = [
  { id: "dry_run", label: "Dry run", desc: "Plan + queue tally, no AI calls billed" },
  { id: "apply", label: "Apply", desc: "Queue real AI work — runs in the executor" },
];

const FEEDBACK_KINDS: {
  id: string;
  label: string;
  desc: string;
  createsGraphEvent?: boolean;
}[] = [
  {
    id: "graph_correction",
    label: "Graph correction",
    desc: "Recorded structure / typed relation is wrong; auto-creates a graph event",
    createsGraphEvent: true,
  },
  {
    id: "project_improvement",
    label: "Project improvement",
    desc: "Use the AI semantic as guidance to improve the code",
  },
  {
    id: "status_observation",
    label: "Status observation",
    desc: "Track this as an observation; not yet actionable",
  },
  {
    id: "false_positive",
    label: "False positive",
    desc: "AI semantic flagged something that isn't real",
  },
  {
    id: "needs_human_signoff",
    label: "Needs human signoff",
    desc: "Flag for follow-up review by a human",
  },
];

const PRIORITIES: { id: "P0" | "P1" | "P2" | "P3"; label: string }[] = [
  { id: "P0", label: "P0 · critical" },
  { id: "P1", label: "P1 · high" },
  { id: "P2", label: "P2 · normal" },
  { id: "P3", label: "P3 · low" },
];

export default function ActionControlPanel({
  open,
  kind,
  target,
  snapshotId,
  aiConfig,
  onClose,
  onSubmitted,
}: Props) {
  // Hooks must run unconditionally — keep above any early return.
  const [enrichTarget, setEnrichTarget] = useState<EnrichTarget>("nodes");
  const [enrichScope, setEnrichScope] = useState<EnrichScope>("selected_node");
  const [enrichMode, setEnrichMode] = useState<EnrichMode>("semanticize");
  const [enrichExec, setEnrichExec] = useState<EnrichExec>("dry_run");
  const [feedbackKind, setFeedbackKind] = useState<string>("graph_correction");
  const [priority, setPriority] = useState<"P0" | "P1" | "P2" | "P3">("P2");
  const [note, setNote] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [showPayload, setShowPayload] = useState<boolean>(false);
  const noteRef = useRef<HTMLTextAreaElement | null>(null);

  // Seed sensible defaults whenever the modal (re-)opens. The seed mirrors
  // the prototype's applyPresetToSemJobModal logic — first, preset overrides
  // (global Action Panel path), then status-based seeds (per-node CTA path).
  useEffect(() => {
    if (!open) return;
    setBusy(false);
    setNote("");
    // Per-target enrich from the FocusCard / drawer goes straight to apply —
    // dry-run only makes sense for the global Action Panel preset flows
    // (full-graph plans where the operator wants a queue tally first).
    setEnrichExec(target?.preset ? "dry_run" : "apply");
    setFeedbackKind("graph_correction");
    setPriority("P2");
    const preset = target?.preset;
    if (preset) {
      switch (preset) {
        case "missing_nodes":
          setEnrichTarget("nodes");
          setEnrichScope("missing");
          setEnrichMode("semanticize");
          break;
        case "missing_edges":
          setEnrichTarget("edges");
          setEnrichScope("missing");
          setEnrichMode("semanticize");
          break;
        case "retry_stale":
          setEnrichTarget("both");
          setEnrichScope("stale");
          setEnrichMode("retry");
          break;
        case "subtree":
          setEnrichTarget("both");
          setEnrichScope("selected_subtree");
          setEnrichMode("semanticize");
          break;
        case "full":
          setEnrichTarget("both");
          setEnrichScope("full");
          setEnrichMode("semanticize");
          break;
        case "global_review":
        case "semantic_health_review":
        case "global_arch_review":
          setEnrichTarget("both");
          setEnrichScope("full");
          setEnrichMode("review");
          break;
        case "review_subtree":
          setEnrichTarget("both");
          setEnrichScope("selected_subtree");
          setEnrichMode("review");
          break;
      }
    } else if (target?.edge) {
      setEnrichTarget("edges");
      setEnrichScope("selected_node");
      setEnrichMode(target.forceMode ?? "semanticize");
    } else if (target?.node) {
      const status = classifyNode(target.node);
      setEnrichTarget("nodes");
      setEnrichScope("selected_node");
      if (target.forceMode) {
        setEnrichMode(target.forceMode);
      } else if (status === "semantic_stale" || status === "semantic_hash_unverified") {
        setEnrichMode("retry");
      } else {
        setEnrichMode("semanticize");
      }
    }
    window.setTimeout(() => noteRef.current?.focus(), 60);
  }, [open, target]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Build the payload reactively so it's available for both submit + the
  // collapsible JSON preview pane.
  const semanticJobPayload = useMemo<SemanticJobPayload | null>(() => {
    if (kind !== "enrich" || !target) return null;
    const t = target;
    const targetId = t.edge
      ? `${t.edge.src}|${t.edge.dst}|${t.edge.type}`
      : t.node?.node_id ?? "";
    const target_ids: string[] = [];
    if ((enrichScope === "selected_node" || enrichScope === "selected_subtree") && targetId) {
      target_ids.push(targetId);
    }
    // Routing rules (server.py:6966 handle_graph_governance_snapshot_semantic_jobs_create):
    // - target_scope=edge → goes to _semantic_jobs_edge_targets (events table).
    //   When no target_ids are given, requires `all_eligible: true` via body
    //   or options/selector to pull every eligible edge from the snapshot.
    // - target_scope=node|subtree|snapshot → goes to the node-semantic path.
    //   options.target=edges is NOT honored here, so bulk edge enrichment
    //   MUST take the target_scope=edge branch above.
    const isEdgeJob = enrichTarget === "edges" || !!t.edge;
    const target_scope = isEdgeJob
      ? "edge"
      : enrichScope === "selected_subtree"
        ? "subtree"
        : enrichScope === "selected_node"
          ? "node"
          : "snapshot";
    const options: SemanticJobPayload["options"] = {
      target: enrichTarget,
      include_nodes: enrichTarget !== "edges",
      include_edges: enrichTarget !== "nodes",
      scope: enrichScope,
      mode: enrichMode,
      dry_run: enrichExec === "dry_run",
      skip_current: enrichScope !== "full",
      retry_stale_failed: enrichMode === "retry" || enrichScope === "stale",
      include_package_markers: false,
    };
    // For bulk edge jobs (target_scope=edge with no specific target_ids) the
    // backend's _semantic_jobs_edge_targets reads all_eligible / edge_types /
    // include_contains / limit out of options or selector. Surface them so the
    // dashboard preset cards actually enqueue edge work.
    if (isEdgeJob && target_ids.length === 0) {
      options.all_eligible = true;
      options.include_contains = false;
      options.limit = 1000;
    }
    return {
      job_type: enrichMode === "review" ? "global_review" : "semantic_enrichment",
      target_scope,
      target_ids,
      options,
      created_by: "dashboard_user",
    };
  }, [kind, target, enrichTarget, enrichScope, enrichMode, enrichExec]);

  const feedbackPayload = useMemo<FeedbackSubmitPayload | null>(() => {
    if (kind !== "feedback" || !target) return null;
    const t = target;
    const isEdge = !!t.edge;
    const target_id = isEdge ? `${t.edge!.src}|${t.edge!.dst}|${t.edge!.type}` : t.node?.node_id ?? "";
    const source_node_ids = isEdge ? [t.edge!.src, t.edge!.dst] : t.node ? [t.node.node_id] : [];
    return {
      feedback_kind: feedbackKind,
      summary: note,
      source_node_ids,
      target_id,
      target_type: isEdge ? "edge" : "node",
      priority,
      paths: t.node?.primary_files ?? [],
      reason: "dashboard.action_panel",
      create_graph_event: feedbackKind === "graph_correction",
      actor: "dashboard_user",
      source_round: "user",
    };
  }, [kind, target, feedbackKind, priority, note]);

  if (!open || !target) return null;

  const tEdge = target.edge ?? null;
  const tNode = target.node ?? null;
  const isEdge = tEdge != null;
  const targetTitle = tEdge
    ? `${shortTitle(tEdge.src)} → ${shortTitle(tEdge.dst)}`
    : shortTitle((tNode && (tNode.title || tNode.node_id)) ?? "");
  const targetMonoId = tEdge
    ? `${tEdge.src} ${tEdge.type} ${tEdge.dst}`
    : tNode?.node_id ?? "";
  const status: SemanticStatus | null = tNode ? classifyNode(tNode) : null;
  const previewPayload = kind === "enrich" ? semanticJobPayload : feedbackPayload;
  const aiReadiness = semanticAiReadiness(aiConfig);
  const isLiveAiApply = kind === "enrich" && enrichExec === "apply";

  async function dispatch() {
    if (!snapshotId) {
      onSubmitted("No active snapshot — refresh and try again.", "error");
      return;
    }
    if (isLiveAiApply && !aiReadiness.ready) {
      onSubmitted(aiReadiness.blockMessage, "error");
      return;
    }
    setBusy(true);
    try {
      if (kind === "enrich" && semanticJobPayload) {
        const res = await api.submitSemanticJob(snapshotId, semanticJobPayload);
        const verb = enrichExec === "dry_run" ? "queued (dry run)" : "queued";
        onSubmitted(
          `AI enrich ${verb} · job_id=${res.job_id ?? "—"} · queued ${res.queued_count ?? 0}`,
          "success",
        );
      } else if (kind === "feedback" && feedbackPayload) {
        if (feedbackPayload.summary.trim().length === 0) {
          onSubmitted("Note is required for feedback.", "error");
          setBusy(false);
          return;
        }
        const res = await api.submitFeedback(snapshotId, feedbackPayload);
        // Backend currently returns { feedback: {...}, event: {...} } (201).
        // Older builds returned { items: [...] } — fall through for resilience.
        const fb = res.feedback ?? res.items?.[0];
        const eventId = res.event?.event_id;
        onSubmitted(
          `Feedback recorded · feedback_id=${fb?.feedback_id ?? "—"}${eventId ? ` · event=${eventId}` : ""} · kind=${fb?.feedback_kind ?? feedbackKind}`,
          "success",
        );
      }
      onClose();
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.message} · ${err.body.slice(0, 200)}`
          : (err as Error).message;
      onSubmitted(`Submit failed: ${msg}`, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="action-panel-backdrop" onClick={onClose}>
      <div
        className={`action-panel kind-${kind}`}
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="action-panel-head">
          <span className={`action-panel-kind kind-${kind}`}>
            {kind === "enrich"
              ? target?.forceMode === "retry"
                ? "↻ Retry AI enrich"
                : "⚡ AI enrich"
              : "Feedback"}
          </span>
          <button className="btn-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        <div className="action-panel-target">
          <div className="action-panel-target-title">{targetTitle}</div>
          <div className="mono action-panel-target-id">{targetMonoId}</div>
          {status ? (
            <div className="action-panel-target-status">
              <span>current state:</span> <strong>{semStatusLabel(status)}</strong>
            </div>
          ) : null}
          {isEdge && tEdge?.evidence ? (
            <div className="action-panel-target-evidence">
              <span>evidence:</span> {tEdge.evidence}
            </div>
          ) : null}
        </div>

        <div className="action-panel-body">
          {kind === "enrich" ? (
            <>
              {/* Simple per-target enrich (FocusCard / drawer CTA, no preset)
                  hides the radio knobs: target type is derived from the
                  selected target, scope=this target only, mode is set by
                  forceMode or inferred from status, exec=apply. Operator
                  just types a note and clicks Apply. The full radio form
                  is only shown for preset-driven flows (global Action
                  Panel "Run scope reconcile" / "Run global review" etc.). */}
              {target?.preset ? (
                <>
                  <RadioGroup
                    legend="Target"
                    name="enrich-target"
                    value={enrichTarget}
                    onChange={(v) => setEnrichTarget(v as EnrichTarget)}
                    options={ENRICH_TARGETS}
                  />
                  <RadioGroup
                    legend="Scope"
                    name="enrich-scope"
                    value={enrichScope}
                    onChange={(v) => setEnrichScope(v as EnrichScope)}
                    options={ENRICH_SCOPES}
                  />
                  <RadioGroup
                    legend="Mode"
                    name="enrich-mode"
                    value={enrichMode}
                    onChange={(v) => setEnrichMode(v as EnrichMode)}
                    options={ENRICH_MODES}
                  />
                  <RadioGroup
                    legend="Execution"
                    name="enrich-exec"
                    value={enrichExec}
                    onChange={(v) => setEnrichExec(v as EnrichExec)}
                    options={ENRICH_EXEC}
                  />
                </>
              ) : null}
              <div className={`action-ai-readiness ${aiReadiness.ready ? "ok" : "blocked"}`}>
                {aiReadiness.ready
                  ? `Live AI route: ${aiReadiness.routeLabel}`
                  : `${aiReadiness.blockMessage} Use AI config before applying live jobs.`}
              </div>
              <fieldset className="action-fieldset">
                <legend>
                  Note <span className="action-optional">optional</span>
                </legend>
                <textarea
                  ref={noteRef}
                  className="action-textarea"
                  rows={3}
                  placeholder="Why now? Anything the AI should know — drift hint, recent refactor, etc."
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </fieldset>
            </>
          ) : (
            <>
              <RadioGroup
                legend="Feedback kind"
                name="feedback-kind"
                value={feedbackKind}
                onChange={setFeedbackKind}
                options={FEEDBACK_KINDS.map((k) => ({
                  id: k.id,
                  label: k.label,
                  desc: k.createsGraphEvent
                    ? `${k.desc} (will create a graph event)`
                    : k.desc,
                }))}
              />
              <fieldset className="action-fieldset">
                <legend>Priority</legend>
                <div className="action-priority-row">
                  {PRIORITIES.map((p) => (
                    <label
                      key={p.id}
                      className={`action-priority-chip${priority === p.id ? " on" : ""}`}
                    >
                      <input
                        type="radio"
                        name="priority"
                        value={p.id}
                        checked={priority === p.id}
                        onChange={() => setPriority(p.id)}
                      />
                      {p.label}
                    </label>
                  ))}
                </div>
              </fieldset>
              <fieldset className="action-fieldset">
                <legend>
                  Note <span className="action-required">required</span>
                </legend>
                <textarea
                  ref={noteRef}
                  className="action-textarea"
                  rows={4}
                  placeholder="What's wrong, or what's the proposed action? Be specific — this lands in the backlog."
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </fieldset>
            </>
          )}

          <details
            className="action-payload-preview"
            open={showPayload}
            onToggle={(e) => setShowPayload((e.target as HTMLDetailsElement).open)}
          >
            <summary>Request payload preview</summary>
            <pre className="mono">{JSON.stringify(previewPayload, null, 2)}</pre>
          </details>
        </div>

        <footer className="action-panel-foot">
          <button className="action-btn" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className={`action-btn-primary kind-${kind}`}
            onClick={dispatch}
            disabled={busy || (kind === "feedback" && note.trim().length === 0) || (isLiveAiApply && !aiReadiness.ready)}
          >
            {busy
              ? "Submitting…"
              : kind === "enrich"
                ? enrichExec === "dry_run"
                  ? "⚡ Dry-run enrich"
                  : target?.forceMode === "retry"
                    ? "↻ Apply retry"
                    : "⚡ Apply enrich"
                : "Submit feedback"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function RadioGroup<T extends string>({
  legend,
  name,
  value,
  onChange,
  options,
}: {
  legend: string;
  name: string;
  value: T;
  onChange(v: T): void;
  options: { id: T; label: string; desc: string }[];
}) {
  return (
    <fieldset className="action-fieldset">
      <legend>{legend}</legend>
      {options.map((s) => (
        <label key={s.id} className="action-radio-row">
          <input
            type="radio"
            name={name}
            value={s.id}
            checked={value === s.id}
            onChange={() => onChange(s.id)}
          />
          <div>
            <div className="action-radio-label">{s.label}</div>
            <div className="action-radio-desc">{s.desc}</div>
          </div>
        </label>
      ))}
    </fieldset>
  );
}

function shortTitle(t: string): string {
  const parts = t.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : t;
}

function semanticAiReadiness(config?: AiConfigResponse | null): {
  ready: boolean;
  routeLabel: string;
  blockMessage: string;
} {
  const route = config?.project_config?.ai?.routing?.semantic;
  const provider = (route?.provider || "").trim();
  const model = (route?.model || "").trim();
  if (!provider || !model) {
    return {
      ready: false,
      routeLabel: "unset",
      blockMessage: "AI enrich blocked: configure this project's semantic provider/model in AI config first.",
    };
  }
  const tool = config?.tool_health?.[provider];
  if (!tool) {
    return {
      ready: false,
      routeLabel: `${provider}/${model}`,
      blockMessage: `AI enrich blocked: no local CLI requirement is registered for provider ${provider}.`,
    };
  }
  if (tool.status !== "detected") {
    return {
      ready: false,
      routeLabel: `${provider}/${model}`,
      blockMessage:
        `AI enrich blocked: ${tool.runtime || provider} is ${tool.status || "not detected"}. ` +
        `Install/configure ${tool.command || provider} or choose another provider.`,
    };
  }
  return { ready: true, routeLabel: `${provider}/${model}`, blockMessage: "" };
}
