"use client";

import { useState } from "react";
import Link from "next/link";
import {
  getCategoryForStatus,
  getStateInfo,
  TASK_CATEGORIES,
  type TaskStateCategoryKey,
} from "../lib/taskState";
import type { SchedulerCandidateDiscovery, Task } from "../lib/types";
import { ApiStatusIndicator } from "./ApiStatus";
import {
  SchedulerCandidateList,
  SchedulerCandidateSummary
} from "./SchedulerCandidatePanel";
import { TaskBoardFilters } from "./TaskBoardFilters";
import { TaskCategorySummary } from "./TaskCategorySummary";

type FilterCategory = TaskStateCategoryKey | "all";

const TERMINAL_STATUSES = new Set([
  "accepted",
  "completed",
  "cleaned",
  "rejected",
  "canceled",
  "blocked",
]);

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value || value === 0) {
    return String(value ?? "—");
  }
  return "—";
}

function relativeDate(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function taskSubtitle(task: Task): string {
  const pieces = [
    task.executor ? `executor: ${task.executor}` : null,
    task.model ? `model: ${task.model}` : null,
    task.provider ? `provider: ${task.provider}` : null,
  ].filter(Boolean);
  return pieces.length > 0 ? pieces.join(" · ") : "No executor metadata";
}

function matchesSearch(task: Task, search: string): boolean {
  if (!search.trim()) return true;
  const q = search.toLowerCase();
  return (
    task.task_key.toLowerCase().includes(q) ||
    (task.title ?? "").toLowerCase().includes(q) ||
    (task.executor ?? "").toLowerCase().includes(q) ||
    (task.model ?? "").toLowerCase().includes(q) ||
    (task.project ?? "").toLowerCase().includes(q) ||
    (task.provider ?? "").toLowerCase().includes(q)
  );
}

function tasksInCategory(tasks: Task[], category: FilterCategory): Task[] {
  if (category === "all") return tasks;
  const cat = TASK_CATEGORIES.find((c) => c.key === category);
  if (!cat) return [];
  const statusSet = new Set(cat.statuses);
  return tasks.filter((t) => statusSet.has(String(t.status)));
}

// Board columns — now driven by state category colors
type BoardColumn = {
  key: string;
  title: string;
  categories: TaskStateCategoryKey[];
  emptyText: string;
};

const COLUMNS: BoardColumn[] = [
  {
    key: "not_started",
    title: "Not Started",
    categories: ["not_started"],
    emptyText: "No queued tasks.",
  },
  {
    key: "running",
    title: "Running",
    categories: ["running"],
    emptyText: "No tasks currently running.",
  },
  {
    key: "review",
    title: "Needs Review",
    categories: ["review"],
    emptyText: "No tasks waiting for review.",
  },
  {
    key: "terminal",
    title: "Terminal",
    categories: ["terminal_success", "terminal_failure", "terminal_blocked", "terminal_skipped"],
    emptyText: "No completed tasks.",
  },
];

function TaskCard({ task }: { task: Task }) {
  const info = getStateInfo(task.status);
  const cat = getCategoryForStatus(task.status);
  const color = cat?.color ?? "var(--muted)";

  return (
    <Link
      className="task-card"
      href={`/tasks/${encodeURIComponent(task.task_key)}`}
      key={task.task_key}
    >
      <div className="task-card-top">
        {/* State badge */}
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "5px",
            padding: "3px 8px",
            background: "var(--panel)",
            border: `1px solid ${color}`,
            borderRadius: "999px",
            fontSize: "0.68rem",
            fontWeight: 750,
            color: color,
          }}
        >
          <span
            style={{
              width: "7px",
              height: "7px",
              borderRadius: "999px",
              background: color,
              flexShrink: 0,
            }}
          />
          {info.label}
        </span>
        {/* Category pill */}
        {cat && (
          <span
            style={{
              fontSize: "0.62rem",
              color: "var(--muted-2)",
              padding: "1px 5px",
              background: "var(--panel-2)",
              border: "1px solid var(--border)",
              borderRadius: "999px",
            }}
          >
            {cat.label}
          </span>
        )}
      </div>

      <h3>{task.title ?? task.task_key}</h3>
      <p>{taskSubtitle(task)}</p>

      <div className="task-card-meta">
        <span className="task-key">{task.task_key}</span>
        <span>{relativeDate(task.updated_at)}</span>
      </div>
    </Link>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div
      style={{
        padding: "18px 10px",
        color: "var(--muted-2)",
        fontSize: "0.84rem",
        textAlign: "center",
        background: "rgb(255,255,255,0.018)",
        border: "1px dashed var(--border-soft)",
        borderRadius: "12px",
      }}
    >
      {message}
    </div>
  );
}

