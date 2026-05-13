"use client";

import type { ApiFailure } from "../lib/types";

export interface ApiErrorPanelProps {
  error: ApiFailure;
  title?: string;
  retryLabel?: string;
  onRetry?: () => void;
}

export function ApiErrorPanel({
  error,
  title = "API Error",
  retryLabel = "Retry",
  onRetry,
}: ApiErrorPanelProps) {
  return (
    <div
      role="alert"
      style={{
        padding: "16px 20px",
        background: "rgba(239,68,68,0.08)",
        border: "1px solid rgba(239,68,68,0.3)",
        borderRadius: "12px",
        color: "var(--red)",
      }}
    >
      <div
        style={{
          fontSize: "0.72rem",
          fontWeight: 800,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          marginBottom: "6px",
        }}
      >
        {title}
      </div>

      <div style={{ fontSize: "0.88rem", fontWeight: 700, marginBottom: "4px" }}>
        {error.message}
      </div>

      {error.status && (
        <div style={{ fontSize: "0.75rem", color: "var(--muted-2)" }}>
          HTTP {error.status}
        </div>
      )}

      <div
        style={{
          marginTop: "12px",
          padding: "8px 12px",
          background: "rgba(239,68,68,0.05)",
          border: "1px solid rgba(239,68,68,0.15)",
          borderRadius: "8px",
          fontSize: "0.76rem",
          color: "var(--muted)",
        }}
      >
        Retry by refreshing the page or navigating away and back.
        If the problem persists, check that the Agent Taskflow API server is running.
      </div>

      {onRetry && (
        <button
          onClick={onRetry}
          style={{
            marginTop: "12px",
            all: "unset",
            cursor: "pointer",
            padding: "7px 14px",
            border: "1px solid var(--red)",
            borderRadius: "8px",
            fontSize: "0.8rem",
            fontWeight: 700,
            color: "var(--red)",
            background: "transparent",
          }}
        >
          {retryLabel}
        </button>
      )}
    </div>
  );
}

export function ApiErrorBanner({
  error,
  action,
}: {
  error: ApiFailure;
  action?: string;
}) {
  return (
    <div
      role="alert"
      style={{
        padding: "12px 16px",
        background: "rgba(239,68,68,0.1)",
        border: "1px solid rgba(239,68,68,0.25)",
        borderRadius: "10px",
        fontSize: "0.82rem",
        color: "var(--red)",
      }}
    >
      <strong>Action failed</strong>
      {action ? <>: {action}</> : null}
      <br />
      {error.message}
    </div>
  );
}