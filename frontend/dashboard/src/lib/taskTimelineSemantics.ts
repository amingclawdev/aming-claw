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
  /** Optional backlog row id when an event reference is known to target another backlog. */
  backlog_id?: string;
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
  lineage_bridge:                         { role: "Observer",                 action: "recorded sibling task lineage bridge coordination" },
  cross_ref_lineage_bridge:               { role: "Observer",                 action: "recorded sibling task lineage bridge coordination" },
  record_cross_ref_lineage_bridge:        { role: "Observer",                 action: "prepared sibling task lineage bridge coordination" },
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

/**
 * Known gate/evidence envelope keys whose contents hold the real relation fields.
 *
 * Envelope rule: when a payload top-level key is in this list AND its value is
 * an object (not an array), buildRelations unwraps it one level and merges it
 * with the outer payload for field extraction purposes.  This covers startup
 * events that nest all fields under mf_subagent_startup_gate, finish-gate events
 * that wrap worker_contract/evidence, and the identity_join sub-object inside the
 * startup gate.  Only one level is unwrapped (depth-1); nested arrays like
 * findings[] or per-item sub-objects are NOT traversed to avoid extracting ids
 * from unrelated noise structures.
 */
const KNOWN_ENVELOPE_KEYS = new Set([
  "mf_subagent_startup_gate",
  "worker_contract",
  "evidence",
  "identity_join",
  "route_token_gate",
  "mf_subagent_finish_gate",
  "parallel_branch_finish_gate",
]);

/**
 * Unwrap one level of known gate/evidence envelopes from a record so that
 * relation-field extraction sees the actual fields regardless of whether they
 * are at the top level or nested under a named envelope.  Only scalar/object
 * envelope values are merged; array envelope values are left in place because
 * they have their own extraction logic.
 */
function unwrapEnvelopes(record: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...record };
  for (const key of KNOWN_ENVELOPE_KEYS) {
    const val = record[key];
    if (val && typeof val === "object" && !Array.isArray(val)) {
      // Merge envelope fields WITHOUT overwriting already-present top-level keys.
      for (const [envKey, envVal] of Object.entries(val as Record<string, unknown>)) {
        if (!(envKey in out)) {
          out[envKey] = envVal;
        }
      }
    }
  }
  return out;
}

