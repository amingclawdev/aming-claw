import { renderToStaticMarkup } from "react-dom/server";

import StaleGraphBanner from "./StaleGraphBanner";
import type { HealthResponse, StatusResponse } from "../types";

const snapshotCommit = "a".repeat(40);
const serviceCommit = "b".repeat(40);
const health: HealthResponse = {
  status: "ok", service: "governance", port: 40008,
  version: serviceCommit, pid: 42, request_id: "req-banner",
};
const base: StatusResponse = {
  ok: true, project_id: "aming-claw", active_snapshot_id: "full-active",
  graph_snapshot_commit: snapshotCommit,
  materialized_graph_baseline_commit: snapshotCommit,
  scan_baseline_commit: snapshotCommit, scan_baseline_id: 1,
  pending_scope_reconcile_count: 0, pending_scope_reconcile: [],
};

function render(status: StatusResponse): string {
  return renderToStaticMarkup(
    <StaleGraphBanner health={health} status={status} busy={false}
      onQueueReconcile={() => { throw new Error("update must not be called"); }} />,
  );
}

function assertBanner(condition: boolean, message: string): void {
  if (!condition) throw new Error(`StaleGraphBanner fixture failed: ${message}`);
}

const unresolved = render({
  ...base,
  current_state: { graph_stale: {
    is_stale: false, active_graph_commit: snapshotCommit,
    head_commit: "", comparison_status: "unresolved",
  } },
});
assertBanner(unresolved.includes("graph target unverified"), "show unverified state");
assertBanner(unresolved.includes("disabled=\"\""), "disable Update for unresolved owner");
assertBanner(!unresolved.includes(serviceCommit.slice(0, 7)), "do not substitute service HEAD");
assertBanner(!unresolved.includes("graph snapshot behind HEAD"), "do not claim stale comparison");

const stale = render({
  ...base,
  current_state: { graph_stale: {
    is_stale: true, active_graph_commit: snapshotCommit,
    head_commit: serviceCommit,
    comparison_status: "verified_linked_main_owner",
  } },
});
assertBanner(stale.includes("graph snapshot behind HEAD"), "verified stale remains visible");
assertBanner(!stale.includes("disabled=\"\""), "verified stale retains Update");

const legacy = render(base);
assertBanner(legacy.includes("graph snapshot behind HEAD"), "legacy service fallback remains");
