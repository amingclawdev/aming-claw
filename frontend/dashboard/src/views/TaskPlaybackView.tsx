import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { useEventStream } from "../lib/sse";
import {
  emptyTaskPlaybackTrace,
  fallbackTaskPlaybackSampleTrace,
  isPrivatePlaybackText,
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
type ActivityMode = "activity" | "history";

const PLAYBACK_BACKLOG_PARAM = "playback_backlog";
const ACTIVITY_TAB_PARAM = "activity_tab";
const PLAYBACK_TIMELINE_LIMIT = 250;
const ACTIVITY_TIMELINE_LIMIT = 250;
const CURRENT_TASK_REFRESH_MS = 5000;
const DIRECT_API = (import.meta.env.VITE_DIRECT_API as string | undefined) === "true";
const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL as string | undefined) || "http://localhost:40000";
const CLOSED_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED", "MERGED", "SUPERSEDED"]);

interface PlaybackLoadState {
  loading: boolean;
  loaded: boolean;
  error: string;
  trace: TaskPlaybackTrace;
  taskTimeline?: TaskTimelineResponse | null;
  gate?: BacklogTimelineGateResponse | null;
}

interface CurrentTaskHint {
  ok?: boolean;
  active: boolean;
  source: string;
  project_id: string;
  backlog_id: string;
  task_id?: string;
  bug?: BacklogBug | null;
  active_backlog?: BacklogBug[];
  active_count?: number;
  latest_event?: Record<string, unknown>;
  single_active_task?: Record<string, unknown>;
  governance_policy?: Record<string, unknown>;
}

interface ActivityLoadState extends PlaybackLoadState {
  bug?: BacklogBug;
  refreshedAt?: string;
}