function buildRelations(event: TaskTimelineEvent): TaskTimelineSemanticRelation[] {
  const relations: TaskTimelineSemanticRelation[] = [];
  // Unwrap known gate/evidence envelopes (e.g. mf_subagent_startup_gate) so that
  // relation fields nested one level deep are visible to the extraction logic.
  // Depth is bounded to 1 — only the named envelope objects are merged, not their
  // nested arrays (e.g. findings[], bridged_identities[]) which have dedicated logic.
  const payload = unwrapEnvelopes(asRecord(event.payload));
  const verification = unwrapEnvelopes(asRecord(event.verification));
  const artifactRefs = unwrapEnvelopes(asRecord(event.artifact_refs));

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
    ["startup_timeline_event_id", "startup timeline event"],
    ["continuation_startup_event_id", "continuation startup event"],
    ["source_event_id", "source event"],
    ["parent_event_id", "parent event"],
    ["dispatch_event_id", "dispatch event"],
    ["dispatch_ref", "dispatch ref"],
    ["precheck_id", "route precheck"],
    ["route_action_precheck_id", "route action precheck"],
    ["reversal_of_event", "reversal of event"],
  ];
  for (const [field, label] of eventRefPaths) {
    const v = payload[field] ?? verification[field] ?? artifactRefs[field];
    if (v != null) pushRel("event_ref", label, normalizeEventId(v), `Referenced ${label}`);
  }

  // Multi-value event id arrays
  for (const [field, label] of [["read_receipt_event_ids", "read receipt"], ["source_event_ids", "source event"], ["startup_event_ids", "worker startup"], ["worker_progress_refs", "worker progress"], ["qa_refs", "QA ref"]] as Array<[string, string]>) {
    const arr = payload[field] ?? verification[field] ?? artifactRefs[field];
    if (Array.isArray(arr)) {
      for (const item of arr.slice(0, 6)) pushRel("event_ref", label, normalizeEventId(item), `Referenced ${label}`);
    }
  }

  // --- QA/verification lane references ---
  const qaVerdictRefs = payload.qa_verdict_refs ?? payload.qa_evidence_refs ?? verification.qa_verdict_refs ?? artifactRefs.qa_verdict_refs;
  if (Array.isArray(qaVerdictRefs)) {
    for (const item of qaVerdictRefs.slice(0, 6)) pushRel("event_ref", "QA verdict", normalizeEventId(item), "Referenced QA verdict event");
  }

  // Lane evidence references
  const laneEvidenceRefs = payload.lane_evidence_refs ?? payload.route_lane_refs ?? verification.lane_evidence_refs;
  if (Array.isArray(laneEvidenceRefs)) {
    for (const item of laneEvidenceRefs.slice(0, 6)) pushRel("event_ref", "lane evidence", normalizeEventId(item), "Referenced lane evidence event");
  }

  // --- checkpoint_id: rendered as non-navigable "backlog_row" kind.
  // The panel only makes event_ref entries clickable when a matching frame is
  // found by source_event_id — checkpoint ids are never frame ids, so event_ref
  // would silently fall back to non-nav anyway. Using "backlog_row" here is the
  // simpler, guaranteed-non-nav in-fence solution; it avoids any future frame-id
  // collision and does not require a new "fact" kind in the type union (which
  // would require changes in TaskPlaybackPanel outside the fence).
  const checkpointId = payload.checkpoint_id ?? verification.checkpoint_id ?? artifactRefs.checkpoint_id;
  if (checkpointId != null) {
    const v = stringFrom(checkpointId);
    if (v) {
      const safe = sanitizePublicTimelineText(v);
      if (safe && safe !== "[private detail redacted]") {
        relations.push({ kind: "backlog_row", label: "checkpoint", value: safe, summary: "Checkpoint id for this branch task (non-navigable)" });
      }
    }
  }

  // --- bridged_identities[].task_id / bridge child_task_ids from cross_ref_lineage_bridge ---
  // Use the original (non-unwrapped) raw payload for array fields to avoid
  // double-extraction when the envelope merged an array into the flat record.
  const rawPayload = asRecord(event.payload);
  const rawVerification = asRecord(event.verification);
  const rawArtifactRefs = asRecord(event.artifact_refs);
  const bridgedIdentities = rawPayload.bridged_identities ?? rawVerification.bridged_identities ?? rawArtifactRefs.bridged_identities;
  if (Array.isArray(bridgedIdentities)) {
    for (const identity of bridgedIdentities.slice(0, 8)) {
      const rec = asRecord(identity);
      const taskIdVal = stringFrom(rec.task_id);
      if (taskIdVal) {
        const safe = sanitizePublicTimelineText(taskIdVal);
        if (safe && safe !== "[private detail redacted]") {
          relations.push({ kind: "backlog_row", label: "bridged task", value: safe, summary: "Task id from cross-ref lineage bridge" });
        }
      }
    }
  }
  const bridgeChildTaskIds = [
    ...stringsFromBridgeAction(rawPayload),
    ...stringsFromBridgeAction(rawVerification),
    ...stringsFromBridgeAction(rawArtifactRefs),
  ];
  for (const taskIdVal of bridgeChildTaskIds.slice(0, 8)) {
    const safe = sanitizePublicTimelineText(taskIdVal);
    if (safe && safe !== "[private detail redacted]") {
      relations.push({ kind: "backlog_row", label: "bridged task", value: safe, summary: "Child task id from cross-ref lineage bridge action" });
    }
  }

  // De-duplicate by kind+value, keeping the first occurrence (which preserves the
  // richer label when qa_refs and qa_verdict_refs produce the same event id).
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

function isLineageBridgeEvent(event: TaskTimelineEvent): boolean {
  const raw = [
    event.event_kind,
    event.event_type,
    event.phase,
    event.actor,
    valueAtPath(event as unknown as Record<string, unknown>, "payload.action"),
    valueAtPath(event as unknown as Record<string, unknown>, "payload.next_legal_action"),
    valueAtPath(event as unknown as Record<string, unknown>, "payload.next_legal_action.action"),
    valueAtPath(event as unknown as Record<string, unknown>, "payload.lineage_bridge_action.action"),
    valueAtPath(event as unknown as Record<string, unknown>, "verification.action"),
    valueAtPath(event as unknown as Record<string, unknown>, "verification.lineage_bridge_action.action"),
  ].flatMap(stringsFromUnknown).join(" ").toLowerCase();
  return /(^|[\s._-])(lineage_bridge|cross_ref_lineage_bridge|record_cross_ref_lineage_bridge)([\s._-]|$)/.test(raw);
}

