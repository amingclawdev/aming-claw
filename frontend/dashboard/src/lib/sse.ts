import { useEffect, useRef, useState } from "react";

const DIRECT = (import.meta.env.VITE_DIRECT_API as string | undefined) === "true";
const BACKEND = (import.meta.env.VITE_BACKEND_URL as string | undefined) || "http://localhost:40000";

function sseBase(): string {
  return DIRECT ? BACKEND : "";
}

/** Seconds of silence (including heartbeats) before the stream is considered stale. */
export const SSE_STALE_THRESHOLD_MS = 45_000;

export type LiveStatus = "connecting" | "live" | "offline";

/**
 * Four distinct freshness states visible to the operator:
 *  - live          – EventSource open, recent message received
 *  - reconnecting  – EventSource errored, scheduled to reconnect (backoff)
 *  - stale         – EventSource open or reconnecting but no message in SSE_STALE_THRESHOLD_MS
 *  - fallback-polling – SSE is stale/closed and we are polling the timeline API
 */
export type SseStreamStatus = "live" | "reconnecting" | "stale" | "fallback-polling";

export interface SseFreshnessMeta {
  /** Current stream status. */
  status: SseStreamStatus;
  /** ISO timestamp of the last received SSE event (any event including heartbeat). */
  lastEventAt: string | null;
  /** event id from the last SSE MessageEvent, if any. */
  lastEventId: string | null;
  /** event type/name of the last SSE event, if any. */
  lastEventType: string | null;
  /** ISO timestamp of the last successful fallback poll. */
  lastPollAt: string | null;
  /** How many seconds since the last SSE event (rounded to 1 s). null if no event seen yet. */
  staleAgeSecs: number | null;
}

interface UseEventStreamOpts {
  /** Disable the stream entirely (e.g. when project id missing). */
  enabled?: boolean;
  /** Fired on every server-pushed event (excluding the initial `ready`). */
  onEvent?: (event: { name: string; payload: unknown }) => void;
  /** Fired once after `ready` lands so callers can clear "connecting" UI. */
  onReady?: () => void;
  /**
   * Fired when the stream transitions to stale (no message in SSE_STALE_THRESHOLD_MS).
   * Callers should switch to fallback polling until SSE recovers.
   */
  onStale?: () => void;
  /** Suppress the stale clock (e.g. in tests). Default: false. */
  disableStaleCheck?: boolean;
}

/**
 * Subscribe to the governance SSE stream for a project. Auto-reconnects with
 * exponential backoff (capped at 30s). Returns both a legacy LiveStatus and
 * the richer SseFreshnessMeta for the freshness badge.
 *
 * Adds last-event bookkeeping + freshness clock + onStale callback on top of
 * the existing auto-reconnect+backoff logic.
 */
export function useEventStream(
  projectId: string,
  opts: UseEventStreamOpts = {},
): LiveStatus {
  const { enabled = true, onEvent, onReady, onStale, disableStaleCheck = false } = opts;
  const [status, setStatus] = useState<LiveStatus>(enabled ? "connecting" : "offline");
  const onEventRef = useRef(onEvent);
  const onReadyRef = useRef(onReady);
  const onStaleRef = useRef(onStale);

  // Keep refs current so the EventSource handlers always see fresh callbacks
  // without us needing to tear down + rebuild on every render.
  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);
  useEffect(() => {
    onReadyRef.current = onReady;
  }, [onReady]);
  useEffect(() => {
    onStaleRef.current = onStale;
  }, [onStale]);

  useEffect(() => {
    if (!enabled || !projectId) {
      setStatus("offline");
      return;
    }

    let es: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let staleCheckTimer: number | null = null;
    let attempt = 0;
    let cancelled = false;
    let lastMessageAt = Date.now();
    let hasBeenLive = false;

    const url = `${sseBase()}/api/graph-governance/${encodeURIComponent(projectId)}/events/stream`;

    const touchLastMessage = () => {
      lastMessageAt = Date.now();
    };

    const scheduleStaleCheck = () => {
      if (disableStaleCheck) return;
      if (staleCheckTimer != null) window.clearTimeout(staleCheckTimer);
      const checkAfter = SSE_STALE_THRESHOLD_MS + 500;
      staleCheckTimer = window.setTimeout(() => {
        if (cancelled) return;
        const age = Date.now() - lastMessageAt;
        if (age >= SSE_STALE_THRESHOLD_MS) {
          try {
            onStaleRef.current?.();
          } catch {
            /* swallow */
          }
          // Re-check periodically while stale
          scheduleStaleCheck();
        }
      }, checkAfter);
    };

    const connect = () => {
      if (cancelled) return;
      setStatus("connecting");
      try {
        es = new EventSource(url);
      } catch {
        scheduleReconnect();
        return;
      }

      es.addEventListener("ready", () => {
        attempt = 0;
        hasBeenLive = true;
        touchLastMessage();
        setStatus("live");
        scheduleStaleCheck();
        try {
          onReadyRef.current?.();
        } catch {
          /* swallow */
        }
      });

      // We don't enumerate every server event name here — the backend uses
      // `event: <name>` for typed events; addEventListener only fires for
      // explicit names. onmessage catches the unnamed default if any.
      // Listen for the events we know about + a generic fallthrough.
      const dispatch = (e: MessageEvent) => {
        touchLastMessage();
        if (!hasBeenLive) {
          hasBeenLive = true;
          setStatus("live");
          scheduleStaleCheck();
        }
        if (!e.data) return;
        let parsed: unknown;
        try {
          parsed = JSON.parse(e.data);
        } catch {
          return;
        }
        const inner =
          parsed && typeof parsed === "object" && parsed !== null
            ? (parsed as { event?: string; payload?: unknown })
            : null;
        const name = inner?.event || e.type || "message";
        const payload = inner?.payload ?? parsed;
        try {
          onEventRef.current?.({ name, payload });
        } catch {
          /* swallow */
        }
      };

      // Subscribe to the event names the backend currently fans out. New
      // names added on the server side need a matching addEventListener here
      // because EventSource only dispatches `message` for unnamed events.
      const known = [
        "dashboard.changed",
        "node.status_changed",
        "node.created",
        "node.deleted",
        "task.created",
        "task.completed",
        "task.failed",
        "task.retry",
        "task_timeline.appended",
        "current_task.changed",
        "gate.blocked",
        "gate.satisfied",
        "backfill.promoted",
        "rollback.executed",
        // MF 2026-05-11 state-transition coverage — worker AI lifecycle.
        "semantic_job.enqueued",
        "semantic_node.running",
        "semantic_node.proposed",
        "edge_semantic.running",
        "edge_semantic.proposed",
        "snapshot.activated",
        // heartbeat keeps the freshness clock alive
        "heartbeat",
        "ping",
      ];
      for (const n of known) es.addEventListener(n, dispatch);
      es.onmessage = dispatch;

      es.onerror = () => {
        if (cancelled) return;
        setStatus("offline");
        es?.close();
        es = null;
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      const delay = Math.min(30_000, 1_000 * Math.pow(2, Math.min(attempt, 5)));
      attempt += 1;
      reconnectTimer = window.setTimeout(connect, delay);
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
      if (staleCheckTimer != null) window.clearTimeout(staleCheckTimer);
      es?.close();
    };
  }, [projectId, enabled, disableStaleCheck]);

  return status;
}

