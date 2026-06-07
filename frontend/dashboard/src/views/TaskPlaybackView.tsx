import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import {
  emptyTaskPlaybackTrace,
  fallbackTaskPlaybackSampleTrace,
  normalizeTaskPlaybackTrace,
  type TaskPlaybackTrace,
} from "../lib/taskPlayback";
import TaskPlaybackPanel from "../components/TaskPlaybackPanel";
import type { BacklogBug, BacklogResponse, BacklogTimelineGateResponse, TaskTimelineResponse } from "../types";

interface Props {
  backlog: BacklogResponse;
  projectId: string;
}

type StatusFilter = "open" | "fixed" | "all";
type GateFilter = "all" | "gate_candidate" | "timeline_loaded" | "blocked_gate" | "no_timeline";

const PLAYBACK_BACKLOG_PARAM = "playback_backlog";
const PLAYBACK_TIMELINE_LIMIT = 250;
const CLOSED_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED", "MERGED", "SUPERSEDED"]);

interface PlaybackLoadState {
  loading: boolean;
  loaded: boolean;
  error: string;
  trace: TaskPlaybackTrace;
  taskTimeline?: TaskTimelineResponse | null;
  gate?: BacklogTimelineGateResponse | null;
}

export default function TaskPlaybackView({ backlog, projectId }: Props) {
  const bugs = backlog.bugs ?? [];
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("open");
  const [gateFilter, setGateFilter] = useState<GateFilter>("all");
  const [selectedBugId, setSelectedBugId] = useState(() => readSelectedBacklogId());
  const [playbackByBug, setPlaybackByBug] = useState<Record<string, PlaybackLoadState>>({});
  const [selectedFrameId, setSelectedFrameId] = useState<string>("");
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const playbackByBugRef = useRef<Record<string, PlaybackLoadState>>({});
  const selectedBugRef = useRef<BacklogBug | null>(null);
  const mountedRef = useRef(true);
  const activeProjectIdRef = useRef(projectId);
  const inFlightPlaybackKeysRef = useRef<Set<string>>(new Set());
  const playbackControllersRef = useRef<Map<string, AbortController>>(new Map());

  useEffect(() => {
    playbackByBugRef.current = playbackByBug;
  }, [playbackByBug]);

  useEffect(() => {
    activeProjectIdRef.current = projectId;
    playbackControllersRef.current.forEach((controller) => controller.abort());
    playbackControllersRef.current.clear();
    inFlightPlaybackKeysRef.current.clear();
    playbackByBugRef.current = {};
    setPlaybackByBug({});
    setSelectedBugId(readSelectedBacklogId());
    setSelectedFrameId("");
    setPlaying(false);
  }, [projectId]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      playbackControllersRef.current.forEach((controller) => controller.abort());
      playbackControllersRef.current.clear();
      inFlightPlaybackKeysRef.current.clear();
    };
  }, []);

  const selectedBug = useMemo(() => {
    if (!selectedBugId) return null;
    return bugs.find((bug) => bug.bug_id === selectedBugId) ?? null;
  }, [bugs, selectedBugId]);

  useEffect(() => {
    selectedBugRef.current = selectedBug;
  }, [selectedBug]);

  const fallbackTrace = useMemo(() => fallbackTaskPlaybackSampleTrace(projectId), [projectId]);
  const selectedState = selectedBugId ? playbackByBug[selectedBugId] : undefined;
  const activeTrace = selectedState?.trace ?? (selectedBug ? emptyTaskPlaybackTrace(projectId, selectedBug) : fallbackTrace);
  const activeFrameId = selectedFrameId || activeTrace.frames[0]?.id || "";
  const selectedLoadBugId = selectedBug?.bug_id || "";

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return bugs
      .filter((bug) => {
        if (statusFilter === "open" && !isOpenBug(bug)) return false;
        if (statusFilter === "fixed" && isOpenBug(bug)) return false;
        if (!matchesGateFilter(gateFilter, bug, playbackByBug[bug.bug_id])) return false;
        if (!q) return true;
        const hay = [
          bug.bug_id,
          bug.title,
          bug.status,
          bug.priority,
          bug.runtime_state,
          bug.chain_stage,
          bug.mf_type,
          ...(Array.isArray(bug.target_files) ? bug.target_files : [bug.target_files ?? ""]),
          ...(Array.isArray(bug.acceptance_criteria) ? bug.acceptance_criteria : [bug.acceptance_criteria ?? ""]),
        ].join(" ").toLowerCase();
        return hay.includes(q);
      })
      .slice()
      .sort(compareBacklogRows);
  }, [bugs, gateFilter, playbackByBug, query, statusFilter]);

  useEffect(() => {
    const bug = selectedBugRef.current;
    if (!selectedLoadBugId || !bug || bug.bug_id !== selectedLoadBugId) return;
    const bugId = selectedLoadBugId;
    const requestKey = `${projectId}:${bugId}`;
    const currentState = playbackByBugRef.current[bugId];
    if (currentState?.loaded || currentState?.loading || inFlightPlaybackKeysRef.current.has(requestKey)) return;

    const controller = new AbortController();
    inFlightPlaybackKeysRef.current.add(requestKey);
    playbackControllersRef.current.set(requestKey, controller);
    setPlaybackByBug((states) => ({
      ...states,
      [bugId]: {
        loading: true,
        loaded: false,
        error: "",
        trace: emptyTaskPlaybackTrace(projectId, bug),
      },
    }));

    Promise.allSettled([
      api.taskTimelineFor(projectId, bugId, PLAYBACK_TIMELINE_LIMIT, controller.signal),
      api.backlogTimelineGateFor(projectId, bugId, PLAYBACK_TIMELINE_LIMIT, controller.signal),
    ])
      .then(([timelineResult, gateResult]) => {
        if (controller.signal.aborted || !mountedRef.current || activeProjectIdRef.current !== projectId) return;
        const taskTimeline = timelineResult.status === "fulfilled" ? timelineResult.value : null;
        const gate = gateResult.status === "fulfilled" ? gateResult.value : null;
        const errors = [
          timelineResult.status === "rejected" ? errorMessage(timelineResult.reason) : "",
          gateResult.status === "rejected" ? errorMessage(gateResult.reason) : "",
        ].filter(Boolean);
        const trace = normalizeTaskPlaybackTrace({
          projectId,
          backlog: bug,
          taskTimeline,
          gateResponse: gate,
          source: taskTimeline && gate ? "governed" : "governed_partial",
        });
        setPlaybackByBug((states) => ({
          ...states,
          [bugId]: {
            loading: false,
            loaded: true,
            error: errors.join(" | "),
            trace,
            taskTimeline,
            gate,
          },
        }));
        if (selectedBugRef.current?.bug_id === bugId) {
          setSelectedFrameId((current) => current || trace.frames[0]?.id || "");
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || !mountedRef.current || activeProjectIdRef.current !== projectId) return;
        const trace = emptyTaskPlaybackTrace(projectId, bug);
        setPlaybackByBug((states) => ({
          ...states,
          [bugId]: {
            loading: false,
            loaded: true,
            error: errorMessage(error),
            trace,
            taskTimeline: null,
            gate: null,
          },
        }));
      })
      .finally(() => {
        inFlightPlaybackKeysRef.current.delete(requestKey);
        playbackControllersRef.current.delete(requestKey);
      });
  }, [projectId, selectedLoadBugId]);

  useEffect(() => {
    if (!playing || activeTrace.frames.length <= 1) return undefined;
    const delay = Math.max(500, 1700 / speed);
    const timer = window.setInterval(() => {
      setSelectedFrameId((current) => {
        const currentIndex = Math.max(0, activeTrace.frames.findIndex((frame) => frame.id === current));
        const next = currentIndex + 1;
        if (next >= activeTrace.frames.length) {
          setPlaying(false);
          return activeTrace.frames[currentIndex]?.id || activeTrace.frames[0]?.id || "";
        }
        return activeTrace.frames[next].id;
      });
    }, delay);
    return () => window.clearInterval(timer);
  }, [activeTrace.frames, playing, speed]);

  const selectBug = (bugId: string) => {
    setSelectedBugId(bugId);
    setSelectedFrameId("");
    setPlaying(false);
    writeSelectedBacklogId(bugId);
  };

  const resetPlayback = () => {
    setPlaying(false);
    setSelectedFrameId(activeTrace.frames[0]?.id || "");
  };

  return (
    <div className="view task-playback-view">
      <div className="view-header">
        <div>
          <h2>Task Playback</h2>
          <p>Replay governed Aming Claw/content-sys task status, timeline lanes, evidence refs, and close-gate state.</p>
        </div>
        <div className="view-header-actions">
          <span className="mono">{projectId}</span>
          <span className={`status-badge ${activeTrace.statuses.has_governed_data ? "status-complete" : "status-unknown"}`}>
            {activeTrace.source === "fallback_sample" ? "sample fallback" : "governed data"}
          </span>
        </div>
      </div>

      <div className="task-playback-layout">
        <aside className="task-playback-selector" aria-label="Backlog playback selector">
          <div className="task-playback-selector-head">
            <strong>Backlog selector</strong>
            <span className="mono">{rows.length} / {bugs.length}</span>
          </div>
          <input
            className="backlog-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search backlog, status, files..."
          />
          <div className="task-playback-filters">
            <SegmentedButton<StatusFilter>
              value={statusFilter}
              options={[
                ["open", "Open"],
                ["fixed", "Fixed"],
                ["all", "All"],
              ]}
              onChange={setStatusFilter}
            />
            <select value={gateFilter} onChange={(event) => setGateFilter(event.target.value as GateFilter)} aria-label="Timeline and gate filter">
              <option value="all">All timeline states</option>
              <option value="gate_candidate">Gate candidates</option>
              <option value="timeline_loaded">Timeline loaded</option>
              <option value="blocked_gate">Blocked gate</option>
              <option value="no_timeline">No timeline loaded</option>
            </select>
          </div>
          {rows.length === 0 ? (
            <div className="timeline-empty">No backlog rows match these playback filters.</div>
          ) : (
            <div className="task-playback-row-list">
              {rows.map((bug) => {
                const state = playbackByBug[bug.bug_id];
                return (
                  <button
                    type="button"
                    key={bug.bug_id}
                    className={bug.bug_id === selectedBugId ? "active" : ""}
                    onClick={() => selectBug(bug.bug_id)}
                  >
                    <div>
                      <strong>{bug.title || bug.bug_id}</strong>
                      <span className="mono">{bug.bug_id}</span>
                    </div>
                    <span className={`status-badge ${statusClass(bug.status)}`}>{normalizeStatus(bug.status)}</span>
                    <em>{playbackRowMeta(bug, state)}</em>
                  </button>
                );
              })}
            </div>
          )}
        </aside>

        <div className="task-playback-main">
          <div className="task-playback-controls">
            <button type="button" className="action-btn" onClick={() => setPlaying((value) => !value)} disabled={activeTrace.frames.length <= 1}>
              {playing ? "Pause" : "Play"}
            </button>
            <button type="button" className="action-btn" onClick={resetPlayback} disabled={activeTrace.frames.length === 0}>
              Reset
            </button>
            <label>
              Speed
              <input
                type="range"
                min="1"
                max="4"
                step="1"
                value={speed}
                onChange={(event) => setSpeed(Number(event.target.value))}
              />
            </label>
            {selectedBug ? (
              <span className="mono">{selectedBug.bug_id}</span>
            ) : (
              <span className="mono">Select a backlog row to fetch governed timeline APIs</span>
            )}
          </div>
          <TaskPlaybackPanel
            trace={activeTrace}
            selectedFrameId={activeFrameId}
            loading={selectedState?.loading ?? false}
            error={selectedState?.error ?? ""}
            onSelectFrame={(frameId) => {
              setSelectedFrameId(frameId);
              setPlaying(false);
            }}
          />
        </div>
      </div>
    </div>
  );
}

