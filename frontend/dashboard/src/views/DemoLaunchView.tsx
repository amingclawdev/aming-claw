import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../lib/api";
import type {
  DemoEnvironment,
  DemoEnvironmentCreateResponse,
  DemoEnvironmentsResponse,
  DemoLaunchPrompt,
  DemoTemplate,
} from "../lib/api";

interface Props {
  projectId: string;
}

export const DAILY_PLANNER_TEMPLATE_ID = "daily-planner-lite";

export const DAILY_PLANNER_TEMPLATE: DemoTemplate = {
  id: DAILY_PLANNER_TEMPLATE_ID,
  template_id: DAILY_PLANNER_TEMPLATE_ID,
  label: "Daily Planner Lite",
  description: "Managed visual fixture for the daily planner happy path.",
};

export interface DemoEnvironmentLink {
  key: "dashboard" | "backlog" | "timeline" | "graph" | "planner";
  label: string;
  href: string;
}

export function demoErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body.trim();
    return body ? `${error.message} ${body}` : error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

export function dailyPlannerTemplateFrom(templates: DemoTemplate[] | undefined): DemoTemplate {
  const match = (templates ?? []).find(
    (template) => template.id === DAILY_PLANNER_TEMPLATE_ID || template.template_id === DAILY_PLANNER_TEMPLATE_ID,
  );
  return match ?? DAILY_PLANNER_TEMPLATE;
}

export function environmentFromCreateResponse(response: DemoEnvironmentCreateResponse): DemoEnvironment {
  if ("id" in response) return response;
  if ("environment" in response) {
    if (response.environment) return response.environment;
    throw new Error(response.error || "Create returned no environment.");
  }
  throw new Error("Create returned no environment.");
}

export function demoEnvironmentLinks(env: DemoEnvironment): DemoEnvironmentLink[] {
  const links: DemoEnvironmentLink[] = [
    { key: "dashboard", label: "Dashboard", href: env.dashboard_url },
    { key: "backlog", label: "Backlog", href: env.backlog_url },
    { key: "timeline", label: "Timeline", href: env.timeline_url },
    { key: "graph", label: "Graph", href: env.graph_url },
    { key: "planner", label: "Planner preview", href: env.planner_preview_url },
  ];
  return links.filter((link) => Boolean(link.href));
}

export function demoLaunchPrompts(env: DemoEnvironment): DemoLaunchPrompt[] {
  const prompts = (env.launch_prompts ?? []).filter((prompt) => prompt.prompt.trim());
  if (prompts.length) return prompts;
  if (env.launch_prompt.trim()) {
    return [{
      id: "legacy",
      label: "Launch prompt",
      prompt: env.launch_prompt,
    }];
  }
  return [];
}

export function demoEnvironmentStatus(env: DemoEnvironment): {
  label: string;
  className: string;
} {
  const status = (env.status || (env.error ? "error" : "ready")).trim().toLowerCase();
  if (status === "ready" || status === "ok" || status === "running") {
    return { label: status === "running" ? "Running" : "Ready", className: "status-running" };
  }
  if (status === "creating" || status === "pending") {
    return { label: "Pending", className: "status-pending" };
  }
  if (status === "deleted" || status === "complete" || status === "completed") {
    return { label: "Complete", className: "status-complete" };
  }
  if (status === "error" || status === "failed") {
    return { label: "Error", className: "status-failed" };
  }
  return { label: status || "Unknown", className: "status-unknown" };
}

export function shortCommit(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  return trimmed.length > 12 ? trimmed.slice(0, 12) : trimmed;
}

function normalizeResponse(response: DemoEnvironmentsResponse, projectId: string): DemoEnvironmentsResponse {
  return {
    ...response,
    project_id: response.project_id || projectId,
    templates: response.templates ?? [],
    environments: response.environments ?? [],
  };
}

function upsertEnvironment(
  environments: DemoEnvironment[] | undefined,
  created: DemoEnvironment,
): DemoEnvironment[] {
  const existing = environments ?? [];
  const withoutCreated = existing.filter((env) => env.id !== created.id);
  return [created, ...withoutCreated];
}