function stringsFromBridgeAction(source: Record<string, unknown>): string[] {
  const values: string[] = [];
  const visit = (value: unknown, depth: number): void => {
    if (depth > 4 || value == null) return;
    if (Array.isArray(value)) {
      for (const item of value) visit(item, depth + 1);
      return;
    }
    if (typeof value !== "object") return;
    const record = value as Record<string, unknown>;
    const actionText = stringsFromUnknown(record.action ?? record.next_legal_action ?? record.event_kind).join(" ").toLowerCase();
    const bridgeShaped = /(^|[\s._-])(lineage_bridge|cross_ref_lineage_bridge|record_cross_ref_lineage_bridge)([\s._-]|$)/.test(actionText)
      || Array.isArray(record.child_task_ids)
      || Array.isArray(record.bridged_identities);
    if (bridgeShaped) {
      values.push(...stringsFromUnknown(record.child_task_ids));
      values.push(...stringsFromUnknown(record.attempt_task_ids));
      const identities = record.bridged_identities;
      if (Array.isArray(identities)) {
        for (const identity of identities) {
          const identityRecord = asRecord(identity);
          values.push(...stringsFromUnknown(identityRecord.task_id));
        }
      }
    }
    for (const key of ["lineage_bridge_action", "bridge_action", "next_legal_action", "deterministic_actions", "repair_summary"]) {
      if (key in record) visit(record[key], depth + 1);
    }
  };
  visit(source, 0);
  return values;
}

