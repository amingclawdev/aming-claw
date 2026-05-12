import { fetchTasks, summarizeTasks, type Task } from "./api";

export async function loadDashboardSummary(): Promise<string> {
  const tasks: Task[] = await fetchTasks();
  return summarizeTasks(tasks);
}

export function renderDashboardTitle(projectName: string): string {
  return `${projectName} dashboard`;
}
