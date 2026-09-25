import { renderToStaticMarkup } from "react-dom/server";

import type { ProjectListItem } from "../lib/api";
import type { StatusResponse } from "../types";
import {
  ProjectRow,
  graphKpiCountsFor,
  lifecycleFor,
  targetCommitFor,
  type ProjectRuntime,
} from "./ProjectConsoleView";

const snapshot = "a".repeat(40);
const head = "b".repeat(40);
const project: ProjectListItem = {
  project_id: "aming-claw", name: "Aming Claw", active_snapshot_id: "full-active",
};

function status(
  isStale: boolean,
  comparisonStatus?: "unresolved" | "verified_linked_main_owner",
  pending = 0,
): StatusResponse {
  return {
    ok: true, project_id: project.project_id,
    active_snapshot_id: "full-active", graph_snapshot_commit: snapshot,
    materialized_graph_baseline_commit: snapshot,
    scan_baseline_commit: snapshot, scan_baseline_id: 1,
    pending_scope_reconcile_count: pending,
    pending_scope_reconcile: pending ? [{ commit_sha: head }] : [],
    current_state: { graph_stale: {
      is_stale: isStale, active_graph_commit: snapshot,
      head_commit: comparisonStatus === "unresolved" ? "" : head,
      comparison_status: comparisonStatus,
      changed_files: [], changed_file_count: 0,
    } },
  };
}

function runtime(value: StatusResponse): ProjectRuntime {
  return { projectId: project.project_id, status: value, errors: {} };
}

function rowMarkup(value: ProjectRuntime): string {
  const noop = () => {};
  return renderToStaticMarkup(
    <table><tbody><ProjectRow
      project={project} runtime={value} selected={true}
      lifecycle={lifecycleFor(project, value)}
      onOpenProject={noop} onOpenAiConfig={noop}
      onBuildGraph={noop} onUpdateGraph={noop} onSelectRef={noop}
    /></tbody></table>,
  );
}

function assertConsole(condition: boolean, message: string): void {
  if (!condition) throw new Error(`Project Console graph-status fixture failed: ${message}`);
}

const unresolvedCurrent = runtime(status(false, "unresolved"));
const unresolvedContradictory = runtime(status(true, "unresolved", 1));
assertConsole(
  graphKpiCountsFor([unresolvedCurrent, unresolvedContradictory]).current === 0
    && graphKpiCountsFor([unresolvedCurrent, unresolvedContradictory]).stale === 0,
  "unresolved graph is neither current nor stale even if is_stale disagrees",
);
for (const value of [unresolvedCurrent, unresolvedContradictory]) {
  const lifecycle = lifecycleFor(project, value);
  const markup = rowMarkup(value);
  assertConsole(lifecycle.kind === "graph_unverified" && !lifecycle.action,
    "unresolved owner has a non-actionable lifecycle before pending or stale");
  assertConsole(targetCommitFor(value) === "",
    "unresolved owner has no pending or snapshot fallback target");
  assertConsole(markup.includes("unverified") && !markup.includes(">Update graph</button>"),
    "rendered unresolved row has no Update graph control");
}

const verifiedCurrent = runtime(status(false, "verified_linked_main_owner"));
const verifiedStale = runtime(status(true, "verified_linked_main_owner"));
assertConsole(graphKpiCountsFor([verifiedCurrent, verifiedStale]).current === 1
  && graphKpiCountsFor([verifiedCurrent, verifiedStale]).stale === 1,
  "verified linked current and stale count normally");
assertConsole(lifecycleFor(project, verifiedCurrent).kind === "ready",
  "verified linked equal-HEAD remains ready");
assertConsole(lifecycleFor(project, verifiedStale).kind === "graph_stale"
  && targetCommitFor(verifiedStale) === head
  && rowMarkup(verifiedStale).includes(">Update graph</button>"),
  "verified linked stale allows Update even with an empty changed-file list");

const legacy = runtime(status(false));
const legacyStale = runtime(status(true));
const legacyPending = runtime(status(false, undefined, 1));
assertConsole(graphKpiCountsFor([legacy]).current === 1
  && lifecycleFor(project, legacy).kind === "ready",
  "legacy current state remains ready");
assertConsole(graphKpiCountsFor([legacyStale]).stale === 1
  && lifecycleFor(project, legacyStale).kind === "graph_stale"
  && rowMarkup(legacyStale).includes(">Update graph</button>"),
  "legacy stale state remains actionable");
assertConsole(lifecycleFor(project, legacyPending).kind === "reconcile_pending"
  && targetCommitFor(legacyPending) === head,
  "legacy pending fallback and Update remain available");
const legacyMissing: ProjectRuntime = {
  projectId: project.project_id,
  errors: { status: { message: "HTTP 404", status: 404 } },
};
assertConsole(lifecycleFor({ ...project, active_snapshot_id: undefined }, legacyMissing).kind
  === "graph_missing", "legacy missing graph remains missing");
