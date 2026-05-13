"use client";

import { getStateInfo, TASK_STATE_MAP } from "../lib/taskState";
import type { TaskStatus } from "../lib/types";

/**
 * Ordered list of key states that form a pipeline timeline.
 * Only shows states that are relevant for tracking progress.
 */
const TIMELINE_ORDER = [
  "queued",
  "preparing",
  "implementing",
  "validating",
  "waiting_approval",
] as const;

type TimelineState = typeof TIMELINE_ORDER[number];

interface TaskStateTimelineProps {
  currentStatus: TaskStatus | string;
  blockedReason?: string | null;
}

interface TimelineStep {
  status: string;
  label: string;
  phase: TimelineState | null;
}

function buildTimeline(currentStatus: string): TimelineStep[] {
  const steps: TimelineStep[] = [];

  for (const phase of TIMELINE_ORDER) {
    if (phase === currentStatus) {
      steps.push({ status: phase, label: TASK_STATE_MAP[phase]?.label ?? phase, phase });
      break;
    }
    steps.push({ status: phase, label: TASK_STATE_MAP[phase]?.label ?? phase, phase });
  }

  // If terminal state (accepted/rejected/failed/blocked/...), add it
  const terminalMap: Record<string, string> = {
    accepted: "Approved",
    completed: "Completed",
    rejected: "Rejected",
    failed: "Failed",
    blocked: "Blocked",
    cleaned: "Cleaned",
    canceled: "Canceled",
  };

  if (terminalMap[currentStatus] && currentStatus !== "waiting_approval") {
    steps.push({ status: currentStatus, label: terminalMap[currentStatus], phase: null });
  }

  return steps;
}

export function TaskStateTimeline({ currentStatus, blockedReason }: TaskStateTimelineProps) {
  const steps = buildTimeline(currentStatus);
  const info = getStateInfo(currentStatus);
  const isTerminal = info.terminal;

  return (
    <div
      style={{
        padding: "16px 20px",
        background: "var(--panel)",
        border: "1px solid var(--border)",
        borderRadius: "14px",
        marginBottom: "16px",
      }}
    >
      {/* Current state banner */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "16px",
          gap: "12px",
        }}
      >
        <div>
          <div style={{ fontSize: "0.72rem", color: "var(--muted-2)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: "4px" }}>
            Current State
          </div>
          <div style={{ fontSize: "1rem", fontWeight: 750, color: "var(--text)" }}>
            {info.label}
          </div>
          {info.category !== "unknown" && (
            <div style={{ fontSize: "0.78rem", color: "var(--muted)", marginTop: "2px" }}>
              {info.category.replace("_", " ")}
            </div>
          )}
        </div>
        {isTerminal ? (
          <span
            style={{
              padding: "4px 10px",
              borderRadius: "999px",
              fontSize: "0.72rem",
              fontWeight: 750,
              background: "var(--panel-2)",
              border: "1px solid var(--border)",
              color: "var(--muted)",
            }}
          >
            terminal
          </span>
        ) : (
          <span
            style={{
              padding: "4px 10px",
              borderRadius: "999px",
              fontSize: "0.72rem",
              fontWeight: 750,
              background: "var(--blue)",
              color: "#fff",
            }}
          >
            in progress
          </span>
        )}
      </div>

      {/* Blocked reason if applicable */}
      {currentStatus === "blocked" && blockedReason && (
        <div
          style={{
            marginBottom: "16px",
            padding: "10px 14px",
            background: "rgba(239,68,68,0.1)",
            border: "1px solid var(--red)",
            borderRadius: "8px",
            fontSize: "0.82rem",
            color: "var(--red)",
          }}
        >
          <strong>Blocked reason:</strong> {blockedReason}
        </div>
      )}

      {/* State description */}
      <div
        style={{
          fontSize: "0.82rem",
          color: "var(--muted)",
          marginBottom: "18px",
          padding: "8px 12px",
          background: "rgb(255,255,255,0.025)",
          borderRadius: "8px",
          border: "1px solid var(--border-soft)",
        }}
      >
        {info.description}
      </div>

      {/* Timeline progress */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0",
          position: "relative",
        }}
      >
        {steps.map((step, index) => {
          const isCurrent = step.status === currentStatus;
          const isPast = index < steps.length - 1 && !isCurrent;
          const isLast = index === steps.length - 1;

          const stepColor = isCurrent
            ? "var(--blue)"
            : isPast
            ? "var(--green)"
            : "var(--border)";

          return (
            <div
              key={step.status}
              style={{
                display: "flex",
                alignItems: "center",
                flex: isLast ? "0 0 auto" : "1",
              }}
            >
              {/* Step circle */}
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: "4px",
                }}
              >
                <div
                  style={{
                    width: "14px",
                    height: "14px",
                    borderRadius: "999px",
                    border: `2px solid ${stepColor}`,
                    background: isPast || isCurrent ? stepColor : "transparent",
                    flexShrink: 0,
                  }}
                />
                <span
                  style={{
                    fontSize: "0.68rem",
                    color: isCurrent ? "var(--text)" : isPast ? "var(--green)" : "var(--muted-2)",
                    fontWeight: isCurrent ? 750 : 500,
                    textAlign: "center",
                    maxWidth: "60px",
                    lineHeight: 1.2,
                  }}
                >
                  {step.label}
                </span>
              </div>

              {/* Connector line */}
              {!isLast && (
                <div
                  style={{
                    flex: 1,
                    height: "2px",
                    margin: "0 4px",
                    marginBottom: "20px",
                    background: isPast ? "var(--green)" : "var(--border)",
                    borderRadius: "999px",
                  }}
                />
              )}
            </div>
          );
        })}
      </div>

      {/* Allowed actions */}
      {info.allowedActions.length > 0 && (
        <div
          style={{
            marginTop: "14px",
            padding: "10px 14px",
            background: "rgba(94,106,210,0.08)",
            border: "1px solid var(--blue)",
            borderRadius: "8px",
            fontSize: "0.8rem",
          }}
        >
          <span style={{ color: "var(--muted-2)" }}>Available actions: </span>
          <span style={{ color: "var(--blue)", fontWeight: 750 }}>
            {info.allowedActions.join(", ")}
          </span>
        </div>
      )}
    </div>
  );
}