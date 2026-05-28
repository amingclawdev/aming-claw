import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../lib/api";
import type {
  BacklogBug,
  ObserverCommand,
  ObserverCommandStatus,
  ObserverConnectionSummary,
  ProjectInboxItem,
  ProjectInboxResponse,
  RawRequirement,
} from "../types";

interface Props {
  projectId: string;
}

type SimpleTab = "before" | "in_progress" | "completed";

const TABS: Array<{ key: SimpleTab; label: string }> = [
  { key: "before", label: "Before development" },
  { key: "in_progress", label: "In progress" },
  { key: "completed", label: "Completed" },
];

const WORKER_CONTROL_TYPES = {
  pause: "pause_worker",
  continue: "continue_worker",
  cancel: "cancel_worker",
} as const;

type WorkerAction = keyof typeof WORKER_CONTROL_TYPES;

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `${error.message} ${error.body}`;
  return error instanceof Error ? error.message : String(error);
}

function isRawRequirement(item: ProjectInboxItem): item is RawRequirement {
  return "raw_id" in item;
}

function isBacklogItem(
  item: ProjectInboxItem,
): item is ProjectInboxItem & BacklogBug {
  return !isRawRequirement(item);
}

function fmtTimestamp(value: string | undefined | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function shortCommit(value: string | undefined | null): string {
  const trimmed = (value || "").trim();
  if (!trimmed) return "";
  return trimmed.length > 12 ? trimmed.slice(0, 12) : trimmed;
}

function rawUiStatus(row: RawRequirement): "unconfirmed" | "confirmed" {
  // User-visible states only: raw_inbox → unconfirmed; everything else → confirmed.
  return row.status === "raw_inbox" ? "unconfirmed" : "confirmed";
}

function commandsForRaw(rawId: string, commands: ObserverCommand[]): ObserverCommand[] {
  return commands.filter((c) => String(c.payload?.raw_id ?? "") === rawId);
}

function latestCommandOfType(
  commands: ObserverCommand[],
  commandType: string,
): ObserverCommand | null {
  const matching = commands.filter((c) => c.command_type === commandType);
  if (!matching.length) return null;
  // commands are sorted DESC by created_at server-side; keep the head.
  return matching[0];
}

function commandsForBacklog(bugId: string, commands: ObserverCommand[]): ObserverCommand[] {
  return commands.filter(
    (c) => String(c.payload?.bug_id ?? c.payload?.backlog_id ?? "") === bugId,
  );
}

function statusToneLabel(status: ObserverCommandStatus | null | undefined): {
  tone: "neutral" | "queued" | "running" | "complete" | "failed";
  label: string;
} {
  if (!status) return { tone: "neutral", label: "Not queued" };
  if (status === "queued" || status === "notified") return { tone: "queued", label: "Queued" };
  if (status === "claimed" || status === "running") return { tone: "running", label: "Running" };
  if (status === "completed") return { tone: "complete", label: "Completed" };
  if (status === "failed") return { tone: "failed", label: "Failed" };
  if (status === "cancelled") return { tone: "failed", label: "Cancelled" };
  return { tone: "neutral", label: String(status) };
}

function commandIsActive(command: ObserverCommand | null | undefined): boolean {
  if (!command) return false;
  return (
    command.status === "queued" ||
    command.status === "notified" ||
    command.status === "claimed" ||
    command.status === "running"
  );
}

function observerStatusLine(
  observer: ObserverConnectionSummary | undefined,
  lastCommand: ObserverCommand | null,
): { tone: "connected" | "waiting" | "queued" | "running" | "failed" | "complete"; label: string } {
  if (!observer?.connected) {
    return { tone: "waiting", label: "Waiting for observer" };
  }
  if (lastCommand) {
    const { tone, label } = statusToneLabel(lastCommand.status);
    if (tone === "queued") return { tone: "queued", label: "Command queued" };
    if (tone === "running") return { tone: "running", label: "Command running" };
    if (tone === "complete") return { tone: "complete", label: "Last command completed" };
    if (tone === "failed") return { tone: "failed", label: `Last command ${label.toLowerCase()}` };
  }
  return { tone: "connected", label: `Observer connected (${observer.connected_count})` };
}

function viewAuditUrl(projectId: string, ref: { bug_id?: string; raw_id?: string }): string {
  if (typeof window === "undefined") return "#";
  const url = new URL(window.location.href);
  url.searchParams.set("project_id", projectId);
  url.searchParams.set("view", "backlog");
  if (ref.bug_id) url.searchParams.set("audit_bug_id", ref.bug_id);
  if (ref.raw_id) url.searchParams.set("audit_raw_id", ref.raw_id);
  return `${url.pathname}${url.search}${url.hash}`;
}

function navigateToAudit(projectId: string, ref: { bug_id?: string; raw_id?: string }) {
  if (typeof window === "undefined") return;
  const href = viewAuditUrl(projectId, ref);
  window.history.pushState({}, "", href);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function plainTextPreview(value: unknown, max = 240): string {
  if (typeof value === "string") return value.length > max ? `${value.slice(0, max)}…` : value;
  if (value === null || value === undefined) return "";
  try {
    const json = JSON.stringify(value, null, 2);
    return json.length > max ? `${json.slice(0, max)}…` : json;
  } catch {
    return String(value);
  }
}

export default function ProjectInboxView({ projectId }: Props) {
  const [inbox, setInbox] = useState<ProjectInboxResponse | null>(null);
  const [rawText, setRawText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState<SimpleTab>("before");
  const [detailRawId, setDetailRawId] = useState<string | null>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      const next = await api.projectInboxFor(projectId, signal);
      setInbox(next);
    },
    [projectId],
  );

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void load(controller.signal).catch((err) => {
      if ((err as { name?: string }).name === "AbortError") return;
      setError(errorMessage(err));
    });
    return () => controller.abort();
  }, [load]);

  const observer = inbox?.observer;
  const observerConnected = Boolean(observer?.connected);
  const commands = inbox?.observer_commands?.items ?? [];
  const commandCounts = inbox?.observer_commands?.counts ?? {};
  const queuedCount = (commandCounts.queued ?? 0) + (commandCounts.notified ?? 0);
  const runningCount = (commandCounts.claimed ?? 0) + (commandCounts.running ?? 0);
  const failedCount = commandCounts.failed ?? 0;
  const completedCount = commandCounts.completed ?? 0;

  const rawLane = inbox?.lanes.raw_inbox.items ?? [];
  const confirmLane = inbox?.lanes.needs_confirmation.items ?? [];
  const readyLane = inbox?.lanes.ready_backlog.items ?? [];
  const inProgressLane = inbox?.lanes.in_progress.items ?? [];
  const reviewLane = inbox?.lanes.review_needed.items ?? [];
  const doneLane = inbox?.lanes.done.items ?? [];

  const allRaw = useMemo(
    () => [...rawLane, ...confirmLane].filter(isRawRequirement),
    [rawLane, confirmLane],
  );

  const confirmedRaw = useMemo(
    () => allRaw.filter((row) => rawUiStatus(row) === "confirmed"),
    [allRaw],
  );

  const lastCommand = commands[0] ?? null;
  const observerLine = observerStatusLine(observer, lastCommand);

  const detailRow = useMemo(() => {
    if (!detailRawId) return null;
    return allRaw.find((row) => row.raw_id === detailRawId) ?? null;
  }, [allRaw, detailRawId]);

  const detailRowCommands = useMemo(
    () => (detailRow ? commandsForRaw(detailRow.raw_id, commands) : []),
    [detailRow, commands],
  );

  const capture = async () => {
    const text = rawText.trim();
    if (!text) return;
    setBusy(true);
    setError("");
    try {
      await api.captureRawRequirementFor(projectId, {
        raw_text: text,
        source: "dashboard_project_inbox",
        actor: "dashboard",
      });
      setRawText("");
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const analyzeRequirement = useCallback(
    async (row: RawRequirement) => {
      setBusy(true);
      setError("");
      try {
        await api.enqueueObserverCommandFor(projectId, {
          command_type: "analyze_requirements",
          payload: { raw_id: row.raw_id, source: "project_inbox" },
          created_by: "dashboard",
        });
        await load();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId, load],
  );

  const confirmRequirement = useCallback(
    async (row: RawRequirement) => {
      setBusy(true);
      setError("");
      try {
        await api.updateRawRequirementStatusFor(projectId, row.raw_id, {
          status: "needs_confirmation",
        });
        await load();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId, load],
  );

  const moveToExecution = useCallback(
    async (row: RawRequirement) => {
      setBusy(true);
      setError("");
      try {
        await api.enqueueObserverCommandFor(projectId, {
          command_type: "move_to_execution_queue",
          payload: {
            raw_id: row.raw_id,
            promoted_bug_id: row.promoted_bug_id || "",
            source: "project_inbox",
          },
          created_by: "dashboard",
        });
        await load();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId, load],
  );

  const controlWorker = useCallback(
    async (bug: BacklogBug, action: WorkerAction) => {
      setBusy(true);
      setError("");
      try {
        await api.enqueueObserverCommandFor(projectId, {
          command_type: WORKER_CONTROL_TYPES[action],
          payload: {
            bug_id: bug.bug_id,
            worktree_branch: bug.worktree_branch || "",
            current_task_id: bug.current_task_id || "",
          },
          created_by: "dashboard",
        });
        await load();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId, load],
  );

  return (
    <div className="view project-inbox-view simple-mode">
      <div className="view-head project-inbox-head">
        <div>
          <h2 className="view-title">Simple Mode</h2>
          <p className="view-subtitle">
            Capture, confirm, track progress, and review completed work. Use Engineer Mode only when you need advanced project details.
          </p>
        </div>
        <div className="project-inbox-stats">
          <Kpi label="Unconfirmed" value={rawLane.length} />
          <Kpi label="Confirmed" value={confirmLane.length} />
          <Kpi label="In progress" value={inProgressLane.length + reviewLane.length} />
          <Kpi label="Completed" value={doneLane.length} />
        </div>
      </div>

      <section className={`project-inbox-observer simple-mode-observer tone-${observerLine.tone}`}>
        <span className={`project-inbox-observer-pill ${observerConnected ? "connected" : "waiting"}`}>
          {observerLine.label}
        </span>
        <span className="project-inbox-command-count">Queued {queuedCount}</span>
        <span className="project-inbox-command-count">Running {runningCount}</span>
        <span className="project-inbox-command-count">Completed {completedCount}</span>
        <span className="project-inbox-command-count">Failed {failedCount}</span>
        {!observerConnected ? (
          <span className="simple-mode-observer-hint">
            AI Analyze and worker controls are disabled until an observer session registers.
          </span>
        ) : null}
      </section>

      <nav className="simple-mode-tabs" role="tablist" aria-label="Simple Mode tabs">
        {TABS.map((tab) => (
          <button
            type="button"
            key={tab.key}
            role="tab"
            aria-selected={activeTab === tab.key}
            className={`simple-mode-tab${activeTab === tab.key ? " is-active" : ""}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {error ? <div className="notice error">{error}</div> : null}

      {activeTab === "before" ? (
        <BeforeDevelopmentTab
          rawText={rawText}
          setRawText={setRawText}
          busy={busy}
          onCapture={capture}
          rawRows={allRaw}
          commands={commands}
          observerConnected={observerConnected}
          readyBacklog={readyLane.filter(isBacklogItem)}
          confirmedRaw={confirmedRaw}
          onAnalyze={analyzeRequirement}
          onConfirm={confirmRequirement}
          onMoveToExecution={moveToExecution}
          onOpenDetail={(row) => setDetailRawId(row.raw_id)}
          onOpenAudit={(ref) => navigateToAudit(projectId, ref)}
        />
      ) : null}

      {activeTab === "in_progress" ? (
        <InProgressTab
          workers={[...inProgressLane, ...reviewLane].filter(
            isBacklogItem,
          )}
          commands={commands}
          observerConnected={observerConnected}
          busy={busy}
          onControl={controlWorker}
          onOpenAudit={(ref) => navigateToAudit(projectId, ref)}
        />
      ) : null}

      {activeTab === "completed" ? (
        <CompletedTab
          rows={doneLane.filter(isBacklogItem)}
          onOpenAudit={(ref) => navigateToAudit(projectId, ref)}
        />
      ) : null}

      {detailRow ? (
        <RawDetailModal
          row={detailRow}
          commands={detailRowCommands}
          onClose={() => setDetailRawId(null)}
          onOpenAudit={(ref) => navigateToAudit(projectId, ref)}
        />
      ) : null}
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: number }) {
  return (
    <div className="project-inbox-kpi">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function BeforeDevelopmentTab({
  rawText,
  setRawText,
  busy,
  onCapture,
  rawRows,
  commands,
  observerConnected,
  readyBacklog,
  confirmedRaw,
  onAnalyze,
  onConfirm,
  onMoveToExecution,
  onOpenDetail,
  onOpenAudit,
}: {
  rawText: string;
  setRawText: (value: string) => void;
  busy: boolean;
  onCapture: () => void;
  rawRows: RawRequirement[];
  commands: ObserverCommand[];
  observerConnected: boolean;
  readyBacklog: BacklogBug[];
  confirmedRaw: RawRequirement[];
  onAnalyze: (row: RawRequirement) => void;
  onConfirm: (row: RawRequirement) => void;
  onMoveToExecution: (row: RawRequirement) => void;
  onOpenDetail: (row: RawRequirement) => void;
  onOpenAudit: (ref: { bug_id?: string; raw_id?: string }) => void;
}) {
  return (
    <div className="simple-mode-tab-panel">
      <section className="project-inbox-capture">
        <textarea
          value={rawText}
          onChange={(event) => setRawText(event.target.value)}
          placeholder="Describe what you need in your own words. Capture mode stores the exact text and does not dispatch work."
          rows={5}
          aria-label="Raw requirement text"
        />
        <div className="project-inbox-capture-actions">
          <span>
            Capture only. The dashboard writes a raw requirement row; AI runs only after AI
            Analyze is clicked and a registered observer claims the command.
          </span>
          <button
            type="button"
            className="action-btn action-btn-primary"
            disabled={busy || !rawText.trim()}
            onClick={onCapture}
          >
            {busy ? "Working…" : "Capture requirement"}
          </button>
        </div>
      </section>

      <section className="simple-mode-section">
        <header className="simple-mode-section-head">
          <h3>Raw requirements</h3>
          <span className="pill pill-mono">{rawRows.length}</span>
        </header>
        {rawRows.length ? (
          <div className="simple-mode-cards">
            {rawRows.map((row) => (
              <RawRequirementCard
                key={row.raw_id}
                row={row}
                commands={commandsForRaw(row.raw_id, commands)}
                observerConnected={observerConnected}
                busy={busy}
                onAnalyze={onAnalyze}
                onConfirm={onConfirm}
                onMoveToExecution={onMoveToExecution}
                onOpenDetail={onOpenDetail}
                onOpenAudit={onOpenAudit}
              />
            ))}
          </div>
        ) : (
          <div className="project-inbox-empty">
            No raw requirements captured yet. Use the box above to write one.
          </div>
        )}
      </section>

      <section className="simple-mode-section">
        <header className="simple-mode-section-head">
          <h3>Execution queue</h3>
          <span className="pill pill-mono">{confirmedRaw.length + readyBacklog.length}</span>
        </header>
        {!confirmedRaw.length && !readyBacklog.length ? (
          <div className="project-inbox-empty">
            Confirmed requirements move here when you click "Move to execution queue".
          </div>
        ) : (
          <div className="simple-mode-cards">
            {confirmedRaw.map((row) => {
              const move = latestCommandOfType(
                commandsForRaw(row.raw_id, commands),
                "move_to_execution_queue",
              );
              const { tone, label } = statusToneLabel(move?.status);
              return (
                <article className="execution-queue-card" key={`raw-${row.raw_id}`}>
                  <div className="execution-queue-title">
                    {(row.raw_text || "Untitled requirement").slice(0, 200)}
                  </div>
                  <div className="execution-queue-meta">
                    <span>Confirmed {fmtTimestamp(row.updated_at)}</span>
                  </div>
                  <div className="project-inbox-command-row">
                    <span className={`project-inbox-command-status tone-${tone}`}>{label}</span>
                    {move?.error ? (
                      <span className="project-inbox-command-error">{move.error}</span>
                    ) : null}
                    {!observerConnected ? (
                      <span className="simple-mode-disabled-reason">
                        Disabled until observer connects
                      </span>
                    ) : null}
                  </div>
                  <div className="project-inbox-card-actions">
                    <button
                      type="button"
                      className="action-btn"
                      disabled={busy || !observerConnected || commandIsActive(move)}
                      onClick={() => onMoveToExecution(row)}
                    >
                      {commandIsActive(move) ? label : "Move to execution queue"}
                    </button>
                    <button
                      type="button"
                      className="action-btn"
                      onClick={() => onOpenDetail(row)}
                    >
                      Open detail
                    </button>
                  </div>
                </article>
              );
            })}
            {readyBacklog.map((bug) => (
              <article className="execution-queue-card backlog" key={`bug-${bug.bug_id}`}>
                <div className="execution-queue-title">{bug.title || "Untitled requirement"}</div>
                <div className="execution-queue-meta">
                  <span>Ready for development {fmtTimestamp(bug.updated_at || bug.created_at)}</span>
                </div>
                <div className="project-inbox-card-actions">
                  <button
                    type="button"
                    className="action-btn"
                    onClick={() => onOpenAudit({ bug_id: bug.bug_id })}
                  >
                    Engineer details
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function RawRequirementCard({
  row,
  commands,
  observerConnected,
  busy,
  onAnalyze,
  onConfirm,
  onMoveToExecution,
  onOpenDetail,
  onOpenAudit,
}: {
  row: RawRequirement;
  commands: ObserverCommand[];
  observerConnected: boolean;
  busy: boolean;
  onAnalyze: (row: RawRequirement) => void;
  onConfirm: (row: RawRequirement) => void;
  onMoveToExecution: (row: RawRequirement) => void;
  onOpenDetail: (row: RawRequirement) => void;
  onOpenAudit: (ref: { bug_id?: string; raw_id?: string }) => void;
}) {
  const uiStatus = rawUiStatus(row);
  const analyze = latestCommandOfType(commands, "analyze_requirements");
  const move = latestCommandOfType(commands, "move_to_execution_queue");
  const { tone: analyzeTone, label: analyzeLabel } = statusToneLabel(analyze?.status);
  return (
    <article className={`simple-raw-card status-${uiStatus}`}>
      <div className="simple-raw-card-head">
        <span className={`simple-raw-status simple-raw-status-${uiStatus}`}>
          {uiStatus === "unconfirmed" ? "Unconfirmed" : "Confirmed"}
        </span>
        <span className="simple-raw-time">{fmtTimestamp(row.created_at)}</span>
      </div>
      <div className="simple-raw-card-text">{row.raw_text}</div>
      <div className="project-inbox-command-row">
        <span className={`project-inbox-command-status tone-${analyzeTone}`}>
          AI Analyze: {analyzeLabel}
        </span>
        {analyze?.error ? (
          <span className="project-inbox-command-error">{analyze.error}</span>
        ) : null}
        {!observerConnected ? (
          <span className="simple-mode-disabled-reason">Waiting for observer</span>
        ) : null}
      </div>
      <div className="project-inbox-card-actions">
        <button
          type="button"
          className="action-btn"
          disabled={busy || !observerConnected || commandIsActive(analyze)}
          onClick={() => onAnalyze(row)}
        >
          {commandIsActive(analyze) ? analyzeLabel : "AI Analyze"}
        </button>
        {uiStatus === "unconfirmed" ? (
          <button
            type="button"
            className="action-btn"
            disabled={busy}
            onClick={() => onConfirm(row)}
          >
            Confirm
          </button>
        ) : (
          <button
            type="button"
            className="action-btn"
            disabled={busy || !observerConnected || commandIsActive(move)}
            onClick={() => onMoveToExecution(row)}
          >
            {commandIsActive(move) ? statusToneLabel(move?.status).label : "Move to execution queue"}
          </button>
        )}
        <button
          type="button"
          className="action-btn"
          onClick={() => onOpenDetail(row)}
        >
          Open detail
        </button>
        {row.promoted_bug_id ? (
          <button
            type="button"
            className="action-btn"
            onClick={() => onOpenAudit({ bug_id: row.promoted_bug_id, raw_id: row.raw_id })}
          >
            Engineer details
          </button>
        ) : null}
      </div>
    </article>
  );
}

function InProgressTab({
  workers,
  commands,
  observerConnected,
  busy,
  onControl,
  onOpenAudit,
}: {
  workers: BacklogBug[];
  commands: ObserverCommand[];
  observerConnected: boolean;
  busy: boolean;
  onControl: (bug: BacklogBug, action: WorkerAction) => void;
  onOpenAudit: (ref: { bug_id?: string; raw_id?: string }) => void;
}) {
  if (!workers.length) {
    return (
      <div className="simple-mode-tab-panel">
        <div className="project-inbox-empty">
          No workers active. Confirmed requirements show up here once a worker starts running.
        </div>
      </div>
    );
  }
  return (
    <div className="simple-mode-tab-panel">
      <div className="simple-mode-cards">
        {workers.map((bug) => (
          <WorkerCard
            key={bug.bug_id}
            bug={bug}
            commands={commandsForBacklog(bug.bug_id, commands)}
            observerConnected={observerConnected}
            busy={busy}
            onControl={onControl}
            onOpenAudit={onOpenAudit}
          />
        ))}
      </div>
    </div>
  );
}

function WorkerCard({
  bug,
  commands,
  observerConnected,
  busy,
  onControl,
  onOpenAudit,
}: {
  bug: BacklogBug;
  commands: ObserverCommand[];
  observerConnected: boolean;
  busy: boolean;
  onControl: (bug: BacklogBug, action: WorkerAction) => void;
  onOpenAudit: (ref: { bug_id?: string; raw_id?: string }) => void;
}) {
  const pause = latestCommandOfType(commands, "pause_worker");
  const cont = latestCommandOfType(commands, "continue_worker");
  const cancel = latestCommandOfType(commands, "cancel_worker");

  const runtimeState = (bug.runtime_state || "").trim().toLowerCase();
  const isPaused = runtimeState === "paused";
  const isCancelled = runtimeState === "cancelled" || runtimeState === "canceled";
  const isBlocked = runtimeState === "blocked" || runtimeState === "failed";

  const disabledReason = !observerConnected
    ? "Waiting for observer"
    : isCancelled
    ? "Worker already cancelled"
    : "";

  const progressLabel = isPaused
    ? "Work is paused"
    : isBlocked
    ? "Work is blocked"
    : "Work is in progress";

  return (
    <article className="worker-card">
      <div className="worker-card-head">
        <div className="worker-card-title">{bug.title || "Untitled requirement"}</div>
      </div>
      <div className="worker-card-meta">
        <span>{progressLabel}</span>
      </div>
      <div className="worker-card-controls">
        <ControlButton
          label={isPaused ? "Pause requested" : "Pause"}
          command={pause}
          disabled={busy || !observerConnected || isCancelled || commandIsActive(pause)}
          onClick={() => onControl(bug, "pause")}
        />
        <ControlButton
          label="Continue"
          command={cont}
          disabled={busy || !observerConnected || isCancelled || commandIsActive(cont)}
          onClick={() => onControl(bug, "continue")}
        />
        <ControlButton
          label="Cancel"
          command={cancel}
          disabled={busy || !observerConnected || isCancelled || commandIsActive(cancel)}
          onClick={() => onControl(bug, "cancel")}
        />
        <button
          type="button"
          className="action-btn"
          onClick={() => onOpenAudit({ bug_id: bug.bug_id })}
        >
          Engineer details
        </button>
      </div>
      {disabledReason ? (
        <div className="simple-mode-disabled-reason">{disabledReason}</div>
      ) : null}
    </article>
  );
}

function ControlButton({
  label,
  command,
  disabled,
  onClick,
}: {
  label: string;
  command: ObserverCommand | null;
  disabled: boolean;
  onClick: () => void;
}) {
  const { tone, label: statusLabel } = statusToneLabel(command?.status);
  const showStatus = Boolean(command);
  return (
    <div className="worker-control">
      <button type="button" className="action-btn" disabled={disabled} onClick={onClick}>
        {label}
      </button>
      {showStatus ? (
        <span className={`project-inbox-command-status tone-${tone}`}>{statusLabel}</span>
      ) : null}
    </div>
  );
}

function CompletedTab({
  rows,
  onOpenAudit,
}: {
  rows: BacklogBug[];
  onOpenAudit: (ref: { bug_id?: string; raw_id?: string }) => void;
}) {
  if (!rows.length) {
    return (
      <div className="simple-mode-tab-panel">
        <div className="project-inbox-empty">
          No completed requirements yet. Finished work appears here with its commit hash.
        </div>
      </div>
    );
  }
  return (
    <div className="simple-mode-tab-panel">
      <div className="simple-mode-cards">
        {rows.map((bug) => (
          <article className="completed-card" key={bug.bug_id}>
            <div className="completed-card-head">
              <div className="completed-card-title">{bug.title || "Untitled requirement"}</div>
            </div>
            <div className="completed-card-meta">
              {bug.status ? <span>Status: {bug.status}</span> : null}
              {bug.fixed_at ? <span>Finished {fmtTimestamp(bug.fixed_at)}</span> : null}
            </div>
            {bug.commit ? (
              <div className="completed-commit">
                <span className="completed-commit-label">commit</span>
                <span className="mono completed-commit-hash">{shortCommit(bug.commit)}</span>
              </div>
            ) : (
              <div className="completed-commit completed-commit-missing">
                No commit recorded
              </div>
            )}
            <div className="project-inbox-card-actions">
              <button
                type="button"
                className="action-btn"
                onClick={() => onOpenAudit({ bug_id: bug.bug_id })}
              >
                Engineer details
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

function simpleAiStatus(
  command: ObserverCommand | null,
): { tone: "queued" | "running" | "complete" | "failed"; message: string } | null {
  if (!command) return null;
  if (command.status === "queued" || command.status === "notified") {
    return {
      tone: "queued",
      message: "AI analysis is queued. It will start once an observer picks it up.",
    };
  }
  if (command.status === "claimed" || command.status === "running") {
    return { tone: "running", message: "AI is analyzing this requirement now." };
  }
  if (command.status === "failed" || command.status === "cancelled") {
    return {
      tone: "failed",
      message: "AI analysis did not complete. Try AI Analyze again from the requirement card.",
    };
  }
  if (command.status === "completed") {
    return { tone: "complete", message: "AI analysis is complete." };
  }
  return null;
}

function RawDetailModal({
  row,
  commands,
  onClose,
  onOpenAudit,
}: {
  row: RawRequirement;
  commands: ObserverCommand[];
  onClose: () => void;
  onOpenAudit: (ref: { bug_id?: string; raw_id?: string }) => void;
}) {
  const analyze = latestCommandOfType(commands, "analyze_requirements");
  const result = (analyze?.result || {}) as Record<string, unknown>;
  const refined =
    result["refined_requirement"] ??
    result["refined_text"] ??
    result["interpretation"] ??
    result["summary"];
  const proposedBacklog =
    result["proposed_backlog"] ?? result["backlog_proposal"] ?? result["proposed_bug"];
  const acceptance = result["acceptance_criteria"] ?? result["suggested_acceptance"];
  const risks = result["risk"] ?? result["risks"] ?? result["missing_context"];
  const aiStatus = simpleAiStatus(analyze);

  useEffect(() => {
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

  return (
    <div className="raw-detail-modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="raw-detail-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="raw-detail-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="raw-detail-modal-head">
          <div>
            <h3 id="raw-detail-modal-title">Requirement detail</h3>
          </div>
          <button
            type="button"
            className="action-btn"
            aria-label="Close detail"
            onClick={onClose}
          >
            Close
          </button>
        </header>
        <section className="raw-detail-section">
          <h4>Original request</h4>
          <p className="raw-detail-text">{row.raw_text}</p>
          <div className="raw-detail-meta">
            <span>Status: {rawUiStatus(row) === "confirmed" ? "Confirmed" : "Unconfirmed"}</span>
            <span>Captured {fmtTimestamp(row.created_at)}</span>
            {row.source ? <span>Source: {row.source}</span> : null}
          </div>
        </section>
        <section className="raw-detail-section">
          <h4>AI interpretation</h4>
          {aiStatus ? (
            <p className={`raw-detail-ai-status tone-${aiStatus.tone}`}>{aiStatus.message}</p>
          ) : null}
          {refined ? (
            <p className="raw-detail-text">{plainTextPreview(refined, 1200)}</p>
          ) : !analyze ? (
            <p className="raw-detail-empty">
              AI Analyze has not run for this requirement.
            </p>
          ) : analyze.status === "completed" ? (
            <p className="raw-detail-empty">
              AI Analyze completed but did not return a refined requirement.
            </p>
          ) : null}
        </section>
        <section className="raw-detail-section">
          <h4>Proposed work plan</h4>
          {proposedBacklog ? (
            <pre className="raw-detail-pre">{plainTextPreview(proposedBacklog, 1200)}</pre>
          ) : (
            <p className="raw-detail-empty">
              No proposed work plan yet. AI Analyze produces this when completed.
            </p>
          )}
        </section>
        {acceptance ? (
          <section className="raw-detail-section">
            <h4>Suggested acceptance criteria</h4>
            <pre className="raw-detail-pre">{plainTextPreview(acceptance, 800)}</pre>
          </section>
        ) : null}
        {risks ? (
          <section className="raw-detail-section">
            <h4>Risk or missing context</h4>
            <pre className="raw-detail-pre">{plainTextPreview(risks, 800)}</pre>
          </section>
        ) : null}
        {row.promoted_bug_id ? (
          <footer className="raw-detail-modal-foot">
            <button
              type="button"
              className="action-btn action-btn-primary"
              onClick={() => onOpenAudit({ bug_id: row.promoted_bug_id, raw_id: row.raw_id })}
            >
              Engineer details · Open in Engineer Mode
            </button>
          </footer>
        ) : null}
      </div>
    </div>
  );
}
