import { readFileSync } from "node:fs";

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
  backlogViewSource.includes("Historical compact ledger (advisory)")
    && backlogViewSource.includes("Historical ledger action (advisory)"),
  "legacy compact-ledger actions must be labeled advisory when canonical authority is present",
);
assertBacklogAuthority(
  playbackPanelSource.includes("Backlog row close authority")
    && playbackPanelSource.includes("partial / continuation required")
    && playbackPanelSource.includes("diagnostic_backlog_id"),
  "shared authority presentation must separate row close, pagination, and bypass diagnostics",
);