// ---------------------------------------------------------------------------
// Richer hook: useEventStreamWithFreshness
// ---------------------------------------------------------------------------

/**
 * Like useEventStream but also returns SseFreshnessMeta for the freshness badge.
 * Uses the same EventSource lifecycle as useEventStream.
 */
export function useEventStreamWithFreshness(
  projectId: string,
  opts: UseEventStreamOpts = {},
): { liveStatus: LiveStatus; freshness: SseFreshnessMeta } {
  const { enabled = true, onEvent, onReady, onStale, disableStaleCheck = false } = opts;

  const [liveStatus, setLiveStatus] = useState<LiveStatus>(enabled ? "connecting" : "offline");
  const [freshness, setFreshness] = useState<SseFreshnessMeta>({
    status: enabled ? "reconnecting" : "fallback-polling",
    lastEventAt: null,
    lastEventId: null,
    lastEventType: null,
    lastPollAt: null,
    staleAgeSecs: null,
  });

  const onEventRef = useRef(onEvent);
  const onReadyRef = useRef(onReady);
  const onStaleRef = useRef(onStale);

  useEffect(() => { onEventRef.current = onEvent; }, [onEvent]);
  useEffect(() => { onReadyRef.current = onReady; }, [onReady]);
  useEffect(() => { onStaleRef.current = onStale; }, [onStale]);

  // Track freshness metadata without re-running the connection effect
  const freshnessRef = useRef<{
    lastEventAt: number | null;
    lastEventId: string | null;
    lastEventType: string | null;
    lastPollAt: string | null;
    stale: boolean;
  }>({
    lastEventAt: null,
    lastEventId: null,
    lastEventType: null,
    lastPollAt: null,
    stale: false,
  });

  const updateFreshness = (streamStatus: SseStreamStatus) => {
    const now = Date.now();
    const fr = freshnessRef.current;
    const staleAgeSecs =
      fr.lastEventAt != null
        ? Math.round((now - fr.lastEventAt) / 1000)
        : null;
    setFreshness({
      status: streamStatus,
      lastEventAt: fr.lastEventAt != null ? new Date(fr.lastEventAt).toISOString() : null,
      lastEventId: fr.lastEventId,
      lastEventType: fr.lastEventType,
      lastPollAt: fr.lastPollAt,
      staleAgeSecs,
    });
  };

  // Expose a way for callers to record a successful fallback poll
  // by calling opts.onEvent on the fallback — but we also allow the
  // ActivityView to call setFreshnessPollAt directly via the hook return.
  // We expose it via a stable ref.
  const recordPollRef = useRef((at: string) => {
    freshnessRef.current.lastPollAt = at;
    updateFreshness("fallback-polling");
  });

  useEffect(() => {
    if (!enabled || !projectId) {
      setLiveStatus("offline");
      setFreshness({
        status: "fallback-polling",
        lastEventAt: null,
        lastEventId: null,
        lastEventType: null,
        lastPollAt: null,
        staleAgeSecs: null,
      });
      return;
    }

    let es: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let staleCheckTimer: number | null = null;
    let attempt = 0;
    let cancelled = false;
    let hasBeenLive = false;

    const url = `${sseBase()}/api/graph-governance/${encodeURIComponent(projectId)}/events/stream`;

    const touchLastMessage = (eventId: string | null, eventType: string | null) => {
      const now = Date.now();
      freshnessRef.current.lastEventAt = now;
      freshnessRef.current.lastEventId = eventId ?? freshnessRef.current.lastEventId;
      freshnessRef.current.lastEventType = eventType ?? freshnessRef.current.lastEventType;
      freshnessRef.current.stale = false;
    };

    const scheduleStaleCheck = () => {
      if (disableStaleCheck) return;
      if (staleCheckTimer != null) window.clearTimeout(staleCheckTimer);
      const checkAfter = SSE_STALE_THRESHOLD_MS + 500;
      staleCheckTimer = window.setTimeout(() => {
        if (cancelled) return;
        const lastAt = freshnessRef.current.lastEventAt;
        const age = lastAt != null ? Date.now() - lastAt : SSE_STALE_THRESHOLD_MS;
        if (age >= SSE_STALE_THRESHOLD_MS) {
          freshnessRef.current.stale = true;
          updateFreshness("stale");
          try {
            onStaleRef.current?.();
          } catch {
            /* swallow */
          }
          scheduleStaleCheck();
        }
      }, checkAfter);
    };

    const connect = () => {
      if (cancelled) return;
      setLiveStatus("connecting");
      if (!hasBeenLive) {
        updateFreshness("reconnecting");
      }
      try {
        es = new EventSource(url);
      } catch {
        scheduleReconnect();
        return;
      }

      es.addEventListener("ready", () => {
        attempt = 0;
        hasBeenLive = true;
        touchLastMessage(null, "ready");
        setLiveStatus("live");
        updateFreshness("live");
        scheduleStaleCheck();
        try {
          onReadyRef.current?.();
        } catch {
          /* swallow */
        }
      });

      const dispatch = (e: MessageEvent) => {
        touchLastMessage(e.lastEventId || null, e.type || null);
        if (!hasBeenLive) {
          hasBeenLive = true;
          setLiveStatus("live");
          scheduleStaleCheck();
        }
        updateFreshness("live");
        if (!e.data) return;
        let parsed: unknown;
        try {
          parsed = JSON.parse(e.data);
        } catch {
          return;
        }
        const inner =
          parsed && typeof parsed === "object" && parsed !== null
            ? (parsed as { event?: string; payload?: unknown })
            : null;
        const name = inner?.event || e.type || "message";
        const payload = inner?.payload ?? parsed;
        try {
          onEventRef.current?.({ name, payload });
        } catch {
          /* swallow */
        }
      };

      const known = [
        "dashboard.changed",
        "node.status_changed",
        "node.created",
        "node.deleted",
        "task.created",
        "task.completed",
        "task.failed",
        "task.retry",
        "task_timeline.appended",
        "current_task.changed",
        "gate.blocked",
        "gate.satisfied",
        "backfill.promoted",
        "rollback.executed",
        "semantic_job.enqueued",
        "semantic_node.running",
        "semantic_node.proposed",
        "edge_semantic.running",
        "edge_semantic.proposed",
        "snapshot.activated",
        "heartbeat",
        "ping",
      ];
      for (const n of known) es.addEventListener(n, dispatch);
      es.onmessage = dispatch;

      es.onerror = () => {
        if (cancelled) return;
        setLiveStatus("offline");
        updateFreshness(freshnessRef.current.stale ? "stale" : "reconnecting");
        es?.close();
        es = null;
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      const delay = Math.min(30_000, 1_000 * Math.pow(2, Math.min(attempt, 5)));
      attempt += 1;
      updateFreshness("reconnecting");
      reconnectTimer = window.setTimeout(connect, delay);
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
      if (staleCheckTimer != null) window.clearTimeout(staleCheckTimer);
      es?.close();
    };
  }, [projectId, enabled, disableStaleCheck]);

  return { liveStatus, freshness, recordPoll: recordPollRef.current } as {
    liveStatus: LiveStatus;
    freshness: SseFreshnessMeta;
    recordPoll: (at: string) => void;
  };
}

/** Compute the tone class for a given freshness status (matches live-pill CSS). */
export function sseStatusTone(status: SseStreamStatus): "tone-green" | "tone-amber" | "tone-red" | "tone-neutral" {
  switch (status) {
    case "live": return "tone-green";
    case "reconnecting": return "tone-amber";
    case "stale": return "tone-red";
    case "fallback-polling": return "tone-amber";
  }
}

/** Human-readable label for a freshness status. */
export function sseStatusLabel(status: SseStreamStatus): string {
  switch (status) {
    case "live": return "live";
    case "reconnecting": return "reconnecting";
    case "stale": return "stale";
    case "fallback-polling": return "fallback-polling";
  }
}
