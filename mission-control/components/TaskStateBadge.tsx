"use client";

import { getStateInfo, stateCategoryColor } from "../lib/taskState";

interface TaskStateBadgeProps {
  status: string;
  showDescription?: boolean;
}

export function TaskStateBadge({ status, showDescription = false }: TaskStateBadgeProps) {
  const info = getStateInfo(status);
  const color = stateCategoryColor(info.category);

  return (
    <span
      style={{
        display: "inline-flex",
        flexDirection: "column",
        gap: "2px",
      }}
    >
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "6px",
          padding: "4px 10px",
          background: "var(--panel)",
          border: `1px solid ${color}`,
          borderRadius: "999px",
          fontSize: "0.8rem",
          fontWeight: 750,
          color: color,
        }}
      >
        <span
          style={{
            width: "8px",
            height: "8px",
            borderRadius: "999px",
            background: color,
            flexShrink: 0,
          }}
        />
        {info.label}
      </span>
      {showDescription && (
        <span
          style={{
            fontSize: "0.72rem",
            color: "var(--muted)",
            paddingLeft: "4px",
          }}
        >
          {info.description}
        </span>
      )}
    </span>
  );
}