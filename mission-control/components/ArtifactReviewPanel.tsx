"use client";

import type { ArtifactFileSummary, MissionContractSummary, TaskReviewEvidence } from "../lib/types";
import { ArtifactPreviewModal } from "./ArtifactPreviewModal";
import { MissionContractViewer } from "./MissionContractViewer";
import { PiMissionPlanViewer } from "./PiMissionPlanViewer";
import { PolicyLogViewer } from "./PolicyLogViewer";
import { getArtifactPreview } from "../lib/api";
import type { ArtifactPreview } from "../lib/types";
import { useState } from "react";

// ─── Artifact classification ────────────────────────────────────────────────

export type ArtifactCategory =
  | "all"
  | "mission"
  | "pi_protocol"
  | "executor_logs"
  | "validator_logs"
  | "prompts"
  | "other";

export function classifyArtifact(a: ArtifactFileSummary): ArtifactCategory {
  if (a.is_mission_contract) return "mission";
  if (a.name === "pi_mission_plan.json") return "pi_protocol";
  if (a.name === "pi_mission_prompt.md") return "pi_protocol";
  if (a.is_executor_log) return "executor_logs";
  if (a.is_validator_log) return "validator_logs";
  if (
    a.name === "implementation_prompt.md" ||
    a.name.endsWith("_prompt.md")
  )
    return "prompts";
  return "other";
}

export const CATEGORY_LABELS: Record<ArtifactCategory, string> = {
  all: "All",
  mission: "Mission",
  pi_protocol: "Pi Protocol",
  executor_logs: "Executor",
  validator_logs: "Validator",
  prompts: "Prompts",
  other: "Other",
};

// ─── Artifact row with inline preview ──────────────────────────────────────

interface ArtifactRowProps {
  artifact: ArtifactFileSummary;
  taskKey: string;
  modalArtifact: ArtifactFileSummary | null;
  onOpenModal: (artifact: ArtifactFileSummary) => void;
  onCloseModal: () => void;
}

