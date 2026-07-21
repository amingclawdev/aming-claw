import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import {
  useEventStreamWithFreshness,
  sseStatusTone,
  sseStatusLabel,
  type SseFreshnessMeta,
} from "../lib/sse";
import {
  emptyTaskPlaybackTrace,
  fallbackTaskPlaybackSampleTrace,
  isBacklogRowPrivate,
  normalizeTaskPlaybackTrace,
  projectRecentTimelineEvents,
  mergeRecentTimelineEvents,
  recentTimelineEventKey,
  projectEventToCard,
  sliceEventPage,
  buildPlaybackUrl,
  readPlaybackEventParam,
  resolveInitialPlaybackFrameId,
  resolveSelectedFrameIdForEventParam,
  contractRuntimeCompatibilityRepairValues,
  taskPlaybackNextLegalActionPresentations,
  PLAYBACK_URL_PARAMS,
  type TaskPlaybackTrace,
  type ActivityEventCard,
} from "../lib/taskPlayback";
import TaskPlaybackPanel from "../components/TaskPlaybackPanel";
import type { BacklogBug, BacklogResponse, BacklogTimelineGateResponse, TaskTimelineEvent, TaskTimelineResponse } from "../types";

interface Props {
  backlog: BacklogResponse;
  projectId: string;
}

type StatusFilter = "open" | "closed" | "all";
type PriorityFilter = "all" | "P0" | "P1" | "P2" | "P3";
type GateFilter = "all" | "gate_candidate" | "timeline_loaded" | "blocked_gate" | "no_timeline";
type ActivityMode = "activity" | "history";

const PLAYBACK_BACKLOG_PARAM = PLAYBACK_URL_PARAMS.playback_backlog;
const ACTIVITY_TAB_PARAM = PLAYBACK_URL_PARAMS.activity_tab;
const PLAYBACK_TIMELINE_LIMIT = 250;
const ACTIVITY_TIMELINE_LIMIT = 250;
const CURRENT_TASK_REFRESH_MS = 5000;
/** Initial + max limit for the project-wide recent events stream in the Current tab. */
const RECENT_EVENTS_LIMIT = 100;
const PLAYBACK_SEARCH_DEBOUNCE_MS = 300;
const PLAYBACK_SEARCH_PAGE_SIZE = 50;
/** Cards per page for the Current tab event card list (IA item A). */
const EVENTS_PAGE_SIZE = 10;
const DIRECT_API = (import.meta.env.VITE_DIRECT_API as string | undefined) === "true";
const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL as string | undefined) || "http://localhost:40000";
const CLOSED_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED", "MERGED", "SUPERSEDED", "WAIVED"]);
const AUDIT_ARCHIVED_RUNTIME_STATES = new Set(["audit_archived"]);

interface PlaybackLoadState {
  loading: boolean;
  loaded: boolean;
  error: string;
  trace: TaskPlaybackTrace;
  taskTimeline?: TaskTimelineResponse | null;
  gate?: BacklogTimelineGateResponse | null;
  authorityCacheKey?: string;
}

interface BacklogDetailLoadState {
  loading: boolean;
  loaded: boolean;
  error: string;
  bug?: BacklogBug | null;
}

interface CompetingCandidate {
  bug_id: string;
  task_id: string;
  last_evidence_at: string;
  event_count: number;
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
  competing_candidates?: CompetingCandidate[];
}

interface ActivityLoadState extends PlaybackLoadState {
  bug?: BacklogBug;
  refreshedAt?: string;
}

