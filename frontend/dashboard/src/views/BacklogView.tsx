import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { BacklogBug, BacklogResponse, FileInventoryRow, NodeRecord } from "../types";

interface Props {
  backlog: BacklogResponse;
  projectId: string;
  snapshotId: string;
  nodes: NodeRecord[];
}

type StatusFilter = "OPEN" | "FIXED" | "ALL";
type PriorityFilter = "ALL" | "P0" | "P1" | "P2" | "P3";
type AttachRole = "doc" | "test" | "config";
type AttachState = "idle" | "writing" | "written_uncommitted" | "error";

const PRIORITIES: PriorityFilter[] = ["ALL", "P0", "P1", "P2", "P3"];
const PRIORITY_WEIGHT: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 };
const CLOSED_STATUSES = new Set(["FIXED", "CLOSED", "DONE", "RESOLVED", "CANCELLED"]);

interface AttachDraft {
  targetNodeId: string;
  role: AttachRole;
}

interface AttachResult {
  state: AttachState;
  message: string;
}

export default function BacklogView({ backlog, projectId, snapshotId, nodes }: Props) {
  const bugs = backlog.bugs ?? [];
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("OPEN");
  const [priorityFilter, setPriorityFilter] = useState<PriorityFilter>("ALL");
  const [query, setQuery] = useState("");
  const [files, setFiles] = useState<FileInventoryRow[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesError, setFilesError] = useState("");
  const [drafts, setDrafts] = useState<Record<string, AttachDraft>>({});
  const [attachResults, setAttachResults] = useState<Record<string, AttachResult>>({});

  const stats = useMemo(() => {
    const open = bugs.filter(isOpenBug);
    return {
      total: bugs.length,
      open: open.length,
      fixed: bugs.filter((b) => normalizeStatus(b.status) === "FIXED").length,
      urgent: open.filter((b) => ["P0", "P1"].includes(normalizePriority(b.priority))).length,
    };
  }, [bugs]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return bugs
      .filter((bug) => {
        if (statusFilter === "OPEN" && !isOpenBug(bug)) return false;
        if (statusFilter === "FIXED" && normalizeStatus(bug.status) !== "FIXED") return false;
        if (priorityFilter !== "ALL" && normalizePriority(bug.priority) !== priorityFilter) return false;
        if (!q) return true;
        const hay = [
          bug.bug_id,
          bug.title,
          bug.details_md,
          bug.status,
          bug.priority,
          ...listFrom(bug.target_files),
          ...listFrom(bug.test_files),
          ...listFrom(bug.acceptance_criteria),
        ]
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      })
      .slice()
      .sort(compareBugs);
  }, [bugs, priorityFilter, query, statusFilter]);

  const nodeOptions = useMemo(
    () =>
      nodes
        .filter((node) => (node.layer || "").toUpperCase() === "L7")
        .slice()
        .sort((a, b) => (a.title || a.node_id).localeCompare(b.title || b.node_id)),
    [nodes],
  );

  const attachableFiles = useMemo(
    () =>
      files.filter((file) => {
        const kind = normalizeFileKind(file.file_kind);
        const status = normalizeStatus(file.scan_status);
        return (
          ["doc", "test", "config"].includes(kind) &&
          status === "ORPHAN" &&
          !hasAttachedNode(file)
        );
      }),
    [files],
  );

  useEffect(() => {
    if (!snapshotId) return;
    const ac = new AbortController();
    setFilesLoading(true);
    setFilesError("");
    api.snapshotFiles(snapshotId, { limit: 1000, sort: "path" }, ac.signal)
      .then((res) => {
        if (!ac.signal.aborted) setFiles(res.files ?? []);
      })
      .catch((error) => {
        if (ac.signal.aborted) return;
        const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
        setFilesError(msg);
      })
      .finally(() => {
        if (!ac.signal.aborted) setFilesLoading(false);
      });
    return () => ac.abort();
  }, [snapshotId, projectId]);

  const updateDraft = (path: string, patch: Partial<AttachDraft>) => {
    setDrafts((current) => {
      const existing = current[path] ?? {
        targetNodeId: nodeOptions[0]?.node_id ?? "",
        role: roleForFile(files.find((file) => file.path === path)),
      };
      return { ...current, [path]: { ...existing, ...patch } };
    });
  };

  const writeHint = async (file: FileInventoryRow) => {
    const draft = drafts[file.path] ?? {
      targetNodeId: nodeOptions[0]?.node_id ?? "",
      role: roleForFile(file),
    };
    if (!draft.targetNodeId) {
      setAttachResults((current) => ({
        ...current,
        [file.path]: { state: "error", message: "Select a target node first." },
      }));
      return;
    }
    setAttachResults((current) => ({
      ...current,
      [file.path]: { state: "writing", message: "Writing governance hint..." },
    }));
    try {
      const result = await api.attachFileGovernanceHint(snapshotId, {
        path: file.path,
        target_node_id: draft.targetNodeId,
        role: draft.role,
        actor: "dashboard_user",
      });
      setAttachResults((current) => ({
        ...current,
        [file.path]: {
          state: "written_uncommitted",
          message: result.message || "Hint written. Commit this file, then run Update graph.",
        },
      }));
    } catch (error) {
      const msg = error instanceof ApiError ? `${error.message} ${error.body}` : String(error);
      setAttachResults((current) => ({
        ...current,
        [file.path]: { state: "error", message: msg },
      }));
    }
  };

  return (
    <div className="view">
      <div className="view-head">
        <h2 className="view-title">Backlog</h2>
        <span className="view-subtitle">
          source <span className="mono">/api/backlog/{projectId}</span> ·{" "}
          {rows.length} shown · {stats.total} total
        </span>
      </div>

      <div className="backlog-guidance">
        <div>
          <strong>Project memory.</strong> Backlog rows stay read-only here; orphan file binding
          writes source-controlled hints that reconcile can materialize.
        </div>
        <span className="mono">manual filing hidden in v1</span>
      </div>

      <div className="score-grid backlog-score-grid">
        <Kpi label="Open" value={stats.open} tone={stats.open > 0 ? "amber" : "green"} />
        <Kpi label="P0/P1 open" value={stats.urgent} tone={stats.urgent > 0 ? "red" : "neutral"} />
        <Kpi label="Fixed" value={stats.fixed} tone="green" />
        <Kpi label="Total" value={stats.total} tone="blue" />
      </div>

      <div className="section">
        <div className="section-head">
          Orphan file binding{" "}
          <span className="head-hint">
            {filesLoading ? "loading" : `${attachableFiles.length} orphan files · write hint, commit, then Update graph`}
          </span>
        </div>
        <div className="backlog-guidance backlog-guidance-amber">
          <div>
            <strong>Direct write flow.</strong> This writes a governance hint into the selected file only.
            Commit that file before clicking <span className="mono">Update graph</span>, because reconcile reads committed source.
          </div>
          <span className="mono">state: source write → commit → graph update</span>
        </div>
        {filesError ? <div className="config-warning error">File inventory load failed: {filesError}</div> : null}
        {attachableFiles.length === 0 ? (
          <div className="empty empty-compact">
            No orphan doc/test/config files are attachable in this snapshot.
          </div>
        ) : (
          <div className="card">
            <table className="table backlog-orphan-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th style={{ width: 92 }}>Role</th>
                  <th style={{ width: 280 }}>Target node</th>
                  <th style={{ width: 168 }}>Action</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {attachableFiles.slice(0, 20).map((file) => {
                  const draft = drafts[file.path] ?? {
                    targetNodeId: nodeOptions[0]?.node_id ?? "",
                    role: roleForFile(file),
                  };
                  const result = attachResults[file.path] ?? { state: "idle", message: "Not written." };
                  const supported = canDirectWriteHint(file.path);
                  const disabled =
                    !supported ||
                    nodeOptions.length === 0 ||
                    result.state === "writing" ||
                    result.state === "written_uncommitted";
                  return (
                    <tr key={file.path}>
                      <td>
                        <div className="cell-strong mono">{file.path}</div>
                        <div className="cell-mono-id">
                          {file.file_kind || "unknown"} · {file.scan_status || "pending"}
                        </div>
                      </td>
                      <td>
                        <select
                          value={draft.role}
                          onChange={(event) => updateDraft(file.path, { role: event.target.value as AttachRole })}
                          disabled={result.state === "writing" || result.state === "written_uncommitted"}
                        >
                          <option value="doc">doc</option>
                          <option value="test">test</option>
                          <option value="config">config</option>
                        </select>
                      </td>
                      <td>
                        <select
                          className="backlog-node-select"
                          value={draft.targetNodeId}
                          onChange={(event) => updateDraft(file.path, { targetNodeId: event.target.value })}
                          disabled={result.state === "writing" || result.state === "written_uncommitted"}
                        >
                          {nodeOptions.map((node) => (
                            <option key={node.node_id} value={node.node_id}>
                              {node.title || node.node_id} · {node.node_id}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <button
                          className="action-btn action-btn-primary"
                          disabled={disabled}
                          onClick={() => writeHint(file)}
                          title={supported ? "Write governance hint into the file" : "This file type cannot be safely commented"}
                        >
                          {result.state === "writing" ? "Writing..." : "Write hint"}
                        </button>
                      </td>
                      <td>
                        <div className={`attach-state attach-state-${result.state}`}>
                          {supported ? result.message : "Direct write unsupported for this file type."}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="backlog-toolbar card">
        <div className="backlog-filter-group">
          {(["OPEN", "FIXED", "ALL"] as StatusFilter[]).map((s) => (
            <button
              key={s}
              className={`chip ${statusFilter === s ? "on" : "off"}`}
              onClick={() => setStatusFilter(s)}
            >
              {s === "ALL" ? "All status" : s}
            </button>
          ))}
        </div>
        <div className="backlog-filter-group">
          {PRIORITIES.map((p) => (
            <button
              key={p}
              className={`chip ${priorityFilter === p ? "on" : "off"}`}
              onClick={() => setPriorityFilter(p)}
            >
              {p === "ALL" ? "All priority" : p}
            </button>
          ))}
        </div>
        <input
          className="backlog-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search backlog, files, criteria..."
        />
      </div>

      <div className="section">
        <div className="section-head">
          Rows <span className="head-hint">read-only, sorted by priority and updated time</span>
        </div>
        {rows.length === 0 ? (
          <div className="empty">
            No backlog rows match the current filters.
            <div className="empty-hint">
              Use an AI-backed graph action to file a row with node/file context.
            </div>
          </div>
        ) : (
          <div className="card">
            <table className="table backlog-table">
              <thead>
                <tr>
                  <th style={{ width: 82 }}>Priority</th>
                  <th style={{ width: 94 }}>Status</th>
                  <th>Backlog</th>
                  <th style={{ width: 260 }}>Scope</th>
                  <th style={{ width: 132 }}>Runtime</th>
                  <th style={{ width: 112 }}>Updated</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((bug) => (
                  <BacklogRow key={bug.bug_id} bug={bug} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function BacklogRow({ bug }: { bug: BacklogBug }) {
  const files = listFrom(bug.target_files);
  const criteria = listFrom(bug.acceptance_criteria);
  const runtime = bug.runtime_state || bug.chain_stage || bug.mf_type || "idle";
  return (
    <tr>
      <td>
        <span className={`backlog-priority tone-${priorityTone(bug.priority)}`}>
          {normalizePriority(bug.priority)}
        </span>
      </td>
      <td>
        <span className={`status-badge ${statusClass(bug.status)}`}>
          {normalizeStatus(bug.status)}
        </span>
      </td>
      <td className="backlog-title-cell">
        <div className="cell-strong">{bug.title || bug.bug_id}</div>
        <div className="cell-mono-id">{bug.bug_id}</div>
        {bug.details_md ? <div className="backlog-details">{truncate(bug.details_md, 220)}</div> : null}
        {criteria.length > 0 ? (
          <div className="backlog-criteria">
            {criteria.slice(0, 2).map((item) => (
              <span key={item}>{item}</span>
            ))}
            {criteria.length > 2 ? <em>+{criteria.length - 2}</em> : null}
          </div>
        ) : null}
      </td>
      <td>
        {files.length > 0 ? (
          <div className="backlog-file-list">
            {files.slice(0, 4).map((file) => (
              <span className="mono" key={file} title={file}>
                {file}
              </span>
            ))}
            {files.length > 4 ? <em>+{files.length - 4} more</em> : null}
          </div>
        ) : (
          <span className="muted">No target files</span>
        )}
      </td>
      <td>
        <div className="mono">{runtime}</div>
        {bug.commit ? <div className="backlog-commit mono">{shortCommit(bug.commit)}</div> : null}
        {bug.worktree_branch ? <div className="backlog-commit mono">{bug.worktree_branch}</div> : null}
      </td>
      <td>
        <span className="mono">{shortDate(bug.updated_at || bug.created_at || bug.fixed_at)}</span>
      </td>
    </tr>
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

function compareBugs(a: BacklogBug, b: BacklogBug): number {
  const openDelta = Number(isOpenBug(b)) - Number(isOpenBug(a));
  if (openDelta !== 0) return openDelta;
  const priorityDelta =
    (PRIORITY_WEIGHT[normalizePriority(a.priority)] ?? 99) -
    (PRIORITY_WEIGHT[normalizePriority(b.priority)] ?? 99);
  if (priorityDelta !== 0) return priorityDelta;
  return dateValue(b.updated_at || b.created_at || b.fixed_at) - dateValue(a.updated_at || a.created_at || a.fixed_at);
}

function isOpenBug(bug: BacklogBug): boolean {
  return !CLOSED_STATUSES.has(normalizeStatus(bug.status));
}

function normalizeStatus(status?: string): string {
  return (status || "OPEN").toUpperCase();
}

function normalizePriority(priority?: string): string {
  return (priority || "P3").toUpperCase();
}

function priorityTone(priority?: string): string {
  const p = normalizePriority(priority);
  if (p === "P0") return "red";
  if (p === "P1") return "amber";
  if (p === "P2") return "blue";
  return "neutral";
}

function statusClass(status?: string): string {
  const s = normalizeStatus(status);
  if (s === "FIXED" || s === "DONE" || s === "RESOLVED") return "status-complete";
  if (s === "FAILED" || s === "BLOCKED") return "status-failed";
  if (s === "RUNNING" || s === "CLAIMED" || s === "IN_CHAIN") return "status-running";
  if (s === "OPEN" || s === "QUEUED") return "status-pending";
  return "status-unknown";
}

function normalizeFileKind(kind?: string): string {
  return (kind || "").trim().toLowerCase();
}

function roleForFile(file?: FileInventoryRow): AttachRole {
  const kind = normalizeFileKind(file?.file_kind);
  if (kind === "test") return "test";
  if (kind === "config") return "config";
  return "doc";
}

function hasAttachedNode(file: FileInventoryRow): boolean {
  return Array.isArray(file.attached_node_ids) && file.attached_node_ids.length > 0;
}

function canDirectWriteHint(path: string): boolean {
  const lower = path.toLowerCase();
  const name = lower.split(/[\\/]/).pop() || "";
  if (name === "dockerfile" || name === "makefile") return true;
  return [
    ".md",
    ".mdx",
    ".html",
    ".htm",
    ".py",
    ".pyw",
    ".sh",
    ".bash",
    ".ps1",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".txt",
    ".rst",
    ".adoc",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
  ].some((suffix) => lower.endsWith(suffix));
}

function listFrom(value?: string[] | string): string[] {
  if (!value) return [];
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  const text = String(value).trim();
  if (!text) return [];
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean);
  } catch {
    // Fall back to line/comma splitting for legacy rows.
  }
  return text
    .split(/\r?\n|,\s+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function truncate(text: string, max: number): string {
  const oneLine = text.replace(/\s+/g, " ").trim();
  if (oneLine.length <= max) return oneLine;
  return `${oneLine.slice(0, max - 1)}…`;
}

function shortDate(value?: string): string {
  if (!value) return "—";
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return value.slice(0, 10) || "—";
  return new Date(time).toISOString().slice(0, 10);
}

function dateValue(value?: string): number {
  if (!value) return 0;
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : 0;
}

function shortCommit(commit: string): string {
  return commit.length > 10 ? commit.slice(0, 7) : commit;
}
