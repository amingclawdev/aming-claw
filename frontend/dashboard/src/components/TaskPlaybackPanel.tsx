import { Fragment, useEffect, useRef, useState } from "react";

import type { TaskPlaybackFrame, TaskPlaybackTrace, PlaybackNavEntry, TaskPlaybackEvidenceRef, ReferenceCategory } from "../lib/taskPlayback";
import { displayPlaybackFrames, latestPlaybackFrameId, pushPlaybackNavStack, popPlaybackNavStack, truncateHash, groupEvidenceRefsByCategory } from "../lib/taskPlayback";
import type { TaskTimelineSemanticRelation } from "../lib/taskTimelineSemantics";
import { segmentTextWithStatusChips } from "../lib/taskTimelineSemantics";

type EvidenceRef = TaskPlaybackFrame["evidence_links"][number];
type EvidenceInspectorRow = { label: string; value: string; source?: string };
type FrameStatusFilter = "all" | "blocked_failed";
type FrameDateFilter = "all" | string;
type GraphTraceLookupState = {
  loading: boolean;
  rows: EvidenceInspectorRow[];
  error: string;
};

/** Lightweight navigation stack entry for cross-event/backlog jumping. */
type NavStackEntry = PlaybackNavEntry;

interface Props {
  trace: TaskPlaybackTrace;
  selectedFrameId?: string;
  loading?: boolean;
  error?: string;
  onSelectFrame?: (frameId: string) => void;
  compact?: boolean;
  /** When true, the event list renders newest event at the top (Current tab). */
  newestFirst?: boolean;
}