function ArtifactRow({
  artifact,
  taskKey,
  modalArtifact,
  onOpenModal,
  onCloseModal,
}: ArtifactRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadPreview() {
    if (preview || artifact.has_secret_warning) return;
    setLoading(true);
    setError(null);
    const result = await getArtifactPreview(taskKey, artifact.name);
    if (result.ok) {
      setPreview(result.data as ArtifactPreview);
    } else {
      setError((result.error as { message: string }).message);
    }
    setLoading(false);
  }

  const kindColorMap: Record<string, string> = {
    mission_contract: "var(--green)",
    executor_log: "var(--blue)",
    validator_log: "var(--yellow)",
    other: "var(--muted)",
  };

  return (
    <>
      <div
        style={{
          borderBottom: "1px solid var(--border-soft)",
        }}
      >
        {/* Row header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "8px",
            padding: "9px 12px",
          }}
        >
          {/* Expand toggle */}
          <button
            onClick={() => {
              setExpanded((v) => !v);
              if (!expanded) loadPreview();
            }}
            disabled={artifact.has_secret_warning}
            title={
              artifact.has_secret_warning
                ? "Preview unavailable — secrets detected"
                : expanded
                ? "Collapse"
                : "Expand"
            }
            style={{
              all: "unset",
              cursor: artifact.has_secret_warning ? "default" : "pointer",
              fontSize: "0.8rem",
              color: artifact.has_secret_warning
                ? "var(--muted-2)"
                : "var(--muted)",
              padding: "2px 6px",
            }}
          >
            {artifact.has_secret_warning
              ? "🔒"
              : expanded
              ? "▼"
              : "▶"}
          </button>

          {/* Name */}
          <code
            style={{
              flex: "1 1 auto",
              fontSize: "0.75rem",
              color: "var(--text)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {artifact.name}
          </code>

          {/* Kind badge */}
          <span
            style={{
              fontSize: "0.65rem",
              color: kindColorMap[artifact.kind] ?? "var(--muted)",
              padding: "1px 5px",
              background: `${kindColorMap[artifact.kind] ?? "var(--muted)"}18`,
              border: `1px solid ${kindColorMap[artifact.kind] ?? "var(--muted)"}44`,
              borderRadius: "999px",
              flexShrink: 0,
            }}
          >
            {artifact.kind}
          </span>

          {/* Size */}
          <span
            style={{
              fontSize: "0.68rem",
              color: "var(--muted-2)",
              flexShrink: 0,
            }}
          >
            {formatBytes(artifact.size_bytes)}
          </span>

          {/* Badges */}
          {artifact.has_secret_warning && (
            <span
              style={{
                fontSize: "0.62rem",
                color: "var(--yellow)",
                padding: "1px 5px",
                background: "rgba(234,179,8,0.1)",
                border: "1px solid rgba(234,179,8,0.25)",
                borderRadius: "999px",
                flexShrink: 0,
              }}
            >
              ⚠ secret
            </span>
          )}
          {preview?.truncated && (
            <span
              style={{
                fontSize: "0.62rem",
                color: "var(--yellow)",
                padding: "1px 5px",
                background: "rgba(234,179,8,0.08)",
                border: "1px solid rgba(234,179,8,0.2)",
                borderRadius: "999px",
                flexShrink: 0,
              }}
            >
              truncated
            </span>
          )}

          {/* Actions */}
          {!artifact.has_secret_warning && (
            <button
              onClick={() => onOpenModal(artifact)}
              style={{
                all: "unset",
                cursor: "pointer",
                fontSize: "0.7rem",
                color: "var(--muted)",
                padding: "2px 6px",
                border: "1px solid var(--border)",
                borderRadius: "6px",
                background: "var(--panel-2)",
                flexShrink: 0,
              }}
              title="Open full preview"
            >
              ⬜ Modal
            </button>
          )}
        </div>

        {/* Inline expanded content */}
        {expanded && (
          <div
            style={{
              padding: "0 12px 10px 48px",
            }}
          >
            {artifact.has_secret_warning ? (
              <div
                style={{
                  padding: "8px 10px",
                  background: "rgba(234,179,8,0.07)",
                  border: "1px solid rgba(234,179,8,0.15)",
                  borderRadius: "8px",
                  fontSize: "0.72rem",
                  color: "var(--yellow)",
                }}
              >
                Preview unavailable — file contains secret-like assignment
                patterns. Open in modal not available.
              </div>
            ) : loading ? (
              <div
                style={{
                  padding: "8px",
                  fontSize: "0.72rem",
                  color: "var(--muted-2)",
                }}
              >
                Loading…
              </div>
            ) : error ? (
              <div
                style={{
                  padding: "8px 10px",
                  background: "rgba(239,68,68,0.07)",
                  border: "1px solid rgba(239,68,68,0.15)",
                  borderRadius: "8px",
                  fontSize: "0.72rem",
                  color: "var(--red)",
                }}
              >
                Error: {error}
              </div>
            ) : preview && preview.content !== null ? (
              <div>
                {preview.truncated && (
                  <div
                    style={{
                      padding: "4px 8px",
                      background: "rgba(234,179,8,0.06)",
                      border: "1px solid rgba(234,179,8,0.12)",
                      borderRadius: "6px",
                      fontSize: "0.68rem",
                      color: "var(--yellow)",
                      marginBottom: "6px",
                    }}
                  >
                    Truncated — showing first 20 KB of{" "}
                    {formatBytes(preview.size_bytes)}.
                  </div>
                )}
                <pre
                  style={{
                    fontSize: "0.68rem",
                    fontFamily: "monospace",
                    color: "var(--muted)",
                    background: "var(--bg)",
                    border: "1px solid var(--border-soft)",
                    borderRadius: "8px",
                    padding: "8px 10px",
                    maxHeight: "240px",
                    overflowY: "auto",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-all",
                    margin: 0,
                  }}
                >
                  {preview.content}
                </pre>
              </div>
            ) : preview && preview.content === null ? (
              <div
                style={{
                  padding: "8px 10px",
                  fontSize: "0.72rem",
                  color: "var(--muted-2)",
                }}
              >
                {preview.preview_reason ?? "Preview not available."}
              </div>
            ) : null}
          </div>
        )}
      </div>

      {/* Modal */}
      {modalArtifact?.name === artifact.name && (
        <ArtifactPreviewModal
          artifact={artifact}
          taskKey={taskKey}
          onClose={onCloseModal}
        />
      )}
    </>
  );
}

// ─── Main ArtifactReviewPanel ────────────────────────────────────────────────

interface ArtifactReviewPanelProps {
  evidence: TaskReviewEvidence;
  taskKey: string;
}

export function ArtifactReviewPanel({
  evidence,
  taskKey,
}: ArtifactReviewPanelProps) {
  const [activeCategory, setActiveCategory] = useState<ArtifactCategory>("all");
  const [modalArtifact, setModalArtifact] = useState<ArtifactFileSummary | null>(
    null
  );

  const artifacts = evidence.artifacts ?? [];

  // Summary counts
  const total = artifacts.length;
  const previewable = artifacts.filter(
    (a) => a.preview_available && !a.has_secret_warning
  ).length;
  const secrets = artifacts.filter((a) => a.has_secret_warning).length;
  const executorLogs = artifacts.filter((a) => a.is_executor_log).length;
  const validatorLogs = artifacts.filter((a) => a.is_validator_log).length;

  // Filtered artifacts
  const filtered =
    activeCategory === "all"
      ? artifacts
      : artifacts.filter((a) => classifyArtifact(a) === activeCategory);

  const categories: ArtifactCategory[] = [
    "all",
    "mission",
    "pi_protocol",
    "executor_logs",
    "validator_logs",
    "prompts",
    "other",
  ];

  const countForCategory = (cat: ArtifactCategory): number => {
    if (cat === "all") return total;
    return artifacts.filter((a) => classifyArtifact(a) === cat).length;
  };

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
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "12px",
          gap: "10px",
          flexWrap: "wrap",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "0.9rem", fontWeight: 760 }}>
          Artifacts
        </h3>
        <div
          style={{
            display: "flex",
            gap: "6px",
            fontSize: "0.68rem",
            color: "var(--muted-2)",
          }}
        >
          <span>{total} total</span>
          <span>·</span>
          <span>{previewable} previewable</span>
          {secrets > 0 && (
            <>
              <span>·</span>
              <span style={{ color: "var(--yellow)" }}>
                {secrets} secrets
              </span>
            </>
          )}
        </div>
      </div>

      {/* Category filter pills */}
      <div
        style={{
          display: "flex",
          gap: "5px",
          flexWrap: "wrap",
          marginBottom: "12px",
        }}
      >
        {categories.map((cat) => {
          const count = countForCategory(cat);
          const isActive = activeCategory === cat;
          const isZero = count === 0;

          return (
            <button
              key={cat}
              onClick={() => !isZero && setActiveCategory(cat)}
              disabled={isZero}
              style={{
                all: "unset",
                cursor: isZero ? "default" : "pointer",
                padding: "4px 10px",
                border: "1px solid",
                borderColor: isActive ? "var(--blue)" : "var(--border)",
                background: isActive
                  ? "rgba(94,106,210,0.1)"
                  : "var(--panel-2)",
                borderRadius: "999px",
                fontSize: "0.7rem",
                fontWeight: 700,
                color: isZero ? "var(--muted-2)" : "var(--muted)",
                opacity: isZero ? 0.5 : 1,
              }}
            >
              {CATEGORY_LABELS[cat]}{" "}
              <span style={{ color: isActive ? "var(--blue)" : "inherit" }}>
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {/* Special viewers */}
      {evidence.mission_contract && evidence.mission_contract.exists && (
        <div style={{ marginBottom: "12px" }}>
          <MissionContractViewer contract={evidence.mission_contract} />
        </div>
      )}

      {artifacts.some((a) => a.name === "pi_mission_plan.json") && (
        <div style={{ marginBottom: "12px" }}>
          <PiMissionPlanViewer taskKey={taskKey} />
        </div>
      )}

      {artifacts.some((a) => a.name === "policy-validate.log") && (
        <div style={{ marginBottom: "12px" }}>
          <PolicyLogViewer
            taskKey={taskKey}
            artifactName="policy-validate.log"
            policyStatus={evidence.policy_status}
          />
        </div>
      )}

      {/* Artifact list */}
      <div
        style={{
          background: "var(--panel-2)",
          border: "1px solid var(--border-soft)",
          borderRadius: "10px",
          overflow: "hidden",
        }}
      >
        {filtered.length === 0 ? (
          <div
            style={{
              padding: "14px",
              textAlign: "center",
              fontSize: "0.8rem",
              color: "var(--muted-2)",
            }}
          >
            No artifacts in this category.
          </div>
        ) : (
          filtered.map((artifact) => (
            <ArtifactRow
              key={artifact.name}
              artifact={artifact}
              taskKey={taskKey}
              modalArtifact={modalArtifact}
              onOpenModal={setModalArtifact}
              onCloseModal={() => setModalArtifact(null)}
            />
          ))
        )}
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