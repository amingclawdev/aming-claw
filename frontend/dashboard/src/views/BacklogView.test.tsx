import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";

import { ContractRuntimeAuthorityPanel } from "../components/TaskPlaybackPanel";
import type { ContractRuntimeAuthorityViewModel } from "../lib/taskPlayback";
import type { BacklogBug } from "../types";
import {
  buildBacklogCanonicalVerificationFixtureDagForTest,
  buildBacklogEmptyVerificationFixtureDagForTest,
  buildBacklogParallelTimelineFixtureDagForTest,
  buildBacklogSemanticLaneParityFixtureDagForTest,
  acceptanceCriteriaFrom,
  acceptanceCriterionVerification,
  BacklogAcceptanceCriteria,
  BacklogRow,
  filterBacklogHotWindowRows,
} from "./BacklogView";

const projectedCommandBug: BacklogBug = {
  bug_id: "AC-OBSERVER-COMMAND-TERMINAL-PROJECTION-FROM-CONTRACT-20260604",
  title: "Project command terminal status",
  status: "FIXED",
  priority: "P1",
  target_files: [],
  test_files: [],
  acceptance_criteria: [],
  created_at: "2026-06-04T00:00:00Z",
  updated_at: "2026-06-04T00:00:00Z",
  observer_command_projection: {
    schema_version: "observer_command_backlog_projection.v1",
    source_of_truth: "Contract/Revision/Event",
    command_id: "cmd-d0e3e3bf7893",
    command_status: "completed",
    canonical_contract_state: "closed",
    command_projection_status: "completed",
    divergence_reason: "superseded_route_identity_reconciled",
    canonical_route_identity: { route_id: "route-repair-e97d980211e2dc1c" },
    superseded_route_identity: { route_id: "route-repair-01c5a0404ba10777" },
    terminal_evidence_refs: [{ request_id: "req-97cd668efd14" }],
    projection: {
      schema_version: "observer_command_terminal_projection.v1",
      source_of_truth: "Contract/Revision/Event",
      command_projection_status: "completed",
    },
  },
};

export function projectedCommandCardLabel(bug: BacklogBug = projectedCommandBug): string {
  const projection = bug.observer_command_projection;
  const status = projection?.command_projection_status || projection?.projection?.command_projection_status || "";
  const reason = projection?.divergence_reason || projection?.projection?.divergence_reason || "";
  return reason ? `command ${status} ${reason}` : `command ${status}`;
}

export const projectedCommandCardFixtureLabel = projectedCommandCardLabel();

const backlogViewSource = readFileSync(new URL("./BacklogView.tsx", import.meta.url), "utf8");
const playbackPanelSource = readFileSync(new URL("../components/TaskPlaybackPanel.tsx", import.meta.url), "utf8");
const stylesSource = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

function assertBacklogAuthority(condition: boolean, message: string): void {
  if (!condition) throw new Error(`Backlog authority fixture failed: ${message}`);
}

function renderBacklogRow(bug: BacklogBug): string {
  return renderToStaticMarkup(
    <table>
      <tbody>
        <BacklogRow bug={bug} projectId="aming-claw" onOpenDetail={() => undefined} />
      </tbody>
    </table>,
  );
}

const unavailableCurrentRow = renderBacklogRow({
  bug_id: "AC-CURRENT-UNAVAILABLE",
  title: "Current unavailable control",
  status: "OPEN",
  priority: "P1",
  runtime_state: "FAILED_LEGACY_RUNTIME",
  chain_stage: "legacy-chain-stage",
  mf_type: "legacy-mf-type",
  contract_summary: {
    has_contract: true,
    template_id: "legacy-template",
    required_evidence_count: 99,
    projection_status: "passed",
    projection_watermark: 188,
    source_of_truth: "legacy-chain-trigger",
  },
  commit: "1234567890abcdef",
  worktree_branch: "codex/retained-branch-fact",
});
assertBacklogAuthority(
  unavailableCurrentRow.includes("Current authority unavailable")
    && unavailableCurrentRow.includes("OPEN")
    && unavailableCurrentRow.includes("1234567")
    && unavailableCurrentRow.includes("codex/retained-branch-fact")
    && !unavailableCurrentRow.includes("FAILED_LEGACY_RUNTIME")
    && !unavailableCurrentRow.includes("legacy-chain-stage")
    && !unavailableCurrentRow.includes("legacy-mf-type")
    && !unavailableCurrentRow.includes("legacy-template")
    && !unavailableCurrentRow.includes("projection passed"),
  "a compact row without current ContractRuntime authority must show unavailable without promoting legacy runtime or contract projection",
);

