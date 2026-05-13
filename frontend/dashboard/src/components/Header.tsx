import type { ReactNode } from "react";
import type {
  ActiveSummaryResponse,
  HealthResponse,
  OperationsQueueResponse,
  StatusResponse,
} from "../types";
import type { LiveStatus } from "../lib/sse";
import type { AiConfigResponse, ProjectListItem } from "../lib/api";

interface Props {
  loading: boolean;
  summary?: ActiveSummaryResponse;
  status?: StatusResponse;
  health?: HealthResponse;
  ops?: OperationsQueueResponse;
  loadedAt?: string;
  projectId: string;
  projects: ProjectListItem[];
  aiConfig?: AiConfigResponse | null;
  // SSE liveness: green dot when streaming, amber while reconnecting, gray
  // when offline. Drives the "live" pill next to the snapshot status.
  liveStatus?: LiveStatus;
  onRefresh(): void;
  onProjectChange(projectId: string): void;
  onOpenAiConfig(): void;
  // Multi-select mode (batch AI enrich many nodes/edges at once). When ON,
  // graph clicks add/remove from a bucket and the header shows the count +
  // Confirm / Clear / Exit controls. OFF state shows just the "Multi-select"
  // toggle button.
  multiSelectMode?: boolean;
  multiSelectCount?: number;
  batchEnrichBusy?: boolean;
  onToggleMultiSelect?(): void;
  onBatchEnrich?(): void;
  onClearMultiSelect?(): void;
}

