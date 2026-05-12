export interface Task {
  id: string;
  title: string;
  status: "queued" | "running" | "done";
}

export async function fetchTasks(): Promise<Task[]> {
  return [
    { id: "demo-1", title: "Check graph snapshot", status: "queued" },
    { id: "demo-2", title: "Review semantic proposal", status: "running" },
  ];
}

export function summarizeTasks(tasks: Task[]): string {
  const open = tasks.filter((task) => task.status !== "done").length;
  return `${open}/${tasks.length} open`;
}
