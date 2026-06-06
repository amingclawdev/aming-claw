import type { TaskPlaybackFrame, TaskPlaybackTrace } from "../lib/taskPlayback";

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
  const gate = trace.close_gate_summary;
  const selectedEvidence = selectedFrame?.evidence_refs ?? [];
  const selectedArtifacts = selectedFrame?.artifact_refs ?? [];

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
          <span>
            {gate.missing_event_kinds.length > 0
              ? `Missing event kinds: ${gate.missing_event_kinds.join(", ")}`
              : gate.missing_requirement_count > 0
                ? `${gate.missing_requirement_count} evidence requirement${gate.missing_requirement_count === 1 ? "" : "s"} missing`
                : "Close is not ready yet."}
          </span>
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
                <span className="mono">{selectedFrame.source_event_id}</span>
              </div>
              <p>{selectedFrame.detail}</p>
              {selectedEvidence.length > 0 ? <ChipSection title="Evidence refs" values={selectedEvidence.map((ref) => `${ref.label}: ${ref.value}`)} /> : null}
              {selectedArtifacts.length > 0 ? <ChipSection title="Artifacts" values={selectedArtifacts.map((ref) => `${ref.kind}: ${ref.value}`)} /> : null}
            </article>
          ) : null}
        </div>
      ) : null}

      <div className="task-playback-privacy">
        <strong>Public boundary</strong>
        <span>Private request text not displayed</span>
        <span>Host paths redacted</span>
        <span>Aming Claw/content-sys evidence only</span>
      </div>
    </section>
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
