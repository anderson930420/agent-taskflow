"use client";

import { useState } from "react";
import { getArtifactPreview } from "../lib/api";
import type { ApiFailure, ArtifactFileSummary, ArtifactPreview } from "../lib/types";

interface ArtifactPreviewModalProps {
  artifact: ArtifactFileSummary;
  taskKey: string;
  onClose: () => void;
}

export function ArtifactPreviewModal({
  artifact,
  taskKey,
  onClose,
}: ArtifactPreviewModalProps) {
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    if (preview) return;
    setLoading(true);
    setError(null);
    const result = await getArtifactPreview(taskKey, artifact.name);
    if (result.ok) {
      setPreview(result.data as ArtifactPreview);
    } else {
      setError((result.error as ApiFailure).message);
    }
    setLoading(false);
  }

  // Keyboard close on Escape
  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") onClose();
    if (e.key === "Enter" && !preview && !loading) load();
  }

  const kindColorMap: Record<string, string> = {
    mission_contract: "var(--green)",
    executor_log: "var(--blue)",
    validator_log: "var(--yellow)",
    other: "var(--muted)",
  };
  const kindColor = kindColorMap[artifact.kind] ?? "var(--muted)";

  return (
    <div
      role="dialog"
      aria-modal
      aria-label={`Preview: ${artifact.name}`}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "20px",
        background: "rgba(0,0,0,0.7)",
        backdropFilter: "blur(4px)",
      }}
      onKeyDown={handleKeyDown}
    >
      <div
        style={{
          width: "min(860px, 100%)",
          maxHeight: "85vh",
          background: "var(--panel)",
          border: "1px solid var(--border)",
          borderRadius: "18px",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Modal header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "14px 18px",
            borderBottom: "1px solid var(--border)",
            gap: "12px",
          }}
        >
          <div>
            <div style={{ fontSize: "0.9rem", fontWeight: 760, color: "var(--text)" }}>
              {artifact.name}
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "8px",
                marginTop: "3px",
              }}
            >
              <span
                style={{
                  fontSize: "0.68rem",
                  color: "var(--muted-2)",
                  fontFamily: "monospace",
                }}
              >
                {formatBytes(artifact.size_bytes)}
              </span>
              <span
                style={{
                  padding: "1px 6px",
                  background: `${kindColor}18`,
                  border: `1px solid ${kindColor}44`,
                  borderRadius: "999px",
                  fontSize: "0.68rem",
                  color: kindColor,
                  fontWeight: 700,
                }}
              >
                {artifact.kind}
              </span>
              {artifact.has_secret_warning && (
                <span
                  style={{
                    padding: "1px 6px",
                    background: "rgba(234,179,8,0.1)",
                    border: "1px solid rgba(234,179,8,0.25)",
                    borderRadius: "999px",
                    fontSize: "0.68rem",
                    color: "var(--yellow)",
                    fontWeight: 700,
                  }}
                >
                  ⚠ secrets detected
                </span>
              )}
            </div>
          </div>

          <button
            onClick={onClose}
            aria-label="Close preview"
            style={{
              all: "unset",
              cursor: "pointer",
              padding: "6px 10px",
              fontSize: "1rem",
              color: "var(--muted)",
              border: "1px solid var(--border)",
              borderRadius: "8px",
              background: "var(--panel-2)",
            }}
          >
            ✕
          </button>
        </div>

        {/* Modal content */}
        <div
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "16px 18px",
          }}
        >
          {/* Secret warning */}
          {artifact.has_secret_warning && (
            <div
              style={{
                padding: "10px 14px",
                background: "rgba(234,179,8,0.1)",
                border: "1px solid rgba(234,179,8,0.25)",
                borderRadius: "10px",
                fontSize: "0.8rem",
                color: "var(--yellow)",
                marginBottom: "12px",
              }}
            >
              <strong>⚠ Preview blocked:</strong> This file contains high-confidence
              secret-like patterns. Full content is not shown.
            </div>
          )}

          {/* Loading */}
          {!preview && !loading && !error && !artifact.has_secret_warning && (
            <button
              onClick={load}
              style={{
                all: "unset",
                cursor: "pointer",
                padding: "10px 20px",
                background: "var(--blue)",
                border: "1px solid var(--blue)",
                borderRadius: "999px",
                color: "#fff",
                fontSize: "0.85rem",
                fontWeight: 750,
              }}
            >
              Load preview
            </button>
          )}

          {loading && (
            <div
              style={{
                padding: "20px",
                textAlign: "center",
                fontSize: "0.85rem",
                color: "var(--muted)",
              }}
            >
              Loading preview…
            </div>
          )}

          {/* Error */}
          {error && (
            <div
              style={{
                padding: "10px 14px",
                background: "rgba(239,68,68,0.08)",
                border: "1px solid rgba(239,68,68,0.2)",
                borderRadius: "10px",
                fontSize: "0.8rem",
                color: "var(--red)",
              }}
            >
              <strong>Error loading preview:</strong> {error}
            </div>
          )}

          {/* Preview content */}
          {preview && preview.content !== null ? (
            <div>
              {preview.truncated && (
                <div
                  style={{
                    padding: "6px 10px",
                    background: "rgba(234,179,8,0.07)",
                    border: "1px solid rgba(234,179,8,0.15)",
                    borderRadius: "8px",
                    fontSize: "0.74rem",
                    color: "var(--yellow)",
                    marginBottom: "8px",
                  }}
                >
                  Preview truncated — showing first 20 KB of{" "}
                  {formatBytes(preview.size_bytes)}.
                </div>
              )}
              <pre
                style={{
                  fontSize: "0.72rem",
                  fontFamily: "monospace",
                  color: "var(--text)",
                  background: "var(--bg)",
                  border: "1px solid var(--border-soft)",
                  borderRadius: "10px",
                  padding: "14px 16px",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-all",
                  margin: 0,
                  lineHeight: 1.6,
                }}
              >
                {preview.content}
              </pre>
            </div>
          ) : preview && preview.content === null ? (
            <div
              style={{
                padding: "14px",
                background: "var(--panel-2)",
                border: "1px dashed var(--border)",
                borderRadius: "10px",
                fontSize: "0.82rem",
                color: "var(--muted-2)",
                textAlign: "center",
              }}
            >
              {preview.preview_reason ?? "Preview not available."}
            </div>
          ) : null}
        </div>

        {/* Footer */}
        <div
          style={{
            padding: "10px 18px",
            borderTop: "1px solid var(--border)",
            display: "flex",
            justifyContent: "flex-end",
          }}
        >
          <button
            onClick={onClose}
            style={{
              all: "unset",
              cursor: "pointer",
              padding: "7px 16px",
              fontSize: "0.8rem",
              color: "var(--muted)",
              border: "1px solid var(--border)",
              borderRadius: "8px",
              background: "var(--panel-2)",
            }}
          >
            Close (Esc)
          </button>
        </div>
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