import { readFileSync } from "node:fs";
import type { Route } from "playwright";
import type { BacklogHotWindowResponse } from "./lib/api";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const apiSource = readFileSync(new URL("./lib/api.ts", import.meta.url), "utf8");
const skipSourceAssertions = process.env.AMING_S1_SKIP_SOURCE_ASSERTS === "1";

function assertViewScopedBootstrap(condition: boolean, message: string): void {
  if (skipSourceAssertions) return;
  if (!condition) throw new Error(`View-scoped bootstrap fixture failed: ${message}`);
}

function assertMountedLifecycle(condition: boolean, message: string): void {
  if (!condition) throw new Error(`Mounted deep-link lifecycle failed: ${message}`);
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
    && appSource.includes('view === "backlog" ? (')
    && appSource.includes("bootstrapPending={!currentBacklog && loading}")
    && appSource.includes('view === "activity" && currentBacklog'),
  "Backlog exact-detail shell must mount before its project-scoped hot window while Activity still requires timeline input",
);
assertViewScopedBootstrap(
  apiSource.includes('getPublicJSONSingleFlight<HealthResponse>("/api/health", signal)')
    && apiSource.includes('getPublicJSONSingleFlight<ProjectsResponse>("/api/projects", signal)')
    && apiSource.includes("return awaitPublicRead(shared, signal)"),
  "shareable common reads must retain consumer-isolated abort semantics",
);