const sourceBackedCommandRow = renderBacklogRow({
  ...projectedCommandBug,
  runtime_state: "FAILED_LEGACY_RUNTIME",
  chain_stage: "legacy-chain-stage",
  mf_type: "legacy-mf-type",
  contract_summary: {
    has_contract: true,
    template_id: "legacy-template",
    required_evidence_count: 99,
    projection_status: "passed",
  },
});
assertBacklogAuthority(
  sourceBackedCommandRow.includes("Current authority unavailable")
    && sourceBackedCommandRow.includes("FIXED")
    && sourceBackedCommandRow.includes("command completed")
    && sourceBackedCommandRow.includes("superseded route identity reconciled")
    && !sourceBackedCommandRow.includes("FAILED_LEGACY_RUNTIME")
    && !sourceBackedCommandRow.includes("legacy-template")
    && !sourceBackedCommandRow.includes("projection passed"),
  "source-backed command and raw backlog facts must remain visible without letting conflicting legacy runtime data become current authority",
);

for (const status of ["OPEN", "FIXED", "WAIVED", "SUPERSEDED"]) {
  const markup = renderBacklogRow({
    bug_id: `AC-RAW-${status}`,
    title: `Raw ${status}`,
    status,
    priority: "P1",
  });
  assertBacklogAuthority(
    markup.includes(`>${status}<`) && markup.includes("Current authority unavailable"),
    `raw ${status} disposition must remain visible and distinct from unavailable current authority`,
  );
}

assertBacklogAuthority(
  backlogViewSource.includes("projectContractRuntimeAuthorityViewModel")
    && backlogViewSource.includes("ContractRuntimeAuthorityPanel"),
  "Backlog detail and Timeline DAG must consume the canonical three-axis authority view",
);
assertBacklogAuthority(
  backlogViewSource.includes("projectContractRuntimeGateMatrix(timeline?.authorityView)")
    && backlogViewSource.includes('data-contract-gate-authority="contract-runtime"')
    && backlogViewSource.includes("ContractRuntime authority matrix")
    && backlogViewSource.includes('data-contract-gate-authority="legacy-advisory"')
    && backlogViewSource.includes("Historical MF close gate (advisory)"),
  "Contract & Gate must render ContractRuntime current authority as primary and legacy MF close-gate data as advisory",
);
assertBacklogAuthority(
  backlogViewSource.includes("Canonical ContractRuntime authority is not loaded")
    && backlogViewSource.includes("cannot establish PASS or BLOCKED")
    && backlogViewSource.includes("legacy MF close gate raw (advisory)"),
  "missing canonical authority must fail closed without promoting legacy timeline-gate state",
);
assertBacklogAuthority(
  backlogViewSource.includes("normalizeTaskPlaybackDag")
    && backlogViewSource.includes("visualization: compactTimeline?.contract_runtime_visualization")
    && backlogViewSource.includes("timeline-event:${timelineEventKey(event, index)}"),
  "Backlog detail and Playback must share the canonical typed-DAG normalizer and event identities",
);