function SegmentedButton<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: Array<[T, string]>;
  onChange: (value: T) => void;
}) {
  return (
    <div className="task-playback-segmented">
      {options.map(([id, label]) => (
        <button type="button" key={id} className={value === id ? "active" : ""} onClick={() => onChange(id)}>
          {label}
        </button>
      ))}
    </div>
  );
}

function matchesGateFilter(filter: GateFilter, bug: BacklogBug, state?: PlaybackLoadState): boolean {
  if (filter === "all") return true;
  if (filter === "gate_candidate") {
    const text = [bug.runtime_state, bug.chain_stage, bug.mf_type, bug.contract_summary?.template_id].join(" ").toLowerCase();
    return Boolean(bug.contract_summary?.has_contract || /manual|mf|worker|gate|parallel|review/.test(text));
  }
  if (filter === "timeline_loaded") return Boolean(state?.loaded && state.trace.frames.length > 0);
  if (filter === "blocked_gate") return Boolean(state?.trace.close_gate_summary.blocked);
  if (filter === "no_timeline") return Boolean(state?.loaded && state.trace.frames.length === 0);
  return true;
}

function playbackRowMeta(bug: BacklogBug, state?: PlaybackLoadState): string {
  if (state?.loading) return "loading timeline";
  if (state?.trace.close_gate_summary.blocked) return "blocked gate";
  if (state?.loaded) return `${state.trace.frames.length} frame${state.trace.frames.length === 1 ? "" : "s"}`;
  if (bug.contract_summary?.has_contract) return "gate candidate";
  return bug.runtime_state || bug.chain_stage || "not loaded";
}

