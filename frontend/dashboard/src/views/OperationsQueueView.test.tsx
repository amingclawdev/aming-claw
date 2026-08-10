import { renderToStaticMarkup } from "react-dom/server";

import type { OperationRow, OperationsQueueResponse } from "../types";
import OperationsQueueView from "./OperationsQueueView";

function operation(operationId: string, status: string): OperationRow {
  return {
    operation_id: operationId,
    operation_type: "current_full_reconcile",
    target_scope: "snapshot",
    target_id: `${operationId}-target`,
    target_label: `${operationId}-label`,
    status,
    progress: { done: 0, total: 2 },
    created_at: "2026-08-10T00:00:00Z",
    updated_at: "2026-08-10T00:00:00Z",
    claimed_by: "",
    worker_id: "governance_current_full_reconcile",
    lease_expires_at: "",
    last_error: "",
    last_result: status,
    trace_id: "",
    supported_actions: ["view_trace"],
  };
}

const operations = [
  operation("current-full-finalizing", "finalizing"),
  operation("current-full-unknown", "unknown"),
];
const markup = renderToStaticMarkup(
  <OperationsQueueView
    ops={{
      ok: true,
      project_id: "aming-claw",
      snapshot_id: "full-test",
      active_snapshot_id: "full-test",
      count: operations.length,
      operations,
      summary: {
        by_type: { current_full_reconcile: operations.length },
        by_status: { finalizing: 1, unknown: 1 },
        pending_scope_reconcile_count: 0,
      },
    } satisfies OperationsQueueResponse}
  />,
);

function assertRecoveryView(condition: boolean, message: string): void {
  if (!condition) throw new Error(`Operations queue recovery fixture failed: ${message}`);
}

assertRecoveryView(
  markup.includes("Running / Recovery"),
  "the in-flight section must explicitly identify recovery rows",
);
assertRecoveryView(
  markup.includes("current-full-finalizing") && markup.includes(">finalizing</span>"),
  "finalizing rows must remain visibly rendered in the running/recovery area",
);
assertRecoveryView(
  markup.includes("current-full-unknown") && markup.includes(">unknown</span>"),
  "unknown rows must remain visibly rendered in the running/recovery area",
);
