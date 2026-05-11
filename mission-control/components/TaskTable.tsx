import Link from "next/link";
import type { Task } from "../lib/types";
import { StatusBadge } from "./StatusBadge";

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  return String(value);
}

export function TaskTable({
  tasks,
  emptyMessage = "No tasks found."
}: {
  tasks: Task[];
  emptyMessage?: string;
}) {
  if (tasks.length === 0) {
    return <div className="empty">{emptyMessage}</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Task key</th>
            <th>Project</th>
            <th>Status</th>
            <th>Executor</th>
            <th>Model</th>
            <th>Validator</th>
            <th>Updated</th>
            <th>Blocked reason</th>
            <th>PR</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.task_key}>
              <td className="mono">
                <Link href={`/tasks/${encodeURIComponent(task.task_key)}`}>
                  {task.task_key}
                </Link>
              </td>
              <td>{task.project}</td>
              <td>
                <StatusBadge status={task.status} />
              </td>
              <td>{valueOrDash(task.executor)}</td>
              <td className="mono">{valueOrDash(task.model)}</td>
              <td>{valueOrDash(task.validator)}</td>
              <td className="mono">{valueOrDash(task.updated_at)}</td>
              <td>{valueOrDash(task.blocked_reason)}</td>
              <td>
                {task.pr_url ? (
                  <a href={task.pr_url} rel="noreferrer" target="_blank">
                    PR {task.pr_number ?? ""}
                  </a>
                ) : (
                  "—"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
