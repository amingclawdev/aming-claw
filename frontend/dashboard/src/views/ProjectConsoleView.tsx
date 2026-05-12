import { useEffect, useMemo, useState } from "react";
import type {
  ActiveSummaryResponse,
  BacklogResponse,
  OperationsQueueResponse,
  StatusResponse,
} from "../types";
import { api, ApiError, type AiConfigResponse, type ProjectListItem } from "../lib/api";

interface Props {
  projects: ProjectListItem[];
  currentProjectId: string;
  loading: boolean;
  onOpenProject(projectId: string): void;
  onOpenAiConfig(): void;
}

interface ProjectRuntime {
  projectId: string;
  status?: StatusResponse;
  summary?: ActiveSummaryResponse;
  ops?: OperationsQueueResponse;
  backlog?: BacklogResponse;
  aiConfig?: AiConfigResponse;
  error?: string;
}

const CLOSED_BACKLOG_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED"]);

export default function ProjectConsoleView({
  projects,
  currentProjectId,
  loading,
  onOpenProject,
  onOpenAiConfig,
}: Props) {
  const [runtime, setRuntime] = useState<Record<string, ProjectRuntime>>({});
  const [runtimeLoading, setRuntimeLoading] = useState(false);
  const projectKey = useMemo(() => projects.map((p) => p.project_id).join("\u0000"), [projects]);

  useEffect(() => {
    if (projects.length === 0) {
      setRuntime({});
      return;
    }
    const ac = new AbortController();
    setRuntimeLoading(true);
    void loadProjectRuntime(projects, ac.signal)
      .then((rows) => {
        if (ac.signal.aborted) return;
        setRuntime(Object.fromEntries(rows.map((row) => [row.projectId, row])));
      })
      .finally(() => {
        if (!ac.signal.aborted) setRuntimeLoading(false);
      });
    return () => ac.abort();
  }, [projectKey, projects]);

  const rows = useMemo(
    () =>
      projects
        .slice()
        .sort((a, b) => {
          if (a.project_id === currentProjectId) return -1;
          if (b.project_id === currentProjectId) return 1;
          return a.project_id.localeCompare(b.project_id);
        }),
    [currentProjectId, projects],
  );

  const stats = useMemo(() => {
    const runtimes = Object.values(runtime);
    return {
      total: projects.length,
      current: runtimes.filter((r) => r.status?.current_state?.graph_stale?.is_stale === false).length,
      stale: runtimes.filter((r) => r.status?.current_state?.graph_stale?.is_stale === true).length,
      backlogOpen: runtimes.reduce((sum, r) => sum + countOpenBacklog(r.backlog), 0),
    };
  }, [projects.length, runtime]);

  return (
    <div className="view project-console">
      <div className="view-head">
        <h2 className="view-title">Projects</h2>
        <span className="view-subtitle">
          local plugin console · {rows.length} registered · current{" "}
          <span className="mono">{currentProjectId}</span>
        </span>
      </div>

      <div className="score-grid project-console-score-grid">
        <Kpi label="Registered" value={stats.total} tone="blue" />
        <Kpi label="Graph current" value={stats.current} tone="green" />
        <Kpi label="Graph stale" value={stats.stale} tone={stats.stale > 0 ? "amber" : "neutral"} />
        <Kpi label="Open backlog" value={stats.backlogOpen} tone={stats.backlogOpen > 0 ? "amber" : "neutral"} />
      </div>

      <div className="section">
        <div className="section-head">
          Project Registry{" "}
          <span className="head-hint">
            {runtimeLoading || loading ? "refreshing" : "live"}
          </span>
        </div>
        <div className="card">
          <table className="table project-console-table">
            <thead>
              <tr>
                <th>Project</th>
                <th style={{ width: 150 }}>Graph</th>
                <th style={{ width: 170 }}>Snapshot</th>
                <th style={{ width: 150 }}>Scale</th>
                <th style={{ width: 140 }}>Work</th>
                <th style={{ width: 180 }}>AI</th>
                <th>Workspace</th>
                <th style={{ width: 150 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((project) => {
                const row = runtime[project.project_id];
                const selected = project.project_id === currentProjectId;
                return (
                  <ProjectRow
                    key={project.project_id}
                    project={project}
                    runtime={row}
                    selected={selected}
                    onOpenProject={onOpenProject}
                    onOpenAiConfig={onOpenAiConfig}
                  />
                );
              })}
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={8} className="empty" style={{ padding: 16 }}>
                    No registered projects.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function ProjectRow({
  project,
  runtime,
  selected,
  onOpenProject,
  onOpenAiConfig,
}: {
  project: ProjectListItem;
  runtime?: ProjectRuntime;
  selected: boolean;
  onOpenProject(projectId: string): void;
  onOpenAiConfig(): void;
}) {
  const graphStale = runtime?.status?.current_state?.graph_stale;
  const summary = runtime?.summary;
  const ops = runtime?.ops;
  const backlogOpen = countOpenBacklog(runtime?.backlog);
  const aiRoute = runtime?.aiConfig?.semantic;
  const hasGraph = Boolean(runtime?.status || runtime?.summary);
  const graphClass = runtime?.error
    ? "status-failed"
    : graphStale?.is_stale
      ? "status-pending"
      : hasGraph
        ? "status-complete"
        : "status-unknown";
  const graphLabel = runtime?.error
    ? "unavailable"
    : graphStale?.is_stale
      ? "stale"
      : hasGraph
        ? "current"
        : "unknown";

  return (
    <tr className={selected ? "project-console-selected" : ""}>
      <td>
        <div className="project-console-name">
          <span className="cell-strong">{project.name || project.project_id}</span>
          {selected ? <span className="project-console-current">current</span> : null}
        </div>
        <div className="cell-mono-id">{project.project_id}</div>
        <div className="project-console-sub">
          {project.status || (project.initialized ? "initialized" : "registered")}
        </div>
      </td>
      <td>
        <span className={`status-badge ${graphClass}`}>{graphLabel}</span>
        {runtime?.status?.pending_scope_reconcile_count ? (
          <div className="project-console-sub mono">
            pending scope {runtime.status.pending_scope_reconcile_count}
          </div>
        ) : null}
        {runtime?.error ? <div className="project-console-error">{runtime.error}</div> : null}
      </td>
      <td>
        <div className="mono">{runtime?.status?.active_snapshot_id || project.active_snapshot_id || "—"}</div>
        <div className="project-console-sub mono">
          {shortCommit(runtime?.status?.graph_snapshot_commit || summary?.commit_sha || "")}
        </div>
      </td>
      <td>
        <MetricLine label="nodes" value={summary?.counts.nodes ?? project.node_count} />
        <MetricLine label="files" value={summary?.counts.files} />
        <MetricLine label="features" value={summary?.counts.features} />
      </td>
      <td>
        <MetricLine label="ops" value={ops?.count} />
        <MetricLine label="backlog" value={backlogOpen} />
        <MetricLine label="review" value={ops?.summary?.feedback_queue?.visible_group_count} />
      </td>
      <td>
        <div>{formatRoute(aiRoute)}</div>
        <div className="project-console-sub">
          {runtime?.aiConfig?.read_only ? "read-only" : runtime?.aiConfig ? "configured" : "—"}
        </div>
      </td>
      <td>
        <span className="project-console-workspace mono" title={project.workspace_path || ""}>
          {project.workspace_path || "—"}
        </span>
      </td>
      <td>
        <button
          className="action-btn"
          onClick={() => onOpenProject(project.project_id)}
          title="Open this project in the dashboard"
        >
          Open
        </button>
        <button
          className="action-btn"
          disabled={!selected}
          onClick={onOpenAiConfig}
          title={selected ? "Open AI configuration" : "Open the project first"}
        >
          AI config
        </button>
      </td>
    </tr>
  );
}

function MetricLine({ label, value }: { label: string; value?: number }) {
  return (
    <div className="project-console-metric">
      <span>{label}</span>
      <span className="mono">{value ?? "—"}</span>
    </div>
  );
}

function Kpi({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "green" | "amber" | "red" | "blue" | "neutral";
}) {
  return (
    <div className={`score-card count-card tone-${tone}`}>
      <div className="accent-bar" />
      <div className="lbl">{label}</div>
      <div className="val">{value}</div>
    </div>
  );
}

async function loadProjectRuntime(projects: ProjectListItem[], signal: AbortSignal): Promise<ProjectRuntime[]> {
  return Promise.all(projects.map((project) => loadOneProjectRuntime(project, signal)));
}

async function loadOneProjectRuntime(project: ProjectListItem, signal: AbortSignal): Promise<ProjectRuntime> {
  const projectId = project.project_id;
  const [status, summary, ops, backlog, aiConfig] = await Promise.allSettled([
    api.statusFor(projectId, signal),
    api.activeSummaryFor(projectId, signal),
    api.operationsQueueFor(projectId, signal),
    api.backlogFor(projectId, signal),
    api.aiConfigFor(projectId, signal),
  ]);
  return {
    projectId,
    status: settledValue(status),
    summary: settledValue(summary),
    ops: settledValue(ops),
    backlog: settledValue(backlog),
    aiConfig: settledValue(aiConfig),
    error: firstError(status, summary),
  };
}

function settledValue<T>(result: PromiseSettledResult<T>): T | undefined {
  return result.status === "fulfilled" ? result.value : undefined;
}

function firstError(...results: PromiseSettledResult<unknown>[]): string | undefined {
  const failed = results.find((result) => result.status === "rejected");
  if (!failed || failed.status !== "rejected") return undefined;
  const reason = failed.reason;
  if (reason instanceof ApiError) return `HTTP ${reason.status}`;
  return reason instanceof Error ? reason.message : "error";
}

function countOpenBacklog(backlog?: BacklogResponse): number {
  return (
    backlog?.bugs?.filter((bug) => {
      const status = String(bug.status || "OPEN").toUpperCase();
      return !CLOSED_BACKLOG_STATUSES.has(status);
    }).length ?? 0
  );
}

function shortCommit(commit: string): string {
  if (!commit) return "—";
  return commit.length > 10 ? commit.slice(0, 7) : commit;
}

function formatRoute(route?: { provider?: string; model?: string } | null): string {
  if (!route) return "—";
  const provider = route.provider || "default";
  const model = route.model || "default";
  return `${provider} / ${model}`;
}
