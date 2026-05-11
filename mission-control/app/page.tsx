import { SummaryCards } from "../components/SummaryCards";
import { TaskTable } from "../components/TaskTable";
import { API_BASE_URL, getTasks } from "../lib/api";
import type { Task } from "../lib/types";

export const dynamic = "force-dynamic";

const ACTIVE_STATUSES = new Set([
  "queued",
  "preparing",
  "implementing",
  "validating"
]);

function byUpdatedDesc(a: Task, b: Task): number {
  return String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? ""));
}

export default async function DashboardPage() {
  const result = await getTasks();

  if (!result.ok) {
    return (
      <main>
        <header className="header">
          <h1>Mission Control</h1>
          <p>Read-only dashboard for Agent Taskflow tasks.</p>
        </header>

        <div className="error">
          {result.error.message}
          <br />
          API base URL: <span className="mono">{API_BASE_URL}</span>
        </div>
      </main>
    );
  }

  const tasks = [...result.data].sort(byUpdatedDesc);
  const activeTasks = tasks.filter((task) => ACTIVE_STATUSES.has(task.status));
  const waitingApprovalTasks = tasks.filter(
    (task) => task.status === "waiting_approval"
  );
  const blockedTasks = tasks.filter((task) => task.status === "blocked");
  const recentTasks = tasks.slice(0, 12);

  return (
    <main>
      <header className="header">
        <h1>Mission Control</h1>
        <p>
          Read-only dashboard for Agent Taskflow. This phase only displays task
          metadata, runs, artifacts, validations, and approvals.
        </p>
        <p className="muted">
          API base URL: <span className="mono">{API_BASE_URL}</span>
        </p>
      </header>

      <SummaryCards tasks={tasks} />

      <section className="section panel">
        <h2>Active Tasks</h2>
        <TaskTable
          tasks={activeTasks}
          emptyMessage="No active tasks currently recorded."
        />
      </section>

      <section className="section panel">
        <h2>Waiting Approval</h2>
        <TaskTable
          tasks={waitingApprovalTasks}
          emptyMessage="No tasks are waiting for approval."
        />
      </section>

      <section className="section panel">
        <h2>Blocked Tasks</h2>
        <TaskTable
          tasks={blockedTasks}
          emptyMessage="No blocked tasks currently recorded."
        />
      </section>

      <section className="section panel">
        <h2>Recent Tasks</h2>
        <TaskTable tasks={recentTasks} emptyMessage="No tasks recorded yet." />
      </section>
    </main>
  );
}