function formatDateTime(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) throw new Error("Clipboard write failed.");
}

export default function DemoLaunchView({ projectId }: Props) {
  const [response, setResponse] = useState<DemoEnvironmentsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const [copiedPromptKey, setCopiedPromptKey] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setLoadError("");
    try {
      const next = await api.demoEnvironmentsFor(projectId, signal);
      setResponse(normalizeResponse(next, projectId));
    } catch (error) {
      if ((error as { name?: string }).name === "AbortError") return;
      setLoadError(demoErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const template = useMemo(() => dailyPlannerTemplateFrom(response?.templates), [response?.templates]);
  const environments = response?.environments ?? [];

  const createEnvironment = useCallback(async () => {
    setCreating(true);
    setActionError("");
    try {
      const created = environmentFromCreateResponse(
        await api.createDemoEnvironmentFor(projectId, { template_id: DAILY_PLANNER_TEMPLATE_ID }),
      );
      setResponse((current) => ({
        project_id: current?.project_id || projectId,
        templates: current?.templates ?? [template],
        environments: upsertEnvironment(current?.environments, created),
      }));
      await load();
    } catch (error) {
      setActionError(demoErrorMessage(error));
    } finally {
      setCreating(false);
    }
  }, [load, projectId, template]);

  const deleteEnvironment = useCallback(async (env: DemoEnvironment) => {
    if (typeof window !== "undefined" && !window.confirm(`Delete managed demo environment ${env.label || env.id}?`)) {
      return;
    }
    setDeletingId(env.id);
    setActionError("");
    try {
      await api.deleteDemoEnvironmentFor(projectId, env.id);
      setResponse((current) => ({
        project_id: current?.project_id || projectId,
        templates: current?.templates ?? [template],
        environments: (current?.environments ?? []).filter((candidate) => candidate.id !== env.id),
      }));
    } catch (error) {
      setActionError(demoErrorMessage(error));
    } finally {
      setDeletingId("");
    }
  }, [projectId, template]);

  const copyPrompt = useCallback(async (env: DemoEnvironment, prompt: DemoLaunchPrompt) => {
    setActionError("");
    const promptKey = `${env.id}:${prompt.id}`;
    try {
      await copyText(prompt.prompt);
      setCopiedPromptKey(promptKey);
      window.setTimeout(() => setCopiedPromptKey((current) => (current === promptKey ? "" : current)), 1800);
    } catch (error) {
      setActionError(demoErrorMessage(error));
    }
  }, []);

  return (
    <div className="view demo-launch-view">
      <div className="view-head demo-launch-head">
        <h2 className="view-title">Demo</h2>
        <span className="view-subtitle">
          <span className="mono">{projectId}</span> managed demo environments
        </span>
        <div className="demo-launch-head-actions">
          <button
            type="button"
            className="action-btn"
            onClick={() => void load()}
            disabled={loading || creating || Boolean(deletingId)}
          >
            {loading ? "Refreshing" : "Refresh"}
          </button>
        </div>
      </div>

      <section className="demo-template-panel card">
        <div className="demo-template-main">
          <div className="demo-template-label-row">
            <span className="status-badge status-not-queued">Template</span>
            <span className="demo-template-id mono">{template.id || template.template_id}</span>
          </div>
          <h3>{template.label || DAILY_PLANNER_TEMPLATE.label}</h3>
          {template.description ? <p>{template.description}</p> : null}
        </div>
        <button
          type="button"
          className="action-btn action-btn-primary demo-create-btn"
          onClick={() => void createEnvironment()}
          disabled={creating || loading}
        >
          {creating ? "Creating" : "Create environment"}
        </button>
      </section>

      {loadError ? (
        <div className="demo-alert demo-alert-error" role="alert">
          <strong>Demo environments failed to load.</strong>
          <span>{loadError}</span>
        </div>
      ) : null}
      {actionError ? (
        <div className="demo-alert demo-alert-error" role="alert">
          <strong>Action failed.</strong>
          <span>{actionError}</span>
        </div>
      ) : null}

      <section className="section">
        <div className="section-head">
          Managed environments
          <span className="head-hint">{loading ? "Loading" : `${environments.length} current`}</span>
        </div>
        {loading && !environments.length ? (
          <div className="empty empty-compact">
            <span className="spinner" /> Loading demo environments…
          </div>
        ) : null}
        {!loading && !loadError && environments.length === 0 ? (
          <div className="empty empty-compact">No managed demo environments.</div>
        ) : null}
        {environments.length ? (
          <div className="demo-env-list">
            {environments.map((env) => (
              <DemoEnvironmentCard
                key={env.id}
                env={env}
                deleting={deletingId === env.id}
                copiedPromptKey={copiedPromptKey}
                onCopyPrompt={copyPrompt}
                onDelete={deleteEnvironment}
              />
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function DemoEnvironmentCard(props: {
  env: DemoEnvironment;
  deleting: boolean;
  copiedPromptKey: string;
  onCopyPrompt(env: DemoEnvironment, prompt: DemoLaunchPrompt): void;
  onDelete(env: DemoEnvironment): void;
}) {
  const { env, deleting } = props;
  const status = demoEnvironmentStatus(env);
  const links = demoEnvironmentLinks(env);
  const prompts = demoLaunchPrompts(env);
  const createdAt = formatDateTime(env.created_at);

  return (
    <article className="demo-env-card card">
      <div className="demo-env-topline">
        <div className="demo-env-title-wrap">
          <span className={`status-badge ${status.className}`}>{status.label}</span>
          <h3>{env.label || env.id}</h3>
        </div>
        <button
          type="button"
          className="action-btn action-btn-danger"
          onClick={() => props.onDelete(env)}
          disabled={deleting}
        >
          {deleting ? "Deleting" : "Delete"}
        </button>
      </div>

      {env.error ? <div className="demo-env-error">{env.error}</div> : null}

      <dl className="demo-env-meta">
        <div>
          <dt>Environment</dt>
          <dd className="mono">{env.id}</dd>
        </div>
        <div>
          <dt>Project</dt>
          <dd className="mono">{env.project_id}</dd>
        </div>
        <div>
          <dt>Baseline</dt>
          <dd className="mono" title={env.baseline_commit}>{shortCommit(env.baseline_commit)}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{createdAt || "Unknown"}</dd>
        </div>
        <div className="demo-env-meta-wide">
          <dt>Fixture root</dt>
          <dd className="mono">{env.fixture_root}</dd>
        </div>
      </dl>

      <div className="demo-link-row" aria-label={`${env.label || env.id} links`}>
        {links.map((link) => (
          <a key={link.key} href={link.href} target="_blank" rel="noreferrer" className="demo-link">
            {link.label}
          </a>
        ))}
      </div>

      <div className="demo-block-grid">
        <section className="demo-code-panel">
          <div className="demo-code-head">Preview command</div>
          <code className="demo-command-line">{env.planner_preview_command || "No preview command"}</code>
        </section>
        {prompts.length ? prompts.map((prompt) => {
          const promptKey = `${env.id}:${prompt.id}`;
          const copied = props.copiedPromptKey === promptKey;
          return (
            <section key={prompt.id} className="demo-code-panel demo-prompt-panel">
              <div className="demo-code-head">
                <span>{prompt.label || "Launch prompt"}</span>
                <button
                  type="button"
                  className="action-btn"
                  onClick={() => props.onCopyPrompt(env, prompt)}
                  disabled={!prompt.prompt}
                >
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
              {prompt.description ? <p className="demo-prompt-description">{prompt.description}</p> : null}
              <pre className="demo-prompt-block"><code>{prompt.prompt}</code></pre>
            </section>
          );
        }) : (
          <section className="demo-code-panel demo-prompt-panel">
            <div className="demo-code-head">Launch prompt</div>
            <pre className="demo-prompt-block"><code>No launch prompt</code></pre>
          </section>
        )}
      </div>
    </article>
  );
}