const semanticLaneDag = buildBacklogSemanticLaneParityFixtureDagForTest();
assertBacklogAuthority(
  semanticLaneDag.lanes.map((lane) => lane.id).join(",") === "observer,verification,gate",
  "generic Aming Claw actor labels must retain the same Observer, Verification, and Close gate lanes as Playback",
);
assertBacklogAuthority(
  semanticLaneDag.lanes.map((lane) => lane.label).join(",") === "Observer,Verification,Close gate",
  "semantic lane ids must render public labels without raw actor or runtime identifiers",
);

const parallelWorkerDag = buildBacklogParallelTimelineFixtureDagForTest();
const parallelWorkerLanes = parallelWorkerDag.lanes.filter((lane) => lane.family === "worker");
assertBacklogAuthority(
  parallelWorkerDag.workerLaneCount === 2
    && parallelWorkerLanes.length === 2
    && new Set(parallelWorkerLanes.flatMap((lane) => lane.nodes.map((node) => node.id))).size === 2,
  "two durable worker identities must render as two distinct Subagents / Workers lanes",
);
assertBacklogAuthority(
  parallelWorkerLanes.every((lane) => lane.label.startsWith("Subagents / Workers · "))
    && parallelWorkerLanes.every((lane) => !lane.label.includes("mf_sub_")),
  "worker lane labels must remain readable aliases rather than raw worker identities",
);

const canonicalVerificationDag = buildBacklogCanonicalVerificationFixtureDagForTest();
const canonicalVerificationLane = canonicalVerificationDag.lanes.find((lane) => lane.id === "verification");
assertBacklogAuthority(
  canonicalVerificationLane?.nodes.length === 2
    && canonicalVerificationLane.nodes.map((node) => node.id).join(",")
      === "contract-line:qa-accepted,verification:blocked",
  "Verification must render every explicit canonical QA/independent-verification node and omit inferred references",
);
assertBacklogAuthority(
  canonicalVerificationLane?.nodes.map((node) => node.status).join(",") === "passed,failed"
    && canonicalVerificationLane.nodes.every((node) => node.syntheticVerification?.authority_source === "contract_runtime.completed_lines"),
  "canonical Verification nodes must retain their real authority and status without manufacturing PASS",
);

const emptyVerificationDag = buildBacklogEmptyVerificationFixtureDagForTest();
const emptyVerificationLane = emptyVerificationDag.lanes.find((lane) => lane.id === "verification");
assertBacklogAuthority(
  emptyVerificationLane?.nodes.length === 0
    && !emptyVerificationDag.nodes.some((node) => node.lane === "verification"),
  "missing authoritative verification evidence must remain an empty lane rather than a synthetic node",
);
assertBacklogAuthority(
  backlogViewSource.includes("No authoritative QA or independent-verification evidence is recorded.")
    && backlogViewSource.includes('role="status"'),
  "an empty Verification lane must state that no authoritative evidence exists",
);
assertBacklogAuthority(
  backlogViewSource.includes('className="backlog-dag-phase-track"')
    && backlogViewSource.includes("title={phase}")
    && backlogViewSource.includes("aria-label={phase}")
    && stylesSource.includes(".backlog-dag-phase-track,\n.backlog-dag-lane-track")
    && stylesSource.includes("text-overflow: ellipsis")
    && stylesSource.includes("--backlog-dag-track-min-width"),
  "phase headers and lane tracks must share stable columns while preserving full accessible phase titles",
);
assertBacklogAuthority(
  backlogViewSource.includes("Typed edges")
    && backlogViewSource.includes("edge.relationship")
    && backlogViewSource.includes("edge.authority_source")
    && backlogViewSource.includes("edge.evidence_ref")
    && backlogViewSource.includes('edge.inferred ? " · inferred" : " · explicit"'),
  "Backlog detail must visibly consume relationship, authority, evidence, and inference fields",
);
assertBacklogAuthority(
  backlogViewSource.includes("Historical compact ledger (advisory)")
    && backlogViewSource.includes("Historical action (advisory)")
    && backlogViewSource.includes("{historicalOpen ? <>")
    && backlogViewSource.includes("<CompactLedgerPanel ledger={compactLedger}"),
  "legacy compact-ledger actions must be labeled advisory when canonical authority is present",
);
assertBacklogAuthority(
  backlogViewSource.includes("BACKLOG_SEARCH_DEBOUNCE_MS = 300")
    && backlogViewSource.includes("api.backlogSearchFor(projectId")
    && backlogViewSource.includes("cursor: historyCursor")
    && !backlogViewSource.includes("offset: searchOffset")
    && !backlogViewSource.includes("status: statusFilter")
    && !backlogViewSource.includes("priority: priorityFilter"),
  "database lookup must debounce search/keyset history without refetching local facets",
);
assertBacklogAuthority(
  backlogViewSource.includes('data-server-search-results="backlog"')
    && backlogViewSource.includes('data-backlog-local-facets="status,priority"')
    && backlogViewSource.includes("Recent ${BACKLOG_HOT_WINDOW_LIMIT} scope.")
    && backlogViewSource.includes("SQLite indexed history.")
    && backlogViewSource.includes("Next indexed page"),
  "backlog must label the recent local-facet window separately from indexed history",
);

