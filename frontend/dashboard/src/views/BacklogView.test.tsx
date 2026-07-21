import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";

import { ContractRuntimeAuthorityPanel } from "../components/TaskPlaybackPanel";
import type { ContractRuntimeAuthorityViewModel } from "../lib/taskPlayback";
import type { BacklogBug } from "../types";

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

function assertBacklogAuthority(condition: boolean, message: string): void {
  if (!condition) throw new Error(`Backlog authority fixture failed: ${message}`);
}

assertBacklogAuthority(
  backlogViewSource.includes("projectContractRuntimeAuthorityViewModel")
    && backlogViewSource.includes("ContractRuntimeAuthorityPanel"),
  "Backlog detail and Timeline DAG must consume the canonical three-axis authority view",
);
assertBacklogAuthority(
  backlogViewSource.includes("normalizeTaskPlaybackDag")
    && backlogViewSource.includes("visualization: compactTimeline?.contract_runtime_visualization")
    && backlogViewSource.includes("timeline-event:${timelineEventKey(event, index)}"),
  "Backlog detail and Playback must share the canonical typed-DAG normalizer and event identities",
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
    && backlogViewSource.includes("Historical ledger action (advisory)"),
  "legacy compact-ledger actions must be labeled advisory when canonical authority is present",
);
assertBacklogAuthority(
  backlogViewSource.includes("BACKLOG_SEARCH_DEBOUNCE_MS = 300")
    && backlogViewSource.includes("api.backlogSearchFor(projectId")
    && backlogViewSource.includes("status: statusFilter")
    && backlogViewSource.includes("priority: priorityFilter")
    && backlogViewSource.includes("offset: searchOffset"),
  "backlog lookup must debounce a status/priority/paginated server query",
);
assertBacklogAuthority(
  backlogViewSource.includes('data-server-search-results="backlog"')
    && backlogViewSource.includes("Server result set.")
    && backlogViewSource.includes("local facet of the labeled server result set")
    && backlogViewSource.includes("Next server page"),
  "backlog lookup must label the server result scope and local facet pagination",
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
