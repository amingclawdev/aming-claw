import { useEffect, useRef, useState } from "react";

const DIRECT = (import.meta.env.VITE_DIRECT_API as string | undefined) === "true";
const BACKEND = (import.meta.env.VITE_BACKEND_URL as string | undefined) || "http://localhost:40000";

function sseBase(): string {
  return DIRECT ? BACKEND : "";
}

export type LiveStatus = "connecting" | "live" | "offline";

interface UseEventStreamOpts {
  /** Disable the stream entirely (e.g. when project id missing). */
  enabled?: boolean;
  /** Fired on every server-pushed event (excluding the initial `ready`). */
  onEvent?: (event: { name: string; payload: unknown }) => void;
  /** Fired once after `ready` lands so callers can clear "connecting" UI. */
  onReady?: () => void;
}

/**
 * Subscribe to the governance SSE stream for a project. Auto-reconnects with
 * exponential backoff (capped at 30s). Returns a connection status the caller
 * can render as a small "live" indicator.
 *
 * The stream is fire-and-forget — the hook does not retry missed events.
 * Callers debounce-refetch on `onEvent` to materialize the live view.
 */
export function useEventStream(
  projectId: string,
  opts: UseEventStreamOpts = {},
): LiveStatus {
  const { enabled = true, onEvent, onReady } = opts;
  const [status, setStatus] = useState<LiveStatus>(enabled ? "connecting" : "offline");
  const onEventRef = useRef(onEvent);
  const onReadyRef = useRef(onReady);

  // Keep refs current so the EventSource handlers always see fresh callbacks
  // without us needing to tear down + rebuild on every render.
  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);
  useEffect(() => {
    onReadyRef.current = onReady;
  }, [onReady]);

  useEffect(() => {
    if (!enabled || !projectId) {
      setStatus("offline");
      return;
    }

    let es: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let attempt = 0;
    let cancelled = false;

    const url = `${sseBase()}/api/graph-governance/${encodeURIComponent(projectId)}/events/stream`;

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
        setStatus("live");
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
        "gate.blocked",
        "gate.satisfied",
        "backfill.promoted",
        "rollback.executed",
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
      es?.close();
    };
  }, [projectId, enabled]);

  return status;
}
