import type { Task } from "../lib/types";

const ACTIVE_STATUSES = new Set([
  "queued",
  "preparing",
  "implementing",
  "validating"
]);

export function SummaryCards({ tasks }: { tasks: Task[] }) {
  const total = tasks.length;
  const active = tasks.filter((task) => ACTIVE_STATUSES.has(task.status)).length;
  const blocked = tasks.filter((task) => task.status === "blocked").length;
  const waitingApproval = tasks.filter(
    (task) => task.status === "waiting_approval"
  ).length;
  const accepted = tasks.filter((task) => task.status === "accepted").length;

  const cards = [
    ["Total tasks", total],
    ["Active tasks", active],
    ["Blocked tasks", blocked],
    ["Waiting approval", waitingApproval],
    ["Accepted tasks", accepted]
  ] as const;

  return (
    <div className="grid summary-grid">
      {cards.map(([label, value]) => (
        <div className="card" key={label}>
          <div className="card-label">{label}</div>
          <div className="card-value">{value}</div>
        </div>
      ))}
    </div>
  );
}
