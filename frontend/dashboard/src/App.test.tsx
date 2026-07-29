import { readFileSync } from "node:fs";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("./lib/api.ts", import.meta.url), "utf8");

function assertViewScopedBootstrap(condition: boolean, message: string): void {
  if (!condition) throw new Error(`View-scoped bootstrap fixture failed: ${message}`);
}

const hotWindowBranch = appSource.indexOf(
  'if (requestView === "backlog" || requestView === "activity")',
);
const snapshotBootstrap = appSource.indexOf("api.statusFor(requestProjectId, signal)");

assertViewScopedBootstrap(
  hotWindowBranch >= 0 && snapshotBootstrap > hotWindowBranch,
  "Backlog and Activity must branch to their bounded hot-window load before snapshot bootstrap",
);
assertViewScopedBootstrap(
  appSource.includes("const plan = dashboardBootstrapPlan(requestView)")
    && appSource.includes("plan.nodes")
    && appSource.includes("plan.edges")
    && appSource.includes("plan.feedback")
    && appSource.includes("plan.assetInbox")
    && appSource.includes("plan.assetImpactReminders"),
  "heavy graph, review, and asset reads must be selected by the active view plan",
);
assertViewScopedBootstrap(
  appSource.includes("fetchEpochRef.current === requestEpoch")
    && appSource.includes("currentProjectIdRef.current === requestProjectId")
    && appSource.includes("dataScope?.projectId === currentProjectId && dataScope.view === view"),
  "late responses must be fenced by request epoch, project, and view scope",
);
assertViewScopedBootstrap(
  appSource.includes("backlogProjectId === currentProjectId ? backlogData : null")
    && appSource.includes('view === "backlog" && currentBacklog')
    && appSource.includes('view === "activity" && currentBacklog'),
  "Backlog and Activity must render from project-scoped hot-window state without a DataBundle gate",
);
assertViewScopedBootstrap(
  apiSource.includes('getPublicJSONSingleFlight<HealthResponse>("/api/health", signal)')
    && apiSource.includes('getPublicJSONSingleFlight<ProjectsResponse>("/api/projects", signal)')
    && apiSource.includes("return awaitPublicRead(shared, signal)"),
  "shareable common reads must retain consumer-isolated abort semantics",
);
