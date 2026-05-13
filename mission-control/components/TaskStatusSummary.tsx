"use client";

import { getStateInfo } from "../lib/taskState";
import type { TaskStatus } from "../lib/types";

interface TaskStatusSummaryProps {
  status: TaskStatus | string;
  taskKey?: string;
  executor?: string | null;
  model?: string | null;
  blockedReason?: string | null;
}

export function TaskStatusSummary({
  status,
  taskKey,
  executor,
  model,
  blockedReason,
}: TaskStatusSummaryProps) {
  const info = getStateInfo(status);

  const categoryColors: Record<string, string> = {
    not_started: "var(--muted)",
    running: "var(--blue)",
    review: "var(--yellow)",
    terminal_success: "var(--green)",
    terminal_failure: "var(--red)",
    terminal_blocked: "var(--red)",
    terminal_skipped: "var(--muted-2)",
    unknown: "var(--muted)",
  };

  const color = categoryColors[info.category] ?? "var(--muted)";

  return (
    <div
      style={{
        padding: "16px 18px",
        background: "var(--panel)",
        border: "1px solid var(--border)",
        borderRadius: "14px",
      }}
    >
      {/* State header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "12px",
          gap: "10px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span
            style={{
              width: "10px",
              height: "10px",
              borderRadius: "999px",
              background: color,
              flexShrink: 0,
            }}
          />
          <span
            style={{
              fontSize: "0.92rem",
              fontWeight: 760,
              color: "var(--text)",
            }}
          >
            {info.label}
          </span>
          <span
            style={{
              fontSize: "0.7rem",
              color: "var(--muted-2)",
              padding: "1px 6px",
              background: "var(--panel-2)",
              border: "1px solid var(--border)",
              borderRadius: "999px",
            }}
          >
            {info.category.replace(/_/g, " ")}
          </span>
        </div>
        {info.terminal ? (
          <span
            style={{
              fontSize: "0.68rem",
              color: "var(--muted-2)",
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}
          >
            terminal
          </span>
        ) : null}
      </div>

      {/* Blocked reason */}
      {status === "blocked" && blockedReason && (
        <div
          style={{
            padding: "8px 12px",
            background: "rgba(239,68,68,0.1)",
            border: "1px solid rgba(239,68,68,0.3)",
            borderRadius: "8px",
            fontSize: "0.78rem",
            color: "var(--red)",
            marginBottom: "10px",
          }}
        >
          {blockedReason}
        </div>
      )}

      {/* Description */}
      <p
        style={{
          fontSize: "0.8rem",
          color: "var(--muted)",
          margin: "0 0 12px",
          lineHeight: 1.5,
        }}
      >
        {info.description}
      </p>

      {/* Metadata row */}
      {(executor || model || taskKey) && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "8px 16px",
            paddingTop: "10px",
            borderTop: "1px solid var(--border-soft)",
          }}
        >
          {executor && (
            <div style={{ fontSize: "0.75rem" }}>
              <span style={{ color: "var(--muted-2)" }}>Executor: </span>
              <span style={{ color: "var(--muted)", fontFamily: "monospace" }}>{executor}</span>
            </div>
          )}
          {model && (
            <div style={{ fontSize: "0.75rem" }}>
              <span style={{ color: "var(--muted-2)" }}>Model: </span>
              <span style={{ color: "var(--muted)", fontFamily: "monospace" }}>{model}</span>
            </div>
          )}
          {taskKey && (
            <div style={{ fontSize: "0.75rem" }}>
              <span style={{ color: "var(--muted-2)" }}>Task: </span>
              <span style={{ color: "var(--muted)", fontFamily: "monospace" }}>{taskKey}</span>
            </div>
          )}
        </div>
      )}

      {/* Allowed actions */}
      {info.allowedActions.length > 0 && (
        <div
          style={{
            marginTop: "10px",
            paddingTop: "10px",
            borderTop: "1px solid var(--border-soft)",
            display: "flex",
            alignItems: "center",
            gap: "6px",
          }}
        >
          <span style={{ fontSize: "0.72rem", color: "var(--muted-2)" }}>Actions:</span>
          {info.allowedActions.map((action) => (
            <span
              key={action}
              style={{
                padding: "2px 8px",
                background: "var(--panel-2)",
                border: "1px solid var(--border)",
                borderRadius: "999px",
                fontSize: "0.72rem",
                color: "var(--blue)",
                fontWeight: 700,
              }}
            >
              {action}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}