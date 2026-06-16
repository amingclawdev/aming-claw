import type { DemoEnvironment, DemoEnvironmentsResponse } from "../lib/api";
import {
  DAILY_PLANNER_TEMPLATE_ID,
  dailyPlannerTemplateFrom,
  demoEnvironmentLinks,
  demoEnvironmentStatus,
  environmentFromCreateResponse,
  shortCommit,
} from "./DemoLaunchView";

export const demoLaunchFixtureEnvironment: DemoEnvironment = {
  id: "daily-planner-lite-vibe-visual-happy-20260616-043027",
  template_id: DAILY_PLANNER_TEMPLATE_ID,
  label: "Daily Planner Lite",
  project_id: "daily-planner-lite-vibe-visual-happy-20260616-043027",
  fixture_root: "/var/folders/ft/q0j_8c5167n0294b0mmkml200000gn/T/ac-vibe-queue-demo/visual-happy-20260616-043027",
  baseline_commit: "881326a51cb48363afcb6acb1d055b6183dceffb",
  created_at: "2026-06-16T04:30:27Z",
  dashboard_url: "http://127.0.0.1:40000/dashboard?project_id=daily-planner-lite-vibe-visual-happy-20260616-043027",
  backlog_url: "http://127.0.0.1:40000/dashboard?project_id=daily-planner-lite-vibe-visual-happy-20260616-043027&view=backlog",
  timeline_url: "http://127.0.0.1:40000/dashboard?project_id=daily-planner-lite-vibe-visual-happy-20260616-043027&view=activity",
  graph_url: "http://127.0.0.1:40000/dashboard?project_id=daily-planner-lite-vibe-visual-happy-20260616-043027&view=graph",
  planner_preview_url: "http://127.0.0.1:4174/",
  planner_preview_command: "npm run preview -- --host 127.0.0.1 --port 4174",
  launch_prompt: "Run the Aming Claw Daily Planner Lite visual happy-path demo from start to finish.",
  status: "ready",
};

export const demoLaunchFixtureResponse: DemoEnvironmentsResponse = {
  ok: true,
  project_id: "aming-claw",
  templates: [
    {
      id: DAILY_PLANNER_TEMPLATE_ID,
      template_id: DAILY_PLANNER_TEMPLATE_ID,
      label: "Daily Planner Lite",
      description: "Managed visual fixture for the daily planner happy path.",
    },
  ],
  environments: [demoLaunchFixtureEnvironment],
};

export function assertDemoLaunchFixtureCoverage(): string[] {
  const template = dailyPlannerTemplateFrom(demoLaunchFixtureResponse.templates);
  const created = environmentFromCreateResponse(demoLaunchFixtureEnvironment);
  const links = demoEnvironmentLinks(created);
  const status = demoEnvironmentStatus(created);
  const shortBaseline = shortCommit(created.baseline_commit);

  if (template.id !== DAILY_PLANNER_TEMPLATE_ID) throw new Error("daily planner template was not selected");
  if (links.length !== 5) throw new Error("all operational demo links should be present");
  if (status.label !== "Ready") throw new Error("ready environment should render with ready status");
  if (shortBaseline !== "881326a51cb") throw new Error("baseline commit should be shortened for compact panels");
  if (!created.launch_prompt.includes("Daily Planner Lite")) throw new Error("launch prompt must be surfaced");

  return links.map((link) => link.label);
}

export const demoLaunchFixtureLabels = assertDemoLaunchFixtureCoverage();