const matchingInitialPage: BacklogHotWindowResponse = {
  bugs: [],
  count: 0,
  generation: 42,
  authority_generation: "sha256:matching-authority",
  hot_limit: 250,
  limit: 250,
  view: "compact",
  scope: {
    project_id: "aming-claw",
    view: "compact",
    pagination: "hot_window",
  },
};
if (!skipSourceAssertions) {
  const { api, backlogInitialPageMatches } = await import("./lib/api");
  const generationlessInitialPage: BacklogHotWindowResponse = { ...matchingInitialPage, generation: undefined };
  const searchedInitialPage: BacklogHotWindowResponse = { ...matchingInitialPage, q: "history" };
  assertViewScopedBootstrap(
    backlogInitialPageMatches("aming-claw", matchingInitialPage),
    "a canonical same-project/query/generation recent page must satisfy first mount",
  );
  assertViewScopedBootstrap(
    !backlogInitialPageMatches("foreign-project", matchingInitialPage)
      && !backlogInitialPageMatches("aming-claw", generationlessInitialPage)
      && !backlogInitialPageMatches("aming-claw", searchedInitialPage)
      && !backlogInitialPageMatches("aming-claw", { bugs: [], count: 0 }),
    "foreign, generation-less, searched, and placeholder pages must not suppress the governed initial read",
  );

  const originalFetch = globalThis.fetch;
  const fetchCalls: string[] = [];
  globalThis.fetch = async (input) => {
    const url = new URL(String(input), "http://s1.fixture");
    fetchCalls.push(`${url.pathname}?${url.searchParams.toString()}`);
    const project = decodeURIComponent(url.pathname.split("/").pop() || "");
    return new Response(JSON.stringify({
      ...matchingInitialPage,
      generation: 43,
      authority_generation: "sha256:revalidated-authority",
      scope: { ...matchingInitialPage.scope, project_id: project },
    }), { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    const seed = {
      ...matchingInitialPage,
      scope: { ...matchingInitialPage.scope, project_id: "s1-generation" },
    };
    await api.backlogInitialPageFor("s1-generation", seed);
    assertViewScopedBootstrap(fetchCalls.length === 0, "a matching governed mount seed must not duplicate its initial read");
    await api.backlogInitialPageFor("s1-generation", { ...seed, generation: 41 });
    await api.backlogInitialPageFor("s1-query", {
      ...matchingInitialPage,
      q: "history",
      scope: { ...matchingInitialPage.scope, project_id: "s1-query" },
    });
    await api.backlogInitialPageFor("s1-project", matchingInitialPage);
    await api.backlogRevalidateFor("s1-refresh");
    assertViewScopedBootstrap(
      fetchCalls.length === 4
        && fetchCalls.some((path) => path.startsWith("/api/backlog/s1-generation?"))
        && fetchCalls.some((path) => path.startsWith("/api/backlog/s1-query?"))
        && fetchCalls.some((path) => path.startsWith("/api/backlog/s1-project?"))
        && fetchCalls.some((path) => path.startsWith("/api/backlog/s1-refresh?")),
      "generation mismatch, search seed, project mismatch, and explicit refresh must each perform a governed list read",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function runMountedDeepLinkLifecycle(): Promise<void> {
  const baseUrl = String(process.env.AMING_S1_BROWSER_BASE_URL || "").replace(/\/$/, "");
  const expectation = process.env.AMING_S1_BROWSER_EXPECTATION;
  if (!baseUrl || !expectation) return;
  const expectEarly = expectation === "fixed";
  const { chromium } = await import("playwright");
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.AMING_S1_BROWSER_EXECUTABLE || undefined,
  });
  const page = await browser.newPage();
  const started = Date.now();
  const observed = {
    healthStarted: 0,
    healthCompleted: 0,
    projectsStarted: 0,
    projectsCompleted: 0,
    listStarted: [] as Array<{ project: string; q: string; at: number }>,
    listCompleted: [] as Array<{ project: string; q: string; at: number }>,
    detailStarted: [] as Array<{ project: string; backlog: string; at: number }>,
    timelineStarted: [] as Array<{ project: string; backlog: string; at: number }>,
    apiRequests: [] as Array<{ path: string; search: string; at: number }>,
    pageErrors: [] as string[],
    consoleErrors: [] as string[],
  };
  let healthCalls = 0;
  const delayedMs = 1_200;
  const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  page.on("pageerror", (error) => observed.pageErrors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") observed.consoleErrors.push(message.text());
  });

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const at = Date.now() - started;
    observed.apiRequests.push({ path, search: url.search, at });
    if (path === "/api/health") {
      healthCalls += 1;
      const failCommonRead = expectEarly && healthCalls > 1;
      observed.healthStarted = at;
      await pause(failCommonRead ? 300 : delayedMs);
      observed.healthCompleted = Date.now() - started;
      await json(
        route,
        failCommonRead
          ? { error: "not_found", message: "late unrelated health fixture failure" }
          : { status: "ok", version: "s1-fixture" },
        failCommonRead ? 404 : 200,
      );
      return;
    }
    if (path === "/api/projects") {
      observed.projectsStarted = at;
      await pause(delayedMs);
      observed.projectsCompleted = Date.now() - started;
      await json(route, { projects: [{ project_id: "aming-claw", display_name: "aming-claw" }] });
      return;
    }
    const listMatch = path.match(/^\/api\/backlog\/([^/]+)$/);
    if (listMatch) {
      const project = decodeURIComponent(listMatch[1]);
      const q = url.searchParams.get("q") || "";
      observed.listStarted.push({ project, q, at });
      await pause(delayedMs);
      observed.listCompleted.push({ project, q, at: Date.now() - started });
      await json(route, {
        bugs: [], count: 0, total_count: 0, filtered_count: 0,
        generation: 42, authority_generation: "sha256:s1-fixture", hot_limit: 250, limit: 250,
        view: "compact", source: "fixture_hot_window",
        scope: { project_id: project, view: "compact", pagination: "hot_window", public_safe: true },
        read_cache: { hit: false, age_ms: 0, eviction_count: 0 },
      });
      return;
    }
    const detailMatch = path.match(/^\/api\/backlog\/([^/]+)\/([^/]+)$/);
    if (detailMatch) {
      const project = decodeURIComponent(detailMatch[1]);
      const backlog = decodeURIComponent(detailMatch[2]);
      observed.detailStarted.push({ project, backlog, at });
      if (backlog === "AC-S1-SLOW") await pause(700);
      if (project !== "aming-claw" && project !== "s1-pending") {
        await json(route, { error: "not_found", message: "fixture wrong project" }, 404);
        return;
      }
      await json(route, {
        bug_id: backlog,
        title: backlog === "AC-S1-SECOND" ? "Second scoped detail" : backlog === "AC-S1-SLOW" ? "Stale slow detail" : "Exact detail ready",
        status: "FIXED", priority: "P1", target_files: [], test_files: [], acceptance_criteria: [],
      });
      return;
    }
    const timelineMatch = path.match(/^\/api\/task\/([^/]+)\/timeline$/);
    if (timelineMatch) {
      const project = decodeURIComponent(timelineMatch[1]);
      const backlog = url.searchParams.get("backlog_id") || "";
      observed.timelineStarted.push({ project, backlog, at });
      await json(route, {
        project_id: project, backlog_id: backlog, events: [], count: 0,
        backlog_timeline_gate: { project_id: project, backlog_id: backlog, events: [], event_count: 0, applicable: true },
      });
      return;
    }
    if (path.endsWith("/events/stream")) {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: ready\ndata: {}\n\n" });
      return;
    }
    await json(route, {});
  });

  try {
    await page.goto(`${baseUrl}/dashboard/?project_id=aming-claw&view=backlog&backlog=AC-S1-TARGET`, { waitUntil: "domcontentloaded" });
    await pause(500);
    if (expectEarly) {
      assertMountedLifecycle(observed.detailStarted.some((item) => item.backlog === "AC-S1-TARGET"), "fixed build must start exact detail while unrelated reads are pending");
      assertMountedLifecycle(observed.timelineStarted.some((item) => item.backlog === "AC-S1-TARGET"), "fixed build must start timeline while unrelated reads are pending");
      assertMountedLifecycle(observed.healthCompleted === 0 && observed.listCompleted.length === 0, "early exact reads must precede delayed health/projects/list completion");
      await page.getByRole("heading", { name: "Exact detail ready" }).waitFor({ state: "visible" });
    } else {
      assertMountedLifecycle(observed.detailStarted.length === 0 && observed.timelineStarted.length === 0, "original build control must retain the pre-fix bootstrap barrier");
    }
    await page.getByText("Exact detail ready").first().waitFor({ state: "visible", timeout: 5_000 });
    await pause(1_350);
    assertMountedLifecycle(
      observed.listStarted.length === (expectEarly ? 1 : 2),
      `${expectation} build recent-page request count must be ${expectEarly ? 1 : 2}, got ${observed.listStarted.length}`,
    );

    if (expectEarly) {
      await page.goto(`${baseUrl}/dashboard/?project_id=s1-pending&view=backlog&backlog=AC-S1-TARGET`, { waitUntil: "domcontentloaded" });
      await page.getByRole("heading", { name: "Exact detail ready" }).waitFor({ state: "visible" });
      await pause(450);
      const pendingMain = await page.locator("main").innerText();
      assertMountedLifecycle(
        pendingMain.includes("Loading the recent Backlog page. Exact selected detail remains independent.")
          && pendingMain.includes("Dashboard bootstrap read failed:")
          && !pendingMain.includes("No backlog rows match the current filters."),
        "a completed common-read failure must leave the independently pending list honest while exact detail remains visible",
      );
      await pause(900);

      await page.getByRole("button", { name: "Close backlog detail" }).click();
      assertMountedLifecycle(!new URL(page.url()).searchParams.has("backlog"), "closing exact detail must clear only its URL selection");
      const listCountBeforeRefresh = observed.listStarted.length;
      await page.getByRole("button", { name: /Refresh/ }).click();
      await page.goBack();
      await page.getByRole("heading", { name: "Exact detail ready" }).waitFor({ state: "visible" });
      await pause(1_350);
      assertMountedLifecycle(
        observed.listStarted.length === listCountBeforeRefresh + 1
          && new URL(page.url()).searchParams.get("view") === "backlog"
          && new URL(page.url()).searchParams.get("backlog") === "AC-S1-TARGET"
          && await page.getByRole("heading", { name: "Exact detail ready" }).isVisible(),
        "explicit refresh must re-read once and a late unrelated common-read 404 must preserve the restored Backlog detail",
      );

      await page.getByRole("button", { name: "Close backlog detail" }).click();
      await page.getByPlaceholder("Search backlog, files, criteria...").fill("history");
      await pause(350);
      assertMountedLifecycle(
        observed.listStarted.some((item) => item.project === "s1-pending" && item.q === "history"),
        "a search query must issue a distinct governed list read",
      );
      await page.getByPlaceholder("Search backlog, files, criteria...").fill("");

      await page.goto(`${baseUrl}/dashboard/?project_id=foreign-project&view=backlog&backlog=AC-S1-TARGET`, { waitUntil: "domcontentloaded" });
      await page.getByText(/Backlog detail load failed:/).waitFor({ state: "visible", timeout: 2_000 });
      await pause(1_350);
      assertMountedLifecycle(!(await page.getByText("Exact detail ready").count()), "wrong-project failure must not retain prior detail");
      assertMountedLifecycle(
        observed.listStarted.some((item) => item.project === "foreign-project")
          && new URL(page.url()).searchParams.get("view") === "backlog",
        "project change must re-read its own list while a late common failure preserves the selected view",
      );

      await page.goto(`${baseUrl}/dashboard/?project_id=aming-claw&view=backlog&backlog=AC-S1-SLOW`, { waitUntil: "domcontentloaded" });
      await pause(100);
      await page.evaluate(() => {
        const next = new URL(window.location.href);
        next.searchParams.set("backlog", "AC-S1-SECOND");
        window.history.pushState({}, "", next);
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      await page.getByRole("heading", { name: "Second scoped detail" }).waitFor({ state: "visible", timeout: 2_000 });
      await pause(800);
      assertMountedLifecycle(await page.getByRole("heading", { name: "Second scoped detail" }).isVisible(), "late prior-row response must not replace rapid navigation target");
      assertMountedLifecycle(!(await page.getByRole("heading", { name: "Stale slow detail" }).count()), "late prior-row title must remain hidden");
    }
    process.stdout.write(`${JSON.stringify({ expectation, observed })}\n`);
  } catch (error) {
    process.stderr.write(`${JSON.stringify({ expectation, observed, pageUrl: page.url() }, null, 2)}\n`);
    throw error;
  } finally {
    await browser.close();
  }
}

await runMountedDeepLinkLifecycle();
