"use client";

import { getArtifactPreview } from "../lib/api";
import type { ArtifactPreview } from "../lib/types";
import { useState } from "react";

interface PolicyLogViewerProps {
  taskKey: string;
  artifactName?: string;
  /** Policy check status passed/failed/blocked/not_run from review evidence */
  policyStatus?: string;
}

type LineHighlight =
  | { kind: "pass" }
  | { kind: "fail" }
  | { kind: "warning" }
  | { kind: "forbidden"; action: string }
  | { kind: "neutral" };

function classifyLine(line: string): LineHighlight {
  const lower = line.toLowerCase();
  if (
    lower.includes("policy check passed") ||
    lower.includes("all checks passed") ||
    lower.includes("✓") ||
    lower.includes("no violations")
  ) {
    return { kind: "pass" };
  }
  if (
    lower.includes("policy check failed") ||
    lower.includes("validation failed") ||
    lower.includes("✗") ||
    lower.includes("violation")
  ) {
    return { kind: "fail" };
  }
  if (
    lower.includes("forbidden action") ||
    lower.includes("forbidden:") ||
    lower.includes("forbidden-action")
  ) {
    // Extract the action
    const match = line.match(/forbidden[\s_-]*action[:\s]+([^\s,]+)/i);
    return { kind: "forbidden", action: match ? match[1] : "unknown" };
  }
  if (
    lower.includes("warning") ||
    lower.includes("secret") ||
    lower.includes("api_key") ||
    lower.includes("token")
  ) {
    return { kind: "warning" };
  }
  return { kind: "neutral" };
}

function LogLine({ line }: { line: string }) {
  const highlight = classifyLine(line);

  const colorMap: Record<string, string> = {
    pass: "var(--green)",
    fail: "var(--red)",
    warning: "var(--yellow)",
    forbidden: "var(--red)",
    neutral: "var(--muted)",
  };

  const bgMap: Record<string, string> = {
    pass: "rgba(38,162,105,0.08)",
    fail: "rgba(239,68,68,0.08)",
    warning: "rgba(234,179,8,0.07)",
    forbidden: "rgba(239,68,68,0.1)",
    neutral: "transparent",
  };

  const color = colorMap[highlight.kind];
  const bg = bgMap[highlight.kind];

  return (
    <div
      style={{
        padding: "1px 6px",
        background: bg,
        fontSize: "0.72rem",
        fontFamily: "monospace",
        color,
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
        borderLeft:
          highlight.kind === "pass"
            ? "3px solid var(--green)"
            : highlight.kind === "fail" || highlight.kind === "forbidden"
            ? "3px solid var(--red)"
            : highlight.kind === "warning"
            ? "3px solid var(--yellow)"
            : "3px solid transparent",
      }}
    >
      {highlight.kind === "forbidden" && (
        <strong>⚠ FORBIDDEN: </strong>
      )}
      {line}
    </div>
  );
}

export function PolicyLogViewer({
  taskKey,
  artifactName = "policy-validate.log",
  policyStatus = "unknown",
}: PolicyLogViewerProps) {
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadPreview() {
    if (preview) return;
    setLoading(true);
    setError(null);

    const result = await getArtifactPreview(taskKey, artifactName);
    if (result.ok) {
      setPreview(result.data as ArtifactPreview);
    } else {
      setError((result.error as { message: string }).message);
    }
    setLoading(false);
  }

  const statusColorMap: Record<string, string> = {
    passed: "var(--green)",
    failed: "var(--red)",
    blocked: "var(--yellow)",
    not_run: "var(--muted-2)",
    not_required: "var(--muted-2)",
    unknown: "var(--muted)",
  };
  const statusColor = statusColorMap[policyStatus] ?? "var(--muted)";

  return (
    <div
      style={{
        padding: "14px 16px",
        background: "var(--panel)",
        border: "1px solid var(--border)",
        borderRadius: "14px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "12px",
          gap: "10px",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "0.9rem", fontWeight: 760 }}>
          Policy Log
        </h3>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <span
            style={{
              fontSize: "0.72rem",
              color: "var(--muted-2)",
            }}
          >
            Status:
          </span>
          <span
            style={{
              padding: "2px 8px",
              background: `${statusColor}18`,
              border: `1px solid ${statusColor}44`,
              borderRadius: "999px",
              fontSize: "0.72rem",
              fontWeight: 750,
              color: statusColor,
              textTransform: "capitalize",
            }}
          >
            {policyStatus.replace(/_/g, " ")}
          </span>
        </div>
      </div>

      {/* Policy status banner */}
      {policyStatus === "failed" && (
        <div
          style={{
            padding: "8px 12px",
            background: "rgba(239,68,68,0.1)",
            border: "1px solid rgba(239,68,68,0.25)",
            borderRadius: "8px",
            fontSize: "0.76rem",
            color: "var(--red)",
            marginBottom: "12px",
          }}
        >
          <strong>Policy check failed.</strong> Review the log below for
          details. The task cannot be approved until policy violations are
          resolved.
        </div>
      )}
      {policyStatus === "passed" && (
        <div
          style={{
            padding: "8px 12px",
            background: "rgba(38,162,105,0.08)",
            border: "1px solid rgba(38,162,105,0.2)",
            borderRadius: "8px",
            fontSize: "0.76rem",
            color: "var(--green)",
            marginBottom: "12px",
          }}
        >
          <strong>Policy check passed.</strong> No forbidden actions detected.
        </div>
      )}

      {/* Load button */}
      {!preview && (
        <button
          onClick={loadPreview}
          disabled={loading}
          style={{
            all: "unset",
            cursor: loading ? "default" : "pointer",
            padding: "7px 14px",
            background: "var(--blue)",
            border: "1px solid var(--blue)",
            borderRadius: "999px",
            color: "#fff",
            fontSize: "0.78rem",
            fontWeight: 700,
            marginBottom: "10px",
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
          }}
        >
          {loading ? "Loading log…" : "Load log"}
        </button>
      )}

      {/* Error */}
      {error && (
        <div
          style={{
            padding: "8px 12px",
            background: "rgba(239,68,68,0.08)",
            border: "1px solid rgba(239,68,68,0.2)",
            borderRadius: "8px",
            fontSize: "0.76rem",
            color: "var(--red)",
          }}
        >
          <strong>Error:</strong> {error}
        </div>
      )}

      {/* Log content */}
      {preview && preview.content !== null ? (
        <div>
          {preview.truncated && (
            <div
              style={{
                padding: "5px 8px",
                background: "rgba(234,179,8,0.07)",
                border: "1px solid rgba(234,179,8,0.15)",
                borderRadius: "6px",
                fontSize: "0.7rem",
                color: "var(--yellow)",
                marginBottom: "6px",
              }}
            >
              Preview truncated — showing first 20 KB of{" "}
              {(preview.size_bytes / 1024).toFixed(1)} KB.
            </div>
          )}
          <div
            style={{
              maxHeight: "500px",
              overflowY: "auto",
              border: "1px solid var(--border-soft)",
              borderRadius: "8px",
            }}
          >
            {(preview.content as string).split("\n").map((line, i) => (
              <LogLine key={i} line={line} />
            ))}
          </div>
        </div>
      ) : preview && preview.content === null ? (
        <div
          style={{
            padding: "10px 14px",
            background: "rgba(239,68,68,0.06)",
            border: "1px solid rgba(239,68,68,0.15)",
            borderRadius: "8px",
            fontSize: "0.76rem",
            color: "var(--muted-2)",
          }}
        >
          {preview.preview_reason ?? "Preview not available."}
        </div>
      ) : null}
    </div>
  );
}