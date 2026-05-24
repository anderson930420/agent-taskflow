import { TaskBoard } from "../components/TaskBoard";
import {
  API_BASE_URL,
  getSchedulerCandidates,
  getTasks
} from "../lib/api";
import type { Task } from "../lib/types";

export const dynamic = "force-dynamic";

function byUpdatedDesc(a: Task, b: Task): number {
  return String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? ""));
}

export default async function DashboardPage() {
  const [tasksResult, candidatesResult] = await Promise.all([
    getTasks(),
    getSchedulerCandidates({ include_not_ready: true, include_no_action: true })
  ]);

  if (!tasksResult.ok) {
    return (
      <main className="error-page">
        <section className="error-panel">
          <div className="error-eyebrow">Mission Control</div>
          <h1>Agent Taskflow API unavailable</h1>
          <p>{tasksResult.error.message}</p>
          <p>
            API base URL: <span className="mono">{API_BASE_URL}</span>
          </p>
        </section>
      </main>
    );
  }

  const tasks = [...tasksResult.data].sort(byUpdatedDesc);
  const schedulerCandidates = candidatesResult.ok
    ? candidatesResult.data
    : null;
  const schedulerCandidatesError = candidatesResult.ok
    ? null
    : candidatesResult.error.message;

  return (
    <TaskBoard
      tasks={tasks}
      schedulerCandidates={schedulerCandidates}
      schedulerCandidatesError={schedulerCandidatesError}
    />
  );
}