const localFacetRows: BacklogBug[] = Array.from({ length: 250 }, (_, index) => ({
  bug_id: `AC-LOCAL-FACET-${index}`,
  title: `Local facet ${index}`,
  status: index % 2 ? "OPEN" : "FIXED",
  priority: `P${index % 4}`,
  created_at: `2026-07-23T00:${String(Math.floor(index / 60)).padStart(2, "0")}:${String(index % 60).padStart(2, "0")}Z`,
  updated_at: `2026-07-23T00:${String(Math.floor(index / 60)).padStart(2, "0")}:${String(index % 60).padStart(2, "0")}Z`,
}));
const localOpenP1 = filterBacklogHotWindowRows(localFacetRows, "OPEN", "P1");
assertBacklogAuthority(
  localOpenP1.length > 0
    && localOpenP1.every((row) => row.status === "OPEN" && row.priority === "P1")
    && localFacetRows.length === 250,
  "open/closed and P0-P3 facets must operate locally over the retained recent-250 window",
);
assertBacklogAuthority(
  playbackPanelSource.includes("Backlog row close authority")
    && playbackPanelSource.includes("partial / continuation required")
    && playbackPanelSource.includes("diagnostic_backlog_id"),
  "shared authority presentation must separate row close, pagination, and bypass diagnostics",
);

const authorityPanelSsr = renderToStaticMarkup(
  <ContractRuntimeAuthorityPanel
    authority={{
      cache_identity: { key: "AC-AUTHORITY-SSR:cex-authority-ssr:18:16090" },
      contract_execution_progress: {
        display_status: "COMPLETED",
        contract_execution_id: "cex-authority-ssr",
        execution_state_revision: 18,
        current_action_source: "backlog_contract_chain_current",
        current_action: { id: "qa_graph_context", action: "record_graph_trace" },
        line_states: [],
        line_states_truncated: false,
        runtime_records_truncated: false,
      },
      backlog_close_readiness: { display_status: "OPEN", state: "open", backlog_status: "OPEN" },
      historical_diagnostics: {
        timeline_events: [],
        bypass_records: [
          {
            decision: "continue_with_audited_bypass",
            reason: "operator approved exception",
            diagnostic_backlog_id: "AC-DIAG-BYPASS",
          },
          {
            disposition: "waiver",
            status: "bypassed",
            reason: "waiver approved",
            diagnostic_backlog_id: "AC-DIAG-WAIVER",
          },
        ],
        legacy_advisories: [],
        truncated: false,
        next_cursor: "",
      },
    } as unknown as ContractRuntimeAuthorityViewModel}
  />,
);