export default function TaskPlaybackView({ backlog, projectId }: Props) {
  const bugs = backlog.bugs ?? [];
  const publicBugs = useMemo(() => bugs.filter((bug) => !isPrivatePlaybackBacklog(bug)), [bugs]);
  const [mode, setMode] = useState<ActivityMode>(() => readActivityMode());
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("open");
  const [gateFilter, setGateFilter] = useState<GateFilter>("all");
  const [selectedBugId, setSelectedBugId] = useState(() => readSelectedBacklogId());
  const [playbackByBug, setPlaybackByBug] = useState<Record<string, PlaybackLoadState>>({});
  const [activityByBug, setActivityByBug] = useState<Record<string, ActivityLoadState>>({});
  const [currentTaskHint, setCurrentTaskHint] = useState<CurrentTaskHint | null>(null);
  const [activityRefreshSeq, setActivityRefreshSeq] = useState(0);
  const [selectedFrameId, setSelectedFrameId] = useState<string>("");
  const [selectedActivityFrameId, setSelectedActivityFrameId] = useState<string>("");
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const playbackByBugRef = useRef<Record<string, PlaybackLoadState>>({});
  const activityByBugRef = useRef<Record<string, ActivityLoadState>>({});
  const selectedBugRef = useRef<BacklogBug | null>(null);
  const activityMountedRef = useRef(true);
  const mountedRef = useRef(true);
  const activeProjectIdRef = useRef(projectId);
  const inFlightPlaybackKeysRef = useRef<Set<string>>(new Set());
  const playbackControllersRef = useRef<Map<string, AbortController>>(new Map());

  useEffect(() => {
    playbackByBugRef.current = playbackByBug;
  }, [playbackByBug]);

  useEffect(() => {
    activityByBugRef.current = activityByBug;
  }, [activityByBug]);

  useEffect(() => {
    activeProjectIdRef.current = projectId;
    playbackControllersRef.current.forEach((controller) => controller.abort());
    playbackControllersRef.current.clear();
    inFlightPlaybackKeysRef.current.clear();
    playbackByBugRef.current = {};
    activityByBugRef.current = {};
    setPlaybackByBug({});
    setActivityByBug({});
    setCurrentTaskHint(null);
    setActivityRefreshSeq(0);
    setSelectedBugId(readSelectedBacklogId());
    setSelectedFrameId("");
    setSelectedActivityFrameId("");
    setPlaying(false);
  }, [projectId]);

  useEffect(() => {
    mountedRef.current = true;
    activityMountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activityMountedRef.current = false;
      playbackControllersRef.current.forEach((controller) => controller.abort());
      playbackControllersRef.current.clear();
      inFlightPlaybackKeysRef.current.clear();
    };
  }, []);

  const selectedBug = useMemo(() => {
    if (!selectedBugId) return null;
    return publicBugs.find((bug) => bug.bug_id === selectedBugId) ?? null;
  }, [publicBugs, selectedBugId]);

  useEffect(() => {
    selectedBugRef.current = selectedBug;
  }, [selectedBug]);

  useEffect(() => {
    const handlePopState = () => {
      setSelectedBugId(readSelectedBacklogId());
      setMode(readActivityMode());
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const fallbackTrace = useMemo(() => fallbackTaskPlaybackSampleTrace(projectId), [projectId]);
  const activityPlaceholderBug = useMemo<BacklogBug>(() => ({
    bug_id: "current-activity",
    title: "Current activity",
    status: "UNKNOWN",
    priority: "P3",
  }), []);
  const selectedState = selectedBugId ? playbackByBug[selectedBugId] : undefined;
  const activeTrace = selectedState?.trace ?? (selectedBug ? emptyTaskPlaybackTrace(projectId, selectedBug) : fallbackTrace);
  const activeFrameId = selectedFrameId || activeTrace.frames[0]?.id || "";
  const selectedLoadBugId = selectedBug?.bug_id || "";
  const hintedCurrentBug = currentTaskHint?.active && currentTaskHint.bug && !isPrivatePlaybackBacklog(currentTaskHint.bug)
    ? currentTaskHint.bug
    : null;
  const activityBug = selectedBug ?? hintedCurrentBug;
  const activityState = activityBug ? activityByBug[activityBug.bug_id] : undefined;
  const activityTrace = activityState?.trace ?? emptyTaskPlaybackTrace(projectId, activityBug ?? activityPlaceholderBug);
  const activityFrameId = selectedActivityFrameId || activityTrace.frames[0]?.id || "";

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return publicBugs
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
  }, [publicBugs, gateFilter, playbackByBug, query, statusFilter]);

  const refreshCurrentTaskHint = useCallback((signal: AbortSignal) => {
    return currentTaskHintFor(projectId, signal)
      .then((hint) => {
        if (signal.aborted || !activityMountedRef.current || activeProjectIdRef.current !== projectId) return;
        setCurrentTaskHint(hint);
      })
      .catch(() => {
        if (!signal.aborted && activityMountedRef.current && activeProjectIdRef.current === projectId) {
          setCurrentTaskHint(null);
        }
      });
  }, [projectId]);

  const refreshActivityTimeline = useCallback((bug: BacklogBug, showLoading: boolean, signal: AbortSignal) => {
    const bugId = bug.bug_id;
    if (showLoading) {
      setActivityByBug((states) => ({
        ...states,
        [bugId]: {
          loading: true,
          loaded: states[bugId]?.loaded ?? false,
          error: "",
          trace: states[bugId]?.trace ?? emptyTaskPlaybackTrace(projectId, bug),
          bug: states[bugId]?.bug ?? bug,
          taskTimeline: states[bugId]?.taskTimeline,
          gate: states[bugId]?.gate,
          refreshedAt: states[bugId]?.refreshedAt,
        },
      }));
    }

    return Promise.allSettled([
      api.backlogBugFor(projectId, bugId, signal),
      api.taskTimelineFor(projectId, bugId, ACTIVITY_TIMELINE_LIMIT, signal),
      api.backlogTimelineGateFor(projectId, bugId, ACTIVITY_TIMELINE_LIMIT, signal),
    ]).then(([detailResult, timelineResult, gateResult]) => {
      if (signal.aborted || !activityMountedRef.current || activeProjectIdRef.current !== projectId) return;
      const detailBug = detailResult.status === "fulfilled" ? detailResult.value : bug;
      const taskTimeline = timelineResult.status === "fulfilled" ? timelineResult.value : null;
      const gate = gateResult.status === "fulfilled" ? gateResult.value : null;
      const errors = [
        detailResult.status === "rejected" ? errorMessage(detailResult.reason) : "",
        timelineResult.status === "rejected" ? errorMessage(timelineResult.reason) : "",
        gateResult.status === "rejected" ? errorMessage(gateResult.reason) : "",
      ].filter(Boolean);
      const trace = normalizeTaskPlaybackTrace({
        projectId,
        backlog: detailBug,
        taskTimeline,
        gateResponse: gate,
        source: taskTimeline && gate ? "governed" : "governed_partial",
      });
      setActivityByBug((states) => ({
        ...states,
        [bugId]: {
          loading: false,
          loaded: true,
          error: errors.join(" | "),
          trace,
          bug: detailBug,
          taskTimeline,
          gate,
          refreshedAt: new Date().toISOString(),
        },
      }));
      setSelectedActivityFrameId((current) => current || trace.frames[0]?.id || "");
    });
  }, [projectId]);

  useEventStream(projectId, {
    enabled: Boolean(projectId),
    onEvent: ({ name }) => {
      if (isActivityLiveEvent(name)) setActivityRefreshSeq((seq) => seq + 1);
    },
  });

  useEffect(() => {
    const controller = new AbortController();
    refreshCurrentTaskHint(controller.signal);
    return () => controller.abort();
  }, [activityRefreshSeq, refreshCurrentTaskHint]);

  useEffect(() => {
    if (!activityBug) return undefined;
    const bug = activityBug;
    const controller = new AbortController();
    let refreshing = false;
    const refresh = (showLoading = false) => {
      if (refreshing || controller.signal.aborted) return;
      refreshing = true;
      refreshActivityTimeline(bug, showLoading, controller.signal)
        .catch((error: unknown) => {
          if (controller.signal.aborted || !activityMountedRef.current || activeProjectIdRef.current !== projectId) return;
          const bugId = bug.bug_id;
          setActivityByBug((states) => ({
            ...states,
            [bugId]: {
              loading: false,
              loaded: true,
              error: errorMessage(error),
              trace: states[bugId]?.trace ?? emptyTaskPlaybackTrace(projectId, bug),
              bug: states[bugId]?.bug ?? bug,
              taskTimeline: states[bugId]?.taskTimeline ?? null,
              gate: states[bugId]?.gate ?? null,
              refreshedAt: states[bugId]?.refreshedAt,
            },
          }));
        })
        .finally(() => {
          refreshing = false;
        });
    };

    refresh(!activityByBugRef.current[bug.bug_id]?.loaded);
    const timer = window.setInterval(() => refresh(false), CURRENT_TASK_REFRESH_MS);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [activityBug?.bug_id, activityRefreshSeq, projectId, refreshActivityTimeline]);

  useEffect(() => {
    if (!activityBug) return;
    const state = activityByBug[activityBug.bug_id];
    if (!state?.loaded || state.loading) return;
    const currentFrameExists = Boolean(selectedActivityFrameId && state.trace.frames.some((frame) => frame.id === selectedActivityFrameId));
    if (!selectedActivityFrameId || currentFrameExists) return;
    setSelectedActivityFrameId(state.trace.frames[0]?.id || "");
  }, [activityBug, activityByBug, selectedActivityFrameId]);

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

  const changeMode = (next: ActivityMode) => {
    setMode(next);
    writeActivityMode(next);
  };

  return (
    <div className="view task-playback-view">
      <div className="view-header">
        <div>
          <h2>Activity</h2>
          <p>Current task/runtime event stream with task playback history as a reachable detail.</p>
        </div>
        <div className="view-header-actions">
          <span className="mono">{projectId}</span>
          <span className={`status-badge ${activityTrace.statuses.has_governed_data ? "status-complete" : "status-unknown"}`}>
            {!activityBug ? "no current activity" : activityTrace.source === "fallback_sample" ? "sample fallback" : "governed current stream"}
          </span>
        </div>
      </div>

      <SegmentedButton<ActivityMode>
        value={mode}
        options={[
          ["activity", "Current activity"],
          ["history", "Playback history"],
        ]}
        onChange={changeMode}
      />

      {mode === "activity" ? (
        <div className="task-playback-layout">
          <aside className="task-playback-selector" aria-label="Current activity task selector">
            <div className="task-playback-selector-head">
              <strong>Current/runtime stream</strong>
              <span className="mono">{activityBug?.bug_id || "no task"}</span>
            </div>
            {activityBug ? (
              <button type="button" className="active" onClick={() => setActivityRefreshSeq((seq) => seq + 1)}>
                <div>
                  <strong>{activityBug.title || activityBug.bug_id}</strong>
                  <span className="mono">{activityBug.bug_id}</span>
                </div>
                <span className={`status-badge ${statusClass(activityBug.status)}`}>{normalizeStatus(activityBug.status)}</span>
                <em>{activityState?.loading ? "loading events" : `${activityTrace.frames.length} event${activityTrace.frames.length === 1 ? "" : "s"}`}</em>
              </button>
            ) : (
              <div className="timeline-empty">No actively running observer task or command is recorded for this project.</div>
            )}
            <ActivityStreamSummary hint={currentTaskHint} trace={activityTrace} />
            <div className="task-playback-controls">
              <button type="button" className="action-btn" onClick={() => setActivityRefreshSeq((seq) => seq + 1)}>
                Refresh
              </button>
              <button type="button" className="action-btn" onClick={() => changeMode("history")}>
                Open playback history
              </button>
            </div>
          </aside>

          <div className="task-playback-main">
            <TaskPlaybackPanel
              trace={activityTrace}
              selectedFrameId={activityFrameId}
              loading={activityState?.loading ?? false}
              error={activityState?.error ?? ""}
              onSelectFrame={setSelectedActivityFrameId}
            />
          </div>
        </div>
      ) : (
        <div className="task-playback-layout">
          <aside className="task-playback-selector" aria-label="Backlog playback selector">
            <div className="task-playback-selector-head">
              <strong>Backlog selector</strong>
              <span className="mono">{rows.length} / {publicBugs.length}</span>
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
      )}
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

function ActivityStreamSummary({ hint, trace }: { hint: CurrentTaskHint | null; trace: TaskPlaybackTrace }) {
  const latestFrame = trace.frames[trace.frames.length - 1] ?? null;
  const latestEvent = hint?.latest_event ?? {};
  const latestEventText = compactJoin([
    latestFrame?.event_kind || hintText(latestEvent, "event_kind") || "",
    latestFrame?.event_type || hintText(latestEvent, "event_type") || "",
    latestFrame?.status || hintText(latestEvent, "status") || "",
    latestFrame?.at || hintText(latestEvent, "created_at") || "",
  ]);
  const laneState = compactJoin([
    laneStatusSummary(trace, "worker", "worker"),
    laneStatusSummary(trace, "verification", "QA"),
    laneStatusSummary(trace, "gate", "close gate"),
  ]);
  const nextExpected = trace.close_gate_summary.next_expected_evidence.length > 0
    ? trace.close_gate_summary.next_expected_evidence.join(", ")
    : firstHintValue(latestEvent, ["next_expected_evidence", "missing_event_kinds", "missing_requirement_ids"]) || "none recorded";
  const nextAction = trace.close_gate_summary.next_expected_action
    || firstHintValue(latestEvent, ["next_legal_action", "next_expected_action", "next_action"])
    || "none recorded";
  const blocker = latestFrame?.failure_diagnosis[0]
    ? `${latestFrame.failure_diagnosis[0].label}: ${latestFrame.failure_diagnosis[0].value}`
    : firstHintValue(latestEvent, ["blocker_ids", "blockers", "missing_event_kinds", "missing_requirement_ids"])
      || (trace.close_gate_summary.blocked ? trace.close_gate_summary.reason_sentence : "none recorded");
  const activeCount = hint?.active_count != null ? `${hint.active_count} active` : "";
  const singleActive = hint?.single_active_task ? singleActiveSummary(hint.single_active_task) : "";
  return (
    <div className="task-playback-chip-section" aria-label="Current stream state">
      <strong>Current stream state</strong>
      <div>
        {activeCount ? <span>{activeCount}</span> : null}
        {singleActive ? <span>{singleActive}</span> : null}
        <span>Latest event: {latestEventText || "none recorded"}</span>
        <span>Worker/QA/close gate: {laneState || "none recorded"}</span>
        <span>Next expected evidence: {nextExpected}</span>
        <span>Blocker/next legal action: {blocker}; {nextAction}</span>
      </div>
    </div>
  );
}

function laneStatusSummary(trace: TaskPlaybackTrace, laneId: string, label: string): string {
  const lane = trace.lanes.find((item) => item.id === laneId);
  if (!lane) return "";
  return `${label} ${lane.status} (${lane.frame_count})`;
}

function firstHintValue(record: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = compactUnknown(record[key]);
    if (value) return value;
  }
  return "";
}

function hintText(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function compactUnknown(value: unknown): string {
  if (value == null || value === "") return "";
  if (Array.isArray(value)) return value.map(compactUnknown).filter(Boolean).slice(0, 6).join(", ");
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => {
        const text = compactUnknown(item);
        return text ? `${key}: ${text}` : "";
      })
      .filter(Boolean);
    return entries.slice(0, 4).join("; ");
  }
  return String(value).trim();
}

function compactJoin(values: string[]): string {
  return values.map((value) => value.trim()).filter(Boolean).join(" / ");
}

function singleActiveSummary(value: Record<string, unknown>): string {
  const required = value.required === true ? "single-active required" : "single-active optional";
  const passed = value.passed === false ? "policy blocked" : "policy ok";
  return `${required}; ${passed}`;
}

function dashboardApiBase(): string {
  return DIRECT_API ? BACKEND_URL : "";
}

async function currentTaskHintFor(projectId: string, signal?: AbortSignal): Promise<CurrentTaskHint> {
  const path = `/api/backlog/${encodeURIComponent(projectId)}/current-task?limit=10`;
  const res = await fetch(`${dashboardApiBase()}${path}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, `GET ${path} -> ${res.status}`, text);
  }
  return (await res.json()) as CurrentTaskHint;
}

function isActivityLiveEvent(name: string): boolean {
  return [
    "task_timeline.appended",
    "current_task.changed",
    "task.created",
    "task.completed",
    "task.failed",
    "task.retry",
    "dashboard.changed",
  ].includes(name);
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

function isPrivatePlaybackBacklog(bug: BacklogBug): boolean {
  const fields = [
    bug.bug_id,
    bug.title,
    bug.runtime_state,
    bug.chain_stage,
    bug.mf_type,
    bug.contract_summary?.template_id,
  ];
  return fields.some((field) => isPrivatePlaybackText(String(field || "")));
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

function readActivityMode(): ActivityMode {
  if (typeof window === "undefined") return "activity";
  const value = new URLSearchParams(window.location.search).get(ACTIVITY_TAB_PARAM)?.trim();
  return value === "history" ? "history" : "activity";
}

function writeSelectedBacklogId(backlogId: string): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set(PLAYBACK_BACKLOG_PARAM, backlogId);
  window.history.replaceState({ playback_backlog: backlogId }, "", `${url.pathname}${url.search}${url.hash}`);
}

function writeActivityMode(mode: ActivityMode): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set(ACTIVITY_TAB_PARAM, mode);
  window.history.replaceState({ activity_tab: mode }, "", `${url.pathname}${url.search}${url.hash}`);
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