export default function Header({
  loading,
  summary,
  status,
  health,
  ops,
  loadedAt,
  projectId,
  projects,
  aiConfig,
  liveStatus = "offline",
  onRefresh,
  onProjectChange,
  onOpenAiConfig,
  multiSelectMode = false,
  multiSelectCount = 0,
  batchEnrichBusy = false,
  onToggleMultiSelect,
  onBatchEnrich,
  onClearMultiSelect,
}: Props) {
  const projectHealth = summary?.health.project_health_score;
  const semanticHealth = summary?.health.semantic_health_score;
  const structureHealth = summary?.health.structure_health_score;
  const sem = summary?.health.semantic_health;
  const counts = summary?.counts;
  const opsCount = ops?.count ?? 0;
  const commit = (status?.graph_snapshot_commit || summary?.commit_sha || "").slice(0, 7);
  const snapshotId = status?.active_snapshot_id || summary?.snapshot_id || "—";
  const updatedAt = loadedAt ? new Date(loadedAt).toLocaleString() : "—";

  // Governed denominators are authoritative for the header semantic count.
  const governedTotal = sem?.feature_count ?? counts?.features ?? 0;
  const semCurrent = sem?.semantic_current_count ?? 0;
  const semStale = sem?.semantic_stale_count ?? 0;
  const semUnverified = sem?.semantic_unverified_hash_count ?? 0;
  const semMissing = sem?.semantic_missing_count ?? 0;
  const edgeEligible = sem?.edge_semantic_eligible_count ?? 0;
  const edgeCurrent = sem?.edge_semantic_current_count ?? 0;
  const edgeMissing = sem?.edge_semantic_missing_count ?? 0;
  const activeProject = projects.find((project) => project.project_id === projectId);
  const activeProjectLabel = activeProject?.name?.trim() || projectId;

  return (
    <header className="header">
      <div className="header-brand">
        <div className="header-logo">a</div>
        <div>
          <div className="header-title-row">
            <div className="header-title" title={projectId}>{activeProjectLabel}</div>
            <span className="pill pill-active">
              <span className="pill-dot" />
              {summary?.snapshot_status ?? "—"}
            </span>
            <LivePill status={liveStatus} />
            <span
              className="pill pill-mono"
              title="Frontend build marker"
            >
              p0 · dashboard
            </span>
          </div>
          <div className="header-meta">
            <select
              className="project-select"
              value={projectId}
              onChange={(event) => onProjectChange(event.target.value)}
              title="Switch registered project"
            >
              {projects.length ? (
                projects.map((project) => (
                  <option key={project.project_id} value={project.project_id}>
                    {project.name?.trim() && project.name.trim() !== project.project_id
                      ? `${project.name.trim()} · ${project.project_id}`
                      : project.project_id}
                  </option>
                ))
              ) : (
                <option value={projectId}>{projectId}</option>
              )}
            </select>
            <span>·</span>
            <span className="mono" title={status?.graph_snapshot_commit ?? ""}>
              {commit || "—"}
            </span>
            <span>·</span>
            <span className="mono">{snapshotId}</span>
            <span>·</span>
            <span title="Loaded at">{updatedAt}</span>
          </div>
        </div>
      </div>

      <div className="header-divider" />

      <div className="header-gauge">
        <Gauge value={projectHealth} />
        <div className="health-pills">
          <HealthPill label="Structure" value={structureHealth} />
          <HealthPill label="Semantic" value={semanticHealth} />
          <HealthPill
            label="Insight"
            value={summary?.health.project_insight_health_score}
            placeholder="pending"
          />
        </div>
      </div>

      <button
        className="btn-action"
        onClick={onOpenAiConfig}
        title="View AI routing and semantic worker configuration"
      >
        <span className="btn-action-icon">⚙</span>
        <span>AI config</span>
        {aiConfig?.read_only ? <span className="btn-action-badge">read</span> : null}
      </button>

      {onToggleMultiSelect ? (
        <div className="multi-select-bar">
          <button
            className={`btn-action${multiSelectMode ? " multi-on" : ""}`}
            onClick={onToggleMultiSelect}
            title={
              multiSelectMode
                ? "Exit multi-select (graph clicks pin / navigate again)"
                : "Enter multi-select (graph clicks add to batch bucket)"
            }
          >
            <span className="btn-action-icon">{multiSelectMode ? "☑" : "☐"}</span>
            <span>{multiSelectMode ? "Multi-select on" : "Multi-select"}</span>
            {multiSelectMode && multiSelectCount > 0 ? (
              <span className="btn-action-badge">{multiSelectCount}</span>
            ) : null}
          </button>
          {multiSelectMode ? (
            <>
              <button
                className="action-btn action-btn-primary"
                onClick={onBatchEnrich}
                disabled={batchEnrichBusy || multiSelectCount === 0}
                title={`Queue AI enrich for ${multiSelectCount} selected target(s)`}
                style={{ flex: "initial", padding: "6px 12px", marginLeft: 6 }}
              >
                {batchEnrichBusy ? "Queuing…" : `⚡ Enrich ${multiSelectCount}`}
              </button>
              {multiSelectCount > 0 ? (
                <button
                  className="action-btn"
                  onClick={onClearMultiSelect}
                  disabled={batchEnrichBusy}
                  title="Clear selection (stays in multi-select mode)"
                >
                  Clear
                </button>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}

      <div className="header-kpis">
        <Kpi
          label="Health · Structure / Semantic"
          value={
            structureHealth != null && semanticHealth != null
              ? `${structureHealth.toFixed(2)} / ${semanticHealth.toFixed(2)}`
              : "—"
          }
          sub={`raw ${summary?.health.raw_project_health_score?.toFixed(2) ?? "—"}`}
        />
        <Kpi
          label="Graph nodes / Features"
          value={`${counts?.nodes ?? "—"} / ${governedTotal}`}
          sub={`raw L7 ${counts?.nodes_by_layer?.L7 ?? "—"} · edges ${counts?.edges ?? "—"}`}
        />
        <Kpi
          label="Files"
          value={`${counts?.files ?? "—"}`}
          sub={`orphan ${counts?.orphan_files ?? 0} · pending ${
            counts?.pending_decision_files ?? 0
          }`}
        />
        <Kpi
          label="Node semantic"
          value={`${semCurrent}/${governedTotal}`}
          sub={
            <>
              <span className="num-current">{semCurrent} current</span>
              {" · "}
              <span className="num-stale">{semStale} stale</span>
              {semUnverified > 0 ? (
                <>
                  {" · "}
                  <span className="num-unverified">{semUnverified} hash-unverified</span>
                </>
              ) : null}
              {semMissing > 0 ? (
                <>
                  {" · "}
                  <span className="num-missing">{semMissing} missing</span>
                </>
              ) : null}
            </>
          }
        />
        <Kpi
          label="Edge semantic"
          value={`${edgeCurrent}/${edgeEligible}`}
          sub={`${edgeMissing} missing · ${(sem?.edge_semantic_requested_count ?? 0)} requested`}
        />
        <Kpi
          label="Operations queue"
          value={`${opsCount}`}
          sub={`pending reconcile ${status?.pending_scope_reconcile_count ?? 0}`}
        />
      </div>

      <button
        className="btn-refresh"
        onClick={onRefresh}
        disabled={loading}
        title="Re-fetch summary, status, projection, nodes, ops, and feedback queue"
      >
        {loading ? <span className="spinner" /> : "↻"} Refresh
      </button>

      <span className="pill pill-mono" title="Service version">
        svc {(health?.version || "—").slice(0, 7)}
      </span>
    </header>
  );
}

function LivePill({ status }: { status: LiveStatus }) {
  const tone = status === "live" ? "green" : status === "connecting" ? "amber" : "neutral";
  const label = status === "live" ? "live" : status === "connecting" ? "connecting" : "offline";
  const title =
    status === "live"
      ? "Connected to governance event stream — dashboard refreshes automatically"
      : status === "connecting"
        ? "Reconnecting to governance event stream…"
        : "Event stream offline — refreshes are manual (↻)";
  return (
    <span className={`pill live-pill tone-${tone}`} title={title}>
      <span className="pill-dot" />
      {label}
    </span>
  );
}

function HealthPill({
  label,
  value,
  placeholder,
}: {
  label: string;
  value?: number;
  placeholder?: string;
}) {
  const tone = value == null ? "neutral" : value >= 85 ? "green" : value >= 70 ? "amber" : "red";
  return (
    <span className={`health-pill tone-${tone}`} title={`${label}: ${value != null ? value.toFixed(2) : placeholder ?? "—"}`}>
      <span className="health-pill-label">{label}</span>
      <span className="health-pill-value">
        {value != null ? Math.round(value) : placeholder ?? "—"}
      </span>
    </span>
  );
}

function Kpi({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub: ReactNode;
}) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value" title={value}>
        {value}
      </div>
      <div className="kpi-sub">{sub}</div>
    </div>
  );
}

function Gauge({ value }: { value: number | undefined }) {
  const v = value ?? 0;
  const r = 26;
  const c = 2 * Math.PI * r;
  const ratio = Math.max(0, Math.min(1, v / 100));
  const offset = c * (1 - ratio);
  const color = v >= 85 ? "var(--green)" : v >= 70 ? "var(--amber)" : "var(--red)";
  return (
    <div
      className="gauge"
      title={
        value != null ? `Project health ${value.toFixed(2)} / 100` : "Project health unavailable"
      }
    >
      <svg width="64" height="64" viewBox="0 0 64 64">
        <circle cx="32" cy="32" r={r} fill="none" stroke="var(--ink-200)" strokeWidth="6" />
        <circle
          cx="32"
          cy="32"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={offset}
        />
      </svg>
      <div className="gauge-label">
        <div className="gauge-value">{value != null ? value.toFixed(0) : "—"}</div>
        <div className="gauge-caption">health</div>
      </div>
    </div>
  );
}