export function TaskBoard({
  tasks,
  schedulerCandidates = null,
  schedulerCandidatesError = null
}: {
  tasks: Task[];
  schedulerCandidates?: SchedulerCandidateDiscovery | null;
  schedulerCandidatesError?: string | null;
}) {
  const [activeCategory, setActiveCategory] = useState<FilterCategory>("all");
  const [search, setSearch] = useState("");

  // Apply category filter
  let visible = tasksInCategory(tasks, activeCategory);
  // Apply search filter
  if (search.trim()) {
    visible = visible.filter((t) => matchesSearch(t, search));
  }

  // Summary counts (on full task list for All)
  const totalTasks = tasks.length;
  const runningCount =
    (tasks.filter((t) => ["running"].includes(t.status)).length) +
    tasks.filter((t) =>
      ["preparing", "implementing", "validating"].includes(t.status)
    ).length;
  const waitingCount = tasks.filter(
    (t) => t.status === "waiting_approval" || t.status === "waiting_for_review"
  ).length;
  const terminalCount = tasks.filter((t) =>
    TERMINAL_STATUSES.has(String(t.status))
  ).length;

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
            <span>Total tasks</span>
            <strong>{totalTasks}</strong>
          </div>
          <div className="mini-stat">
            <span>Running</span>
            <strong>{runningCount}</strong>
          </div>
          <div className="mini-stat">
            <span>Needs review</span>
            <strong>{waitingCount}</strong>
          </div>
          <div className="mini-stat">
            <span>Terminal</span>
            <strong>{terminalCount}</strong>
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
            <ApiStatusIndicator />
            <div className="readonly-pill">Read-only</div>
            <Link className="ghost-button" href="/tasks/new">
              Create Task
            </Link>
          </div>
        </header>

        {/* Category summary bar */}
        <TaskCategorySummary
          tasks={tasks}
          activeCategory={activeCategory}
          onSelectCategory={setActiveCategory}
        />

        <section
          className="section panel"
          id="scheduler-candidates"
          aria-label="Scheduler Candidates"
        >
          <h2>Scheduler Candidates</h2>
          {schedulerCandidatesError ? (
            <div className="empty">
              Scheduler candidate readback unavailable: {schedulerCandidatesError}.
              NOT execution permission. Read-only discovery.
              Human/operator confirmation required.
            </div>
          ) : (
            <>
              <SchedulerCandidateSummary bundle={schedulerCandidates} />
              <SchedulerCandidateList bundle={schedulerCandidates} />
            </>
          )}
        </section>

        {/* Search */}
        <TaskBoardFilters search={search} onSearchChange={setSearch} />

        {/* Search active indicator */}
        {search.trim() && (
          <div
            style={{
              marginBottom: "12px",
              padding: "8px 12px",
              background: "var(--panel)",
              border: "1px solid var(--border)",
              borderRadius: "10px",
              fontSize: "0.8rem",
              color: "var(--muted)",
            }}
          >
            <strong>{visible.length}</strong> result{visible.length !== 1 ? "s" : ""} for
            &ldquo;<span style={{ color: "var(--text)" }}>{search}</span>&rdquo;
            {activeCategory !== "all" && (
              <> · filtered to <span style={{ color: "var(--blue)" }}>{activeCategory}</span></>
            )}
          </div>
        )}

        {/* Board columns */}
        <div className="board" id="board">
          {COLUMNS.map((column) => {
            const columnTasks = visible.filter((task) =>
              column.categories.some((cat) => {
                const meta = TASK_CATEGORIES.find((c) => c.key === cat);
                return meta?.statuses.includes(task.status);
              })
            );

            const firstCat = TASK_CATEGORIES.find((c) => c.key === column.categories[0]);
            const colColor = firstCat?.color ?? "var(--border)";

            return (
              <section className="board-column" key={column.key}>
                <header className="column-header">
                  <div className="column-title-wrap">
                    <span
                      style={{
                        width: "12px",
                        height: "12px",
                        borderRadius: "999px",
                        border: `2px solid ${colColor}`,
                        background: `${colColor}20`,
                        flexShrink: 0,
                      }}
                    />
                    <h2>{column.title}</h2>
                    <span className="column-count">{columnTasks.length}</span>
                  </div>
                  <span className="column-menu">•••</span>
                </header>

                <div className="task-card-list">
                  {columnTasks.length === 0 ? (
                    <EmptyState
                      message={
                        search.trim()
                          ? "No matching tasks."
                          : column.emptyText
                      }
                    />
                  ) : (
                    columnTasks.map((task) => <TaskCard key={task.task_key} task={task} />)
                  )}
                </div>
              </section>
            );
          })}
        </div>

        {/* Empty board state */}
        {tasks.length === 0 && (
          <div
            style={{
              padding: "48px 24px",
              textAlign: "center",
              background: "var(--panel)",
              border: "1px dashed var(--border)",
              borderRadius: "16px",
            }}
          >
            <div style={{ fontSize: "2rem", marginBottom: "12px" }}>📭</div>
            <h2 style={{ margin: "0 0 8px", color: "var(--muted)" }}>
              No tasks yet
            </h2>
            <p style={{ color: "var(--muted-2)", fontSize: "0.85rem" }}>
              Create a task to get started with Mission Control.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}