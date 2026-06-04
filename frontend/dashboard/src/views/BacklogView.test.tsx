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
