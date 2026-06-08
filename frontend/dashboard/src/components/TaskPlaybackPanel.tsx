import { useEffect, useState } from "react";

import { isPrivatePlaybackText } from "../lib/taskPlayback";
import type { TaskPlaybackFrame, TaskPlaybackTrace } from "../lib/taskPlayback";

type EvidenceRef = TaskPlaybackFrame["evidence_links"][number];
type EvidenceInspectorRow = { label: string; value: string; source?: string };

interface Props {
  trace: TaskPlaybackTrace;
  selectedFrameId?: string;
  loading?: boolean;
  error?: string;
  onSelectFrame?: (frameId: string) => void;
  compact?: boolean;
}

export default function TaskPlaybackPanel({
  trace,
  selectedFrameId,
  loading = false,
  error = "",
  onSelectFrame,
  compact = false,
}: Props) {
  const selectedFrame = trace.frames.find((frame) => frame.id === selectedFrameId) ?? trace.frames[0] ?? null;
  const selectedFrameKey = selectedFrame?.id ?? "";
  const [advancedEvidenceOpenFrameId, setAdvancedEvidenceOpenFrameId] = useState("");
  const [selectedEvidenceRef, setSelectedEvidenceRef] = useState<EvidenceRef | null>(null);
  const advancedEvidenceOpen = Boolean(selectedFrameKey && advancedEvidenceOpenFrameId === selectedFrameKey);
  const gate = trace.close_gate_summary;
  const selectedEvidenceLinks = selectedFrame?.evidence_links ?? selectedFrame?.evidence_refs ?? [];
  const selectedInspector = selectedFrame?.detail_inspector ?? null;

  useEffect(() => {
    setAdvancedEvidenceOpenFrameId("");
    setSelectedEvidenceRef(null);
  }, [selectedFrameKey]);

  useEffect(() => {
    if (!selectedEvidenceRef) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelectedEvidenceRef(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selectedEvidenceRef]);

  return (
    <section className={`task-playback-panel${compact ? " compact" : ""}`} aria-label="Task playback trace">
      <div className="task-playback-panel-head">
        <div>
          <span className="task-playback-eyebrow">Aming Claw / content-sys playback</span>
          <h3>{trace.backlog_title || trace.backlog_id}</h3>
        </div>
        <div className="task-playback-head-meta">
          <span className="mono">{trace.schema_version}</span>
          <span className={`status-badge ${statusClass(gate.status)}`}>{gate.label}</span>
        </div>
      </div>

      {loading ? <div className="timeline-empty"><span className="spinner" /> Loading governed timeline data...</div> : null}
      {error ? <div className="timeline-empty timeline-error">Playback load failed: {error}</div> : null}
      {!loading && !error && trace.frames.length === 0 ? (
        <div className="timeline-empty">No governed timeline events are available for this backlog row.</div>
      ) : null}
      {!loading && !error && gate.blocked ? (
        <div className="task-playback-blocked">
          <strong>Blocked close gate</strong>
          {gate.missing_event_kinds.length > 0 ? <span>Missing event kinds: {gate.missing_event_kinds.join(", ")}</span> : null}
          <span>{gate.reason_sentence}</span>
          <em>{gate.next_expected_action}</em>
        </div>
      ) : null}

      <div className="task-playback-metrics">
        <Metric label="Frames" value={String(trace.statuses.total_frames)} />
        <Metric label="Lanes" value={String(trace.lanes.length)} />
        <Metric label="Evidence" value={String(trace.evidence_refs.length)} />
        <Metric label="Artifacts" value={String(trace.artifact_refs.length)} />
      </div>

      {trace.lanes.length > 0 ? (
        <div className="task-playback-lanes" aria-label="Playback lanes">
          {trace.lanes.map((lane) => (
            <div className={`task-playback-lane lane-${lane.family}`} key={lane.id}>
              <div>
                <strong>{lane.label}</strong>
                <span className="mono">{lane.frame_count} frame{lane.frame_count === 1 ? "" : "s"}</span>
              </div>
              <span className={`status-badge ${statusClass(lane.status)}`}>{lane.status}</span>
            </div>
          ))}
        </div>
      ) : null}

      {trace.frames.length > 0 ? (
        <div className="task-playback-body">
          <ol className="task-playback-frame-list">
            {trace.frames.map((frame) => (
              <li key={frame.id}>
                <button
                  type="button"
                  className={frame.id === selectedFrame?.id ? "active" : ""}
                  onClick={() => onSelectFrame?.(frame.id)}
                >
                  <span className={`task-playback-dot status-${frame.status}`} />
                  <div>
                    <strong>{frame.title}</strong>
                    <span>{frame.event_kind}</span>
                  </div>
                  <em>{formatFrameTime(frame)}</em>
                </button>
              </li>
            ))}
          </ol>

          {selectedFrame ? (
            <article className={`task-playback-current status-${selectedFrame.status}`}>
              <div className="task-playback-current-head">
                <div>
                  <span className="mono">frame {selectedFrame.sequence}</span>
                  <h4>{selectedFrame.title}</h4>
                </div>
                <span className={`status-badge ${statusClass(selectedFrame.status)}`}>{selectedFrame.status}</span>
              </div>
              <div className="task-playback-current-meta">
                <span>{selectedFrame.actor}</span>
                <span className="mono">{selectedFrame.event_type}</span>
                <span className="mono">{selectedFrame.phase}</span>
                <span className="mono">{selectedFrame.source_event_id}</span>
              </div>
              <div className="task-playback-chip-section">
                <strong>Event summary</strong>
                <p>{selectedFrame.summary}</p>
              </div>
              <StructuredFactSection title="Specific facts" facts={selectedFrame.specific_facts} />
              <StructuredFactSection title="Failure/blocker diagnosis" facts={selectedFrame.failure_diagnosis} />
              <EvidenceLinkSection links={selectedEvidenceLinks} onInspect={setSelectedEvidenceRef} />
              <ChipSection title="Auxiliary explanation / Actor-context narrative" values={narrativeValues(selectedFrame)} />
              <EventQueryHook frame={selectedFrame} />
              <AdvancedRawDataDetails
                key={selectedFrame.id}
                inspector={selectedInspector}
                open={advancedEvidenceOpen}
                onOpenChange={(open) => setAdvancedEvidenceOpenFrameId(open ? selectedFrameKey : "")}
              />
              {selectedEvidenceRef ? (
                <EvidenceInspectorModal frame={selectedFrame} evidenceRef={selectedEvidenceRef} onClose={() => setSelectedEvidenceRef(null)} />
              ) : null}
            </article>
          ) : null}
        </div>
      ) : null}

      <div className="task-playback-privacy">
        <strong>Public boundary</strong>
        <span>Private request text not displayed</span>
        <span>Private refs redacted</span>
        <span>Aming Claw/content-sys evidence only</span>
      </div>
    </section>
  );
}

function narrativeValues(frame: TaskPlaybackFrame): string[] {
  return [
    `Template detail: ${frame.detail}`,
    `Who acted: ${frame.narrative.actor}`,
    `What changed: ${frame.narrative.information}`,
    `Context: ${frame.narrative.context}`,
    `Why it mattered: ${frame.narrative.purpose}`,
    `Outcome: ${frame.narrative.outcome}`,
  ];
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="task-playback-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ChipSection({ title, values }: { title: string; values: string[] }) {
  return (
    <div className="task-playback-chip-section">
      <strong>{title}</strong>
      <div>
        {values.slice(0, 10).map((value) => (
          <span key={value}>{value}</span>
        ))}
        {values.length > 10 ? <em>+{values.length - 10}</em> : null}
      </div>
    </div>
  );
}

function StructuredFactSection({ title, facts }: { title: string; facts: TaskPlaybackFrame["specific_facts"] }) {
  if (facts.length === 0) return null;
  return <ChipSection title={title} values={facts.map((fact) => `${fact.label}: ${fact.value}`)} />;
}

function EvidenceLinkSection({ links, onInspect }: { links: TaskPlaybackFrame["evidence_links"]; onInspect: (ref: EvidenceRef) => void }) {
  if (links.length === 0) return null;
  return (
    <div className="task-playback-chip-section">
      <strong>Evidence links</strong>
      <div>
        {links.slice(0, 16).map((ref) => (
          <button
            type="button"
            key={`${ref.kind}:${ref.label}:${ref.value}`}
            onClick={() => onInspect(ref)}
            title={`Inspect ${ref.kind}`}
            aria-haspopup="dialog"
          >
            {ref.kind}: {ref.label}: {ref.value}
          </button>
        ))}
        {links.length > 16 ? <em>+{links.length - 16}</em> : null}
      </div>
    </div>
  );
}

function EvidenceInspectorModal({ frame, evidenceRef, onClose }: { frame: TaskPlaybackFrame; evidenceRef: EvidenceRef; onClose: () => void }) {
  const contextRows = evidenceContextRows(frame, evidenceRef);
  const diagnosisRows = frame.failure_diagnosis.map((fact) => ({ label: fact.label, value: fact.value, source: fact.source }));
  const detailRows = evidenceDetailRows(frame, evidenceRef);
  return (
    <div className="task-playback-evidence-modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="task-playback-evidence-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-playback-evidence-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="task-playback-evidence-modal-head">
          <div>
            <span className="task-playback-eyebrow">Public-safe evidence context</span>
            <h5 id="task-playback-evidence-title">{evidenceKindTitle(evidenceRef.kind)}</h5>
          </div>
          <button type="button" className="task-playback-evidence-close" onClick={onClose} aria-label="Close evidence inspector">
            Close
          </button>
        </div>
        <div className="task-playback-evidence-meta">
          <span>{evidenceRef.kind}</span>
          <span>{evidenceRef.label}</span>
          <span className="mono">{evidenceRef.value}</span>
        </div>
        <p className="task-playback-evidence-summary">{evidenceSummary(evidenceRef, frame)}</p>
        <EvidenceRowSection title="Structured context" rows={contextRows} />
        <EvidenceRowSection title="Failure/blocker diagnosis" rows={diagnosisRows} />
        <EvidenceRowSection title="Sanitized detail rows" rows={detailRows} />
        <div className="task-playback-evidence-actions">
          <button type="button" onClick={() => copyEvidenceLink(evidenceRef)}>
            Copy evidence ref
          </button>
          <span>Advanced raw data stays collapsed in the event panel.</span>
        </div>
      </section>
    </div>
  );
}

function EvidenceRowSection({ title, rows }: { title: string; rows: EvidenceInspectorRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="task-playback-evidence-section">
      <strong>{title}</strong>
      <dl>
        {stableEvidenceRows(rows).slice(0, 18).map((row) => (
          <div key={`${row.label}:${row.value}:${row.source ?? ""}`}>
            <dt>{row.label}</dt>
            <dd>
              {row.value}
              {row.source ? <span>{row.source}</span> : null}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function EventQueryHook({ frame }: { frame: TaskPlaybackFrame }) {
  return (
    <div className="task-playback-chip-section task-playback-event-hook">
      <strong>Explain/query this event</strong>
      <div>
        <button type="button" data-event-id={frame.source_event_id} onClick={() => copyEventRefs(frame)}>
          Copy event refs
        </button>
        <span className="mono">event {frame.source_event_id}</span>
        <span className="mono">semantic {frame.semantic_entry_id}</span>
      </div>
    </div>
  );
}

function AdvancedRawDataDetails({
  inspector,
  open,
  onOpenChange,
}: {
  inspector: TaskPlaybackFrame["detail_inspector"] | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const rawSections = inspector?.raw_sections ?? [];
  if (rawSections.length === 0) return null;
  return (
    <details className="backlog-inspector-raw task-playback-inspector" open={open} onToggle={(event) => onOpenChange(event.currentTarget.open)}>
      <summary>Advanced raw data</summary>
      {inspector?.redaction_count ? <ChipSection title="Raw data redactions" values={[String(inspector.redaction_count)]} /> : null}
      <div className="backlog-inspector-json">
        {rawSections.map((section) => (
          <div key={section.label}>
            <span>{section.redacted ? `${section.label} redacted` : section.label}</span>
            <pre>{formatInspectorValue(section.value)}</pre>
          </div>
        ))}
      </div>
    </details>
  );
}

function copyEventRefs(frame: TaskPlaybackFrame): void {
  const text = eventRefsText(frame);
  void navigator.clipboard?.writeText(text);
}

function copyEvidenceLink(ref: TaskPlaybackFrame["evidence_links"][number]): void {
  void navigator.clipboard?.writeText(`${ref.kind}:${ref.label}=${ref.value}`);
}

function evidenceSummary(evidenceRef: EvidenceRef, frame: TaskPlaybackFrame): string {
  if (evidenceRef.kind === "route_context") {
    return `This route-context ref is tied to ${frame.title} ${frame.source_event_id}; the visible context below is assembled from the event summary, structured facts, blocker diagnosis, and sanitized inspector rows.`;
  }
  if (evidenceRef.kind === "prompt_contract") {
    return `This prompt-contract ref is tied to ${frame.title} ${frame.source_event_id}; target scope, acceptance criteria, required evidence, and route identity appear when they were recorded as public facts.`;
  }
  if (["timeline_event", "source_event", "gate"].includes(evidenceRef.kind)) {
    return `This event ref points at ${frame.title} ${frame.source_event_id}; the diagnosis rows show why the event passed, blocked, or needs another legal action.`;
  }
  if (evidenceRef.kind === "commit") {
    return `This commit ref is public evidence for ${frame.title} ${frame.source_event_id}; related facts and artifacts are shown without raw timeline JSON.`;
  }
  return `This ${evidenceRef.kind} ref is public evidence for ${frame.title} ${frame.source_event_id}; raw payload data remains available only in Advanced raw data.`;
}

function evidenceContextRows(frame: TaskPlaybackFrame, evidenceRef: EvidenceRef): EvidenceInspectorRow[] {
  const rows: EvidenceInspectorRow[] = [
    { label: "event", value: `${frame.title} ${frame.source_event_id}` },
    { label: "status", value: frame.status },
    { label: "event type", value: frame.event_type },
    { label: "phase", value: frame.phase },
    { label: "summary", value: frame.summary },
  ];
  const preferredFacts = preferredFactKinds(evidenceRef.kind);
  for (const fact of frame.specific_facts) {
    if (preferredFacts.has(fact.kind) || preferredFacts.has(fact.label)) rows.push({ label: fact.label, value: fact.value, source: fact.source });
  }
  rows.push(...rawPathRowsForEvidence(frame, evidenceRef));
  return evidenceContextPersistenceRows(evidenceRef.kind, rows).filter((row) => publicEvidenceText(row.value));
}

function evidenceContextPersistenceRows(kind: EvidenceRef["kind"], rows: EvidenceInspectorRow[]): EvidenceInspectorRow[] {
  if (!["route_context", "prompt_contract", "source_event"].includes(kind)) return rows;
  const baseLabels = new Set(["event", "status", "event type", "phase", "summary"]);
  const hasPersistedContext = rows.some((row) => !baseLabels.has(row.label) && publicEvidenceText(row.value));
  return [
    ...rows,
    {
      label: "context persistence",
      value: hasPersistedContext ? "public-safe context persisted on this event" : "public-safe context not persisted on this event",
      source: kind,
    },
  ];
}

function preferredFactKinds(kind: EvidenceRef["kind"]): Set<string> {
  const shared = ["actor", "lane_receiver", "backlog_id", "stage"];
  const routeScope = [
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "visible_injection_manifest_hash",
    "topology",
    "target_file_count",
    "acceptance_criteria_count",
    "required_evidence",
    "source_event_refs",
    "read_receipt_refs",
    "startup_refs",
  ];
  if (kind === "route_context" || kind === "prompt_contract") return new Set([...shared, ...routeScope]);
  if (kind === "timeline_event" || kind === "source_event" || kind === "gate") return new Set([...shared, ...routeScope, "decision"]);
  if (kind === "commit" || kind === "artifact" || kind === "file" || kind === "test") return new Set([...shared, "decision", "closed_rows", "implemented_and_merged"]);
  return new Set([...shared, ...routeScope, "decision"]);
}

function rawPathRowsForEvidence(frame: TaskPlaybackFrame, evidenceRef: EvidenceRef): EvidenceInspectorRow[] {
  const rows: EvidenceInspectorRow[] = [];
  const paths = rawPathsForEvidenceKind(evidenceRef.kind);
  for (const section of frame.detail_inspector.raw_sections) {
    const record = section.value && typeof section.value === "object" && !Array.isArray(section.value) ? (section.value as Record<string, unknown>) : null;
    if (!record) continue;
    for (const path of paths) {
      const value = valueAtPath(record, path);
      const text = publicEvidenceText(value);
      if (!text) continue;
      rows.push({ label: labelFromPath(path), value: text, source: section.redacted ? `${section.label} redacted` : section.label });
    }
  }
  return rows;
}

function rawPathsForEvidenceKind(kind: EvidenceRef["kind"]): string[] {
  const identity = [
    "route_id",
    "route_context_hash",
    "route_identity.route_id",
    "route_identity.route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "route_identity.prompt_contract_id",
    "route_identity.prompt_contract_hash",
    "visible_injection_manifest_hash",
    "route_identity.visible_injection_manifest_hash",
  ];
  const sourceRefs = [
    "source_event_id",
    "source_event_ids",
    "source_event_refs",
    "source_event_type",
    "source_events",
    "read_receipt_event_id",
    "read_receipt_event_ids",
    "read_receipt_event_refs",
    "read_receipt_hash",
    "startup_event_id",
    "startup_event_ids",
    "startup_event_refs",
    "startup_intent_event_id",
  ];
  const scope = [
    "stage",
    "selected_topology",
    "recommended_topology",
    "topology",
    "target_files",
    "owned_files",
    "acceptance_criteria",
    "required_evidence",
    "evidence_required",
    ...sourceRefs,
  ];
  const blockers = [
    "blocker_ids",
    "missing_event_kinds",
    "missing_required_evidence",
    "missing_requirement_ids",
    "required_before_protected_evidence",
    "route_identity_mismatch",
    "route_context_gate.missing_requirement_ids",
    "route_context_gate.missing_required_evidence",
    "route_context_gate.route_identity_mismatch",
    "stale_reason",
    "timeout_reason",
    "route_context_stale_reason",
    "route_context_timeout_reason",
    "route_token_expired_reason",
    "next_legal_action",
    "next_action",
    "next_expected_action",
  ];
  if (kind === "route_context" || kind === "prompt_contract") return [...identity, ...scope, ...blockers];
  if (kind === "timeline_event" || kind === "source_event" || kind === "gate" || kind === "precheck") return ["event_id", ...sourceRefs, ...identity, ...blockers];
  if (kind === "commit") return ["commit", "commit_hash", "target_commit", "head_commit", "source_commit", "artifact_refs", ...identity];
  if (kind === "artifact" || kind === "file" || kind === "test") return ["artifact_refs", "artifacts", "files", "changed_files", "tests_run", "test_commands", ...identity];
  return [...identity, ...scope, ...blockers];
}

function evidenceDetailRows(frame: TaskPlaybackFrame, evidenceRef: EvidenceRef): EvidenceInspectorRow[] {
  const terms = detailTermsForEvidence(evidenceRef.kind);
  const rows = frame.detail_inspector.rows
    .filter((row) => {
      const haystack = `${row.kind} ${row.label} ${row.value}`.toLowerCase();
      return terms.some((term) => haystack.includes(term));
    })
    .map((row) => ({ label: row.label, value: row.value, source: row.kind }));
  return rows.filter((row) => publicEvidenceText(row.value));
}

function detailTermsForEvidence(kind: EvidenceRef["kind"]): string[] {
  if (kind === "route_context") return ["route", "topology", "stage", "target", "acceptance", "evidence", "blocker", "missing", "next"];
  if (kind === "prompt_contract") return ["prompt", "contract", "target", "acceptance", "evidence", "stage"];
  if (kind === "timeline_event" || kind === "source_event" || kind === "gate") return ["event", "status", "gate", "blocker", "missing", "next"];
  if (kind === "commit") return ["commit", "artifact", "source"];
  return [kind.replace(/_/g, " "), kind, "evidence", "artifact", "status"];
}

function valueAtPath(record: Record<string, unknown>, path: string): unknown {
  return path.split(".").reduce<unknown>((current, part) => {
    if (!current || typeof current !== "object" || Array.isArray(current)) return undefined;
    return (current as Record<string, unknown>)[part];
  }, record);
}

function publicEvidenceText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) {
    const values = value.map(publicEvidenceText).filter(Boolean);
    if (values.length === 0) return "";
    const shown = values.slice(0, 6).join(", ");
    return values.length > 6 ? `${shown}, +${values.length - 6}` : shown;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const values = Object.entries(record)
      .filter(([key]) => !isPrivateEvidenceField(key))
      .map(([key, item]) => {
        const text = publicEvidenceText(item);
        return text ? `${labelFromPath(key)}: ${text}` : "";
      })
      .filter(Boolean);
    if (values.length === 0) return "";
    const shown = values.slice(0, 4).join("; ");
    return values.length > 4 ? `${shown}; +${values.length - 4}` : shown;
  }
  const text = String(value).replace(/(^|\s)(\/Users\/[^\s,;]+|\/home\/[^\s,;]+|\/var\/folders\/[^\s,;]+|[A-Za-z]:\\[^\s,;]+)/g, "$1[local path redacted]").trim();
  if (!text || isPrivatePlaybackText(text)) return "";
  return text;
}

function isPrivateEvidenceField(key: string): boolean {
  return /(^|[_\s-])(raw_prompt|prompt_text|prompt_body|hidden|private|secret|token|provider|filesystem|cwd|worktree_path|host_path|host_home|route_context)([_\s-]|$)/i.test(key);
}

function stableEvidenceRows(rows: EvidenceInspectorRow[]): EvidenceInspectorRow[] {
  const seen = new Set<string>();
  const result: EvidenceInspectorRow[] = [];
  for (const row of rows) {
    const value = publicEvidenceText(row.value);
    if (!value) continue;
    const key = `${row.label}:${value}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push({ ...row, value });
  }
  return result;
}

function labelFromPath(path: string): string {
  return path.split(".").slice(-1)[0].replace(/[_-]+/g, " ");
}

function evidenceKindTitle(kind: EvidenceRef["kind"]): string {
  return `${kind.replace(/_/g, " ")} evidence`;
}

function eventRefsText(frame: TaskPlaybackFrame): string {
  const refs = [
    `event_id=${frame.source_event_id}`,
    `event_type=${frame.event_type}`,
    `event_kind=${frame.event_kind}`,
    `semantic_entry=${frame.semantic_entry_id}`,
    `summary=${frame.summary}`,
    ...frame.evidence_refs.map((ref) => `${ref.kind}:${ref.label}=${ref.value}`),
  ];
  return refs.join("\n");
}

function formatInspectorValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2);
}

function formatFrameTime(frame: TaskPlaybackFrame): string {
  if (!frame.at) return "recorded";
  const date = new Date(frame.at);
  if (Number.isNaN(date.getTime())) return frame.at;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function statusClass(status: string): string {
  const normalized = status.toLowerCase();
  if (["passed", "complete", "ready", "recorded"].some((item) => normalized.includes(item))) return "status-complete";
  if (["blocked", "failed", "missing", "error"].some((item) => normalized.includes(item))) return "status-failed";
  if (["running", "waiting", "pending"].some((item) => normalized.includes(item))) return "status-running";
  return "status-unknown";
}
