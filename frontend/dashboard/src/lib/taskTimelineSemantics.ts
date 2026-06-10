import type { TaskTimelineEvent } from "../types";
import semanticCatalog from "./taskTimelineSemanticCatalog.json";

export type TaskTimelineSemanticStatus =
  | "passed"
  | "blocked"
  | "failed"
  | "running"
  | "waiting"
  | "missing"
  | "recorded"
  | "unknown";

export type TaskTimelineSemanticLane = "observer" | "worker" | "verification" | "gate" | "content_sys";

export interface TaskTimelineSemanticChip {
  kind: string;
  label: string;
  value: string;
  path?: string;
}

export interface TaskTimelineEvidenceInspectorRow {
  kind: string;
  label: string;
  value: string;
}

export interface TaskTimelineEvidenceInspectorSection {
  label: string;
  value: unknown;
  redacted: boolean;
}

export interface TaskTimelineEvidenceInspector {
  rows: TaskTimelineEvidenceInspectorRow[];
  raw_sections: TaskTimelineEvidenceInspectorSection[];
  redaction_count: number;
}

export interface TaskTimelineSemanticNarrative {
  actor: string;
  information: string;
  context: string;
  purpose: string;
  outcome: string;
}

/** A single relation entry pointing at a referenced event or backlog row. */
export interface TaskTimelineSemanticRelation {
  /** Relation category: "event_ref" for timeline events, "backlog_row" for parent/child rows. */
  kind: "event_ref" | "backlog_row";
  /** Human-readable label, e.g. "read receipt" or "parent backlog". */
  label: string;
  /** The id/value to jump to, e.g. "#3568" or "AC-TIMELINE-..." */
  value: string;
  /** One-line semantic summary shown next to the link. */
  summary: string;
}

export interface TaskTimelineSemanticProjection {
  schema_version: "task_timeline_semantic_projection.v1";
  catalog_schema_version: string;
  catalog_entry_id: string;
  template_id: string;
  fallback: boolean;
  /** Role-action headline: WHO (actor role) did WHAT (business action) in sentence form. */
  headline: string;
  title: string;
  detail: string;
  status: TaskTimelineSemanticStatus;
  status_label: string;
  lane_id: TaskTimelineSemanticLane;
  lane_label: string;
  lane_family: TaskTimelineSemanticLane;
  actor: string;
  actor_label: string;
  narrative: TaskTimelineSemanticNarrative;
  event_type_label: string;
  event_kind_label: string;
  phase_label: string;
  chips: TaskTimelineSemanticChip[];
  evidence: TaskTimelineSemanticChip[];
  artifacts: TaskTimelineSemanticChip[];
  inspector: TaskTimelineEvidenceInspector;
  /** Structured list of referenced event ids and backlog row ids with summaries, for cross-navigation. */
  relations: TaskTimelineSemanticRelation[];
}

interface CatalogPath {
  label?: string;
  path: string;
  kind?: string;
}

interface CatalogEntry {
  id: string;
  match: Partial<Record<"event_type" | "event_kind" | "phase" | "status", string[]>>;
  title: string;
  detail: string;
  lane?: TaskTimelineSemanticLane;
  actor?: string;
  narrative?: Partial<TaskTimelineSemanticNarrative>;
  chip_paths?: CatalogPath[];
  evidence_paths?: CatalogPath[];
  artifact_paths?: CatalogPath[];
  detail_paths?: CatalogPath[];
}

interface CatalogFallback {
  title: string;
  detail: string;
  chip_paths?: CatalogPath[];
  evidence_paths?: CatalogPath[];
  artifact_paths?: CatalogPath[];
  detail_paths?: CatalogPath[];
}

interface TaskTimelineSemanticCatalog {
  schema_version: string;
  fallback: CatalogFallback;
  status_labels?: Record<string, string>;
  templates: CatalogEntry[];
}

const CATALOG = semanticCatalog as TaskTimelineSemanticCatalog;

export const TASK_TIMELINE_SEMANTIC_PROJECTION_SCHEMA = "task_timeline_semantic_projection.v1";
export const TASK_TIMELINE_SEMANTIC_CATALOG_SCHEMA = CATALOG.schema_version;

/**
 * PRIVATE_TIMELINE_TEXT_KEY is used exclusively for redacting rendered private
 * BODY TEXT inside timeline event payloads.  It must never be used to decide
 * row-level visibility (whether a backlog row appears in the playback list).
 * Row visibility is driven by the explicit privacy_level / public_safe flags
 * emitted by the backend compact-bug serialiser.
 */
