"use client";

import { getArtifactPreview } from "../lib/api";
import type { ArtifactPreview } from "../lib/types";
import { useState } from "react";

interface PiMissionPlanViewerProps {
  taskKey: string;
  artifactName?: string;
}

interface PlanStep {
  name: string;
  description: string;
  [key: string]: unknown;
}

interface PiMissionPlan {
  schema_version?: string;
  task_key?: string;
  executor?: string;
  goal?: string;
  steps?: PlanStep[];
  required_validators?: string[];
  human_approval_required?: boolean;
  [key: string]: unknown;
}

function tryParse(text: string): PiMissionPlan | null {
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      return parsed as PiMissionPlan;
    }
    return null;
  } catch {
    return null;
  }
}

function StepRow({ step, index }: { step: PlanStep; index: number }) {
  return (
    <div
      style={{
        padding: "8px 10px",
        background: "var(--panel-2)",
        border: "1px solid var(--border-soft)",
        borderRadius: "8px",
        marginBottom: "6px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "8px",
          marginBottom: "4px",
        }}
      >
        <span
          style={{
            width: "20px",
            height: "20px",
            borderRadius: "999px",
            background: "var(--blue)",
            color: "#fff",
            fontSize: "0.68rem",
            fontWeight: 900,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
          }}
        >
          {index + 1}
        </span>
        <span
          style={{
            fontSize: "0.8rem",
            fontWeight: 750,
            color: "var(--text)",
          }}
        >
          {step.name}
        </span>
      </div>
      <p
        style={{
          fontSize: "0.75rem",
          color: "var(--muted)",
          margin: 0,
          lineHeight: 1.5,
          paddingLeft: "28px",
        }}
      >
        {step.description}
      </p>
    </div>
  );
}

export function PiMissionPlanViewer({
  taskKey,
  artifactName = "pi_mission_plan.json",
}: PiMissionPlanViewerProps) {
  const [content, setContent] = useState<string | null>(null);
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [parsed, setParsed] = useState<PiMissionPlan | null>(null);

  async function loadPreview() {
    if (preview) return; // already loaded
    setLoading(true);
    setError(null);

    const result = await getArtifactPreview(taskKey, artifactName);
    if (result.ok) {
      const p = result.data as ArtifactPreview;
      setPreview(p);
      setContent(p.content);
      if (p.content) {
        setParsed(tryParse(p.content));
      }
    } else {
      setError((result.error as { message: string }).message);
    }
    setLoading(false);
  }

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
          Pi Mission Plan
        </h3>
        {!preview && (
          <button
            onClick={loadPreview}
            disabled={loading}
            style={{
              all: "unset",
              cursor: loading ? "default" : "pointer",
              padding: "5px 12px",
              background: "var(--blue)",
              border: "1px solid var(--blue)",
              borderRadius: "999px",
              color: "#fff",
              fontSize: "0.75rem",
              fontWeight: 700,
              opacity: loading ? 0.6 : 1,
            }}
          >
            {loading ? "Loading…" : "Load plan"}
          </button>
        )}
      </div>

      {/* Note */}
      <div
        style={{
          padding: "8px 12px",
          background: "rgba(94,106,210,0.07)",
          border: "1px solid rgba(94,106,210,0.2)",
          borderRadius: "8px",
          fontSize: "0.72rem",
          color: "var(--blue)",
          marginBottom: "12px",
        }}
      >
        <strong>Note:</strong> These are protocol steps, not autonomous agents.
        The UI does not execute steps. Deterministic validators run separately
        through the governance pipeline.
      </div>

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

      {/* Structured view */}
      {parsed ? (
        <div>
          {/* Header fields */}
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "8px",
              marginBottom: "12px",
              padding: "10px",
              background: "var(--panel-2)",
              border: "1px solid var(--border-soft)",
              borderRadius: "10px",
            }}
          >
            {parsed.schema_version && (
              <span style={{ fontSize: "0.72rem", color: "var(--muted-2)" }}>
                Schema:{" "}
                <code style={{ fontSize: "0.7rem" }}>
                  {parsed.schema_version}
                </code>
              </span>
            )}
            {parsed.task_key && (
              <span style={{ fontSize: "0.72rem", color: "var(--muted-2)" }}>
                Task:{" "}
                <code style={{ fontSize: "0.7rem" }}>{parsed.task_key}</code>
              </span>
            )}
            {parsed.executor && (
              <span style={{ fontSize: "0.72rem", color: "var(--muted-2)" }}>
                Executor:{" "}
                <code style={{ fontSize: "0.7rem" }}>{parsed.executor}</code>
              </span>
            )}
            {parsed.human_approval_required !== undefined && (
              <span
                style={{
                  fontSize: "0.72rem",
                  color: parsed.human_approval_required
                    ? "var(--yellow)"
                    : "var(--muted-2)",
                }}
              >
                Human approval:{" "}
                <strong>
                  {parsed.human_approval_required ? "required" : "not required"}
                </strong>
              </span>
            )}
          </div>

          {/* Goal */}
          {parsed.goal && (
            <div
              style={{
                padding: "8px 10px",
                background: "var(--panel-2)",
                border: "1px solid var(--border-soft)",
                borderRadius: "8px",
                fontSize: "0.78rem",
                color: "var(--text)",
                marginBottom: "12px",
                lineHeight: 1.5,
              }}
            >
              <strong style={{ color: "var(--muted-2)", fontSize: "0.7rem" }}>
                GOAL
              </strong>
              <br />
              {parsed.goal}
            </div>
          )}

          {/* Required validators */}
          {parsed.required_validators && parsed.required_validators.length > 0 && (
            <div style={{ marginBottom: "12px" }}>
              <div
                style={{
                  fontSize: "0.72rem",
                  color: "var(--muted-2)",
                  marginBottom: "4px",
                }}
              >
                Required validators
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
                {parsed.required_validators.map((v) => (
                  <span
                    key={v}
                    style={{
                      padding: "2px 8px",
                      background: "rgba(94,106,210,0.1)",
                      border: "1px solid rgba(94,106,210,0.25)",
                      borderRadius: "999px",
                      fontSize: "0.7rem",
                      color: "var(--blue)",
                      fontWeight: 700,
                    }}
                  >
                    {v}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Steps */}
          {parsed.steps && parsed.steps.length > 0 && (
            <div>
              <div
                style={{
                  fontSize: "0.72rem",
                  color: "var(--muted-2)",
                  marginBottom: "6px",
                }}
              >
                Protocol steps
              </div>
              {parsed.steps.map((step, i) => (
                <StepRow key={i} step={step} index={i} />
              ))}
            </div>
          )}
        </div>
      ) : preview && content === null ? (
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
      ) : preview && content && !parsed ? (
        // Raw text fallback
        <pre
          style={{
            fontSize: "0.7rem",
            fontFamily: "monospace",
            color: "var(--muted)",
            background: "var(--bg)",
            border: "1px solid var(--border-soft)",
            borderRadius: "8px",
            padding: "10px 12px",
            maxHeight: "400px",
            overflowY: "auto",
            whiteSpace: "pre-wrap",
            wordBreak: "break-all",
            margin: 0,
          }}
        >
          {content}
        </pre>
      ) : !preview ? (
        <div
          style={{
            padding: "14px",
            background: "var(--panel-2)",
            border: "1px dashed var(--border-soft)",
            borderRadius: "10px",
            fontSize: "0.8rem",
            color: "var(--muted-2)",
            textAlign: "center",
          }}
        >
          Click &ldquo;Load plan&rdquo; to view the Pi mission plan.
        </div>
      ) : null}
    </div>
  );
}