function laneIdForEvent(event: TaskTimelineEvent): TaskTimelineSemanticLane {
  if (isLineageBridgeEvent(event)) return "observer";
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

/**
 * Public helper: returns the semantic short label for a lane family string.
 * Accepts both `TaskTimelineSemanticLane` values and free-form lane strings
 * (e.g. raw event lane keys) so DAG/backlog detail can reuse the same labels
 * without duplicating the mapping table.
 *
 * Returns null when the string does not map to a recognised lane family —
 * callers should fall back to their own label in that case.
 */
export function semanticLaneLabel(raw: string): string | null {
  const normalized = (raw || "").trim().toLowerCase();
  if (!normalized) return null;
  // Exact matches for canonical lane ids
  if (normalized === "content_sys" || normalized === "content-sys") return "content-sys";
  if (normalized === "gate") return "Close gate";
  if (normalized === "verification") return "Verification";
  if (normalized === "worker") return "Bounded worker";
  if (normalized === "observer") return "Observer";
  // Prefix/substring matches for composite lane ids (e.g. "worker_frontend_1")
  if (normalized.startsWith("worker")) return "Bounded worker";
  if (normalized.includes("gate") || normalized.includes("merge") || normalized.includes("close")) return "Close gate";
  if (normalized.includes("verify") || normalized.includes("test") || normalized.includes("qa")) return "Verification";
  if (normalized.includes("content") && normalized.includes("sys")) return "content-sys";
  if (normalized === "observer" || normalized.includes("observer")) return "Observer";
  return null;
}

function actorForEvent(event: TaskTimelineEvent, lane: TaskTimelineSemanticLane): string {
  if (isLineageBridgeEvent(event)) return "Observer";
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

/** Normalize int or numeric-string event ids to string form. */
function normalizeEventId(value: unknown): string {
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value.trim();
  return stringFrom(value);
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

// ── Semantic status-word emphasis ────────────────────────────────────────────

export type StatusWordClass = "positive" | "negative" | "neutral";

/**
 * Classify a single word for inline chip rendering.
 *
 * Returns a chip class when the word (case-insensitive, whole-word) belongs to
 * the governance status vocabulary, or null for everything else (no false chips).
 *
 * Whole-word contract: callers must split on word boundaries before calling
 * this function. "passed" inside "surpassed" must NOT be classified.
 */
export function classifyStatusWord(word: string): StatusWordClass | null {
  if (!word) return null;
  if (/^(passed|accepted|ok|validated|close_satisfying|succeeded)$/i.test(word)) return "positive";
  if (/^(blocked|failed|refused|rejected|denied)$/i.test(word)) return "negative";
  if (/^(allowed|requested|pending|running)$/i.test(word)) return "neutral";
  return null;
}

export interface TextSegment {
  text: string;
  chipClass: StatusWordClass | null;
}

/**
 * Split a plain-text string into segments for inline chip rendering.
 *
 * Non-word runs (spaces, punctuation) are emitted as plain segments (chipClass
 * null). Word tokens are classified via classifyStatusWord; if classified they
 * get the matching chip class, otherwise they are plain.
 *
 * Empty string → [].
 */
export function segmentTextWithStatusChips(text: string): TextSegment[] {
  if (!text) return [];
  // Split on word-boundary transitions: capture both word tokens and non-word runs.
  const tokens = text.split(/(\b\w+\b)/);
  const segments: TextSegment[] = [];
  for (const token of tokens) {
    if (!token) continue;
    // A token is a "word" if it matches \w+ exactly (captured group).
    const isWord = /^\w+$/.test(token);
    const chipClass = isWord ? classifyStatusWord(token) : null;
    const last = segments[segments.length - 1];
    if (chipClass === null && last && last.chipClass === null) {
      // Merge consecutive plain segments for compact output.
      last.text += token;
    } else {
      segments.push({ text: token, chipClass });
    }
  }
  return segments;
}

// ── Contract × Gate verification matrix projection ───────────────────────────

/**
 * A single row in the contract × gate verification matrix.
 *
 * Each row represents one requirement/gate check.
 * The matrix groups rows by gate family (timeline / route-context / receipt /
 * cross-ref / contract / impact) so the operator can scan gate families at a
 * glance and see whether each item passed.
 */
export interface GateMatrixRow {
  /** Raw requirement id from the gate (e.g. "implementation", "route_context"). */
  id: string;
  /** Plain-English label (operator-readable). */
  label: string;
  /** Gate family group. */
  family: "timeline" | "route_context" | "contract" | "receipt_projection" | "cross_ref" | "impact" | "audit_close" | "other";
  /** Human-readable family label. */
  familyLabel: string;
  /** Whether this gate item is mandatory for close. */
  required: boolean;
  /** Derived pass/fail/missing/not_applicable status. */
  status: "passed" | "failed" | "missing" | "not_applicable" | "unknown";
  /** Short next-action or missing-reason text (from gate's own fields). */
  nextAction: string;
  /** Event ids that count as evidence for this row (for deep-link to playback). */
  evidenceEventIds: string[];
  /**
   * AC1: enriched labels for evidence events.
   * Each entry corresponds 1-to-1 with evidenceEventIds.
   * Format: "event_kind · status" (e.g. "route_action_precheck · allowed").
   * Empty string when kind/status are not available.
   */
  evidenceLabels: string[];
}

/** The full matrix projection consumed by the ContractGateMatrix component. */
export interface GateMatrixProjection {
  schema_version: "gate_matrix_projection.v1";
  rows: GateMatrixRow[];
  /** Whether the overall close gate passed. */
  overallPassed: boolean;
  /** Whether the gate response was present at all. */
  gatePresent: boolean;
  /** Whether this backlog is subject to the close gate. */
  applicable: boolean;
}

// ── Plain-English labels for well-known gate requirement ids ─────────────────

const GATE_ROW_LABELS: Record<string, string> = {
  // Timeline gate
  implementation:                          "Implementation evidence recorded",
  verification:                            "Verification evidence recorded",
  close_ready:                             "Close-ready evidence recorded",
  // Route-context gate
  route_context:                           "Route context bundle obtained",
  route_action_precheck:                   "Route action precheck passed",
  bounded_implementation_worker_dispatch:  "Bounded worker dispatched",
  mf_subagent_startup:                     "Bounded worker started",
  independent_verification_lane:           "Independent QA verified",
  architecture_review_lane:               "Architecture review completed",
  route_identity_mismatch:                "Route identity mismatch resolved",
  same_route_identity:                    "Route identity consistent",
  route_identity_cleanup:                 "Stale route identity cleaned up",
  // Contract gate
  contract_gate:                           "Contract requirements met",
  // Receipt / projection
  mf_subagent_read_receipt:               "Worker read-receipt recorded before counted evidence",
  read_receipt:                           "Read receipt ordered before counted evidence",
  // Cross-ref gate
  cross_ref_gate:                         "Cross-ref foreign-row evidence verified",
  // Worker graph trace
  worker_graph_trace:                     "Worker graph trace recorded",
  independent_qa:                         "Independent QA gate passed",
  normal_close_gate:                      "Normal MF close remains blocked",
  audit_close_gate:                       "Audit close accepted",
  qa_acceptance:                          "QA acceptance passed",
  evidence_reconstruction:                "Historical evidence not reconstructed",
  // Post-verification
  post_verification_actions:              "Post-verification actions completed",
  // Approval
  approval_scope:                         "Approval scope valid for close",
  // Command disposition
  command_disposition:                    "Originating command terminal",
};

function gateRowLabel(id: string): string {
  return GATE_ROW_LABELS[id] ?? id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function gateFamily(id: string): GateMatrixRow["family"] {
  if (["implementation", "verification", "close_ready"].includes(id)) return "timeline";
  if ([
    "route_context", "route_action_precheck", "bounded_implementation_worker_dispatch",
    "mf_subagent_startup", "independent_verification_lane", "architecture_review_lane",
    "route_identity_mismatch", "same_route_identity", "route_identity_cleanup",
  ].includes(id)) return "route_context";
  if (id === "cross_ref_gate" || id.startsWith("cross_ref")) return "cross_ref";
  if (id === "audit_close_gate" || id === "normal_close_gate" || id === "qa_acceptance" || id === "evidence_reconstruction") return "audit_close";
  if (id.includes("read_receipt") || id === "contract_projection") return "receipt_projection";
  if (id === "contract_gate" || id.includes("contract")) return "contract";
  return "other";
}

const FAMILY_LABELS: Record<GateMatrixRow["family"], string> = {
  timeline:          "Timeline evidence",
  route_context:     "Route context & worker lanes",
  contract:          "Contract requirements",
  receipt_projection:"Receipt / projection",
  cross_ref:         "Cross-ref gate",
  impact:            "Impact",
  audit_close:       "Audit close & QA",
  other:             "Other checks",
};

function eventIdsFromGateEvents(gateEvents: unknown): { ids: string[]; labels: string[] } {
  const ids: string[] = [];
  const labels: string[] = [];
  if (!Array.isArray(gateEvents)) return { ids, labels };
  for (const item of gateEvents) {
    if (item && typeof item === "object") {
      const rec = item as Record<string, unknown>;
      const id = rec.id ?? rec.event_id ?? rec.timeline_event_id;
      if (id != null) {
        ids.push(String(id));
        // AC1: build "event_kind · status" label for each evidence event
        const kind = typeof rec.event_kind === "string" ? rec.event_kind : (typeof rec.phase === "string" ? rec.phase : "");
        const status = typeof rec.status === "string" ? rec.status : "";
        const label = [kind, status].filter(Boolean).join(" · ");
        labels.push(label);
      }
    }
  }
  return { ids, labels };
}

/**
 * Pure projection: MfCloseTimelineGate (from BacklogTimelineGateResponse) →
 * GateMatrixProjection.
 *
 * No side effects. Safe to call from tests directly without DOM or React.
 * Unknown/extra gate fields fall back to a generic row — never throws.
 */
export function projectGateMatrix(
  gate: {
    passed?: boolean;
    status?: string;
    required_event_kinds?: string[];
    present_event_kinds?: string[];
    missing_event_kinds?: string[];
    contract_gate?: {
      passed?: boolean;
      required_requirement_ids?: string[];
      present_requirement_ids?: string[];
      missing_requirement_ids?: string[];
      evidence_events?: unknown[];
    };
    route_context_gate?: {
      passed?: boolean;
      required?: boolean;
      required_requirement_ids?: string[];
      present_requirement_ids?: string[];
      missing_requirement_ids?: string[];
      evidence_events?: Record<string, unknown>;
    };
    contract_projection?: { status?: string; stale?: boolean; divergent?: boolean; read_receipt_gate?: { passed?: boolean; status?: string; read_receipt_event_id?: number | string } };
    cross_ref_gate?: { passed?: boolean; status?: string };
    worker_graph_trace_gate?: { required?: boolean; passed?: boolean; status?: string; trace_ids?: string[] };
    independent_qa_gate?: { required?: boolean; passed?: boolean; status?: string };
    normal_close_gate?: Record<string, unknown>;
    audit_archive?: Record<string, unknown>;
    audit_close_gate?: Record<string, unknown>;
    qa_acceptance?: Record<string, unknown>;
    fixed_close_waiver_alert?: Record<string, unknown>;
    contract_projection_gate?: { passed?: boolean; status?: string };
    checks?: Record<string, boolean | number | string>;
  } | undefined,
  applicable: boolean,
): GateMatrixProjection {
  if (!gate || !applicable) {
    return {
      schema_version: "gate_matrix_projection.v1",
      rows: [],
      overallPassed: applicable ? (gate?.passed ?? false) : true,
      gatePresent: Boolean(gate),
      applicable,
    };
  }

  const rows: GateMatrixRow[] = [];

  // ── 1. Timeline gate rows (required_event_kinds) ─────────────────────────
  const required = gate.required_event_kinds ?? ["implementation", "verification", "close_ready"];
  const present = new Set(gate.present_event_kinds ?? []);
  const missing = new Set(gate.missing_event_kinds ?? []);
  for (const id of required) {
    const isPassed = present.has(id);
    const isMissing = missing.has(id);
    rows.push({
      id,
      label: gateRowLabel(id),
      family: gateFamily(id),
      familyLabel: FAMILY_LABELS[gateFamily(id)],
      required: true,
      status: isPassed ? "passed" : isMissing ? "missing" : "unknown",
      nextAction: isMissing ? `Append ${id.replace(/_/g, " ")} event to the task timeline` : "",
      evidenceEventIds: [],
      evidenceLabels: [],
    });
  }

  // ── 2. Route-context gate rows ────────────────────────────────────────────
  const rg = gate.route_context_gate;
  if (rg) {
    const rgRequired = new Set(rg.required_requirement_ids ?? []);
    const rgPresent = new Set(rg.present_requirement_ids ?? []);
    const rgMissing = new Set(rg.missing_requirement_ids ?? []);
    const evidenceMap = rg.evidence_events ?? {};
    for (const id of Array.from(rgRequired)) {
      const eventsForId = (evidenceMap as Record<string, unknown>)[id];
      const { ids: evidenceIds, labels: evidenceLabels } = eventIdsFromGateEvents(eventsForId);
      rows.push({
        id,
        label: gateRowLabel(id),
        family: gateFamily(id),
        familyLabel: FAMILY_LABELS[gateFamily(id)],
        required: rg.required !== false,
        status: rgPresent.has(id) ? "passed" : rgMissing.has(id) ? "missing" : "unknown",
        nextAction: rgMissing.has(id) ? `Record ${gateRowLabel(id).toLowerCase()}` : "",
        evidenceEventIds: evidenceIds,
        evidenceLabels,
      });
    }
    // Add any present-but-not-required items as informational
    for (const id of Array.from(rgPresent)) {
      if (!rgRequired.has(id)) {
        const { ids: evidenceIds, labels: evidenceLabels } = eventIdsFromGateEvents((evidenceMap as Record<string, unknown>)[id]);
        rows.push({
          id,
          label: gateRowLabel(id),
          family: gateFamily(id),
          familyLabel: FAMILY_LABELS[gateFamily(id)],
          required: false,
          status: "passed",
          nextAction: "",
          evidenceEventIds: evidenceIds,
          evidenceLabels,
        });
      }
    }
  }

  // ── 3. Contract gate rows ─────────────────────────────────────────────────
  const cg = gate.contract_gate;
  if (cg) {
    const cgRequired = new Set(cg.required_requirement_ids ?? []);
    const cgPresent = new Set(cg.present_requirement_ids ?? []);
    const cgMissing = new Set(cg.missing_requirement_ids ?? []);
    const cgEvents = cg.evidence_events ?? [];
    const { ids: cgEventIds, labels: cgEventLabels } = eventIdsFromGateEvents(cgEvents);
    if (cgRequired.size > 0) {
      for (const id of Array.from(cgRequired)) {
        rows.push({
          id,
          label: gateRowLabel(id),
          family: "contract",
          familyLabel: FAMILY_LABELS["contract"],
          required: true,
          status: cgPresent.has(id) ? "passed" : cgMissing.has(id) ? "missing" : "unknown",
          nextAction: cgMissing.has(id) ? `Record contract evidence for ${id}` : "",
          evidenceEventIds: cgPresent.has(id) ? cgEventIds : [],
          evidenceLabels: cgPresent.has(id) ? cgEventLabels : [],
        });
      }
    } else if (cg.passed !== undefined) {
      // Contract gate present but no explicit requirement ids: show summary row
      rows.push({
        id: "contract_gate",
        label: "Contract requirements met",
        family: "contract",
        familyLabel: FAMILY_LABELS["contract"],
        required: true,
        status: cg.passed ? "passed" : "failed",
        nextAction: cg.passed ? "" : "Check contract requirement ids",
        evidenceEventIds: cg.passed ? cgEventIds : [],
        evidenceLabels: cg.passed ? cgEventLabels : [],
      });
    }
  }

  // ── 4. Receipt / projection row ───────────────────────────────────────────
  const cp = gate.contract_projection;
  if (cp) {
    const rrGate = cp.read_receipt_gate;
    if (rrGate) {
      const rrId = rrGate.read_receipt_event_id;
      rows.push({
        id: "mf_subagent_read_receipt",
        label: gateRowLabel("mf_subagent_read_receipt"),
        family: "receipt_projection",
        familyLabel: FAMILY_LABELS["receipt_projection"],
        required: false,
        status: rrGate.passed ? "passed" : rrGate.status === "not_required" ? "not_applicable" : "missing",
        nextAction: rrGate.passed ? "" : "Record mf_subagent_read_receipt before counted evidence events",
        evidenceEventIds: rrId != null ? [String(rrId)] : [],
        evidenceLabels: [],
      });
    }
    rows.push({
      id: "contract_projection",
      label: "Contract projection current",
      family: "receipt_projection",
      familyLabel: FAMILY_LABELS["receipt_projection"],
      required: false,
      status: cp.status === "current" ? "passed" : cp.divergent ? "failed" : cp.stale ? "failed" : "unknown",
      nextAction: cp.stale || cp.divergent ? "Re-run the route service to refresh the contract projection" : "",
      evidenceEventIds: [],
      evidenceLabels: [],
    });
  }

  // ── 5. Cross-ref gate row ─────────────────────────────────────────────────
  if (gate.cross_ref_gate) {
    rows.push({
      id: "cross_ref_gate",
      label: gateRowLabel("cross_ref_gate"),
      family: "cross_ref",
      familyLabel: FAMILY_LABELS["cross_ref"],
      required: false,
      status: gate.cross_ref_gate.passed ? "passed" : "failed",
      nextAction: gate.cross_ref_gate.passed ? "" : "Check cross-ref gate: foreign-row identity mismatch",
      evidenceEventIds: [],
      evidenceLabels: [],
    });
  }

  // ── 6. Worker graph trace gate row ────────────────────────────────────────
  if (gate.worker_graph_trace_gate?.required) {
    const wg = gate.worker_graph_trace_gate;
    rows.push({
      id: "worker_graph_trace",
      label: gateRowLabel("worker_graph_trace"),
      family: "other",
      familyLabel: FAMILY_LABELS["other"],
      required: true,
      status: wg.passed ? "passed" : "missing",
      nextAction: wg.passed ? "" : "Run graph_query with query_source=mf_subagent and record trace evidence",
      evidenceEventIds: wg.trace_ids ?? [],
      evidenceLabels: (wg.trace_ids ?? []).map(() => ""),
    });
  }

  // ── 7. Independent QA gate row ────────────────────────────────────────────
  if (gate.independent_qa_gate?.required) {
    const qa = gate.independent_qa_gate;
    rows.push({
      id: "independent_qa",
      label: gateRowLabel("independent_qa"),
      family: "other",
      familyLabel: FAMILY_LABELS["other"],
      required: true,
      status: qa.passed ? "passed" : "missing",
      nextAction: qa.passed ? "" : "Run the independent QA verification lane",
      evidenceEventIds: [],
      evidenceLabels: [],
    });
  }

  // ── 8. Audit archive / audit close path rows ────────────────────────────
  const auditRows = auditCloseRows(gate);
  rows.push(...auditRows);

  // Deduplicate by id (first occurrence wins)
  const seen = new Set<string>();
  const deduped = rows.filter((r) => {
    if (seen.has(r.id)) return false;
    seen.add(r.id);
    return true;
  });

  return {
    schema_version: "gate_matrix_projection.v1",
    rows: deduped,
    overallPassed: gate.passed ?? false,
    gatePresent: true,
    applicable,
  };
}

function auditCloseRows(gate: {
  normal_close_gate?: Record<string, unknown>;
  audit_archive?: Record<string, unknown>;
  audit_close_gate?: Record<string, unknown>;
  qa_acceptance?: Record<string, unknown>;
  fixed_close_waiver_alert?: Record<string, unknown>;
}): GateMatrixRow[] {
  const archive = asRecord(gate.audit_archive);
  const archiveEvidence = asRecord(archive.evidence);
  const normalGate = firstRecord(gate.normal_close_gate, archive.normal_close_gate);
  const auditGate = firstRecord(gate.audit_close_gate, archive.audit_close_gate, archive);
  const qaAcceptance = firstRecord(gate.qa_acceptance, archive.qa_acceptance, archiveEvidence.verification);
  const alert = asRecord(gate.fixed_close_waiver_alert);
  const hasAuditClose =
    Object.keys(archive).length > 0
    || Object.keys(auditGate).length > 0
    || Object.keys(qaAcceptance).length > 0
    || Object.keys(normalGate).length > 0
    || Object.keys(alert).length > 0;
  if (!hasAuditClose) return [];

  const rows: GateMatrixRow[] = [];
  const normalBlocked = normalCloseBlocked(normalGate, archive, alert);
  if (Object.keys(normalGate).length > 0 || Object.keys(archive).length > 0 || Object.keys(alert).length > 0) {
    rows.push({
      id: "normal_close_gate",
      label: gateRowLabel("normal_close_gate"),
      family: "audit_close",
      familyLabel: FAMILY_LABELS.audit_close,
      required: true,
      status: normalBlocked ? "failed" : "unknown",
      nextAction: normalBlocked
        ? "Normal close remains blocked; do not backfill missing startup or close_ready evidence."
        : "Confirm normal close remains separate from audit close.",
      evidenceEventIds: eventIdsFromLooseRecord(normalGate, archive),
      evidenceLabels: eventIdsFromLooseRecord(normalGate, archive).map(() => "audit archive"),
    });
  }

  const auditAccepted = acceptedLike(auditGate) || stringFrom(archive.status) === "audit_archived" || stringFrom(archive.row_status).toUpperCase() === "WAIVED";
  if (Object.keys(auditGate).length > 0 || Object.keys(archive).length > 0) {
    rows.push({
      id: "audit_close_gate",
      label: gateRowLabel("audit_close_gate"),
      family: "audit_close",
      familyLabel: FAMILY_LABELS.audit_close,
      required: true,
      status: auditAccepted ? "passed" : "unknown",
      nextAction: auditAccepted ? "" : "Record audit close gate acceptance.",
      evidenceEventIds: eventIdsFromLooseRecord(auditGate, archive),
      evidenceLabels: eventIdsFromLooseRecord(auditGate, archive).map(() => "audit close"),
    });
  }

  if (Object.keys(qaAcceptance).length > 0) {
    const qaPassed = acceptedLike(qaAcceptance);
    rows.push({
      id: "qa_acceptance",
      label: gateRowLabel("qa_acceptance"),
      family: "audit_close",
      familyLabel: FAMILY_LABELS.audit_close,
      required: true,
      status: qaPassed ? "passed" : "missing",
      nextAction: qaPassed ? "" : "Record independent QA acceptance before audit close.",
      evidenceEventIds: eventIdsFromLooseRecord(qaAcceptance),
      evidenceLabels: eventIdsFromLooseRecord(qaAcceptance).map(() => "QA acceptance"),
    });
  }

  if (Object.keys(archive).length > 0) {
    const reason = stringFrom(archive.non_reconstructable_evidence_reason);
    const evidence = firstRecord(archive.evidence);
    const failureAudit = firstRecord(archive.failure_audit, evidence.failure_audit);
    const reconstructed =
      evidence.reconstructed
      ?? archive.reconstructed
      ?? archive.historical_evidence_reconstructed
      ?? failureAudit.historical_evidence_reconstructed;
    const notReconstructed = reconstructed === false || Boolean(reason);
    rows.push({
      id: "evidence_reconstruction",
      label: gateRowLabel("evidence_reconstruction"),
      family: "audit_close",
      familyLabel: FAMILY_LABELS.audit_close,
      required: true,
      status: notReconstructed ? "passed" : "unknown",
      nextAction: notReconstructed
        ? reason || "Historical MF evidence was not reconstructed."
        : "Record why missing historical evidence cannot be reconstructed.",
      evidenceEventIds: eventIdsFromLooseRecord(archive),
      evidenceLabels: eventIdsFromLooseRecord(archive).map(() => "audit archive"),
    });
  }

  return rows;
}

function firstRecord(...values: unknown[]): Record<string, unknown> {
  for (const value of values) {
    const record = asRecord(value);
    if (Object.keys(record).length > 0) return record;
  }
  return {};
}

function acceptedLike(record: Record<string, unknown>): boolean {
  if (record.accepted === true || record.passed === true || record.approved === true) return true;
  const text = [
    stringFrom(record.status),
    stringFrom(record.decision),
    stringFrom(record.result),
  ].join(" ").toLowerCase();
  return /\b(accepted|passed|approved|ok|success|audit_archived)\b/.test(text);
}

function normalCloseBlocked(
  normalGate: Record<string, unknown>,
  archive: Record<string, unknown>,
  alert: Record<string, unknown>,
): boolean {
  if (normalGate.normal_close_gate_passed === false || normalGate.can_close === false || normalGate.can_close_claimed === false) return true;
  if (normalGate.close_ready_emitted === false || normalGate.ordinary_mf_close_claimed === false) return true;
  if (alert.alert === true) return true;
  const timelinePrecheck = asRecord(asRecord(archive.evidence).timeline_precheck_failure_summary);
  if (timelinePrecheck.can_close === false) return true;
  return stringFrom(archive.status) === "audit_archived";
}

function eventIdsFromLooseRecord(...records: Record<string, unknown>[]): string[] {
  const ids: string[] = [];
  for (const record of records) {
    for (const key of ["event_id", "id", "timeline_event_id"]) {
      const id = normalizeEventId(record[key]);
      if (id) ids.push(id);
    }
    for (const key of ["event_ids", "evidence_event_ids", "timeline_event_ids"]) {
      const value = record[key];
      if (Array.isArray(value)) ids.push(...value.map(normalizeEventId).filter(Boolean));
    }
    const { ids: nested } = eventIdsFromGateEvents(record.evidence_events);
    ids.push(...nested);
  }
  return Array.from(new Set(ids)).slice(0, 8);
}