assertBacklogAuthority(
  authorityPanelSsr.includes('class="status-badge status-unknown">COMPLETED</b>')
    && !authorityPanelSsr.includes('class="status-badge status-complete">COMPLETED</b>'),
  "contract-complete progress must render with neutral rather than success/PASS visual semantics",
);
assertBacklogAuthority(
  authorityPanelSsr.includes("BYPASSED · record 1 · operator approved exception · diagnostic AC-DIAG-BYPASS")
    && authorityPanelSsr.includes("WAIVED · record 2 · waiver approved · diagnostic AC-DIAG-WAIVER")
    && !authorityPanelSsr.includes("CONTINUE_WITH_AUDITED_BYPASS"),
  "bypass history must render canonical BYPASSED/WAIVED labels while retaining reason and diagnostic refs",
);

const cl188AcceptanceObjects = Array.from({ length: 6 }, (_, index) => ({
  id: `CL188-0${index + 1}`,
  text: `CL188 criterion ${index + 1}`,
  required_scope: {
    kind: index === 1 ? "files_and_nodes" : "files",
    files: index === 1 ? ["corridor_kit/compiler.py", "tests/test_corridor_kit.py"] : ["tests/test_corridor_kit.py"],
    ...(index === 1 ? { nodes: ["source_pipeline_admission"] } : {}),
  },
}));
const normalizedCl188 = acceptanceCriteriaFrom(cl188AcceptanceObjects);
assertBacklogAuthority(
  normalizedCl188.length === 6
    && normalizedCl188[1]?.id === "CL188-02"
    && normalizedCl188[1]?.text === "CL188 criterion 2"
    && normalizedCl188[1]?.required_scope?.nodes?.[0] === "source_pipeline_admission",
  "CL188 six-object acceptance must preserve stable id, text, and required scope",
);
const cl188UnknownMarkup = renderToStaticMarkup(<BacklogAcceptanceCriteria criteria={normalizedCl188} events={[]} />);
assertBacklogAuthority(
  cl188UnknownMarkup.includes("CL188-01")
    && cl188UnknownMarkup.includes("CL188 criterion 6")
    && cl188UnknownMarkup.includes("file: tests/test_corridor_kit.py")
    && cl188UnknownMarkup.includes("verification unknown")
    && !cl188UnknownMarkup.includes("[object Object]"),
  "structured acceptance SSR must render six objects without object coercion or fabricated verification",
);
const boundCriterionEvent = {
  id: 18801,
  event_id: "18801",
  project_id: "aming-claw",
  event_type: "qa.acceptance",
  event_kind: "verification",
  backlog_id: "CL188",
  task_id: "cex-cl188",
  provenance: {
    schema_version: "contract_runtime.event_provenance.v1",
    classification: "authority_bound",
    label: "Authority-bound evidence",
    source: "server-verified QA session evidence",
    projection_source: "contract_runtime_visualization._event_provenance",
    projection_verified: true,
    authority_bound: true,
    scope: {
      project_id: "aming-claw",
      backlog_id: "CL188",
      task_id: "cex-cl188",
      source_event_id: "18801",
    },
  },
  acceptance_evidence: [{
      criterion_id: "CL188-02",
      required_scope: cl188AcceptanceObjects[1]?.required_scope,
      evidence_ref: "timeline:18801",
      authority_bound: true,
      authority_source: "qa_session_verification",
  }],
  payload: {},
};
assertBacklogAuthority(
  acceptanceCriterionVerification(normalizedCl188[1]!, [boundCriterionEvent]) === "verified",
  "only an explicit matching authority, scope, and evidence ref may verify acceptance",
);
const mismatchedBinding = structuredClone(boundCriterionEvent);
mismatchedBinding.acceptance_evidence[0]!.required_scope = { kind: "files", files: ["different.py"] };
assertBacklogAuthority(
  acceptanceCriterionVerification(normalizedCl188[1]!, [mismatchedBinding]) === "unlinked",
  "mismatched acceptance scope must remain unlinked",
);
const legacyAcceptance = acceptanceCriteriaFrom(["legacy acceptance string"]);
assertBacklogAuthority(
  legacyAcceptance[0]?.legacy === true && legacyAcceptance[0]?.text === "legacy acceptance string",
  "legacy string acceptance must remain supported",
);