export default function TaskPlaybackPanel({
  trace,
  selectedFrameId,
  loading = false,
  error = "",
  onSelectFrame,
  compact = false,
  newestFirst = false,
}: Props) {
  const [internalSelectedFrameId, setInternalSelectedFrameId] = useState("");
  const [statusFilter, setStatusFilter] = useState<FrameStatusFilter>("all");
  const [laneFilter, setLaneFilter] = useState("all");
  const [eventKindFilter, setEventKindFilter] = useState("all");
  const [eventIdFilter, setEventIdFilter] = useState("");
  const [dateFilter, setDateFilter] = useState<FrameDateFilter>("all");
  // Navigation stack: records the sequence of frame visits so Back works across cross-refs.
  const [navStack, setNavStack] = useState<NavStackEntry[]>([]);

  // Ref for the currently-selected frame list item so we can scroll it into view.
  const selectedFrameItemRef = useRef<HTMLLIElement | null>(null);

  // Follow-latest state: tracks whether the user has manually navigated away from
  // the newest event so we can show a "N new events — jump to latest" affordance.
  const [followMode, setFollowMode] = useState(true);
  const prevFrameCountRef = useRef(0);
  const [newEventCount, setNewEventCount] = useState(0);

  const allFrames = trace.frames;
  const laneOptions = stableStrings(allFrames.map((frame) => frame.lane_id || "unknown"));
  const eventKindOptions = stableStrings(allFrames.map((frame) => frame.event_kind || frame.event_type || "event"));
  const dateOptions = frameDateOptions(allFrames);
  const filteredFrames = filterPlaybackFrames(allFrames, statusFilter, laneFilter, eventKindFilter, eventIdFilter, dateFilter);

  // In newestFirst mode: newest event is frames[frames.length - 1] (oldest-first in
  // the underlying array). The list is displayed reversed; the "latest" is always
  // allFrames[allFrames.length - 1].
  const latestFrameId = latestPlaybackFrameId(allFrames);

  const effectiveSelectedFrameId = selectedFrameId || internalSelectedFrameId;
  const selectedFrame =
    allFrames.find((frame) => frame.id === effectiveSelectedFrameId) ??
    filteredFrames[0] ??
    allFrames[0] ??
    null;
  const selectedFrameKey = selectedFrame?.id ?? "";
  const [advancedEvidenceOpenFrameId, setAdvancedEvidenceOpenFrameId] = useState("");
  const [selectedEvidenceRef, setSelectedEvidenceRef] = useState<EvidenceRef | null>(null);
  const advancedEvidenceOpen = Boolean(selectedFrameKey && advancedEvidenceOpenFrameId === selectedFrameKey);
  const gate = trace.close_gate_summary;
  const selectedEvidenceLinks = selectedFrame?.evidence_links ?? selectedFrame?.evidence_refs ?? [];
  const selectedInspector = selectedFrame?.detail_inspector ?? null;

  // In newestFirst mode: reverse the filtered list so newest events appear at top.
  const displayFrames = displayPlaybackFrames(filteredFrames, newestFirst);
  const groupedFrames = groupFramesByDay(displayFrames);

  const selectFrame = (frameId: string, pushNav?: { fromFrameId: string; fromLabel: string }, fromUser = false) => {
    if (!frameId) return;
    if (pushNav && pushNav.fromFrameId) {
      setNavStack((stack) => pushPlaybackNavStack(stack, { frameId: pushNav.fromFrameId, label: pushNav.fromLabel }));
    }
    onSelectFrame?.(frameId);
    if (!onSelectFrame) setInternalSelectedFrameId(frameId);
    // If user manually selects a frame that isn't the latest, leave follow mode.
    if (newestFirst && fromUser) {
      if (frameId !== latestFrameId) {
        setFollowMode(false);
      } else {
        setFollowMode(true);
        setNewEventCount(0);
      }
    }
  };

  const jumpToLatest = () => {
    if (!latestFrameId) return;
    setFollowMode(true);
    setNewEventCount(0);
    onSelectFrame?.(latestFrameId);
    if (!onSelectFrame) setInternalSelectedFrameId(latestFrameId);
  };

  const navigateBack = () => {
    const { entry, stack } = popPlaybackNavStack(navStack);
    if (!entry) return;
    setNavStack(stack);
    onSelectFrame?.(entry.frameId);
    if (!onSelectFrame) setInternalSelectedFrameId(entry.frameId);
  };

  // Follow-latest effect: when newestFirst is active and follow mode is on,
  // auto-select the newest frame whenever new frames arrive. When follow mode
  // is off, accumulate the count of new frames for the affordance banner.
  useEffect(() => {
    if (!newestFirst) return;
    const prev = prevFrameCountRef.current;
    const current = allFrames.length;
    prevFrameCountRef.current = current;
    if (current <= prev) return; // no new frames
    const added = current - prev;
    if (followMode) {
      // Auto-follow: select the newest frame.
      if (latestFrameId) {
        onSelectFrame?.(latestFrameId);
        if (!onSelectFrame) setInternalSelectedFrameId(latestFrameId);
      }
    } else {
      // User navigated away — accumulate new event count for the affordance.
      setNewEventCount((n) => n + added);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allFrames.length, newestFirst]);

  useEffect(() => {
    setInternalSelectedFrameId("");
    setStatusFilter("all");
    setLaneFilter("all");
    setEventKindFilter("all");
    setEventIdFilter("");
    setDateFilter("all");
    setNavStack([]);
    // Reset follow state when backlog changes.
    if (newestFirst) {
      setFollowMode(true);
      setNewEventCount(0);
      prevFrameCountRef.current = 0;
    }
  }, [trace.backlog_id, newestFirst]);

  useEffect(() => {
    setAdvancedEvidenceOpenFrameId("");
    setSelectedEvidenceRef(null);
    // Do NOT clear navStack here — the user may have just navigated via a relation link
    // and we want Back to work from the new frame.
  }, [selectedFrameKey]);

  // F1 (AC2): scroll the selected frame item into view inside the bounded scroll column
  // whenever the effective selection changes. Uses block:'nearest' so the item only
  // scrolls when it is outside the visible area (no jarring jumps when already visible).
  // Covers: manual select, follow-latest auto-select, jump-to-latest, relation-nav back.
  useEffect(() => {
    if (!effectiveSelectedFrameId) return;
    selectedFrameItemRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [effectiveSelectedFrameId]);

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
          <div className="task-playback-event-column task-playback-main">
            {newestFirst && !followMode && newEventCount > 0 ? (
              <div className="task-playback-new-events-banner" role="status" aria-live="polite">
                <span>{newEventCount} new event{newEventCount === 1 ? "" : "s"}</span>
                <button type="button" className="action-btn task-playback-jump-latest" onClick={jumpToLatest}>
                  Jump to latest
                </button>
              </div>
            ) : null}
            <div className="task-playback-controls" aria-label="Playback event controls">
              <button type="button" className="action-btn" onClick={() => { selectFrame(latestFrameId || filteredFrames[0]?.id || allFrames[allFrames.length - 1]?.id || "", undefined, true); }}>
                Latest
              </button>
              <button type="button" className="action-btn" onClick={() => selectFrame(selectedFrame?.id || filteredFrames[0]?.id || "")}>
                Current
              </button>
              <button
                type="button"
                className={`action-btn ${statusFilter === "blocked_failed" ? "active" : ""}`}
                onClick={() => setStatusFilter((value) => value === "blocked_failed" ? "all" : "blocked_failed")}
              >
                Blocked/failed
              </button>
              <select value={laneFilter} onChange={(event) => setLaneFilter(event.target.value)} aria-label="Filter playback by lane">
                <option value="all">All lanes</option>
                {laneOptions.map((lane) => (
                  <option value={lane} key={lane}>{laneLabel(lane)}</option>
                ))}
              </select>
              <select value={eventKindFilter} onChange={(event) => setEventKindFilter(event.target.value)} aria-label="Filter playback by event kind">
                <option value="all">All event kinds</option>
                {eventKindOptions.map((kind) => (
                  <option value={kind} key={kind}>{kind}</option>
                ))}
              </select>
              <select value={dateFilter} onChange={(event) => setDateFilter(event.target.value)} aria-label="Filter playback by date">
                <option value="all">All dates</option>
                {dateOptions.map((option) => (
                  <option value={option.key} key={option.key}>{option.label}</option>
                ))}
              </select>
              <input
                className="backlog-search"
                value={eventIdFilter}
                onChange={(event) => setEventIdFilter(event.target.value)}
                placeholder="Event id"
                aria-label="Filter playback by event id"
              />
            </div>
            <ol className="task-playback-frame-list">
              {groupedFrames.map((group) => (
                <Fragment key={group.day}>
                  <li className="task-playback-day" aria-label={`Timeline events for ${group.day}`}>
                    <span className="mono">{group.day}</span>
                  </li>
                  {group.frames.map((frame) => (
                    <li key={frame.id} ref={frame.id === selectedFrame?.id ? selectedFrameItemRef : null}>
                      <button
                        type="button"
                        className={frame.id === selectedFrame?.id ? "active" : ""}
                        onClick={() => selectFrame(frame.id, undefined, true)}
                      >
                        <span className={`task-playback-dot status-${frame.status}`} />
                        <div>
                          <strong>{frame.title}</strong>
                          <span>{frame.event_kind}</span>
                        </div>
                        <em>{formatFrameDateTime(frame)}</em>
                      </button>
                    </li>
                  ))}
                </Fragment>
              ))}
              {filteredFrames.length === 0 ? (
                <li>
                  <div className="timeline-empty">No playback events match these controls.</div>
                </li>
              ) : null}
            </ol>
          </div>

          {selectedFrame ? (
            <article className={`task-playback-detail-column task-playback-current status-${selectedFrame.status}`}>
              <div className="task-playback-current-head">
                <div>
                  <span className="mono">frame {selectedFrame.sequence}</span>
                  <h4>{selectedFrame.title}</h4>
                </div>
                <span className={`status-badge ${statusClass(selectedFrame.status)}`}>{selectedFrame.status}</span>
              </div>

              {/* Navigation back button — only visible when the nav stack is non-empty */}
              {navStack.length > 0 ? (
                <div className="task-playback-nav-back">
                  <button type="button" className="action-btn" onClick={navigateBack} aria-label="Go back to previous event">
                    &#8592; Back to {navStack[navStack.length - 1].label}
                  </button>
                </div>
              ) : null}

              {/* 1-3. LAYERED SEMANTIC DETAIL (shared EventSemanticDetail: L1 headline, L2 summary, L3 facts/blockers) */}
              <EventSemanticDetail frame={selectedFrame} />
              {/* Timestamp — kept here since it belongs to the per-frame context, not the shared piece */}
              <div className="task-playback-current-meta task-playback-current-meta--ts">
                <span className="mono">{formatFrameDateTime(selectedFrame)}</span>
              </div>

              {/* 3. MERGED REFERENCES & EVIDENCE — replaces separate Relations + Evidence sections */}
              <ReferencesAndEvidenceSection
                relations={selectedFrame.relation_links ?? []}
                links={selectedEvidenceLinks}
                frames={allFrames}
                onJump={(frameId) => selectFrame(frameId, { fromFrameId: selectedFrame.id, fromLabel: selectedFrame.title }, true)}
                onInspect={setSelectedEvidenceRef}
                frameId={selectedFrame.id}
              />

              {/* 4. AUXILIARY EXPLANATION — collapsed under Advanced by default */}
              <details className="task-playback-narrative-collapse">
                <summary className="task-playback-narrative-summary">Auxiliary explanation / Actor-context narrative</summary>
                <ChipSection title="" values={narrativeValues(selectedFrame)} emphasize />
              </details>

              {/* 5. HASHES / IDS / RAW PAYLOAD — collapsed, IDs selectable but demoted */}
              <AdvancedRawDataDetails
                key={selectedFrame.id}
                inspector={selectedInspector}
                open={advancedEvidenceOpen}
                onOpenChange={(open) => setAdvancedEvidenceOpenFrameId(open ? selectedFrameKey : "")}
              />
              {selectedEvidenceRef ? (
                <EvidenceInspectorModal projectId={trace.project_id} frame={selectedFrame} evidenceRef={selectedEvidenceRef} onClose={() => setSelectedEvidenceRef(null)} />
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

function filterPlaybackFrames(
  frames: TaskPlaybackFrame[],
  statusFilter: FrameStatusFilter,
  laneFilter: string,
  eventKindFilter: string,
  eventIdFilter: string,
  dateFilter: FrameDateFilter,
): TaskPlaybackFrame[] {
  const eventIdQuery = eventIdFilter.trim().toLowerCase();
  return frames.filter((frame) => {
    if (statusFilter === "blocked_failed" && !["blocked", "failed", "missing"].includes(frame.status)) return false;
    if (laneFilter !== "all" && frame.lane_id !== laneFilter) return false;
    if (eventKindFilter !== "all" && frame.event_kind !== eventKindFilter && frame.event_type !== eventKindFilter) return false;
    if (dateFilter !== "all" && frameDateKey(frame) !== dateFilter) return false;
    if (eventIdQuery) {
      const haystack = [frame.id, frame.source_event_id, frame.event_type, frame.event_kind, frame.semantic_entry_id].join(" ").toLowerCase();
      if (!haystack.includes(eventIdQuery)) return false;
    }
    return true;
  });
}

function groupFramesByDay(frames: TaskPlaybackFrame[]): Array<{ day: string; frames: TaskPlaybackFrame[] }> {
  const groups: Array<{ day: string; frames: TaskPlaybackFrame[] }> = [];
  for (const frame of frames) {
    const day = formatFrameDay(frame);
    const current = groups[groups.length - 1];
    if (current?.day === day) current.frames.push(frame);
    else groups.push({ day, frames: [frame] });
  }
  return groups;
}

function frameDateOptions(frames: TaskPlaybackFrame[]): Array<{ key: string; label: string }> {
  const seen = new Set<string>();
  const options: Array<{ key: string; label: string }> = [];
  for (const frame of frames) {
    const key = frameDateKey(frame);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    options.push({ key, label: formatFrameDay(frame) });
  }
  return options;
}

function frameDateKey(frame: TaskPlaybackFrame): string {
  if (!frame.at) return "";
  const date = new Date(frame.at);
  if (Number.isNaN(date.getTime())) return frame.at;
  return date.toISOString().slice(0, 10);
}

function formatFrameDay(frame: TaskPlaybackFrame): string {
  if (!frame.at) return "Recorded date unavailable";
  const date = new Date(frame.at);
  if (Number.isNaN(date.getTime())) return frame.at;
  return date.toLocaleDateString([], { weekday: "short", year: "numeric", month: "long", day: "numeric" });
}

function stableStrings(values: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of values.map((item) => item.trim()).filter(Boolean)) {
    if (seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

function laneLabel(id: string): string {
  if (id === "content_sys") return "content-sys";
  if (id === "gate") return "Close gate";
  if (id === "verification") return "Verification";
  if (id === "worker") return "Bounded worker";
  if (id === "observer") return "Observer";
  return id.replace(/[_-]+/g, " ");
}

/**
 * Renders a text string with governance status words highlighted as inline
 * chips (positive/negative/neutral). Non-status text is emitted as plain text.
 * Whole-word matching only — "surpassed" does not chip "passed".
 */
function StatusWordText({ text }: { text: string }) {
  const segments = segmentTextWithStatusChips(text);
  if (segments.length === 0) return null;
  return (
    <>
      {segments.map((seg, i) =>
        seg.chipClass ? (
          <span key={i} className={`status-word-chip status-word-chip--${seg.chipClass}`}>{seg.text}</span>
        ) : (
          <span key={i}>{seg.text}</span>
        )
      )}
    </>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="task-playback-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ChipSection({ title, values, emphasize = false }: { title: string; values: string[]; emphasize?: boolean }) {
  return (
    <div className="task-playback-chip-section">
      <strong>{title}</strong>
      <div>
        {values.slice(0, 10).map((value) => (
          <span key={value}>{emphasize ? <StatusWordText text={value} /> : value}</span>
        ))}
        {values.length > 10 ? <em>+{values.length - 10}</em> : null}
      </div>
    </div>
  );
}

/**
 * TruncatedHashSpan — renders a hash value in compact form (prefix+4…4) with
 * click-to-copy full value. Falls back to plain text for non-hash strings.
 */
function TruncatedHashSpan({
  value,
  mono = false,
  refTint = false,
}: {
  value: string;
  mono?: boolean;
  refTint?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const short = truncateHash(value ?? "");
  if (!short) return null;
  const isHash = short !== value;
  const className = [
    mono ? "mono" : "",
    refTint ? "ref-tint" : "",
    isHash ? "hash-truncated" : "",
  ].filter(Boolean).join(" ");

  const handleCopy = isHash
    ? () => {
        void navigator.clipboard?.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }
    : undefined;

  return (
    <span
      className={className || undefined}
      title={isHash ? value : undefined}
      onClick={handleCopy}
      role={isHash ? "button" : undefined}
      tabIndex={isHash ? 0 : undefined}
      onKeyDown={isHash ? (e) => { if (e.key === "Enter" || e.key === " ") handleCopy?.(); } : undefined}
      aria-label={isHash ? `Copy full hash: ${value}` : undefined}
      style={isHash ? { cursor: "copy" } : undefined}
    >
      {copied ? "Copied!" : short}
    </span>
  );
}

/** Human-readable heading for each reference category. */
const CATEGORY_LABELS: Record<ReferenceCategory, string> = {
  timeline_events: "Timeline events",
  backlog_and_task: "Backlog & task",
  route_and_prompt: "Route & prompt",
  gate_and_verification: "Gate & verification",
  commit_and_artifact: "Commit & artifact",
  graph_and_trace: "Graph & trace",
};

const CATEGORY_ORDER: ReferenceCategory[] = [
  "timeline_events",
  "backlog_and_task",
  "route_and_prompt",
  "gate_and_verification",
  "commit_and_artifact",
  "graph_and_trace",
];

/**
 * ReferencesAndEvidenceSection — merged section replacing the separate
 * "Event relations & references" + "Evidence links" sections.
 *
 * Refs are grouped into 6 typed categories (IA item C).
 * - event_ref relations jump to the matching frame in-page.
 * - backlog_row relations navigate cross-backlog via the parent view.
 * - Evidence refs grouped by category; evidence items open the inspector modal.
 * - "Copy event refs" button sits top-right of the section header.
 */
export function ReferencesAndEvidenceSection({
  relations,
  links,
  frames,
  onJump,
  onInspect,
  frameId,
}: {
  relations: TaskTimelineSemanticRelation[];
  links: TaskPlaybackEvidenceRef[];
  frames: TaskPlaybackFrame[];
  onJump: (frameId: string) => void;
  onInspect: (ref: TaskPlaybackEvidenceRef) => void;
  frameId: string;
}) {
  const grouped = groupEvidenceRefsByCategory(links);
  const hasRelations = relations.length > 0;
  const hasLinks = links.length > 0;
  if (!hasRelations && !hasLinks) return null;

  const frame = frames.find((f) => f.id === frameId) ?? null;

  const handleCopyEventRefs = () => {
    if (!frame) return;
    void navigator.clipboard?.writeText(eventRefsText(frame));
  };

  return (
    <div className="task-playback-references-section">
      <div className="task-playback-references-header">
        <strong>References &amp; Evidence</strong>
        {frame ? (
          <button
            type="button"
            className="action-btn task-playback-copy-refs-btn"
            onClick={handleCopyEventRefs}
            title="Copy event refs to clipboard"
          >
            Copy event refs
          </button>
        ) : null}
      </div>

      {/* Relation links (from semantic projection: event_ref / backlog_row) */}
      {hasRelations ? (
        <div className="task-playback-references-category">
          <span className="task-playback-references-category-label">Related events</span>
          <dl className="task-playback-relations-list">
            {relations.slice(0, 16).map((rel) => {
              const targetFrame = rel.kind === "event_ref"
                ? frames.find((f) => f.source_event_id === rel.value || f.id === rel.value)
                : null;
              return (
                <div key={`${rel.kind}:${rel.value}`} className="task-playback-relation-row">
                  <dt className="task-playback-relation-label">
                    <span className={`task-playback-relation-kind task-playback-relation-kind--${rel.kind}`}>{rel.label}</span>
                  </dt>
                  <dd className="task-playback-relation-detail">
                    {targetFrame ? (
                      <button
                        type="button"
                        className="task-playback-relation-link"
                        title={`Jump to: ${targetFrame.title}`}
                        onClick={() => onJump(targetFrame.id)}
                        aria-label={`Navigate to ${rel.label}: ${rel.value}`}
                      >
                        <span className="mono">{rel.value}</span>
                        <span className="task-playback-relation-summary">{targetFrame.headline || targetFrame.summary || targetFrame.title}</span>
                      </button>
                    ) : (
                      <span className="task-playback-relation-nonav">
                        <span className="mono">{rel.value}</span>
                        <span className="task-playback-relation-summary">
                          {rel.kind === "backlog_row" ? "Backlog row — open in history selector" : (rel.summary ?? "")}
                        </span>
                      </span>
                    )}
                  </dd>
                </div>
              );
            })}
          </dl>
        </div>
      ) : null}

      {/* Evidence refs grouped by category */}
      {CATEGORY_ORDER.map((cat) => {
        const catRefs = grouped[cat];
        if (catRefs.length === 0) return null;
        return (
          <div key={cat} className="task-playback-references-category">
            <span className="task-playback-references-category-label">{CATEGORY_LABELS[cat]}</span>
            <div className="task-playback-references-category-items">
              {catRefs.slice(0, 12).map((ref) => (
                <button
                  type="button"
                  key={`${ref.kind}:${ref.label}:${ref.value}`}
                  className="task-playback-evidence-ref-btn"
                  onClick={() => onInspect(ref)}
                  title={`Inspect ${ref.kind}: ${ref.value}`}
                  aria-haspopup="dialog"
                >
                  <span className="task-playback-evidence-ref-kind">{ref.kind}</span>
                  <span className="task-playback-evidence-ref-label">{ref.label}</span>
                  <TruncatedHashSpan value={ref.value} mono />
                </button>
              ))}
              {catRefs.length > 12 ? <em className="task-playback-evidence-ref-overflow">+{catRefs.length - 12} more</em> : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/**
 * EventSemanticDetail — shared layered detail component (AC-4).
 *
 * Renders the L1 role-action headline, L2 business-summary block (event type /
 * phase / status / key facts), and L3 blocker diagnosis so the same piece can
 * be consumed both in the activity-frame detail pane (TaskPlaybackPanel) and in
 * the backlog evidence inspector (BacklogView's EvidenceInspector).
 *
 * Hashes / raw payload stay in the caller-owned collapsed section (not here).
 */
export function EventSemanticDetail({
  frame,
}: {
  frame: Pick<
    TaskPlaybackFrame,
    "headline" | "actor" | "lane_id" | "event_type" | "phase" | "source_event_id" | "summary" | "specific_facts" | "failure_diagnosis"
  >;
}) {
  return (
    <>
      {/* L1: Role-action headline */}
      {frame.headline ? (
        <div className="task-playback-headline">
          <span className="task-playback-headline-text">{frame.headline}</span>
          <span className={`task-playback-headline-lane lane-pill-${frame.lane_id}`}>{frame.actor}</span>
        </div>
      ) : null}
      {/* L2: Business-relevant summary block */}
      <div className="task-playback-current-meta">
        <span className="mono ref-tint">{frame.event_type}</span>
        <span className="mono ref-tint">{frame.phase}</span>
        <TruncatedHashSpan value={frame.source_event_id} mono refTint />
      </div>
      <div className="task-playback-chip-section">
        <strong>Event summary</strong>
        <p><StatusWordText text={frame.summary} /></p>
      </div>
      {/* L3: Key facts + blockers */}
      <StructuredFactSection title="Key facts" facts={frame.specific_facts} />
      <StructuredFactSection title="Failure/blocker diagnosis" facts={frame.failure_diagnosis} />
    </>
  );
}

/**
 * Returns true when a fact value looks like a sha256/sha-prefixed hash or
 * a bare 64-hex string.  These should render truncated (click-to-copy) instead
 * of as a raw StatusWordText so long sha256 hashes do not overflow the card.
 * F2 (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611).
 */
function isHashValue(value: string): boolean {
  return /^(sha256:|sha512:|sha1:)?[0-9a-f]{48,}$/i.test(value.trim());
}

function StructuredFactSection({ title, facts }: { title: string; facts: TaskPlaybackFrame["specific_facts"] }) {
  if (facts.length === 0) return null;
  return (
    <div className="task-playback-chip-section">
      <strong>{title}</strong>
      <div>
        {facts.slice(0, 10).map((fact) => (
          <span key={`${fact.label}:${fact.value}`}>
            {fact.label}:{" "}
            {isHashValue(fact.value)
              ? <TruncatedHashSpan value={fact.value} mono />
              : <StatusWordText text={fact.value} />}
          </span>
        ))}
        {facts.length > 10 ? <em>+{facts.length - 10}</em> : null}
      </div>
    </div>
  );
}

function EvidenceInspectorModal({
  projectId,
  frame,
  evidenceRef,
  onClose,
}: {
  projectId: string;
  frame: TaskPlaybackFrame;
  evidenceRef: EvidenceRef;
  onClose: () => void;
}) {
  const contextRows = evidenceContextRows(frame, evidenceRef);
  const diagnosisRows = frame.failure_diagnosis.map((fact) => ({ label: fact.label, value: fact.value, source: fact.source }));
  const detailRows = evidenceDetailRows(frame, evidenceRef);
  const rawSections = evidenceRawSections(frame, evidenceRef);
  const graphTraceLookup = useGraphTraceLookup(projectId, frame, evidenceRef);
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
        {evidenceRef.kind === "graph_trace" ? <EvidenceRowSection title="Persisted graph trace" rows={graphTraceLookup.rows} /> : null}
        <EvidenceRowSection title="Failure/blocker diagnosis" rows={diagnosisRows} />
        <EvidenceRowSection title="Sanitized detail rows" rows={detailRows} />
        <EvidenceRawSection sections={rawSections} />
        <div className="task-playback-evidence-actions">
          <button type="button" onClick={() => copyEvidenceLink(evidenceRef)}>
            Copy evidence ref
          </button>
          <span>Raw payload, verification, and artifact_refs_json are inspectable here. Advanced raw data stays collapsed in the event panel.</span>
        </div>
      </section>
    </div>
  );
}

function useGraphTraceLookup(projectId: string, frame: TaskPlaybackFrame, evidenceRef: EvidenceRef): GraphTraceLookupState {
  const [state, setState] = useState<GraphTraceLookupState>({ loading: false, rows: [], error: "" });

  useEffect(() => {
    if (evidenceRef.kind !== "graph_trace") {
      setState({ loading: false, rows: [], error: "" });
      return undefined;
    }
    const traceId = evidenceRef.value.trim();
    const sourceRows = graphTraceSourceRows(frame, evidenceRef);
    if (!traceId || !projectId) {
      setState({
        loading: false,
        error: "missing trace id",
        rows: [
          ...sourceRows,
          { label: "graph trace unavailable", value: "trace id or project id missing; source event refs are the only verifiable evidence", source: "graph_trace" },
        ],
      });
      return undefined;
    }

    let cancelled = false;
    setState({
      loading: true,
      error: "",
      rows: [
        ...sourceRows,
        { label: "Graph trace persisted lookup", value: `loading ${traceId}`, source: "graph_trace" },
      ],
    });

    fetch(`/api/graph-governance/${encodeURIComponent(projectId)}/query-traces/${encodeURIComponent(traceId)}`, {
      headers: { Accept: "application/json" },
    })
      .then(async (response) => {
        const text = await response.text();
        let payload: unknown = {};
        try {
          payload = text ? JSON.parse(text) : {};
        } catch {
          payload = {};
        }
        if (!response.ok) {
          throw new Error(`GET query trace ${traceId} -> ${response.status}${text ? ` ${text.slice(0, 180)}` : ""}`);
        }
        return payload;
      })
      .then((payload) => {
        if (cancelled) return;
        setState({ loading: false, error: "", rows: graphTraceRowsFromPayload(payload, frame, evidenceRef) });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState({
          loading: false,
          error: String(error),
          rows: [
            ...sourceRows,
            { label: "graph trace unavailable", value: String(error), source: "graph_trace" },
            { label: "query args", value: "not persisted/unavailable from the trace API response in this view", source: "graph_trace" },
            { label: "resolved graph nodes/files", value: "not persisted/unavailable; inspect the source trace or event refs listed above", source: "graph_trace" },
          ],
        });
      });

    return () => {
      cancelled = true;
    };
  }, [projectId, evidenceRef.kind, evidenceRef.value, frame.source_event_id]);

  return state;
}

function EvidenceRowSection({ title, rows }: { title: string; rows: EvidenceInspectorRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="task-playback-evidence-section">
      <strong>{title}</strong>
      <dl>
        {stableEvidenceRows(rows).slice(0, 32).map((row) => (
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

function EvidenceRawSection({ sections }: { sections: TaskPlaybackFrame["detail_inspector"]["raw_sections"] }) {
  if (sections.length === 0) return null;
  return (
    <details className="backlog-inspector-raw task-playback-evidence-section">
      <summary>Raw event data</summary>
      <div className="backlog-inspector-json">
        {sections.map((section) => (
          <div key={section.label}>
            <span>{section.redacted ? `${section.label} field-level redactions` : section.label}</span>
            <pre>{formatInspectorValue(section.value)}</pre>
          </div>
        ))}
      </div>
    </details>
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

function copyEvidenceLink(ref: TaskPlaybackFrame["evidence_links"][number]): void {
  void navigator.clipboard?.writeText(`${ref.kind}:${ref.label}=${ref.value}`);
}

function graphTraceRowsFromPayload(payload: unknown, frame: TaskPlaybackFrame, evidenceRef: EvidenceRef): EvidenceInspectorRow[] {
  const trace = recordFromUnknown(recordFromUnknown(payload).trace);
  const events = arrayFromUnknown(trace.events).map(recordFromUnknown);
  const rows: EvidenceInspectorRow[] = [
    ...graphTraceSourceRows(frame, evidenceRef),
    { label: "trace id", value: publicEvidenceText(trace.trace_id) || evidenceRef.value, source: "graph_query_traces" },
    { label: "query source", value: publicEvidenceText(trace.query_source) || "not persisted/unavailable", source: "graph_query_traces" },
    { label: "query purpose", value: publicEvidenceText(trace.query_purpose) || "not persisted/unavailable", source: "graph_query_traces" },
    { label: "trace status", value: publicEvidenceText(trace.status) || "not persisted/unavailable", source: "graph_query_traces" },
    { label: "snapshot", value: publicEvidenceText(trace.snapshot_id) || "not persisted/unavailable", source: "graph_query_traces" },
  ];

  if (events.length === 0) {
    rows.push(
      { label: "tool", value: "not persisted/unavailable; no graph_query_events rows were returned", source: "graph_query_events" },
      { label: "query args", value: "not persisted/unavailable; source trace/event refs are the only verifiable evidence", source: "graph_query_events" },
      { label: "result summary", value: "not persisted/unavailable; source trace/event refs are the only verifiable evidence", source: "graph_query_events" },
      { label: "resolved graph nodes/files", value: "not persisted/unavailable; source trace/event refs are the only verifiable evidence", source: "graph_query_events" },
    );
    return rows;
  }

  for (const event of events.slice(0, 8)) {
    const suffix = publicEvidenceText(event.seq) || String(rows.length);
    rows.push(
      { label: `tool #${suffix}`, value: publicEvidenceText(event.tool) || "not persisted/unavailable", source: "graph_query_events" },
      { label: `query args #${suffix}`, value: graphTraceArgsText(event), source: "graph_query_events" },
      { label: `result summary #${suffix}`, value: graphTraceResultSummaryText(event), source: "graph_query_events" },
      { label: `resolved graph nodes/files #${suffix}`, value: graphTraceResolvedText(event), source: "graph_query_events" },
    );
  }
  return rows;
}

function graphTraceSourceRows(frame: TaskPlaybackFrame, evidenceRef: EvidenceRef): EvidenceInspectorRow[] {
  const refs = stableStrings([
    evidenceRef.value,
    frame.source_event_id,
    ...frame.evidence_refs
      .filter((ref) => ref.kind === "graph_trace" || ref.kind === "timeline_event" || ref.kind === "source_event")
      .map((ref) => `${ref.kind}:${ref.label}:${ref.value}`),
  ]);
  return [
    { label: "source trace/event refs", value: refs.join(", ") || "none", source: "timeline_event" },
  ];
}

function graphTraceArgsText(event: Record<string, unknown>): string {
  const args = recordFromUnknown(event.args);
  const argsHash = publicEvidenceText(event.args_hash);
  if (Object.keys(args).length > 0) {
    const persisted = args.persisted !== false && args.available !== false;
    if (!persisted) return `query args not persisted; args hash ${argsHash || publicEvidenceText(args.args_hash) || "unavailable"}`;
    return publicEvidenceText(args) || `query args not persisted; args hash ${argsHash || "unavailable"}`;
  }
  return argsHash ? `query args not persisted; args hash ${argsHash}` : "query args not persisted/unavailable";
}

function graphTraceResultSummaryText(event: Record<string, unknown>): string {
  const summary = recordFromUnknown(event.result_summary);
  const count = publicEvidenceText(summary.result_count ?? event.result_count);
  const hash = publicEvidenceText(summary.result_hash ?? event.result_hash);
  const duration = publicEvidenceText(summary.duration_ms ?? event.duration_ms);
  const parts = [
    count ? `result_count ${count}` : "",
    hash ? `result_hash ${hash}` : "",
    duration ? `duration_ms ${duration}` : "",
  ].filter(Boolean);
  return parts.length > 0 ? parts.join("; ") : "result summary not persisted/unavailable";
}

function graphTraceResolvedText(event: Record<string, unknown>): string {
  const resolved = recordFromUnknown(event.resolved_graph);
  const nodes = publicEvidenceText(resolved.nodes ?? resolved.node_ids ?? event.resolved_nodes ?? event.nodes);
  const files = publicEvidenceText(resolved.files ?? resolved.file_paths ?? event.resolved_files ?? event.files);
  const persisted = resolved.persisted !== false && Boolean(nodes || files);
  if (persisted) return [nodes ? `nodes ${nodes}` : "", files ? `files ${files}` : ""].filter(Boolean).join("; ");
  return "resolved graph nodes/files not persisted by graph_query_events; inspect the source trace or event refs listed above";
}

function evidenceSummary(evidenceRef: EvidenceRef, frame: TaskPlaybackFrame): string {
  if (isRouteContextOrReadReceiptRef(evidenceRef)) {
    return `This route-context/read-receipt ref is tied to ${frame.title} ${frame.source_event_id}; it shows only the public-safe route or canonical visible contract fields the worker acknowledged, plus explicit body persistence status.`;
  }
  if (evidenceRef.kind === "graph_trace") {
    return `This graph trace ref is tied to ${frame.title} ${frame.source_event_id}; persisted trace identity, tools, result summaries, and resolved node/file details are shown when available, otherwise the unavailable fields are named explicitly.`;
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
  return evidenceContextPersistenceRows(evidenceRef, rows).filter((row) => publicEvidenceText(row.value));
}

function evidenceContextPersistenceRows(evidenceRef: EvidenceRef, rows: EvidenceInspectorRow[]): EvidenceInspectorRow[] {
  const kind = evidenceRef.kind;
  if (!["route_context", "read_receipt", "prompt_contract", "source_event"].includes(kind)) return rows;
  const baseLabels = new Set(["event", "status", "event type", "phase", "summary"]);
  const hasPersistedContext = rows.some((row) => !baseLabels.has(row.label) && publicEvidenceText(row.value));
  const identityVerified = routeIdentityVerified(rows);
  const readReceiptPresent = readReceiptEvidencePresent(rows);
  const next = [
    ...(isRouteContextOrReadReceiptRef(evidenceRef) ? routeContextBoundaryRows(rows) : []),
    ...rows,
    ...(isRouteContextOrReadReceiptRef(evidenceRef) ? [{
      label: "identity verification",
      value: identityVerified
        ? "identity verified; showing best canonical/visible route bundle and read receipt evidence"
        : "identity not verified from persisted public fields",
      source: "route boundary",
    }] : []),
    {
      label: "context persistence",
      value: hasPersistedContext ? "public-safe context persisted on this event" : "public-safe context not persisted on this event",
      source: kind,
    },
  ];
  if (isRouteContextOrReadReceiptRef(evidenceRef) && !rows.some((row) => row.label === "body persisted status")) {
    next.push({
      label: "body persisted status",
      value: "context body unavailable; only public ids, hashes, event refs, and canonical visible contract fields are shown",
      source: "route boundary",
    });
  }
  if (isRouteContextOrReadReceiptRef(evidenceRef) && !readReceiptPresent) {
    next.push({
      label: "read receipt status",
      value: "no mf_subagent_read_receipt exists for this event; only verifiable ids, hashes, and source events are shown",
      source: "route boundary",
    });
  }
  return next;
}

function routeIdentityVerified(rows: EvidenceInspectorRow[]): boolean {
  return rows.some((row) => {
    const label = row.label.toLowerCase();
    const value = row.value.toLowerCase();
    return /route context hash|prompt contract|visible injection|read receipt|route id/.test(label)
      || /^sha256:/.test(value)
      || value.includes("rprompt-")
      || value.includes("route-");
  });
}

function routeContextBoundaryRows(rows: EvidenceInspectorRow[]): EvidenceInspectorRow[] {
  const hasCanonicalContract = rows.some((row) => /canonical visible contract|target files|acceptance criteria|allowed actions|blocked actions|required/i.test(row.label));
  return [
    {
      label: "raw private prompt text",
      value: "hidden and not exposed",
      source: "route boundary",
    },
    {
      label: "public-safe context source",
      value: hasCanonicalContract
        ? "actual persisted public-safe body fields from the canonical visible contract or route/prompt bundle are shown below"
        : "specific context body unavailable; showing persisted public refs and hashes",
      source: "route boundary",
    },
  ];
}

// A preview/static placeholder route id (e.g. "event.route_prompt_context.preview")
// is a source-event preview pointer, not the canonical external route identity.
// Canonical route ids look like "route-…" / "route-repair-…".
function isPreviewRouteIdText(value: string): boolean {
  const normalized = (value || "").trim();
  if (!normalized) return true;
  if (/^route-/i.test(normalized)) return false;
  return /(^|[._])route_prompt_context[._]preview$|^event\.route|(^|[._])preview$/i.test(normalized);
}

function isRouteContextOrReadReceiptRef(ref: EvidenceRef): boolean {
  return ref.kind === "route_context" || ref.kind === "read_receipt" || (ref.kind === "source_event" && /read[-_\s]?receipt/i.test(`${ref.label} ${ref.value}`));
}

function readReceiptEvidencePresent(rows: EvidenceInspectorRow[]): boolean {
  return rows.some((row) => /read[-_\s]?receipt|mf_subagent_read_receipt/i.test(`${row.label} ${row.value}`));
}

function preferredFactKinds(kind: EvidenceRef["kind"]): Set<string> {
  const shared = ["actor", "lane_receiver", "backlog_id", "stage", "work_mode", "work_mode_transition"];
  const routeScope = [
    "route_id",
    "route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "visible_injection_manifest_hash",
    "launch_text_hash",
    "topology",
    "target_file_count",
    "acceptance_criteria_count",
    "required_evidence",
    "required_lanes_evidence",
    "route_alerts",
    "allowed_actions",
    "blocked_actions",
    "source_event_refs",
    "read_receipt_refs",
    "startup_refs",
    "graph_query_schema_trace_id",
    "loaded_skills",
    "loaded_resources",
    "session_token_evidence_type",
    "agent_id_match_mode",
    "surrogate_close_satisfying",
    "blocker_resolution_gate",
    "cross_ref_gate",
    "stale_route_evidence_gate",
  ];
  if (kind === "route_context" || kind === "read_receipt" || kind === "prompt_contract") return new Set([...shared, ...routeScope]);
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
      // Never surface the preview/static placeholder route id as a canonical
      // "route id" inspector row; only canonical route-* identities are shown.
      if (path.endsWith("route_id") && isPreviewRouteIdText(text)) continue;
      rows.push({ label: labelFromPath(path), value: text, source: section.redacted ? `${section.label} redacted` : section.label });
    }
  }
  return rows;
}

function evidenceRawSections(frame: TaskPlaybackFrame, _evidenceRef: EvidenceRef): TaskPlaybackFrame["detail_inspector"]["raw_sections"] {
  return frame.detail_inspector.raw_sections
    .filter((section) => ["payload", "verification", "artifact_refs"].includes(section.label))
    .map((section) => ({
      ...section,
      value: sanitizeModalRawValue(section.value, section.label),
    }));
}

function rawPathsForEvidenceKind(kind: EvidenceRef["kind"]): string[] {
  const identity = [
    "canonical_route_identity.route_id",
    "route_context.canonical_route_identity.route_id",
    "route_id",
    "route_context_hash",
    "route_context.route_id",
    "route_context.route_context_hash",
    "route_identity.route_id",
    "route_identity.route_context_hash",
    "prompt_contract_id",
    "prompt_contract_hash",
    "prompt_contract.prompt_contract_id",
    "prompt_contract.prompt_contract_hash",
    "route_identity.prompt_contract_id",
    "route_identity.prompt_contract_hash",
    "visible_injection_manifest_hash",
    "visible_injection_manifest.hash",
    "route_context.visible_injection_manifest_hash",
    "route_context.visible_bundle.visible_injection_manifest_hash",
    "route_identity.visible_injection_manifest_hash",
    "launch_text_hash",
    "route_context.launch_text_hash",
    "canonical_visible_contract_text_hash",
    "revision_receipt.canonical_visible_contract_text_hash",
    "runtime_context_id",
    "contract_revision_id",
    "runtime_contract_revision_id",
    "observer_command_id",
    "fence_token",
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
    "work_mode",
    "to_work_mode",
    "from_work_mode",
    "default_work_mode",
    "route_context.work_mode",
    "next_legal_action",
    "next_legal_action.action",
    "next_legal_action.detail",
    "graph_query_schema_trace_id",
    "query_schema_trace_id",
    "route_context.graph_query_schema_trace_id",
    "canonical_route_identity.graph_query_schema_trace_id",
    "loaded_skills",
    "loaded_resources",
    "route_context.loaded_skills",
    "route_context.loaded_resources",
    "session_token_evidence_type",
    "mf_subagent_startup_gate.session_token_evidence_type",
    "identity_join.session_token_evidence_type",
    "agent_id_match_mode",
    "mf_subagent_startup_gate.agent_id_match_mode",
    "identity_join.agent_id_match_mode",
    "close_satisfying",
    "counts_as_real_worker_evidence",
    "surrogate_startup_evidence_gate.close_satisfying",
    "blocker_resolution_gate.status",
    "cross_ref_gate.status",
    "stale_route_evidence_gate.status",
    "body_persisted_status",
    "route_context.body_persisted_status",
    "read_receipt.body_persisted_status",
    "raw_launch_text_persisted",
    "route_alerts",
    "route_alert_codes",
    "alerts",
    "route_context.route_alerts",
    "route_context.alerts",
    "stage",
    "selected_topology",
    "recommended_topology",
    "topology",
    "target_files",
    "owned_files",
    "prompt_contract.target_files",
    "route_context.target_files",
    "acceptance_criteria",
    "prompt_contract.acceptance_criteria",
    "route_context.acceptance_criteria",
    "allowed_actions",
    "acknowledged_allowed_actions",
    "route_context.allowed_actions",
    "route_context.visible_bundle.allowed_actions",
    "visible_bundle.allowed_actions",
    "blocked_actions",
    "acknowledged_forbidden_actions",
    "forbidden_actions",
    "route_context.blocked_actions",
    "route_context.visible_bundle.blocked_actions",
    "visible_bundle.blocked_actions",
    "required_evidence",
    "required_lanes_evidence",
    "route_context.required_lanes_evidence",
    "route_context.visible_bundle.required_lanes_evidence",
    "visible_bundle.required_lanes_evidence",
    "evidence_required",
    "required_output",
    "visible_injection_manifest.refs",
    "visible_injection_manifest.allowed_injections",
    "visible_injection_manifest.source_ref_hashes",
    "route_context.visible_injection_manifest.refs",
    "route_context.visible_injection_manifest.allowed_injections",
    "route_context.visible_injection_manifest.source_ref_hashes",
    "route_context.route_docs",
    "route_context.source_label",
    "route_docs",
    "source_label",
    "visible_injection_refs",
    "visible_injection_manifest_hash",
    ...sourceRefs,
  ];
  const graphTrace = [
    "trace_id",
    "graph_trace_id",
    "graph_query_trace_id",
    "graph_trace_ids",
    "graph_query_trace_ids",
    "graph_query_trace.trace_id",
    "graph_query_trace.query_source",
    "graph_query_trace.query_purpose",
    "graph_query_trace.tool",
    "graph_query_trace.args",
    "graph_query_trace.result_summary",
    "graph_query_trace.resolved_nodes",
    "graph_query_trace.resolved_files",
    "graph_query_audit.trace_id",
    "graph_query_audit.query_source",
    "graph_query_audit.query_purpose",
    "graph_query_audit.tool",
    "graph_query_audit.args",
    "graph_query_audit.result_summary",
    "graph_query_context.resolved_nodes",
    "graph_query_context.resolved_files",
    "graph_query_context.path_bindings",
    "graph_query_context.nodes",
    "graph_query_context.files",
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
  if (kind === "route_context" || kind === "read_receipt" || kind === "prompt_contract") return [...identity, ...scope, ...blockers];
  if (kind === "graph_trace") return [...graphTrace, ...identity, ...sourceRefs];
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
  if (kind === "read_receipt") return ["read", "receipt", "route", "target", "acceptance", "allowed", "blocked", "evidence", "hash"];
  if (kind === "prompt_contract") return ["prompt", "contract", "target", "acceptance", "evidence", "stage"];
  if (kind === "graph_trace") return ["graph", "trace", "query", "tool", "args", "result", "node", "file"];
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

function recordFromUnknown(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function arrayFromUnknown(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function publicEvidenceText(value: unknown, path = ""): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) {
    const values = value.map((item, index) => publicEvidenceText(item, `${path}.${index}`)).filter(Boolean);
    if (values.length === 0) return "";
    const shown = values.slice(0, 6).join(", ");
    return values.length > 6 ? `${shown}, +${values.length - 6}` : shown;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const values = Object.entries(record)
      .filter(([key]) => !isPrivateEvidenceField(path ? `${path}.${key}` : key))
      .map(([key, item]) => {
        const text = publicEvidenceText(item, path ? `${path}.${key}` : key);
        return text ? `${labelFromPath(key)}: ${text}` : "";
      })
      .filter(Boolean);
    if (values.length === 0) return "";
    const shown = values.slice(0, 4).join("; ");
    return values.length > 4 ? `${shown}; +${values.length - 4}` : shown;
  }
  const text = sanitizeModalText(String(value), path);
  if (!text || text === "[private detail redacted]") return "";
  return text;
}

function isPrivateEvidenceField(key: string): boolean {
  const normalized = key.trim().toLowerCase();
  const leaf = normalized.split(".").pop() ?? normalized;
  if (
    leaf === "route_context"
    || leaf === "route_identity"
    || leaf === "prompt_contract"
    || leaf === "visible_injection_manifest"
    || leaf === "visible_injection_manifest_hash"
    || leaf === "judgment_brain_label"
    || leaf === "route_docs"
    || leaf === "source_label"
    || leaf === "body_persisted_status"
    || /(^|_)(hash|id|ids|refs)$/.test(leaf)
    || leaf.endsWith("_hash")
    || leaf.endsWith("_id")
    || leaf.endsWith("_ids")
    || leaf.endsWith("_refs")
  ) {
    return false;
  }
  return /(^|[._\s-])(raw_prompt|raw_private_prompt_text|private_prompt|prompt_text|prompt_body|prompt_payload|hidden_prompt|hidden_context|system_prompt|developer_prompt|secret|credential|credentials|password|api_key|access_token|refresh_token|auth_token|one_time_auth|filesystem|cwd|worktree_path|host_path|host_paths|host_home|raw_private_context|raw_private_route_body|private_route_context_body|private_body|observer_only_context|unmanifested_prompt_text)([._\s-]|$)|(^|[._\s-])token([._\s-]|$)(?!hash)/i.test(normalized);
}

function sanitizeModalRawValue(value: unknown, path: string): unknown {
  if (value == null || value === "") return value;
  if (isPrivateEvidenceField(path)) return "[private detail redacted]";
  if (Array.isArray(value)) return value.map((item, index) => sanitizeModalRawValue(item, `${path}.${index}`));
  if (typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      sanitizeModalRawValue(item, `${path}.${key}`),
    ]));
  }
  return sanitizeModalText(String(value), path);
}

function sanitizeModalText(value: string, path: string): string {
  const text = value
    .replace(/(^|\s)(\/Users\/[^\s,;]+|\/home\/[^\s,;]+|\/var\/folders\/[^\s,;]+|[A-Za-z]:\\[^\s,;]+)/g, "$1[local path redacted]")
    .replace(/\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{8,}\b/g, "[token redacted]")
    .trim();
  if (isPrivateEvidenceField(path)) return "[private detail redacted]";
  if (/\[fixture private route context body\]|raw private route body|raw private context body|private route context body/i.test(text)) return "[private detail redacted]";
  if (/(system|developer|hidden)[-_\s]?prompt\s*[:=]/i.test(text)) return "[private detail redacted]";
  if (/(one[-_\s]?time[-_\s]?auth|credential|password|api[-_\s]?key|secret)\s*[:=]/i.test(text)) return "[private detail redacted]";
  return text;
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

function formatFrameDateTime(frame: TaskPlaybackFrame): string {
  if (!frame.at) return "recorded";
  const date = new Date(frame.at);
  if (Number.isNaN(date.getTime())) return frame.at;
  return date.toLocaleString([], {
    weekday: "short",
    year: "numeric",
    month: "long",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  });
}

function statusClass(status: string): string {
  const normalized = status.toLowerCase();
  if (["passed", "complete", "ready", "recorded"].some((item) => normalized.includes(item))) return "status-complete";
  if (["blocked", "failed", "missing", "error"].some((item) => normalized.includes(item))) return "status-failed";
  if (["running", "waiting", "pending"].some((item) => normalized.includes(item))) return "status-running";
  return "status-unknown";
}
