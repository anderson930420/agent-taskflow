"use client";

import {
  TASK_CATEGORIES,
  countTasksByCategory,
  type TaskStateCategoryKey,
} from "../lib/taskState";
import type { Task } from "../lib/types";

interface TaskCategorySummaryProps {
  tasks: Task[];
  activeCategory?: TaskStateCategoryKey | "all";
  onSelectCategory?: (key: TaskStateCategoryKey | "all") => void;
}

const CATEGORY_ICONS: Record<TaskStateCategoryKey, string> = {
  not_started: "○",
  running: "◐",
  review: "◑",
  terminal_success: "◉",
  terminal_failure: "✕",
  terminal_blocked: "⬡",
  terminal_skipped: "○",
  unknown: "?",
};

export function TaskCategorySummary({
  tasks,
  activeCategory,
  onSelectCategory,
}: TaskCategorySummaryProps) {
  const counts = countTasksByCategory(tasks);

  return (
    <div
      style={{
        display: "flex",
        gap: "10px",
        padding: "14px 16px",
        background: "var(--panel)",
        border: "1px solid var(--border)",
        borderRadius: "14px",
        marginBottom: "18px",
        flexWrap: "wrap",
      }}
    >
      {/* All */}
      <button
        onClick={() => onSelectCategory?.("all")}
        style={{
          all: "unset",
          cursor: "pointer",
          padding: "10px 14px",
          borderRadius: "12px",
          fontSize: "0.78rem",
          fontWeight: 700,
          border: "1px solid",
          borderColor: activeCategory === "all" ? "var(--text)" : "var(--border)",
          background:
            activeCategory === "all" ? "var(--panel-2)" : "transparent",
          color:
            activeCategory === "all"
              ? "var(--text)"
              : "var(--muted)",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: "3px",
          minWidth: "72px",
          transition: "all 140ms ease",
        }}
      >
        <span style={{ fontSize: "1.2rem", fontWeight: 900 }}>
          {counts.total}
        </span>
        <span>All</span>
      </button>

      {TASK_CATEGORIES.map((cat) => {
        const count = counts.byCategory[cat.key] ?? 0;
        const isActive = activeCategory === cat.key;
        const isZero = count === 0;

        return (
          <button
            key={cat.key}
            onClick={() => !isZero && onSelectCategory?.(cat.key)}
            disabled={isZero}
            title={cat.description}
            style={{
              all: "unset",
              cursor: isZero ? "default" : "pointer",
              padding: "10px 14px",
              borderRadius: "12px",
              fontSize: "0.78rem",
              fontWeight: 700,
              border: "1px solid",
              borderColor: isActive ? cat.color : "var(--border)",
              background: isActive ? `${cat.color}18` : "transparent",
              color: isZero ? "var(--muted-2)" : cat.color,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: "3px",
              minWidth: "72px",
              transition: "all 140ms ease",
              opacity: isZero ? 0.5 : 1,
            }}
          >
            <span style={{ fontSize: "1.2rem", fontWeight: 900 }}>
              {count}
            </span>
            <span>{cat.label}</span>
          </button>
        );
      })}
    </div>
  );
}