export default function TaskPlaybackView({ backlog, projectId }: Props) {
  const bugs = backlog.bugs ?? [];
  const [mode, setMode] = useState<ActivityMode>(() => readActivityMode());
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("open");
  const [priorityFilter, setPriorityFilter] = useState<PriorityFilter>("all");
  const [gateFilter, setGateFilter] = useState<GateFilter>("all");
  const [searchOffset, setSearchOffset] = useState(0);
  const [serverBacklog, setServerBacklog] = useState<BacklogResponse>(backlog);
  const [timelineSearch, setTimelineSearch] = useState<TaskTimelineResponse | null>(null);
  const [serverSearchLoading, setServerSearchLoading] = useState(false);
  const [serverSearchError, setServerSearchError] = useState("");
  const [selectedBugId, setSelectedBugId] = useState(() => readSelectedBacklogId());
  const [selectedBacklogDetailById, setSelectedBacklogDetailById] = useState<Record<string, BacklogDetailLoadState>>({});
  const [playbackByBug, setPlaybackByBug] = useState<Record<string, PlaybackLoadState>>({});
  const [activityByBug, setActivityByBug] = useState<Record<string, ActivityLoadState>>({});
  const [currentTaskHint, setCurrentTaskHint] = useState<CurrentTaskHint | null>(null);
  // Project-wide recent events for the Current tab event list (newest-first, cross-row).
  // These are plain TaskTimelineEvents; each carries its own backlog_id/task_id.
  const [recentEvents, setRecentEvents] = useState<TaskTimelineEvent[]>([]);
  const [recentEventsLoaded, setRecentEventsLoaded] = useState(false);
  const recentEventIdsRef = useRef<Set<string>>(new Set());
  // Frontend-local override: when multiple candidates compete and the user
  // clicks a competing-candidates selector entry, we rebind the activity view
  // to that bug_id locally (no server mutation).
  const [localActivityBugId, setLocalActivityBugId] = useState<string>("");
  const [activityRefreshSeq, setActivityRefreshSeq] = useState(0);
  const [selectedFrameId, setSelectedFrameId] = useState<string>("");
  // B1: event-id deep-link param — when the user arrives via a card click that
  // included a playback_event param, we hold the raw event-id string here and
  // resolve it to a frame once the trace finishes loading (async race guard).
  const [selectedEventParam, setSelectedEventParamState] = useState<string>(() => readPlaybackEventParam());
  const selectedEventParamRef = useRef(readPlaybackEventParam());
  const setSelectedEventParam = useCallback((value: string) => {
    selectedEventParamRef.current = value;
    setSelectedEventParamState(value);
  }, []);
  const [selectedActivityFrameId, setSelectedActivityFrameId] = useState<string>("");
  // Current tab event card pager state (IA item A — 10 cards/page).
  const [eventsPage, setEventsPage] = useState(0);
  // F6 (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611):
  // Track the count of new events that arrived while the user was on page>0
  // so we can show a "new events — back to latest" affordance on the pager.
  // Cleared when the user navigates back to page 0.
  const prevRecentEventsCountRef = useRef(0);
  const [pendingNewEventsCount, setPendingNewEventsCount] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const playbackByBugRef = useRef<Record<string, PlaybackLoadState>>({});
  const authorityTraceCacheRef = useRef<Record<string, TaskPlaybackTrace>>({});
  const activityByBugRef = useRef<Record<string, ActivityLoadState>>({});
  const selectedBacklogDetailByIdRef = useRef<Record<string, BacklogDetailLoadState>>({});
  const selectedBugRef = useRef<BacklogBug | null>(null);
  const activityMountedRef = useRef(true);
  const mountedRef = useRef(true);
  const activeProjectIdRef = useRef(projectId);
  const inFlightPlaybackKeysRef = useRef<Set<string>>(new Set());
  const playbackControllersRef = useRef<Map<string, AbortController>>(new Map());
  // Stable ref so refreshActivityTimeline can call recordPoll without a
  // forward-reference issue (useEventStreamWithFreshness is declared later).
  const recordPollRef = useRef<(at: string) => void>(() => undefined);

  const timelineSearchBugs = useMemo<BacklogBug[]>(() => (
    (timelineSearch?.events ?? [])
      .map((event) => event.backlog)
      .filter((bug): bug is NonNullable<TaskTimelineEvent["backlog"]> => Boolean(bug?.bug_id))
      .map((bug) => ({
        bug_id: bug.bug_id,
        title: bug.title || bug.bug_id,
        status: bug.status || "UNKNOWN",
        priority: bug.priority || "P3",
        commit: bug.commit,
        public_safe: true,
        compact: true,
      }))
  ), [timelineSearch]);
  const selectorBugs = useMemo(
    () => mergePublicBacklogRows(serverBacklog.bugs ?? [], timelineSearchBugs),
    [serverBacklog.bugs, timelineSearchBugs],
  );
  const publicBugs = useMemo(
    () => mergePublicBacklogRows(bugs, selectorBugs),
    [bugs, selectorBugs],
  );

  useEffect(() => {
    playbackByBugRef.current = playbackByBug;
  }, [playbackByBug]);

  useEffect(() => {
    activityByBugRef.current = activityByBug;
  }, [activityByBug]);

  useEffect(() => {
    selectedBacklogDetailByIdRef.current = selectedBacklogDetailById;
  }, [selectedBacklogDetailById]);

  useEffect(() => {
    activeProjectIdRef.current = projectId;
    playbackControllersRef.current.forEach((controller) => controller.abort());
    playbackControllersRef.current.clear();
    inFlightPlaybackKeysRef.current.clear();
    playbackByBugRef.current = {};
    authorityTraceCacheRef.current = {};
    activityByBugRef.current = {};
    selectedBacklogDetailByIdRef.current = {};
    setPlaybackByBug({});
    setActivityByBug({});
    setSelectedBacklogDetailById({});
    setCurrentTaskHint(null);
    setServerBacklog(backlog);
    setTimelineSearch(null);
    setServerSearchLoading(false);
    setServerSearchError("");
    setSearchOffset(0);
    setLocalActivityBugId("");
    setActivityRefreshSeq(0);
    setSelectedBugId(readSelectedBacklogId());
    setSelectedFrameId("");
    setSelectedEventParam(readPlaybackEventParam());
    setSelectedActivityFrameId("");
    setEventsPage(0);
    setPlaying(false);
    setRecentEvents([]);
    setRecentEventsLoaded(false);
    recentEventIdsRef.current = new Set();
  }, [projectId]);

  useEffect(() => {
    setSearchOffset(0);
  }, [priorityFilter, query, statusFilter]);

  useEffect(() => {
    if (mode !== "history") return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setServerSearchLoading(true);
      setServerSearchError("");
      const backlogRequest = api.backlogSearchFor(projectId, {
        q: query,
        status: statusFilter.toUpperCase(),
        priority: priorityFilter,
        limit: PLAYBACK_SEARCH_PAGE_SIZE,
        offset: searchOffset,
        include_closed: true,
      }, controller.signal);
      const timelineRequest = query.trim()
        ? api.taskTimelineSearchFor(projectId, {
          q: query,
          backlog_status: statusFilter.toUpperCase(),
          priority: priorityFilter,
          limit: PLAYBACK_SEARCH_PAGE_SIZE,
          offset: searchOffset,
          scan_limit: 5000,
        }, controller.signal)
        : Promise.resolve(null);
      Promise.all([backlogRequest, timelineRequest])
        .then(([backlogResponse, timelineResponse]) => {
          if (controller.signal.aborted) return;
          setServerBacklog(backlogResponse);
          setTimelineSearch(timelineResponse);
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) setServerSearchError(errorMessage(error));
        })
        .finally(() => {
          if (!controller.signal.aborted) setServerSearchLoading(false);
        });
    }, PLAYBACK_SEARCH_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [mode, priorityFilter, projectId, query, searchOffset, statusFilter]);

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

  const cachedSelectedBug = useMemo(() => {
    if (!selectedBugId) return null;
    return publicBugs.find((bug) => bug.bug_id === selectedBugId) ?? null;
  }, [publicBugs, selectedBugId]);
  const selectedBacklogDetail = selectedBugId ? selectedBacklogDetailById[selectedBugId] : undefined;
  const fetchedSelectedBug = selectedBacklogDetail?.bug && !isPrivatePlaybackBacklog(selectedBacklogDetail.bug)
    ? selectedBacklogDetail.bug
    : null;
  const selectedBug = cachedSelectedBug ?? fetchedSelectedBug;

  useEffect(() => {
    selectedBugRef.current = selectedBug;
  }, [selectedBug]);

  // Stale backlog-cache deep links need a detail row before timeline/gate requests can be scoped.
  useEffect(() => {
    if (!selectedBugId || cachedSelectedBug) return undefined;
    const current = selectedBacklogDetailByIdRef.current[selectedBugId];
    if (current?.loaded) return undefined;
    const bugId = selectedBugId;
    const controller = new AbortController();
    setSelectedBacklogDetailById((states) => {
      const next = {
        ...states,
        [bugId]: {
          loading: true,
          loaded: states[bugId]?.loaded ?? false,
          error: "",
          bug: states[bugId]?.bug ?? null,
        },
      };
      selectedBacklogDetailByIdRef.current = next;
      return next;
    });

    api.backlogBugFor(projectId, bugId, controller.signal)
      .then((bug) => {
        if (controller.signal.aborted || !mountedRef.current || activeProjectIdRef.current !== projectId) return;
        setSelectedBacklogDetailById((states) => {
          const next = {
            ...states,
            [bugId]: isPrivatePlaybackBacklog(bug)
              ? {
                loading: false,
                loaded: true,
                error: "Backlog detail is private and cannot be shown in playback history.",
                bug: null,
              }
              : {
                loading: false,
                loaded: true,
                error: "",
                bug,
              },
          };
          selectedBacklogDetailByIdRef.current = next;
          return next;
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || !mountedRef.current || activeProjectIdRef.current !== projectId) return;
        setSelectedBacklogDetailById((states) => {
          const next = {
            ...states,
            [bugId]: {
              loading: false,
              loaded: true,
              error: `Unable to load backlog detail: ${errorMessage(error)}`,
              bug: states[bugId]?.bug ?? null,
            },
          };
          selectedBacklogDetailByIdRef.current = next;
          return next;
        });
      });

    return () => controller.abort();
  }, [projectId, selectedBugId, cachedSelectedBug?.bug_id]);

  useEffect(() => {
    const handlePopState = () => {
      setSelectedBugId(readSelectedBacklogId());
      setMode(readActivityMode());
      // B1: restore the event-id deep-link param on browser Back/Forward.
      setSelectedEventParam(readPlaybackEventParam());
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
  const selectedPlaceholderBug = useMemo<BacklogBug | null>(() => {
    if (!selectedBugId || selectedBug) return null;
    return {
      bug_id: selectedBugId,
      title: selectedBacklogDetail?.loading ? "Loading backlog detail" : "Backlog detail unavailable",
      status: "UNKNOWN",
      priority: "P3",
    };
  }, [selectedBacklogDetail?.loading, selectedBug, selectedBugId]);
  const activeTrace = selectedState?.trace
    ?? (selectedBug ? emptyTaskPlaybackTrace(projectId, selectedBug) : selectedPlaceholderBug ? emptyTaskPlaybackTrace(projectId, selectedPlaceholderBug) : fallbackTrace);
  const activeFrameId = selectedFrameId || activeTrace.frames[0]?.id || "";
  const selectedLoadBugId = selectedBug?.bug_id || "";
  const selectedPlaybackLoading = (selectedState?.loading ?? false) || (!selectedBug && (selectedBacklogDetail?.loading ?? false));
  const selectedPlaybackError = selectedState?.error || (!selectedBug ? selectedBacklogDetail?.error ?? "" : "");
  const hintedCurrentBug = currentTaskHint?.active && currentTaskHint.bug && !isPrivatePlaybackBacklog(currentTaskHint.bug)
    ? currentTaskHint.bug
    : null;
  // Resolve the locally-overridden bug when the user clicked a competing-candidate switch.
  const localOverrideBug = useMemo(() => {
    if (!localActivityBugId) return null;
    // Search in active_backlog from the hint first, then publicBugs.
    const fromHint = currentTaskHint?.active_backlog?.find((b) => b.bug_id === localActivityBugId);
    if (fromHint) return isPrivatePlaybackBacklog(fromHint) ? null : fromHint;
    const fromPublic = publicBugs.find((b) => b.bug_id === localActivityBugId);
    return fromPublic && !isPrivatePlaybackBacklog(fromPublic) ? fromPublic : null;
  }, [localActivityBugId, currentTaskHint, publicBugs]);
  const activityBug = selectedBug ?? localOverrideBug ?? hintedCurrentBug;
  const activityState = activityBug ? activityByBug[activityBug.bug_id] : undefined;
  const activityTrace = activityState?.trace ?? emptyTaskPlaybackTrace(projectId, activityBug ?? activityPlaceholderBug);

  const rows = useMemo(() => {
    return selectorBugs
      .filter((bug) => {
        if (statusFilter === "open" && !isOpenBug(bug)) return false;
        if (statusFilter === "closed" && !isClosedBug(bug)) return false;
        if (priorityFilter !== "all" && bug.priority !== priorityFilter) return false;
        if (!matchesGateFilter(gateFilter, bug, playbackByBug[bug.bug_id])) return false;
        return true;
      })
      .slice()
      .sort(compareBacklogRows);
  }, [selectorBugs, gateFilter, playbackByBug, priorityFilter, statusFilter]);

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

  const refreshActivityTimeline = useCallback((bug: BacklogBug, showLoading: boolean, signal: AbortSignal, isFallbackPoll = false) => {
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
          authorityCacheKey: states[bugId]?.authorityCacheKey,
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
      const authorityCacheKey = trace.authority_view?.cache_identity.key;
      if (authorityCacheKey) authorityTraceCacheRef.current[authorityCacheKey] = trace;
      // Merge new events without duplicating (deduplication by event id is
      // handled inside normalizeTaskPlaybackTrace via mergeTimelineEvents).
      setActivityByBug((states) => {
        const prev = states[bugId];
        // Preserve frame selection: if the previous trace had frames the user
        // selected, keep them unless the new trace is genuinely larger.
        return {
          ...states,
          [bugId]: {
            loading: false,
            loaded: true,
            error: errors.join(" | "),
            trace,
            bug: detailBug,
            taskTimeline,
            gate,
            authorityCacheKey,
            refreshedAt: new Date().toISOString(),
            // Preserve existing frame selection when merging poll results
            ...(prev?.loaded && isFallbackPoll ? { _preserveFrameId: true } : {}),
          },
        };
      });
      if (isFallbackPoll) {
        recordPollRef.current(new Date().toISOString());
      }
      // Current tab is newest-first: default to the newest (last) frame.
      setSelectedActivityFrameId((current) => current || trace.frames[trace.frames.length - 1]?.id || "");
    });
  }, [projectId]);

  /**
   * Refresh the project-wide recent events list.
   * Initial load fetches RECENT_EVENTS_LIMIT newest events from the /recent endpoint.
   * On SSE-triggered refresh, we re-fetch and merge new events at the front
   * (deduplicate by event id so the list stays append-only from the top).
   */
  const refreshRecentEvents = useCallback((signal: AbortSignal) => {
    return api.recentTimelineFor(projectId, RECENT_EVENTS_LIMIT, signal)
      .then((response) => {
        if (signal.aborted || activeProjectIdRef.current !== projectId) return;
        const incoming = projectRecentTimelineEvents(response);
        setRecentEvents((prev) => {
          const merged = mergeRecentTimelineEvents(
            [...incoming, ...prev],
            RECENT_EVENTS_LIMIT * 2,
          );
          recentEventIdsRef.current = new Set(
            merged.map((event, index) => recentTimelineEventKey(event, index)),
          );
          return merged;
        });
        setRecentEventsLoaded(true);
      })
      .catch(() => {
        if (!signal.aborted && activeProjectIdRef.current === projectId) {
          setRecentEventsLoaded(true); // mark loaded even on error so we don't spin
        }
      });
  }, [projectId]);

  // SSE with freshness metadata. onStale triggers fallback-polling mode.
  const [sseStaleTrigger, setSseStaleTrigger] = useState(0);
  const { freshness, recordPoll } = useEventStreamWithFreshness(projectId, {
    enabled: Boolean(projectId),
    onEvent: ({ name }) => {
      if (isActivityLiveEvent(name)) setActivityRefreshSeq((seq) => seq + 1);
    },
    onStale: () => {
      // When SSE goes stale, trigger an immediate fallback poll
      setSseStaleTrigger((n) => n + 1);
    },
  }) as { liveStatus: string; freshness: SseFreshnessMeta; recordPoll: (at: string) => void };
  // Keep the stable ref in sync so refreshActivityTimeline can call it without
  // a forward-reference hoisting issue (recordPollRef is declared above the callback).
  recordPollRef.current = recordPoll;

  useEffect(() => {
    const controller = new AbortController();
    refreshCurrentTaskHint(controller.signal);
    return () => controller.abort();
  }, [activityRefreshSeq, refreshCurrentTaskHint]);

  // Load/refresh project-wide recent events whenever SSE fires or initial mount.
  useEffect(() => {
    const controller = new AbortController();
    void refreshRecentEvents(controller.signal);
    return () => controller.abort();
  }, [activityRefreshSeq, sseStaleTrigger, refreshRecentEvents]);

  // F6: Track new events arriving while the user is browsing page>0.
  // When recentEvents grows and eventsPage>0, accumulate the delta count.
  // When the user goes back to page 0, clear the counter.
  useEffect(() => {
    const prev = prevRecentEventsCountRef.current;
    const current = recentEvents.length;
    prevRecentEventsCountRef.current = current;
    if (eventsPage === 0) {
      // On page 0 (following latest): clear any pending count.
      setPendingNewEventsCount(0);
      return;
    }
    if (current > prev) {
      setPendingNewEventsCount((n) => n + (current - prev));
    }
  }, [recentEvents.length, eventsPage]);

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
    // sseStaleTrigger fires when SSE has been silent > SSE_STALE_THRESHOLD_MS,
    // causing an immediate fallback poll to keep the activity view current.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activityBug?.bug_id, activityRefreshSeq, sseStaleTrigger, projectId, refreshActivityTimeline]);

  useEffect(() => {
    if (!activityBug) return;
    const state = activityByBug[activityBug.bug_id];
    if (!state?.loaded || state.loading) return;
    const currentFrameExists = Boolean(selectedActivityFrameId && state.trace.frames.some((frame) => frame.id === selectedActivityFrameId));
    if (!selectedActivityFrameId || currentFrameExists) return;
    // QA #3636 F2: use newest frame (last in newest-first array) for stale-frame fallback.
    setSelectedActivityFrameId(state.trace.frames[state.trace.frames.length - 1]?.id || "");
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
        const authorityCacheKey = trace.authority_view?.cache_identity.key;
        if (authorityCacheKey) authorityTraceCacheRef.current[authorityCacheKey] = trace;
        setPlaybackByBug((states) => ({
          ...states,
          [bugId]: {
            loading: false,
            loaded: true,
            error: errors.join(" | "),
            trace,
            taskTimeline,
            gate,
            authorityCacheKey,
          },
        }));
        if (selectedBugRef.current?.bug_id === bugId) {
          setSelectedFrameId((current) => (
            resolveInitialPlaybackFrameId(
              trace.frames,
              selectedEventParamRef.current || readPlaybackEventParam(),
              current,
            )
          ));
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
    if (mode !== "activity") return;
    setSelectedEventParam("");
    clearPlaybackEventParam();
  }, [mode]);

  // B1: Async + warm-cache deep-link race guard.
  // Whenever playback_event changes and frames are already available, resolve
  // it immediately. This covers both cold loads and current-backlog warm clicks.
  useEffect(() => {
    if (!selectedEventParam) return;
    const state = selectedBugId ? playbackByBug[selectedBugId] : undefined;
    const frames = state?.trace.frames ?? [];
    if (frames.length === 0 || state?.loading) return;
    const resolution = resolveSelectedFrameIdForEventParam(frames, selectedEventParam, selectedFrameId);
    if (resolution.matched) {
      setSelectedFrameId(resolution.frameId);
      setPlaying(false);
    }
    // Clear the pending state regardless of whether we found a match so later
    // event-card clicks for the same warm trace can re-resolve a new param.
    setSelectedEventParam("");
  }, [selectedEventParam, selectedBugId, selectedFrameId, playbackByBug]);

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
    // Clear any pending deep-link event param — user is manually switching rows.
    setSelectedEventParam("");
    setPlaying(false);
    writeSelectedBacklogId(bugId);
  };

  const resetPlayback = () => {
    setPlaying(false);
    setSelectedFrameId(activeTrace.frames[0]?.id || "");
  };

  const navigateToPlaybackEvent = useCallback((backlogId: string, eventId?: string | number | null) => {
    const targetBacklogId = backlogId.trim();
    if (!targetBacklogId) return;
    const nextEventId = eventId != null ? String(eventId) : "";
    navigateToPlayback(targetBacklogId, nextEventId);
    setSelectedBugId(targetBacklogId);
    setMode("history");
    setPlaying(false);
    if (nextEventId) {
      const warmFrames = playbackByBugRef.current[targetBacklogId]?.trace.frames ?? [];
      const resolution = resolveSelectedFrameIdForEventParam(warmFrames, nextEventId, "");
      setSelectedFrameId(resolution.matched ? resolution.frameId : "");
      setSelectedEventParam(nextEventId);
    } else {
      setSelectedFrameId("");
      setSelectedEventParam("");
    }
  }, [setSelectedEventParam]);

  const changeMode = (next: ActivityMode) => {
    setMode(next);
    writeActivityMode(next);
  };

  const openActivityPlaybackHistory = () => {
    const backlogId = activityBug?.bug_id || "";
    if (!backlogId) {
      changeMode("history");
      return;
    }

    const selectedActivityFrame = selectedActivityFrameId
      ? activityTrace.frames.find((frame) => frame.id === selectedActivityFrameId)
      : null;
    const frame = selectedActivityFrame ?? activityTrace.frames[activityTrace.frames.length - 1] ?? null;
    const eventId = frame?.source_event_id || frame?.id || "";
    navigateToPlayback(backlogId, eventId);
    setSelectedBugId(backlogId);
    if (eventId) {
      const warmFrames = playbackByBugRef.current[backlogId]?.trace.frames ?? [];
      const resolution = resolveSelectedFrameIdForEventParam(warmFrames, eventId, "");
      setSelectedFrameId(resolution.matched ? resolution.frameId : "");
    } else {
      setSelectedFrameId("");
    }
    setSelectedEventParam(eventId);
    setPlaying(false);
    setMode("history");
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
          <SseFreshnessBadge freshness={freshness} />
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
        <div className="task-playback-activity-page">
          {/* Compact top status strip */}
          <div className="task-playback-activity-strip">
            <SseFreshnessDetail
              freshness={freshness}
              onReconnect={() => {
                recentEventIdsRef.current = new Set();
                setRecentEvents([]);
                setRecentEventsLoaded(false);
                setActivityRefreshSeq((seq) => seq + 1);
              }}
            />
            <ActivityStreamSummary hint={currentTaskHint} trace={activityTrace} />
            <CompetingCandidatesSelector
              hint={currentTaskHint}
              currentBugId={activityBug?.bug_id || ""}
              onSelect={(bugId) => {
                setLocalActivityBugId(bugId);
                setSelectedActivityFrameId("");
              }}
            />
            <div className="task-playback-controls">
              <button
                type="button"
                className="action-btn"
                title="Force a fresh fetch of project-wide recent events"
                onClick={() => {
                  recentEventIdsRef.current = new Set();
                  setRecentEvents([]);
                  setRecentEventsLoaded(false);
                  setActivityRefreshSeq((seq) => seq + 1);
                }}
              >
                Refresh
              </button>
              <button type="button" className="action-btn" onClick={openActivityPlaybackHistory}>
                Open playback history
              </button>
              <span className="mono">{recentEventsLoaded ? `${recentEvents.length} event${recentEvents.length === 1 ? "" : "s"}` : "loading…"}</span>
            </div>
          </div>

          <NextLegalActionCallout
            trace={activityTrace}
            selectedFrameId={activityTrace.frames[activityTrace.frames.length - 1]?.id || ""}
            surface="Current"
          />

          {/* Full-width event card list (IA item A) */}
          {/* F3: animateNew is only true when the user is on page 0 (following latest).
              On page>1 or when browsing, the banner pulse is the only animation. */}
          <ActivityEventCardList
            events={recentEvents}
            loaded={recentEventsLoaded}
            page={eventsPage}
            pageSize={EVENTS_PAGE_SIZE}
            animateNew={eventsPage === 0}
            pendingNewEventsCount={pendingNewEventsCount}
            onPageChange={(page) => setEventsPage(page)}
            onCardClick={(card) => {
              if (card.backlog_id) {
                // B1 (AC-ACTIVITY-PLAYBACK-IA-UE-BLOCKERS-20260611):
                // Carry the event id in the URL (playback_event param) so that
                // the Playback view can select the matching frame after load.
                // F4: Use pushState so the browser Back button restores the
                // Current tab card list (popstate handler reads ACTIVITY_TAB_PARAM).
                const eventId = card.id != null ? String(card.id) : "";
                navigateToPlayback(card.backlog_id, eventId);
                setSelectedBugId(card.backlog_id);
                const warmFrames = playbackByBugRef.current[card.backlog_id]?.trace.frames ?? [];
                const resolution = resolveSelectedFrameIdForEventParam(warmFrames, eventId, "");
                setSelectedFrameId(resolution.matched ? resolution.frameId : "");
                // Store the event param so the async deep-link effect can resolve
                // it once the trace finishes loading, or re-resolve a warm trace
                // if the clicked backlog is already open.
                setSelectedEventParam(eventId);
                setPlaying(false);
                setMode("history");
              }
            }}
          />
        </div>
      ) : (
        <div className="task-playback-layout">
          <aside className="task-playback-selector" aria-label="Backlog playback selector">
            <div className="task-playback-selector-head">
              <strong>Backlog selector</strong>
              <span className="mono">{rows.length} local facet / {serverBacklog.filtered_count ?? selectorBugs.length} server</span>
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
                  ["closed", "Closed"],
                  ["all", "All"],
                ]}
                onChange={setStatusFilter}
              />
              <select
                value={priorityFilter}
                onChange={(event) => setPriorityFilter(event.target.value as PriorityFilter)}
                aria-label="Backlog priority filter"
              >
                <option value="all">All priorities</option>
                <option value="P0">P0</option>
                <option value="P1">P1</option>
                <option value="P2">P2</option>
                <option value="P3">P3</option>
              </select>
              <select value={gateFilter} onChange={(event) => setGateFilter(event.target.value as GateFilter)} aria-label="Timeline and gate filter">
                <option value="all">All timeline states</option>
                <option value="gate_candidate">Gate candidates</option>
                <option value="timeline_loaded">Timeline loaded</option>
                <option value="blocked_gate">Blocked gate</option>
                <option value="no_timeline">No timeline loaded</option>
              </select>
            </div>
            <div data-server-search-results="playback" aria-live="polite">
              <strong>Server result set</strong>
              <p>
                {serverSearchLoading
                  ? "Searching backlog and public-safe timeline evidence…"
                  : `${selectorBugs.length} backlog rows; ${timelineSearch?.total ?? 0} exact timeline matches. Timeline-state filter is a local facet of these server results.`}
              </p>
              {serverSearchError ? <p className="timeline-error">{serverSearchError}</p> : null}
              <div className="task-playback-controls">
                <button
                  type="button"
                  className="action-btn"
                  disabled={searchOffset <= 0 || serverSearchLoading}
                  onClick={() => setSearchOffset((offset) => Math.max(0, offset - PLAYBACK_SEARCH_PAGE_SIZE))}
                >
                  Previous server page
                </button>
                <button
                  type="button"
                  className="action-btn"
                  disabled={!(serverBacklog.has_more || timelineSearch?.has_more) || serverSearchLoading}
                  onClick={() => setSearchOffset(Math.max(
                    serverBacklog.next_offset ?? 0,
                    timelineSearch?.next_offset ?? 0,
                    searchOffset + PLAYBACK_SEARCH_PAGE_SIZE,
                  ))}
                >
                  Next server page
                </button>
              </div>
            </div>
            {(timelineSearch?.events.length ?? 0) > 0 ? (
              <div className="task-playback-row-list" data-timeline-search-results="public-safe">
                <strong>Timeline server matches</strong>
                {timelineSearch?.events.map((event, index) => {
                  const backlogId = event.backlog_id || event.backlog?.bug_id || "";
                  const eventId = String(event.event_id || event.id || "");
                  const exactHref = event.deep_link || buildPlaybackUrl(projectId, backlogId, eventId);
                  return (
                    <a
                      key={`timeline-search:${eventId || index}`}
                      href={exactHref}
                      onClick={(clickEvent) => {
                        clickEvent.preventDefault();
                        navigateToPlaybackEvent(backlogId, eventId);
                      }}
                    >
                      <div>
                        <strong>{event.backlog?.title || backlogId || "Timeline event"}</strong>
                        <span className="mono">{backlogId} · event {eventId}</span>
                      </div>
                      <span className={`status-badge ${statusClass(event.status || "")}`}>{normalizeStatus(event.status)}</span>
                      <em>{[event.event_kind, event.event_type, event.phase].filter(Boolean).join(" · ")}</em>
                      {event.blocker_semantics ? (
                        <em>
                          {event.blocker_semantics.message
                            || `Repair ${event.blocker_semantics.governed_action || "governed action"} for ${event.blocker_semantics.repair_target_id || backlogId}; blocker ids: ${(event.blocker_semantics.blocker_ids ?? []).join(", ")}`}
                        </em>
                      ) : null}
                    </a>
                  );
                })}
              </div>
            ) : null}
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
              {selectedBug || selectedBugId ? (
                <span className="mono">{selectedBug?.bug_id || selectedBugId}</span>
              ) : (
                <span className="mono">Select a backlog row to fetch governed timeline APIs</span>
              )}
            </div>
            <NextLegalActionCallout trace={activeTrace} selectedFrameId={activeFrameId} surface="Playback" />
            <CompatibilityRepairTargets trace={activeTrace} surface="Playback" />
            <TaskPlaybackPanel
              trace={activeTrace}
              selectedFrameId={activeFrameId}
              loading={selectedPlaybackLoading}
              error={selectedPlaybackError}
              onSelectFrame={(frameId) => {
                setSelectedFrameId(frameId);
                setPlaying(false);
              }}
              onNavigateToPlayback={navigateToPlaybackEvent}
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

/**
 * Dedicated next-action surface shared by Activity Current and Playback.
 * It intentionally uses semantic block markup and full-width text instead of
 * the compact metadata-chip treatment used by surrounding runtime details.
 */
function NextLegalActionCallout({
  trace,
  surface,
  selectedFrameId = "",
}: {
  trace: TaskPlaybackTrace;
  surface: "Current" | "Playback";
  selectedFrameId?: string;
}) {
  const actions = taskPlaybackNextLegalActionPresentations(trace, selectedFrameId);
  if (actions.length === 0) return null;
  return (
    <section
      aria-label={`${surface} next legal actions`}
      data-next-legal-action-callout={surface.toLowerCase()}
      style={{
        background: "var(--ink-50)",
        border: "1px solid var(--blue)",
        borderLeft: "5px solid var(--blue)",
        borderRadius: "var(--radius-card)",
        boxShadow: "var(--shadow-card)",
        display: "grid",
        gap: 10,
        margin: "12px 0",
        padding: "14px 16px",
      }}
    >
      <header style={{ alignItems: "baseline", display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "space-between" }}>
        <strong style={{ color: "var(--ink-900)", fontSize: 15 }}>{surface} · Next legal action</strong>
        <small style={{ color: "var(--ink-600)" }}>ContractRuntime/current-chain first · historical actions advisory</small>
      </header>
      <div style={{ display: "grid", gap: 8 }}>
        {actions.map((item) => {
          const blocked = item.disposition === "BLOCKED";
          const advisory = item.advisory_only || item.disposition === "BYPASSED" || item.disposition === "WAIVED";
          const accent = blocked ? "var(--red-strong)" : advisory ? "var(--amber-strong)" : "var(--blue)";
          const background = blocked ? "var(--red-bg)" : advisory ? "var(--amber-bg)" : "var(--blue-bg)";
          const foreground = blocked ? "var(--red-fg)" : advisory ? "var(--amber-fg)" : "var(--blue-fg)";
          return (
            <article
              key={item.key}
              data-next-legal-action-authority={item.advisory_only ? "advisory" : "authoritative"}
              data-next-legal-action-disposition={item.disposition}
              style={{ background, border: `1px solid ${accent}`, borderLeft: `4px solid ${accent}`, borderRadius: 8, padding: "11px 12px" }}
            >
              <div style={{ alignItems: "baseline", display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "space-between" }}>
                <strong style={{ color: foreground, fontSize: 12, letterSpacing: ".02em" }}>{item.label}</strong>
                <b style={{ color: foreground, fontSize: 11 }}>{item.disposition}</b>
              </div>
              <p style={{ color: "var(--ink-900)", fontSize: 14, fontWeight: 650, lineHeight: 1.45, margin: "6px 0 0", overflowWrap: "anywhere", whiteSpace: "normal" }}>
                {item.action_text}
              </p>
              {item.detail ? <p style={{ color: "var(--ink-700)", lineHeight: 1.4, margin: "5px 0 0", overflowWrap: "anywhere" }}>{item.detail}</p> : null}
              <small style={{ color: "var(--ink-600)", display: "block", marginTop: 5, overflowWrap: "anywhere" }}>Source: {item.source}</small>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function ActivityStreamSummary({ hint, trace }: { hint: CurrentTaskHint | null; trace: TaskPlaybackTrace }) {
  const authority = trace.authority_view;
  const currentSnapshot = trace.current_snapshot.row;
  const currentAction = authority?.contract_execution_progress.current_action;
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
  const nextExpected = authority
    ? currentAction?.evidence_kind || "none recorded"
    : trace.close_gate_summary.next_expected_evidence.length > 0
    ? trace.close_gate_summary.next_expected_evidence.join(", ")
    : firstHintValue(latestEvent, ["next_expected_evidence", "missing_event_kinds", "missing_requirement_ids"]) || "none recorded";
  const authorityBlocker = currentAction?.block_reason
    || authority?.contract_execution_progress.line_states.find((line) => line.display_status === "BLOCKED" || line.display_status === "FAILED")?.line_id
    || "";
  const blocker = authority
    ? authorityBlocker || "none recorded"
    : latestFrame?.failure_diagnosis[0]
    ? `${latestFrame.failure_diagnosis[0].label}: ${latestFrame.failure_diagnosis[0].value}`
    : firstHintValue(latestEvent, ["blocker_ids", "blockers", "missing_event_kinds", "missing_requirement_ids"])
      || (trace.close_gate_summary.blocked ? trace.close_gate_summary.reason_sentence : "none recorded");
  const activeCount = hint?.active_count != null ? `${hint.active_count} active` : "";
  const singleActive = hint?.single_active_task ? singleActiveSummary(hint.single_active_task) : "";
  const compactRuntimeState = currentSnapshot
    ? compactJoin([
      currentSnapshot.readiness_state || currentSnapshot.latest_status,
      currentSnapshot.current_contract_execution_id || currentSnapshot.contract_execution_id,
      currentSnapshot.next_legal_action.action || currentSnapshot.next_legal_action.id,
    ])
    : "";
  const compatibilityRepairTargets = contractRuntimeCompatibilityRepairValues(authority);
  return (
    <div className="task-playback-chip-section" aria-label="Current stream state">
      <strong>Current stream state</strong>
      <div>
        {activeCount ? <span>{activeCount}</span> : null}
        {singleActive ? <span>{singleActive}</span> : null}
        <span>Current snapshot freshness: {trace.current_snapshot.freshness_label}</span>
        {compactRuntimeState ? <span>ContractRuntime current: {compactRuntimeState}</span> : null}
        <span>Latest event: {latestEventText || "none recorded"}</span>
        <span>Worker/QA/close gate: {laneState || "none recorded"}</span>
        {authority ? <span>Contract progress: {authority.contract_execution_progress.display_status}</span> : null}
        {authority ? <span>Backlog row close authority: {authority.backlog_close_readiness.display_status}</span> : null}
        {authority ? (
          <span>
            Historical diagnostics: {authority.historical_diagnostics.timeline_events.length} events
            {authority.historical_diagnostics.truncated ? `; partial; next cursor ${authority.historical_diagnostics.next_cursor || "available"}` : "; response complete"}
          </span>
        ) : null}
        <span>Next expected evidence: {nextExpected}</span>
        <span>Blocker: {blocker}</span>
        {compatibilityRepairTargets.map((target) => (
          <span key={target}>Compatibility repair target (advisory): {target}</span>
        ))}
      </div>
    </div>
  );
}

function CompatibilityRepairTargets({ trace, surface }: { trace: TaskPlaybackTrace; surface: string }) {
  const targets = contractRuntimeCompatibilityRepairValues(trace.authority_view);
  if (targets.length === 0) return null;
  return (
    <div className="task-playback-chip-section" aria-label={`${surface} compatibility repair targets`}>
      <strong>{surface} compatibility repair targets (advisory)</strong>
      <div>
        {targets.map((target) => <span key={target}>{target}</span>)}
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
    "contract_runtime.changed",
    "contract_chain.current_changed",
    "runtime_context.changed",
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

function mergePublicBacklogRows(...groups: BacklogBug[][]): BacklogBug[] {
  const rows = new Map<string, BacklogBug>();
  for (const bug of groups.flat()) {
    if (!bug?.bug_id || isPrivatePlaybackBacklog(bug)) continue;
    rows.set(bug.bug_id, { ...(rows.get(bug.bug_id) ?? {}), ...bug });
  }
  return [...rows.values()];
}

function isPrivatePlaybackBacklog(bug: BacklogBug): boolean {
  return isBacklogRowPrivate(bug);
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
  url.searchParams.delete(PLAYBACK_URL_PARAMS.playback_event);
  window.history.replaceState({ playback_backlog: backlogId }, "", `${url.pathname}${url.search}${url.hash}`);
}

function writeActivityMode(mode: ActivityMode): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set(ACTIVITY_TAB_PARAM, mode);
  if (mode === "activity") url.searchParams.delete(PLAYBACK_URL_PARAMS.playback_event);
  window.history.replaceState({ activity_tab: mode }, "", `${url.pathname}${url.search}${url.hash}`);
}

function clearPlaybackEventParam(): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (!url.searchParams.has(PLAYBACK_URL_PARAMS.playback_event)) return;
  url.searchParams.delete(PLAYBACK_URL_PARAMS.playback_event);
  window.history.replaceState({ activity_tab: "activity" }, "", `${url.pathname}${url.search}${url.hash}`);
}

/**
 * Navigate from the Current tab card list to the Playback history view for a
 * specific backlog row.  Uses pushState so the browser Back button restores the
 * Current tab (the popstate handler reads ACTIVITY_TAB_PARAM and PLAYBACK_BACKLOG_PARAM).
 *
 * B1 (AC-ACTIVITY-PLAYBACK-IA-UE-BLOCKERS-20260611): optional eventId carries the
 * clicked event through the deep-link so the playback view selects that frame after
 * the trace loads.
 *
 * F4 (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611): card clicks must
 * push a new history entry, not replace, so Back returns to the Activity card list.
 */
function navigateToPlayback(backlogId: string, eventId?: string): void {
  if (typeof window === "undefined") return;
  const projectId = new URLSearchParams(window.location.search).get("project_id") || "";
  const path = buildPlaybackUrl(projectId, backlogId, eventId ?? null);
  window.history.pushState({ playback_backlog: backlogId, activity_tab: "history", playback_event: eventId ?? "" }, "", path);
}

function isOpenBug(bug: BacklogBug): boolean {
  return !isClosedBug(bug);
}

function isClosedBug(bug: BacklogBug): boolean {
  return CLOSED_STATUSES.has(normalizeStatus(bug.status)) || isAuditClosedBug(bug);
}

function isAuditClosedBug(bug: BacklogBug): boolean {
  return normalizeStatus(bug.status) === "WAIVED" || AUDIT_ARCHIVED_RUNTIME_STATES.has(normalizeRuntimeState(bug.runtime_state));
}

function normalizeStatus(status?: string): string {
  return (status || "UNKNOWN").trim().toUpperCase();
}

function normalizeRuntimeState(runtimeState?: string): string {
  return (runtimeState || "").trim().toLowerCase();
}

function priorityWeight(priority: string): number {
  return { P0: 0, P1: 1, P2: 2, P3: 3 }[priority.toUpperCase()] ?? 9;
}

function compareBacklogRows(a: BacklogBug, b: BacklogBug): number {
  const openDelta = Number(isOpenBug(b)) - Number(isOpenBug(a));
  if (openDelta !== 0) return openDelta;
  const priority = priorityWeight(a.priority) - priorityWeight(b.priority);
  if (priority !== 0) return priority;
  return Date.parse(b.updated_at || b.created_at || "") - Date.parse(a.updated_at || a.created_at || "");
}

function statusClass(status: string): string {
  const normalized = status.toLowerCase();
  if (["waived", "cancelled", "audit_archived"].some((item) => normalized.includes(item))) return "status-unknown";
  if (["fixed", "closed", "done", "resolved", "passed", "complete"].some((item) => normalized.includes(item))) return "status-complete";
  if (["blocked", "failed", "missing", "error"].some((item) => normalized.includes(item))) return "status-failed";
  if (["progress", "running", "claimed", "open"].some((item) => normalized.includes(item))) return "status-running";
  return "status-unknown";
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.message} ${error.body}`.trim();
  return String(error);
}

// ---------------------------------------------------------------------------
// Activity event card list (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611)
// ---------------------------------------------------------------------------

/**
 * Full-width paginated event card list for the Current tab (IA items A, B, F).
 *
 * Each card shows: time, event kind, status chip, actor/lane, backlog id tag,
 * one-line semantic headline, evidence count/types.
 * Clicking a card navigates to the Playback history view bound to that backlog.
 * New cards fade in with a light positive tint (decays ~2s). Animation is
 * disabled when the user prefers reduced motion.
 *
 * F3 (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611):
 * Animation is follow-gated — the `animateNew` prop must be true for new-card
 * tint+fadeIn to apply.  Callers set animateNew=false when the user is browsing
 * (page>0) or paused so only the banner/pager shows the arrival affordance.
 * When animateNew is false, seenIds still accumulates so that switching back
 * to page 0 does not re-animate already-seen cards.
 */
function ActivityEventCardList({
  events,
  loaded,
  page,
  pageSize,
  animateNew = true,
  pendingNewEventsCount = 0,
  onPageChange,
  onCardClick,
}: {
  events: TaskTimelineEvent[];
  loaded: boolean;
  page: number;
  pageSize: number;
  /** When false, new-card tint+fadeIn animation is suppressed (follow-gate). */
  animateNew?: boolean;
  /**
   * F6 (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611):
   * Count of new events that arrived while the user is browsing page>0.
   * When non-zero and page>0, a "N new events — back to latest" button is
   * shown adjacent to the pager controls.  Clicking it navigates to page 0.
   */
  pendingNewEventsCount?: number;
  onPageChange: (page: number) => void;
  onCardClick: (card: ActivityEventCard) => void;
}) {
  // Track which event ids have been seen before to animate new arrivals.
  const seenIdsRef = useRef<Set<number | string>>(new Set());

  const cards = useMemo(() => events.map(projectEventToCard), [events]);
  const { items: pageCards, totalPages } = sliceEventPage<ActivityEventCard>(cards, page, pageSize);

  if (!loaded && events.length === 0) {
    return <div className="timeline-empty activity-event-card-loading"><span className="spinner" /> Loading project events…</div>;
  }
  if (loaded && events.length === 0) {
    return <div className="timeline-empty">No timeline events recorded for this project yet.</div>;
  }

  return (
    <div className="activity-event-card-section">
      <ol className="activity-event-card-list" aria-label="Project-wide recent events">
        {pageCards.map((card) => {
          const isNew = !seenIdsRef.current.has(card.id);
          if (isNew) seenIdsRef.current.add(card.id);
          // F3: only apply the isNew tint+fadeIn CSS class when animateNew is
          // true (follow mode, page 0).  When browsing/paused the banner pulse
          // is the only visual affordance for new arrivals.
          const showNewAnimation = isNew && animateNew;
          const statusCls = eventCardStatusClass(card.status);
          const at = card.at
            ? new Date(card.at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })
            : "";
          const canOpenPlayback = Boolean(card.backlog_id);
          return (
            <li
              key={card.id}
              className={`activity-event-card activity-event-card--${statusCls}${showNewAnimation ? " activity-event-card--new" : ""}${canOpenPlayback ? "" : " activity-event-card--disabled"}`}
            >
              <button
                type="button"
                className="activity-event-card-btn"
                onClick={() => onCardClick(card)}
                disabled={!canOpenPlayback}
                aria-label={canOpenPlayback ? `Open playback history for ${card.backlog_id}` : `Playback unavailable for ${card.event_kind}`}
                title={canOpenPlayback ? `Open playback history for ${card.backlog_id}` : "Playback unavailable: event has no backlog_id"}
              >
                <div className="activity-event-card-meta">
                  <span className="activity-event-card-time mono">{at}</span>
                  <span className={`activity-event-card-status status-badge status-badge--${statusCls}`}>{card.status}</span>
                  {card.actor ? <span className="activity-event-card-actor mono">{card.actor}</span> : null}
                  {card.backlog_id ? (
                    <span className="activity-event-card-backlog-tag mono" title={card.backlog_id}>{card.backlog_id}</span>
                  ) : null}
                </div>
                <div className="activity-event-card-body">
                  <strong className="activity-event-card-kind">{card.event_kind}</strong>
                  <p className="activity-event-card-headline">{card.headline}</p>
                </div>
                {card.evidence_count > 0 ? (
                  <div className="activity-event-card-evidence">
                    <span>{card.evidence_count} ref{card.evidence_count === 1 ? "" : "s"}</span>
                    {card.evidence_types.slice(0, 4).map((type) => (
                      <span key={type} className="activity-event-card-evidence-type">{type}</span>
                    ))}
                    {card.evidence_types.length > 4 ? <span>+{card.evidence_types.length - 4}</span> : null}
                  </div>
                ) : null}
              </button>
            </li>
          );
        })}
      </ol>

      {totalPages > 1 ? (
        <div className="activity-event-pager" aria-label="Event card page navigation">
          <button
            type="button"
            className="action-btn"
            onClick={() => onPageChange(0)}
            disabled={page === 0}
            aria-label="First page"
          >
            &laquo;
          </button>
          <button
            type="button"
            className="action-btn"
            onClick={() => onPageChange(page - 1)}
            disabled={page === 0}
            aria-label="Previous page"
          >
            &lsaquo;
          </button>
          <span className="activity-event-pager-info">
            Page {page + 1} / {totalPages}
          </span>
          <button
            type="button"
            className="action-btn"
            onClick={() => onPageChange(page + 1)}
            disabled={page >= totalPages - 1}
            aria-label="Next page"
          >
            &rsaquo;
          </button>
          <button
            type="button"
            className="action-btn"
            onClick={() => onPageChange(totalPages - 1)}
            disabled={page >= totalPages - 1}
            aria-label="Last page"
          >
            &raquo;
          </button>
          {/* F6 (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611):
              Show a "N new events — back to latest" affordance adjacent to
              the pager when new events arrive while the user is on page>0.
              Clicking navigates to page 0 (no auto page jump). */}
          {pendingNewEventsCount > 0 && page > 0 ? (
            <button
              type="button"
              className="action-btn activity-event-pager-new-btn"
              onClick={() => onPageChange(0)}
              aria-label={`${pendingNewEventsCount} new event${pendingNewEventsCount === 1 ? "" : "s"} — back to latest`}
            >
              {pendingNewEventsCount} new event{pendingNewEventsCount === 1 ? "" : "s"} — back to latest
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function eventCardStatusClass(status: string): string {
  const n = (status || "").toLowerCase();
  if (["passed", "complete", "fixed", "closed", "done", "resolved", "ready", "recorded"].some((s) => n.includes(s))) return "complete";
  if (["blocked", "failed", "missing", "error"].some((s) => n.includes(s))) return "failed";
  if (["running", "progress", "pending", "waiting", "claimed"].some((s) => n.includes(s))) return "running";
  return "unknown";
}

// ---------------------------------------------------------------------------
// Competing-candidates selector (AC-CURRENT-STREAM-BINDING-ACTIVE-LANE-SELECTION-20260610)
// ---------------------------------------------------------------------------

/**
 * Compact "N active — switch" selector shown when multiple candidates compete.
 * Selection is frontend-local; no server mutation is performed.
 */
function CompetingCandidatesSelector({
  hint,
  currentBugId,
  onSelect,
}: {
  hint: CurrentTaskHint | null;
  currentBugId: string;
  onSelect: (bugId: string) => void;
}) {
  const candidates = hint?.competing_candidates;
  if (!candidates || candidates.length <= 1) return null;
  return (
    <div className="task-playback-chip-section competing-candidates-selector" aria-label="Competing active candidates">
      <strong>{candidates.length} active — switch</strong>
      <div className="competing-candidates-list">
        {candidates.map((c) => (
          <button
            key={c.bug_id}
            type="button"
            className={`competing-candidate-btn${c.bug_id === currentBugId ? " active" : ""}`}
            title={`${c.bug_id} | ${c.event_count} event${c.event_count === 1 ? "" : "s"} | last: ${c.last_evidence_at || "unknown"}`}
            onClick={() => onSelect(c.bug_id)}
          >
            <span className="mono">{c.bug_id}</span>
            <em>{c.event_count} evt</em>
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SSE Freshness UI components (criteria b / e)
// ---------------------------------------------------------------------------

/**
 * Compact badge shown in the view-header-actions area.
 * Four states are visually distinct via tone classes that match the existing
 * live-pill CSS.
 */
function SseFreshnessBadge({ freshness }: { freshness: SseFreshnessMeta }) {
  const tone = sseStatusTone(freshness.status);
  const label = sseStatusLabel(freshness.status);
  const title = [
    `SSE: ${label}`,
    freshness.lastEventAt ? `last event: ${freshness.lastEventAt}` : "",
    freshness.lastEventType ? `type: ${freshness.lastEventType}` : "",
    freshness.staleAgeSecs != null ? `stale age: ${freshness.staleAgeSecs}s` : "",
    freshness.lastPollAt ? `last poll: ${freshness.lastPollAt}` : "",
  ].filter(Boolean).join(" | ");

  return (
    <span
      className={`sse-freshness-badge live-pill ${tone}`}
      title={title}
      aria-label={`SSE stream: ${label}`}
    >
      <span className="pill-dot" aria-hidden="true" />
      {label}
      {freshness.staleAgeSecs != null && freshness.status !== "live"
        ? ` (${freshness.staleAgeSecs}s)`
        : null}
    </span>
  );
}

/**
 * Inline detail row shown under the "Current/runtime stream" header.
 * Renders a small metadata block with connection state, last event info,
 * and fallback poll time. Only shows the stale warning visually when stale.
 * When stale, a one-click reconnect/refresh button appears (IA item G).
 *
 * F5 (AC-ACTIVITY-PLAYBACK-IA-EVENT-CARDS-REFERENCES-20260611):
 * useEventStreamWithFreshness / useEventStream do NOT expose an explicit
 * reconnect/restart function — the EventSource auto-reconnects on error via
 * exponential backoff without needing a caller-initiated teardown/reinit.
 * The "Reconnect" button here therefore refreshes application data (clears
 * the recent events cache, increments the activity refresh sequence) to pull
 * the latest state immediately, which is the best action the UI can take.
 * The SSE stream itself relies on the built-in auto-reconnect mechanism in
 * the hook; a manual stream reinit would require unmounting and remounting
 * the hook, which a button cannot do without lifting state to a key prop.
 * This is honest about the limitation: the button says "Reconnect" but its
 * action is "refresh data now + let auto-reconnect handle the SSE stream".
 */
function SseFreshnessDetail({ freshness, onReconnect }: { freshness: SseFreshnessMeta; onReconnect?: () => void }) {
  const isStale = freshness.status === "stale" || freshness.status === "fallback-polling";
  return (
    <div className={`sse-freshness-detail ${isStale ? "sse-freshness-detail--stale" : ""}`} aria-label="SSE connection detail">
      <span className="sse-freshness-row">
        <span className="sse-freshness-label">Connection</span>
        <span className={`sse-freshness-value sse-freshness-value--${freshness.status}`}>
          {sseStatusLabel(freshness.status)}
        </span>
        {isStale && onReconnect ? (
          <button
            type="button"
            className="action-btn sse-reconnect-btn"
            onClick={onReconnect}
            aria-label="Reconnect / refresh event stream"
          >
            Reconnect
          </button>
        ) : null}
      </span>
      {freshness.lastEventAt ? (
        <span className="sse-freshness-row">
          <span className="sse-freshness-label">Last event</span>
          <span className="sse-freshness-value">
            {freshness.lastEventType ?? "event"}
            {freshness.lastEventId ? ` #${freshness.lastEventId}` : ""}
            {" at "}
            {freshness.lastEventAt.slice(11, 19)}
          </span>
        </span>
      ) : null}
      {freshness.staleAgeSecs != null && freshness.status !== "live" ? (
        <span className="sse-freshness-row sse-freshness-row--warn">
          <span className="sse-freshness-label">Stale age</span>
          <span className="sse-freshness-value">{freshness.staleAgeSecs}s without SSE message</span>
        </span>
      ) : null}
      {freshness.lastPollAt ? (
        <span className="sse-freshness-row">
          <span className="sse-freshness-label">Last poll</span>
          <span className="sse-freshness-value">{freshness.lastPollAt.slice(11, 19)}</span>
        </span>
      ) : null}
    </div>
  );
}
