import { TaskBoard } from "../components/TaskBoard";
import { API_BASE_URL, getTasks } from "../lib/api";
import type { Task } from "../lib/types";

export const dynamic = "force-dynamic";

function byUpdatedDesc(a: Task, b: Task): number {
  return String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? ""));
}

export default async function DashboardPage() {
  const result = await getTasks();

  if (!result.ok) {
    return (
      <main className="error-page">
        <section className="error-panel">
          <div className="error-eyebrow">Mission Control</div>
          <h1>Agent Taskflow API unavailable</h1>
          <p>{result.error.message}</p>
          <p>
            API base URL: <span className="mono">{API_BASE_URL}</span>
          </p>
        </section>
      </main>
    );
  }

  const tasks = [...result.data].sort(byUpdatedDesc);

  return <TaskBoard tasks={tasks} />;
}
