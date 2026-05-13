"use client";

import { useEffect, useState } from "react";
import { requestJson } from "../lib/api";

export type ApiReachabilityStatus = "loading" | "connected" | "degraded" | "unknown";

interface ApiHealthResponse {
  status: string;
  service: string;
}

export function useApiReachability() {
  const [status, setStatus] = useState<ApiReachabilityStatus>("loading");
  const [message, setMessage] = useState<string>("");

  useEffect(() => {
    let cancelled = false;

    async function check() {
      try {
        const result = await requestJson<ApiHealthResponse>("/health");
        if (!cancelled) {
          if (result.ok) {
            setStatus("connected");
            setMessage(
              result.data.status === "ok"
                ? `Connected to ${result.data.service}`
                : `Connected — ${result.data.status}`
            );
          } else {
            setStatus("degraded");
            setMessage(result.error.message);
          }
        }
      } catch {
        if (!cancelled) {
          setStatus("degraded");
          setMessage("Unable to reach Agent Taskflow API");
        }
      }
    }

    check();
    const timer = setInterval(check, 30_000); // re-check every 30s
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return { status, message };
}

interface ApiStatusIndicatorProps {
  compact?: boolean;
}

export function ApiStatusIndicator({ compact = false }: ApiStatusIndicatorProps) {
  const { status, message } = useApiReachability();

  const colorMap: Record<ApiReachabilityStatus, string> = {
    loading: "var(--muted-2)",
    connected: "var(--green)",
    degraded: "var(--red)",
    unknown: "var(--muted-2)",
  };

  const color = colorMap[status];
  const labelMap: Record<ApiReachabilityStatus, string> = {
    loading: "Checking API…",
    connected: "API connected",
    degraded: "API error",
    unknown: "Unknown",
  };

  if (compact) {
    return (
      <span
        title={message}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "5px",
          fontSize: "0.72rem",
          color,
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
        {labelMap[status]}
      </span>
    );
  }

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "8px",
        padding: "6px 12px",
        background: "var(--panel)",
        border: "1px solid var(--border-soft)",
        borderRadius: "999px",
        fontSize: "0.78rem",
        color,
      }}
    >
      <span
        style={{
          width: "8px",
          height: "8px",
          borderRadius: "999px",
          background: color,
          flexShrink: 0,
          animation: status === "loading" ? "pulse 1.5s ease-in-out infinite" : undefined,
        }}
      />
      {labelMap[status]}
      {message && (
        <span style={{ color: "var(--muted-2)", fontSize: "0.72rem" }}>
          {message}
        </span>
      )}
    </div>
  );
}