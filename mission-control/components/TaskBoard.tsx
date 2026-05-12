import Link from "next/link";
import type { Task } from "../lib/types";

type BoardColumn = {
  key: string;
  title: string;
  statuses: string[];
  accent: string;
  emptyText: string;
};

const COLUMNS: BoardColumn[] = [
  {
    key: "backlog",
    title: "Backlog",
    statuses: ["queued"],
    accent: "neutral",
    emptyText: "No queued tasks."
  },
  {
    key: "todo",
    title: "Todo",
    statuses: ["preparing"],
    accent: "slate",
    emptyText: "No tasks ready to prepare."
  },
  {
    key: "in-progress",
    title: "In Progress",
    statuses: ["implementing", "validating"],
    accent: "yellow",
    emptyText: "No tasks currently running."
  },
  {
    key: "in-review",
    title: "In Review",
    statuses: ["waiting_approval", "waiting_for_review"],
    accent: "green",
    emptyText: "No tasks waiting for review."
  }
];

const TERMINAL_STATUSES = new Set([
  "accepted",
  "completed",
  "cleaned",
  "rejected",
  "canceled"
]);

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  return String(value);
}

function relativeDate(value?: string | null): string {
  if (!value) return "No update time";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;

  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  }).format(parsed);
}

function taskSubtitle(task: Task): string {
  const pieces = [
    task.executor ? `executor: ${task.executor}` : null,
    task.model ? `model: ${task.model}` : null,
    task.provider ? `provider: ${task.provider}` : null
  ].filter(Boolean);

  return pieces.length > 0 ? pieces.join(" · ") : "No executor metadata";
}

function tasksForColumn(tasks: Task[], column: BoardColumn): Task[] {
  return tasks.filter((task) => column.statuses.includes(String(task.status)));
}

function ungroupedTasks(tasks: Task[]): Task[] {
  const groupedStatuses = new Set(COLUMNS.flatMap((column) => column.statuses));
  return tasks.filter(
    (task) =>
      !groupedStatuses.has(String(task.status)) &&
      !TERMINAL_STATUSES.has(String(task.status))
  );
}

export function TaskBoard({ tasks }: { tasks: Task[] }) {
  const activeOrVisibleTasks = tasks.filter(
    (task) => !TERMINAL_STATUSES.has(String(task.status))
  );
  const acceptedCount = tasks.filter((task) => task.status === "accepted").length;
  const completedCount = tasks.filter(
    (task) => task.status === "completed" || task.status === "cleaned"
  ).length;
  const blockedCount = tasks.filter((task) => task.status === "blocked").length;
  const waitingCount = tasks.filter(
    (task) => task.status === "waiting_approval" || task.status === "waiting_for_review"
  ).length;
  const otherTasks = ungroupedTasks(tasks);

  return (
    <div className="linear-shell">
      <aside className="linear-sidebar" aria-label="Mission Control navigation">
        <div className="workspace-switcher">
          <div className="workspace-icon">MC</div>
          <div>
            <div className="workspace-name">Mission Control</div>
            <div className="workspace-subtitle">Agent Taskflow</div>
          </div>
        </div>

        <nav className="sidebar-nav">
          <a className="sidebar-item active" href="#board">
            <span className="sidebar-icon">◫</span>
            Board
          </a>
          <a className="sidebar-item" href="#review">
            <span className="sidebar-icon">◎</span>
            Review Queue
            <span className="sidebar-count">{waitingCount}</span>
          </a>
          <a className="sidebar-item" href="#blocked">
            <span className="sidebar-icon">!</span>
            Blocked
            <span className="sidebar-count">{blockedCount}</span>
          </a>
        </nav>

        <div className="sidebar-section">
          <div className="sidebar-heading">Views</div>
          <div className="sidebar-item muted-item">
            <span className="sidebar-dot purple" />
            Executor Metadata
          </div>
          <div className="sidebar-item muted-item">
            <span className="sidebar-dot blue" />
            Artifacts
          </div>
          <div className="sidebar-item muted-item">
            <span className="sidebar-dot green" />
            Validation
          </div>
        </div>

        <div className="sidebar-footer">
          <div className="sidebar-heading">Summary</div>
          <div className="mini-stat">
            <span>Visible tasks</span>
            <strong>{activeOrVisibleTasks.length}</strong>
          </div>
          <div className="mini-stat">
            <span>Accepted</span>
            <strong>{acceptedCount}</strong>
          </div>
          <div className="mini-stat">
            <span>Completed</span>
            <strong>{completedCount}</strong>
          </div>
        </div>
      </aside>

      <section className="linear-main">
        <header className="linear-topbar">
          <div>
            <div className="breadcrumb">Agent Taskflow / Mission Control</div>
            <h1>Executor Mission Board</h1>
          </div>
          <div className="topbar-actions">
            <div className="readonly-pill">Read-only</div>
            <Link className="ghost-button" href="/tasks/new">
              Create Task
            </Link>
          </div>
        </header>

        <div className="board-tabs">
          <span className="board-tab active">Issues</span>
          <span className="board-tab">Runs</span>
          <span className="board-tab">Artifacts</span>
          <span className="board-tab">Approvals</span>
        </div>

        <div className="board" id="board">
          {COLUMNS.map((column) => {
            const columnTasks = tasksForColumn(tasks, column);

            return (
              <section className="board-column" key={column.key}>
                <header className="column-header">
                  <div className="column-title-wrap">
                    <span className={`column-status-dot ${column.accent}`} />
                    <h2>{column.title}</h2>
                    <span className="column-count">{columnTasks.length}</span>
                  </div>
                  <span className="column-menu">•••</span>
                </header>

                <div className="column-add-placeholder">+</div>

                <div className="task-card-list">
                  {columnTasks.length === 0 ? (
                    <div className="empty-column">{column.emptyText}</div>
                  ) : (
                    columnTasks.map((task) => (
                      <Link
                        className="task-card"
                        href={`/tasks/${encodeURIComponent(task.task_key)}`}
                        key={task.task_key}
                      >
                        <div className="task-card-top">
                          <span className="task-key">{task.task_key}</span>
                          <span className="task-status">{task.status}</span>
                        </div>
                        <h3>{task.title ?? task.task_key}</h3>
                        <p>{taskSubtitle(task)}</p>
                        <div className="task-card-meta">
                          <span>{valueOrDash(task.project)}</span>
                          <span>{relativeDate(task.updated_at)}</span>
                        </div>
                      </Link>
                    ))
                  )}
                </div>
              </section>
            );
          })}

          {otherTasks.length > 0 ? (
            <section className="board-column compact-column" id="blocked">
              <header className="column-header">
                <div className="column-title-wrap">
                  <span className="column-status-dot red" />
                  <h2>Other Active</h2>
                  <span className="column-count">{otherTasks.length}</span>
                </div>
                <span className="column-menu">•••</span>
              </header>

              <div className="task-card-list">
                {otherTasks.map((task) => (
                  <Link
                    className="task-card"
                    href={`/tasks/${encodeURIComponent(task.task_key)}`}
                    key={task.task_key}
                  >
                    <div className="task-card-top">
                      <span className="task-key">{task.task_key}</span>
                      <span className="task-status">{task.status}</span>
                    </div>
                    <h3>{task.title ?? task.task_key}</h3>
                    <p>{taskSubtitle(task)}</p>
                    <div className="task-card-meta">
                      <span>{valueOrDash(task.project)}</span>
                      <span>{relativeDate(task.updated_at)}</span>
                    </div>
                  </Link>
                ))}
              </div>
            </section>
          ) : null}
        </div>
      </section>
    </div>
  );
}