export const PRIVATE_TIMELINE_TEXT_KEY =
  /(raw[-_\s]?prompt|prompt[-_\s]?(text|body|payload)|hidden|private|secret|\btoken\b(?![-_\s]?hash)|provider[-_\s]?(context|payload|config)|filesystem|cwd|worktree[-_\s]?path|host[-_\s]?(path|home|cwd)|judgment[-_\s]?brain|\bjb[-_][a-z0-9][a-z0-9_-]*|\bac[-_]judge[-_][a-z0-9][a-z0-9_-]*|private[-_\s]?judge|judge[-_\s]?mode|judge[-_\s]?(private|route|routing|precheck|provider|prompt|context|memory|brain|lineage|contract))/i;

const PRIVATE_TIMELINE_PATH =
  /(^|\.)(raw_prompt|prompt_text|prompt_body|prompt_payload|hidden|private|secret|token|provider_context|provider_payload|provider_config|filesystem|cwd|worktree_path|host_path|host_paths|host_home|route_context)$/i;
const PUBLIC_HASH_OR_ID_PATH = /(^|\.)(event_type|event_kind|phase|status|event_id|trace_id|request_id|route_id|receipt_id|upsert_id|fence_id|task_id|backlog_id|correlation_id|commit_sha|.*_hash|.*_id)$/i;
const ABSOLUTE_HOST_PATH = /(^|\s)(\/Users\/[^\s,;:]+|\/home\/[^\s,;:]+|\/var\/folders\/[^\s,;:]+|[A-Za-z]:\\[^\s,;:]+)/g;
const TOKEN_VALUE = /\b(?:sk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{8,}\b/g;
const MAX_VALUES_PER_PATH = 8;

export function isPrivateTimelineText(value?: string | null): boolean {
  return Boolean(value && PRIVATE_TIMELINE_TEXT_KEY.test(value));
}

export function isPrivateTimelinePath(path: string): boolean {
  const normalized = path.trim();
  if (!normalized) return true;
  if (PUBLIC_HASH_OR_ID_PATH.test(normalized)) return false;
  return PRIVATE_TIMELINE_PATH.test(normalized);
}

export function sanitizePublicTimelineText(value: string): string {
  const redacted = value
    .replace(ABSOLUTE_HOST_PATH, "$1[local path redacted]")
    .replace(TOKEN_VALUE, "[token redacted]")
    .replace(/\s+/g, " ")
    .trim();
  return isPrivateTimelineText(redacted) ? "[private detail redacted]" : redacted;
}

export function sanitizeTimelineInspectorValue(value: unknown): { value: unknown; redaction_count: number } {
  return sanitizeInspectorValue(value, "");
}

export function projectTaskTimelineEvent(event: TaskTimelineEvent, index = 0): TaskTimelineSemanticProjection {
  const entry = selectCatalogEntry(event);
  const fallback = !entry;
  const spec = entry ?? fallbackEntry();
  const status = timelineStatusFromEvent(event);
  const statusLabel = statusLabelFor(event, status);
  const laneId = spec.lane ?? laneIdForEvent(event);
  const actorLabel = publicLabel(spec.actor || actorForEvent(event, laneId), "Aming Claw");
  const narrative = buildNarrative(event, spec, actorLabel, statusLabel, laneId);
  const chips = collectChips(event, spec.chip_paths ?? CATALOG.fallback.chip_paths ?? [], "fact");
  const details = collectChips(event, spec.detail_paths ?? [], "detail");
  const evidence = stableChips([
    { kind: "timeline", label: "event", value: eventDisplayId(event), path: "event_id" },
    ...collectChips(event, spec.evidence_paths ?? [], "timeline"),
  ]);
  const artifacts = collectChips(event, spec.artifact_paths ?? [], "artifact");
  const inspector = buildInspector(event, spec, chips, details, evidence, artifacts);
  const headline = buildHeadline(event, spec, actorLabel, statusLabel, laneId, fallback);
  const relations = buildRelations(event);

  return {
    schema_version: TASK_TIMELINE_SEMANTIC_PROJECTION_SCHEMA,
    catalog_schema_version: CATALOG.schema_version,
    catalog_entry_id: spec.id,
    template_id: spec.id,
    fallback,
    headline,
    title: catalogLabel(spec.title, CATALOG.fallback.title),
    detail: catalogLabel(spec.detail, CATALOG.fallback.detail),
    status,
    status_label: statusLabel,
    lane_id: laneId,
    lane_label: laneLabel(laneId),
    lane_family: laneId,
    actor: actorLabel,
    actor_label: actorLabel,
    narrative,
    event_type_label: publicLabel(event.event_type || "timeline_event", "timeline_event"),
    event_kind_label: publicLabel(event.event_kind || event.phase || "event", "event"),
    phase_label: publicLabel(event.phase || event.event_kind || `event ${index + 1}`, `event ${index + 1}`),
    chips,
    evidence,
    artifacts,
    inspector,
    relations,
  };
}

export function timelineStatusFromEvent(event: TaskTimelineEvent): TaskTimelineSemanticStatus {
  const verification = asRecord(event.verification);
  const text = [
    event.status,
    event.decision,
    event.event_type,
    event.event_kind,
    event.phase,
    typeof verification.status === "string" ? verification.status : "",
  ].join(" ").toLowerCase();
  if (text.includes("blocked")) return "blocked";
  if (text.includes("missing")) return "missing";
  if (text.includes("fail") || text.includes("error") || text.includes("reject")) return "failed";
  if (text.includes("running") || text.includes("claimed") || text.includes("progress") || text.includes("started")) return "running";
  if (text.includes("pending") || text.includes("queued") || text.includes("waiting") || text.includes("requested")) return "waiting";
  if (verification.passed === true || text.includes("pass") || text.includes("success") || text.includes("accepted") || text.includes("complete")) return "passed";
  if (text.includes("record") || text.includes("acknowledged") || text.includes("upserted")) return "recorded";
  return "unknown";
}

function fallbackEntry(): CatalogEntry {
  return {
    id: "fallback.system_timeline_event",
    match: {},
    title: CATALOG.fallback.title,
    detail: CATALOG.fallback.detail,
    chip_paths: CATALOG.fallback.chip_paths,
    evidence_paths: CATALOG.fallback.evidence_paths,
    artifact_paths: CATALOG.fallback.artifact_paths,
    detail_paths: CATALOG.fallback.detail_paths,
  };
}

function selectCatalogEntry(event: TaskTimelineEvent): CatalogEntry | null {
  let best: { entry: CatalogEntry; score: number } | null = null;
  for (const entry of CATALOG.templates) {
    const score = catalogEntryMatchScore(entry, event);
    if (score <= 0) continue;
    if (!best || score > best.score) best = { entry, score };
  }
  return best?.entry ?? null;
}

function catalogEntryMatchScore(entry: CatalogEntry, event: TaskTimelineEvent): number {
  const weights: Record<"event_type" | "event_kind" | "phase" | "status", number> = {
    event_type: 100,
    event_kind: 30,
    phase: 20,
    status: 10,
  };
  return (["event_type", "event_kind", "phase", "status"] as const).some((field) => {
    const expected = entry.match[field]?.map(normalizeMatchValue) ?? [];
    if (expected.length === 0) return false;
    return expected.includes(normalizeMatchValue(stringFrom(event[field])));
  }) ? (["event_type", "event_kind", "phase", "status"] as const).reduce((score, field) => {
    const expected = entry.match[field]?.map(normalizeMatchValue) ?? [];
    if (expected.length === 0) return score;
    return expected.includes(normalizeMatchValue(stringFrom(event[field]))) ? score + weights[field] : score;
  }, 0) : 0;
}

function buildNarrative(
  event: TaskTimelineEvent,
  entry: CatalogEntry,
  actorLabel: string,
  statusLabel: string,
  laneId: TaskTimelineSemanticLane,
): TaskTimelineSemanticNarrative {
  const eventKind = publicLabel(event.event_kind || event.event_type || "timeline event", "timeline event");
  const lane = laneLabel(laneId);
  const fallback: TaskTimelineSemanticNarrative = {
    actor: `${actorLabel} acted in the ${lane} lane.`,
    information: `${actorLabel} recorded public ${eventKind} evidence.`,
    context: "Public-safe event fields are summarized; raw request bodies and provider context are not displayed.",
    purpose: "This event updates the task timeline used by review and close-gate checks.",
    outcome: `Outcome/status changed to ${statusLabel}.`,
  };
  const template = entry.narrative ?? {};
  return {
    actor: catalogLabel(template.actor, fallback.actor),
    information: catalogLabel(template.information, fallback.information),
    context: catalogLabel(template.context, fallback.context),
    purpose: catalogLabel(template.purpose, fallback.purpose),
    outcome: catalogLabel(template.outcome, fallback.outcome),
  };
}

/**
 * Per-kind headline registry (top ~20 event kinds from real DB data).
 * Keyed by event_kind (primary) or event_type substring (fallback).
 * Each entry: { role, action } → produces "$role $action."
 */
const HEADLINE_REGISTRY: Record<string, { role: string; action: string }> = {
  // Worker lane
  mf_subagent_startup:                    { role: "Bounded worker (mf_sub)",  action: "started in assigned worktree and recorded startup evidence" },
  mf_subagent_read_receipt:               { role: "Bounded worker (mf_sub)",  action: "acknowledged task contract and recorded read receipt" },
  mf_subagent_dispatch:                   { role: "Observer",                 action: "dispatched a bounded implementation worker" },
  bounded_implementation_worker_dispatch: { role: "Observer",                 action: "dispatched a bounded implementation worker" },
  worker_progress:                        { role: "Bounded worker (mf_sub)",  action: "reported code change progress" },
  implementation:                         { role: "Bounded worker (mf_sub)",  action: "completed implementation and recorded evidence" },
  // Verification lane
  verification:                           { role: "Verification lane",        action: "checked the recorded work and produced QA evidence" },
  qa_verification:                        { role: "QA reviewer",              action: "evaluated implementation for correctness" },
  independent_verification:               { role: "Independent verifier",     action: "independently verified worker output" },
  independent_verification_lane:          { role: "Independent verifier",     action: "completed independent verification lane" },
  // Gate/close lane
  close_ready:                            { role: "Observer",                 action: "recorded close-ready evidence for final review" },
  review_ready:                           { role: "Observer",                 action: "marked the task as review-ready" },
  route_waiver:                           { role: "Route gate",               action: "recorded a route evidence waiver" },
  blocker:                                { role: "Governance system",        action: "recorded a blocker preventing progress" },
  // Observer/route lane
  service_route:                          { role: "Service router",           action: "completed a scoped route service action" },
  route_action_precheck:                  { role: "Route service",            action: "ran a route action precheck for the observer" },
  route_context:                          { role: "Route service",            action: "prepared public task scope for the next lane" },
  route_identity_cleanup:                 { role: "Observer",                 action: "cleaned up stale route identity entries" },
  route_identity_supersede:               { role: "Observer",                 action: "superseded a stale route identity with a fresh one" },
  dispatch:                               { role: "Observer",                 action: "dispatched a governed worker or action" },
  planning:                               { role: "Observer",                 action: "recorded planning or scenario specification" },
  scenario_spec:                          { role: "Observer",                 action: "recorded a scenario specification" },
  observer_poll:                          { role: "Observer",                 action: "polled the governance runtime for status" },
  observer_mode:                          { role: "Observer",                 action: "transitioned work mode or recorded posture" },
  precheck:                               { role: "Governance system",        action: "ran a precommit or route action precheck" },
  audit:                                  { role: "Observer",                 action: "recorded an audit or scope review" },
  failure:                                { role: "Governance system",        action: "recorded a failure or error event" },
  architecture_review_lane:              { role: "Architecture reviewer",    action: "reviewed the system design and recorded findings" },
  observer_cli_terminal_blocker:         { role: "Governance system",        action: "detected a terminal CLI blocker for the observer" },
};

// Telemetry counter for unmapped event_kinds (V1: console only, no network).
const _unmappedKindSeen = new Set<string>();

function recordUnmappedKindTelemetry(event_kind: string): void {
  if (_unmappedKindSeen.has(event_kind)) return;
  _unmappedKindSeen.add(event_kind);
  // V1: console-debug only so the developer console shows coverage gaps.
  if (typeof console !== "undefined" && typeof console.debug === "function") {
    console.debug("[aming-claw:semantics] unmapped event_kind", event_kind, "— add to HEADLINE_REGISTRY for a richer headline");
  }
}

function buildHeadline(
  event: TaskTimelineEvent,
  entry: CatalogEntry,
  actorLabel: string,
  statusLabel: string,
  laneId: TaskTimelineSemanticLane,
  fallback: boolean,
): string {
  const eventKind = (event.event_kind || "").trim().toLowerCase();
  const eventType = (event.event_type || "").trim().toLowerCase();

  // Try per-kind registry first (primary: event_kind, secondary: event_type key match).
  const registryEntry = HEADLINE_REGISTRY[eventKind] ?? (
    Object.keys(HEADLINE_REGISTRY).find((key) => eventType.includes(key)) ? HEADLINE_REGISTRY[Object.keys(HEADLINE_REGISTRY).find((key) => eventType.includes(key))!] : undefined
  );

  if (registryEntry) {
    const suffix = statusLabel && statusLabel !== "unknown" ? ` (${statusLabel})` : "";
    return `${registryEntry.role} ${registryEntry.action}${suffix}.`;
  }

  // Unmapped kind: fire telemetry and produce a generic fallback.
  if (eventKind && eventKind !== "") {
    recordUnmappedKindTelemetry(eventKind);
  }

  // Generic fallback from catalog entry title + actor + lane.
  if (!fallback) {
    const title = catalogLabel(entry.title, "");
    if (title) {
      return `${actorLabel} — ${title.charAt(0).toLowerCase()}${title.slice(1)}.`;
    }
  }

  const lane = laneLabel(laneId);
  return `${actorLabel} acted in the ${lane} lane${statusLabel && statusLabel !== "unknown" ? ` (${statusLabel})` : ""}.`;
}

function buildRelations(event: TaskTimelineEvent): TaskTimelineSemanticRelation[] {
  const relations: TaskTimelineSemanticRelation[] = [];
  const payload = asRecord(event.payload);
  const verification = asRecord(event.verification);
  const artifactRefs = asRecord(event.artifact_refs);

  // Helper: push a relation if value is non-empty.
  const pushRel = (kind: TaskTimelineSemanticRelation["kind"], label: string, rawValue: unknown, summary: string) => {
    const v = stringFrom(rawValue);
    if (!v) return;
    const safe = sanitizePublicTimelineText(v);
    if (!safe || safe === "[private detail redacted]") return;
    relations.push({ kind, label, value: safe, summary });
  };

  // --- Parent/child backlog row relations ---
  for (const field of ["backlog_id", "bug_id", "root_backlog_ids", "parent_backlog_id", "child_backlog_ids"]) {
    const v = payload[field] ?? verification[field] ?? artifactRefs[field] ?? (event as unknown as Record<string, unknown>)[field];
    if (Array.isArray(v)) {
      for (const item of v) pushRel("backlog_row", field === "parent_backlog_id" ? "parent backlog" : "backlog row", item, "Referenced backlog row");
    } else {
      pushRel("backlog_row", field === "parent_backlog_id" ? "parent backlog" : field === "child_backlog_ids" ? "child backlog" : "backlog row", v, "Referenced backlog row");
    }
  }

  // --- Referenced event ids ---
  const eventRefPaths: Array<[string, string]> = [
    ["read_receipt_event_id", "read receipt"],
    ["startup_event_id", "worker startup"],
    ["source_event_id", "source event"],
    ["parent_event_id", "parent event"],
    ["dispatch_event_id", "dispatch event"],
    ["precheck_id", "route precheck"],
    ["route_action_precheck_id", "route action precheck"],
  ];
  for (const [field, label] of eventRefPaths) {
    const v = payload[field] ?? verification[field] ?? artifactRefs[field];
    if (v != null) pushRel("event_ref", label, v, `Referenced ${label}`);
  }

  // Multi-value event id arrays
  for (const [field, label] of [["read_receipt_event_ids", "read receipt"], ["source_event_ids", "source event"], ["startup_event_ids", "worker startup"]] as Array<[string, string]>) {
    const arr = payload[field] ?? verification[field];
    if (Array.isArray(arr)) {
      for (const item of arr.slice(0, 6)) pushRel("event_ref", label, item, `Referenced ${label}`);
    }
  }

  // --- QA/verification lane references ---
  const qaVerdictRefs = payload.qa_verdict_refs ?? payload.qa_evidence_refs ?? verification.qa_verdict_refs;
  if (Array.isArray(qaVerdictRefs)) {
    for (const item of qaVerdictRefs.slice(0, 6)) pushRel("event_ref", "QA verdict", item, "Referenced QA verdict event");
  }

  // Lane evidence references
  const laneEvidenceRefs = payload.lane_evidence_refs ?? payload.route_lane_refs ?? verification.lane_evidence_refs;
  if (Array.isArray(laneEvidenceRefs)) {
    for (const item of laneEvidenceRefs.slice(0, 6)) pushRel("event_ref", "lane evidence", item, "Referenced lane evidence event");
  }

  // De-duplicate by kind+value
  const seen = new Set<string>();
  return relations.filter((rel) => {
    const key = `${rel.kind}:${rel.value}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 20);
}

function normalizeMatchValue(value: string): string {
  return value.trim().toLowerCase();
}

function collectChips(event: TaskTimelineEvent, paths: CatalogPath[], defaultKind: string): TaskTimelineSemanticChip[] {
  const chips: TaskTimelineSemanticChip[] = [];
  for (const spec of paths) {
    for (const value of valuesAtPublicPath(event, spec.path)) {
      chips.push({
        kind: spec.kind || defaultKind,
        label: publicLabel(spec.label || labelFromPath(spec.path), "detail"),
        value,
        path: spec.path,
      });
    }
  }
  return stableChips(chips);
}

function valuesAtPublicPath(event: TaskTimelineEvent, path: string): string[] {
  if (isPrivateTimelinePath(path)) return [];
  return stringsFromUnknown(valueAtPath(event as unknown as Record<string, unknown>, path))
    .map(sanitizePublicTimelineText)
    .filter((value) => value && value !== "[private detail redacted]")
    .slice(0, MAX_VALUES_PER_PATH);
}

function valueAtPath(root: Record<string, unknown>, path: string): unknown {
  return path.split(".").reduce<unknown>((current, key) => {
    if (current == null) return undefined;
    if (Array.isArray(current)) return current.map((item) => asRecord(item)[key]);
    return asRecord(current)[key];
  }, root);
}

function buildInspector(
  event: TaskTimelineEvent,
  entry: CatalogEntry,
  chips: TaskTimelineSemanticChip[],
  details: TaskTimelineSemanticChip[],
  evidence: TaskTimelineSemanticChip[],
  artifacts: TaskTimelineSemanticChip[],
): TaskTimelineEvidenceInspector {
  const payload = sanitizeTimelineInspectorValue(event.payload ?? {});
  const verification = sanitizeTimelineInspectorValue(event.verification ?? {});
  const artifactRefs = sanitizeTimelineInspectorValue(event.artifact_refs ?? {});
  const rows = stableRows([
    { kind: "catalog", label: "template", value: entry.id },
    { kind: "status", label: "status", value: statusLabelFor(event, timelineStatusFromEvent(event)) },
    ...chips.map(chipToRow),
    ...details.map(chipToRow),
    ...evidence.map(chipToRow),
    ...artifacts.map(chipToRow),
  ]);
  return {
    rows,
    raw_sections: [
      { label: "verification", value: verification.value, redacted: verification.redaction_count > 0 },
      { label: "artifact_refs", value: artifactRefs.value, redacted: artifactRefs.redaction_count > 0 },
      { label: "payload", value: payload.value, redacted: payload.redaction_count > 0 },
    ],
    redaction_count: payload.redaction_count + verification.redaction_count + artifactRefs.redaction_count,
  };
}

function chipToRow(chip: TaskTimelineSemanticChip): TaskTimelineEvidenceInspectorRow {
  return {
    kind: chip.kind,
    label: chip.label,
    value: chip.value,
  };
}

function sanitizeInspectorValue(value: unknown, path: string): { value: unknown; redaction_count: number } {
  if (value == null || value === "") return { value, redaction_count: 0 };
  if (Array.isArray(value)) {
    let redactionCount = 0;
    const items = value.map((item, index) => {
      const result = sanitizeInspectorValue(item, `${path}.${index}`);
      redactionCount += result.redaction_count;
      return result.value;
    });
    return { value: items, redaction_count: redactionCount };
  }
  if (typeof value === "object") {
    let redactionCount = 0;
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      const nextPath = path ? `${path}.${key}` : key;
      if (isPrivateTimelinePath(nextPath)) {
        out[key] = "[private detail redacted]";
        redactionCount += 1;
        continue;
      }
      const result = sanitizeInspectorValue(item, nextPath);
      out[key] = result.value;
      redactionCount += result.redaction_count;
    }
    return { value: out, redaction_count: redactionCount };
  }
  const text = String(value);
  const safe = sanitizePublicTimelineText(text);
  return {
    value: safe,
    redaction_count: safe !== text ? 1 : 0,
  };
}

function stringsFromUnknown(value: unknown): string[] {
  if (value == null || value === "") return [];
  if (Array.isArray(value)) return value.flatMap(stringsFromUnknown);
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !isPrivateTimelinePath(key))
      .slice(0, 8)
      .map(([key, item]) => `${labelFromPath(key)}: ${compactUnknown(item)}`);
  }
  return [String(value)];
}

function compactUnknown(value: unknown): string {
  if (value == null || value === "") return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
  const keys = Object.keys(value as Record<string, unknown>).filter((key) => !isPrivateTimelinePath(key));
  return keys.length > 0 ? keys.slice(0, 3).join(", ") : "record";
}

function statusLabelFor(event: TaskTimelineEvent, status: TaskTimelineSemanticStatus): string {
  const raw = sanitizePublicTimelineText(event.status || event.decision || "");
  if (raw && raw !== "[private detail redacted]") return CATALOG.status_labels?.[raw.toLowerCase()] ?? raw;
  return CATALOG.status_labels?.[status] ?? status;
}

function laneIdForEvent(event: TaskTimelineEvent): TaskTimelineSemanticLane {
  const raw = [
    valueAtPath(event as unknown as Record<string, unknown>, "payload.lane"),
    valueAtPath(event as unknown as Record<string, unknown>, "payload.worker_lane"),
    valueAtPath(event as unknown as Record<string, unknown>, "payload.agent_lane"),
    event.actor,
    event.phase,
    event.event_kind,
    event.event_type,
  ].map(stringFrom).join(" ").toLowerCase();
  if (/content[-_\s]?sys|docker|fixture/.test(raw)) return "content_sys";
  if (/gate|close|merge|token/.test(raw)) return "gate";
  if (/verify|test|qa|browser|playwright|screenshot/.test(raw)) return "verification";
  if (/worker|subagent|mf_sub|front|back|implementation/.test(raw)) return "worker";
  return "observer";
}

function laneLabel(id: TaskTimelineSemanticLane): string {
  if (id === "content_sys") return "content-sys";
  if (id === "gate") return "Close gate";
  if (id === "verification") return "Verification";
  if (id === "worker") return "Bounded worker";
  return "Observer";
}

function actorForEvent(event: TaskTimelineEvent, lane: TaskTimelineSemanticLane): string {
  if (lane === "worker") return "Bounded worker";
  if (lane === "gate") return "Aming Claw gate";
  if (lane === "verification") return "Verification";
  if (lane === "content_sys") return "content-sys";
  return (event.actor || "").toLowerCase().includes("observer") ? "Observer" : publicLabel(event.actor || "Aming Claw", "Aming Claw");
}

function publicLabel(value: string, fallback = ""): string {
  const safe = sanitizePublicTimelineText(value);
  return safe && safe !== "[private detail redacted]" ? safe : fallback;
}

function catalogLabel(value: string | undefined, fallback = ""): string {
  const safe = sanitizeCatalogText(value || "");
  return safe || fallback;
}

function sanitizeCatalogText(value: string): string {
  return value
    .replace(ABSOLUTE_HOST_PATH, "$1[local path redacted]")
    .replace(TOKEN_VALUE, "[token redacted]")
    .replace(/\s+/g, " ")
    .trim();
}

function eventDisplayId(event: TaskTimelineEvent): string {
  if (event.event_id && !isPrivateTimelineText(event.event_id)) return sanitizePublicTimelineText(event.event_id);
  if (event.id != null) return `#${event.id}`;
  if (event.trace_id && !isPrivateTimelineText(event.trace_id)) return sanitizePublicTimelineText(event.trace_id);
  return "recorded";
}

function labelFromPath(path: string): string {
  return sanitizePublicTimelineText(path.split(".").slice(-1)[0]?.replace(/[_./-]+/g, " ") || "detail");
}

function stringFrom(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stableChips(chips: TaskTimelineSemanticChip[]): TaskTimelineSemanticChip[] {
  const seen = new Set<string>();
  return chips.filter((chip) => {
    const key = `${chip.kind}:${chip.label}:${chip.value}`;
    if (!chip.value || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function stableRows(rows: TaskTimelineEvidenceInspectorRow[]): TaskTimelineEvidenceInspectorRow[] {
  const seen = new Set<string>();
  return rows.filter((row) => {
    const key = `${row.kind}:${row.label}:${row.value}`;
    if (!row.value || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 36);
}