function readSelectedBacklogId(): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(PLAYBACK_BACKLOG_PARAM)?.trim() || "";
}

function writeSelectedBacklogId(backlogId: string): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set(PLAYBACK_BACKLOG_PARAM, backlogId);
  window.history.replaceState({ playback_backlog: backlogId }, "", `${url.pathname}${url.search}${url.hash}`);
}

function isOpenBug(bug: BacklogBug): boolean {
  return !CLOSED_STATUSES.has(normalizeStatus(bug.status));
}

function normalizeStatus(status: string): string {
  return (status || "UNKNOWN").trim().toUpperCase();
}

function priorityWeight(priority: string): number {
  return { P0: 0, P1: 1, P2: 2, P3: 3 }[priority.toUpperCase()] ?? 9;
}

function compareBacklogRows(a: BacklogBug, b: BacklogBug): number {
  const priority = priorityWeight(a.priority) - priorityWeight(b.priority);
  if (priority !== 0) return priority;
  return Date.parse(b.updated_at || b.created_at || "") - Date.parse(a.updated_at || a.created_at || "");
}

function statusClass(status: string): string {
  const normalized = status.toLowerCase();
  if (["fixed", "closed", "done", "resolved", "passed", "complete"].some((item) => normalized.includes(item))) return "status-complete";
  if (["blocked", "failed", "missing", "error"].some((item) => normalized.includes(item))) return "status-failed";
  if (["progress", "running", "claimed", "open"].some((item) => normalized.includes(item))) return "status-running";
  return "status-unknown";
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.message} ${error.body}`.trim();
  return String(error);
}
