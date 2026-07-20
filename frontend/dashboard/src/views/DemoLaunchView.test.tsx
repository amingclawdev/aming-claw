import type { DemoEnvironment, DemoEnvironmentsResponse } from "../lib/api";
import {
  DAILY_PLANNER_TEMPLATE_ID,
  dailyPlannerTemplateFrom,
  demoEnvironmentLinks,
  demoEnvironmentStatus,
  demoLaunchPrompts,
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
  launch_prompt: [
    "Run the Aming Claw Daily Planner Lite visual happy-path demo from start to finish.",
    "",
    "Intent:",
    "Implement one concrete user-facing requirement in this fixture, not just the setup flow:",
    "Today Focus and reminder visual planner board.",
    "",
    "Parallel implementation shape:",
    "Create exactly one backlog row for that requirement, then use bounded mf_sub worker lanes where safe:",
    "- Focus/UI lane: src/app.js, index.html, styles.css, tests/planner.test.mjs",
    "- Reminder/domain lane: src/reminders.js, tests/reminders.test.mjs",
  ].join("\n"),
  launch_prompts: [
    {
      id: "direct_main",
      label: "Direct Main",
      description: "Observer-supervised direct_main path.",
      prompt: [
        "Run the Aming Claw Daily Planner Lite Direct Main happy-path demo from start to finish.",
        "Use operator_supervised_direct_main for a tiny deterministic implementation.",
        "Start through onboard_route_guide and run rg then graph_query.",
        "Copy-safe prompt rule:",
        "Never paste or persist raw session, fence, or route tokens.",
      ].join("\n"),
    },
    {
      id: "mf_parallel",
      label: "MF Parallel",
      description: "Single-backlog mf_parallel path.",
      prompt: [
        "Run the Aming Claw Daily Planner Lite MF Parallel happy-path demo from start to finish.",
        "Use mf_parallel for exactly one backlog row.",
        "Parallel implementation shape:",
        "Focus/UI lane",
        "Reminder/domain lane",
        "The observer must use the system CLI agent service or a host-created bounded worker/subagent.",
        "Do not act as the worker from the observer session.",
      ].join("\n"),
    },
    {
      id: "mf_batch_parallel",
      label: "MF Batch Parallel",
      description: "Two-backlog mf_batch_parallel path.",
      prompt: [
        "Run the Aming Claw Daily Planner Lite MF Batch Parallel happy-path demo from start to finish.",
        "Use mf_batch_parallel for two compatible backlog rows.",
        "Row A: Today Focus",
        "Row B: reminder toggle",
        "Independent QA must run from a distinct verifier lane/session where possible.",
        "QA runs its own rg + graph_query with query_source=qa and query_purpose=independent_verification.",
      ].join("\n"),
    },
  ],
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
  const prompts = demoLaunchPrompts(created);
  const mixedIdPrompts = demoLaunchPrompts({
    ...created,
    launch_prompts: [
      { id: "mf-batch-parallel", label: "Batch", prompt: " Batch prompt\nwith exact spacing " },
      { id: "direct_main", label: "Direct", prompt: "Direct prompt" },
      { id: "mf-parallel", label: "Parallel", prompt: "Parallel prompt" },
    ],
  });
  const legacyPrompts = demoLaunchPrompts({
    ...created,
    launch_prompt: "Legacy launch prompt",
    launch_prompts: [],
  });
  const status = demoEnvironmentStatus(created);
  const shortBaseline = shortCommit(created.baseline_commit);

  if (template.id !== DAILY_PLANNER_TEMPLATE_ID) throw new Error("daily planner template was not selected");
  if (links.length !== 5) throw new Error("all operational demo links should be present");
  if (status.label !== "Ready") throw new Error("ready environment should render with ready status");
  if (shortBaseline !== "881326a51cb4") throw new Error("baseline commit should be shortened for compact panels");
  if (!created.launch_prompt.includes("Daily Planner Lite")) throw new Error("launch prompt must be surfaced");
  if (!created.launch_prompt.includes("Intent:")) throw new Error("launch prompt must include explicit intent");
  if (!created.launch_prompt.includes("Today Focus and reminder visual planner board")) throw new Error("launch prompt must name the demo requirement");
  if (!created.launch_prompt.includes("Parallel implementation shape:")) throw new Error("launch prompt must require parallel implementation shape");
  if (!created.launch_prompt.includes("Focus/UI lane")) throw new Error("launch prompt must name the Focus/UI lane");
  if (!created.launch_prompt.includes("Reminder/domain lane")) throw new Error("launch prompt must name the Reminder/domain lane");
  if (prompts.length !== 3) throw new Error("daily planner demo should surface three launch prompts");
  if (prompts.map((prompt) => prompt.label).join("|") !== "Direct Main|MF Parallel|MF Batch Parallel") throw new Error("three launch prompts must use the canonical panel order");
  if (!prompts.some((prompt) => prompt.id === "direct_main" && prompt.prompt.includes("operator_supervised_direct_main"))) throw new Error("direct_main prompt must be available");
  if (!prompts.some((prompt) => prompt.id === "mf_parallel" && prompt.prompt.includes("mf_parallel"))) throw new Error("mf_parallel prompt must be available");
  if (!prompts.some((prompt) => prompt.id === "mf_batch_parallel" && prompt.prompt.includes("mf_batch_parallel"))) throw new Error("mf_batch_parallel prompt must be available");
  if (!prompts.some((prompt) => prompt.prompt.includes("Copy-safe prompt rule:"))) throw new Error("copy-safe prompt rule must be surfaced");
  if (!prompts.some((prompt) => prompt.prompt.includes("system CLI agent service or a host-created bounded worker/subagent"))) throw new Error("worker prompt must force a host-created worker lane");
  if (!prompts.some((prompt) => prompt.prompt.includes("Do not act as the worker from the observer session"))) throw new Error("observer must not impersonate worker");
  if (!prompts.some((prompt) => prompt.prompt.includes("query_source=qa") && prompt.prompt.includes("query_purpose=independent_verification"))) throw new Error("QA graph query identity must be explicit");
  if (mixedIdPrompts.map((prompt) => prompt.label).join("|") !== "Direct Main|MF Parallel|MF Batch Parallel") throw new Error("underscore and hyphen ids must resolve to stable labels and order");
  if (mixedIdPrompts[2]?.prompt !== " Batch prompt\nwith exact spacing ") throw new Error("panel copy text must preserve the exact prompt value");
  if (legacyPrompts.length !== 1 || legacyPrompts[0]?.id !== "legacy" || legacyPrompts[0]?.label !== "Launch prompt") throw new Error("legacy launch_prompt fallback must be preserved");
  if (legacyPrompts[0]?.prompt !== "Legacy launch prompt") throw new Error("legacy launch_prompt text must remain readable");

  return links.map((link) => link.label);
}

export const demoLaunchFixtureLabels = assertDemoLaunchFixtureCoverage();
