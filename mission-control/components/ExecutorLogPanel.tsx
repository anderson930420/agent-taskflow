"use client";

import { useState } from "react";
import { getArtifactPreview, getTaskReviewEvidence } from "../lib/api";
import type { ArtifactFileSummary, ArtifactPreview } from "../lib/types";

interface ExecutorLogPanelProps {
  taskKey: string;
  /** Initial list of executor log artifacts from review evidence */
  executorLogs?: ArtifactFileSummary[];
}

interface LogEntry {
  artifact: ArtifactFileSummary;
  preview: ArtifactPreview | null;
  loading: boolean;
  error: string | null;
}

function logLabel(name: string): string {
  if (name.includes("pi-executor")) return "Pi executor log";
  if (name.includes("opencode")) return "OpenCode log";
  if (name.includes("executor")) return "Executor log";
  return name;
}

export function ExecutorLogPanel({
  taskKey,
  executorLogs = [],
}: ExecutorLogPanelProps) {
  const [entries, setEntries] = useState<LogEntry[]>(
    executorLogs.map((artifact) => ({
      artifact,
      preview: null,
      loading: false,
      error: null,
    }))
  );

  async function loadPreview(index: number) {
    const entry = entries[index];
    if (!entry) return;

    setEntries((prev) =>
      prev.map((e, i) =>
        i === index ? { ...e, loading: true, error: null } : e
      )
    );

    const result = await getArtifactPreview(taskKey, entry.artifact.name);

    if (result.ok) {
      setEntries((prev) =>
        prev.map((e, i) =>
          i === index
            ? { ...e, preview: result.data as ArtifactPreview, loading: false }
            : e
        )
      );
    } else {
      setEntries((prev) =>
        prev.map((e, i) =>
          i === index
            ? { ...e, loading: false, error: (result.error as { message: string }).message }
            : e
        )
      );
    }
  }

  if (executorLogs.length === 0) {
    return (
      <div
        style={{
          padding: "14px 16px",
          background: "var(--panel)",
          border: "1px solid var(--border)",
          borderRadius: "14px",
          fontSize: "0.82rem",
          color: "var(--muted-2)",
          textAlign: "center",
        }}
      >
        No executor log artifacts found for this task.
        <br />
        <span style={{ fontSize: "0.72rem" }}>
          Logs are produced when a task executor runs and are stored in the task artifact directory.
        </span>
      </div>
    );
  }

  return (
    <div
      style={{
        padding: "14px 16px",
        background: "var(--panel)",
        border: "1px solid var(--border)",
        borderRadius: "14px",
        marginBottom: "14px",
      }}
    >
      <h3
        style={{
          margin: "0 0 12px",
          fontSize: "0.9rem",
          fontWeight: 760,
        }}
      >
        Executor Logs
      </h3>

      <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
        {entries.map((entry, index) => {
          const { artifact, preview, loading, error } = entry;

          return (
            <div
              key={artifact.name}
              style={{
                padding: "12px",
                background: "var(--panel-2)",
                border: "1px solid var(--border-soft)",
                borderRadius: "10px",
              }}
            >
              {/* Log header */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  marginBottom: "8px",
                  gap: "8px",
                }}
              >
                <div>
                  <div
                    style={{
                      fontSize: "0.82rem",
                      fontWeight: 700,
                      color: "var(--text)",
                    }}
                  >
                    {logLabel(artifact.name)}
                  </div>
                  <code
                    style={{
                      fontSize: "0.7rem",
                      color: "var(--muted-2)",
                      fontFamily: "monospace",
                    }}
                  >
                    {artifact.name}
                  </code>
                </div>

                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    fontSize: "0.72rem",
                    color: "var(--muted)",
                  }}
                >
                  <span>{formatBytes(artifact.size_bytes)}</span>
                  {artifact.has_secret_warning && (
                    <span
                      style={{
                        padding: "1px 6px",
                        background: "rgba(234,179,8,0.1)",
                        border: "1px solid rgba(234,179,8,0.25)",
                        borderRadius: "999px",
                        color: "var(--yellow)",
                        fontWeight: 700,
                      }}
                    >
                      secrets detected
                    </span>
                  )}
                  {!preview && !loading && !error && !artifact.has_secret_warning && (
                    <button
                      onClick={() => loadPreview(index)}
                      style={{
                        all: "unset",
                        cursor: "pointer",
                        padding: "4px 10px",
                        background: "var(--blue)",
                        border: "1px solid var(--blue)",
                        borderRadius: "999px",
                        color: "#fff",
                        fontSize: "0.72rem",
                        fontWeight: 700,
                      }}
                    >
                      Load preview
                    </button>
                  )}
                  {loading && (
                    <span style={{ color: "var(--muted-2)" }}>Loading…</span>
                  )}
                </div>
              </div>

              {/* Secret warning */}
              {artifact.has_secret_warning && (
                <div
                  style={{
                    padding: "8px 10px",
                    background: "rgba(234,179,8,0.08)",
                    border: "1px solid rgba(234,179,8,0.2)",
                    borderRadius: "8px",
                    fontSize: "0.76rem",
                    color: "var(--yellow)",
                  }}
                >
                  Preview unavailable — file contains secret-like assignment patterns.
                </div>
              )}

              {/* Error */}
              {error && (
                <div
                  style={{
                    padding: "8px 10px",
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

              {/* Preview content */}
              {preview && (
                <div>
                  {preview.content !== null ? (
                    <div>
                      {preview.truncated && (
                        <div
                          style={{
                            padding: "5px 8px",
                            background: "rgba(234,179,8,0.07)",
                            border: "1px solid rgba(234,179,8,0.15)",
                            borderRadius: "6px",
                            fontSize: "0.72rem",
                            color: "var(--yellow)",
                            marginBottom: "6px",
                          }}
                        >
                          Preview truncated — showing first 20 KB of{" "}
                          {formatBytes(preview.size_bytes)}.
                        </div>
                      )}
                      <pre
                        style={{
                          fontSize: "0.7rem",
                          fontFamily: "monospace",
                          color: "var(--muted)",
                          background: "var(--bg)",
                          border: "1px solid var(--border-soft)",
                          borderRadius: "8px",
                          padding: "10px 12px",
                          maxHeight: "320px",
                          overflowY: "auto",
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-all",
                          margin: 0,
                        }}
                      >
                        {preview.content}
                      </pre>
                    </div>
                  ) : (
                    <div
                      style={{
                        padding: "8px 10px",
                        background: "rgba(239,68,68,0.06)",
                        border: "1px solid rgba(239,68,68,0.15)",
                        borderRadius: "8px",
                        fontSize: "0.76rem",
                        color: "var(--muted-2)",
                      }}
                    >
                      {preview.preview_reason ?? "Preview not available."}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}