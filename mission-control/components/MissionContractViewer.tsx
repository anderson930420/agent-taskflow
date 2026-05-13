"use client";

import type { ArtifactFileSummary, MissionContractSummary } from "../lib/types";

interface MissionContractViewerProps {
  contract: MissionContractSummary;
}

function StatusBadge({ status }: { status: string }) {
  const colorMap: Record<string, string> = {
    present: "var(--green)",
    missing: "var(--muted-2)",
    invalid: "var(--red)",
  };
  const color = colorMap[status] ?? "var(--muted)";
  return (
    <span
      style={{
        padding: "2px 8px",
        background: `${color}18`,
        border: `1px solid ${color}44`,
        borderRadius: "999px",
        fontSize: "0.72rem",
        fontWeight: 750,
        color,
        textTransform: "capitalize",
      }}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

function FieldRow({
  label,
  value,
  highlight = false,
}: {
  label: string;
  value: string | null | undefined;
  highlight?: boolean;
}) {
  if (!value) return null;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: "8px",
        padding: "5px 0",
        borderBottom: "1px solid var(--border-soft)",
      }}
    >
      <span
        style={{
          fontSize: "0.75rem",
          color: "var(--muted-2)",
          minWidth: "140px",
          flexShrink: 0,
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontSize: "0.75rem",
          color: highlight ? "var(--yellow)" : "var(--text)",
          fontWeight: highlight ? 700 : 400,
          wordBreak: "break-all",
        }}
      >
        {value}
      </span>
    </div>
  );
}

export function MissionContractViewer({ contract }: MissionContractViewerProps) {
  if (!contract.exists && contract.status === "missing") {
    return (
      <div
        style={{
          padding: "14px 16px",
          background: "var(--panel-2)",
          border: "1px solid var(--border-soft)",
          borderRadius: "12px",
          fontSize: "0.82rem",
          color: "var(--muted-2)",
          textAlign: "center",
        }}
      >
        No mission contract found. The task may not have generated one yet.
      </div>
    );
  }

  if (!contract.exists && contract.status === "invalid") {
    return (
      <div
        style={{
          padding: "14px 16px",
          background: "rgba(239,68,68,0.08)",
          border: "1px solid rgba(239,68,68,0.25)",
          borderRadius: "12px",
          fontSize: "0.82rem",
          color: "var(--red)",
        }}
      >
        <strong>Invalid mission contract:</strong> {contract.error}
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
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "14px",
          gap: "10px",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "0.9rem", fontWeight: 760 }}>
          Mission Contract
        </h3>
        <StatusBadge status={contract.status ?? "unknown"} />
      </div>

      {/* Schema version */}
      {contract.schema_version && (
        <div
          style={{
            fontSize: "0.72rem",
            color: "var(--muted-2)",
            marginBottom: "12px",
          }}
        >
          Schema version:{" "}
          <code style={{ fontSize: "0.7rem" }}>{contract.schema_version}</code>
        </div>
      )}

      {/* Fields */}
      <div>
        <FieldRow label="Task key" value={contract.task_key} />
        <FieldRow label="Executor" value={contract.executor} />
        <FieldRow label="Goal" value={contract.goal} />
        <FieldRow
          label="Human approval"
          value={String(contract.human_approval_required ?? "unknown")}
          highlight={contract.human_approval_required === true}
        />
      </div>

      {/* Required validators */}
      {contract.required_validators.length > 0 && (
        <div style={{ marginTop: "10px" }}>
          <div style={{ fontSize: "0.72rem", color: "var(--muted-2)", marginBottom: "4px" }}>
            Required validators
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
            {contract.required_validators.map((v) => (
              <span
                key={v}
                style={{
                  padding: "2px 8px",
                  background: "rgba(94,106,210,0.12)",
                  border: "1px solid rgba(94,106,210,0.3)",
                  borderRadius: "999px",
                  fontSize: "0.72rem",
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

      {/* Forbidden actions */}
      {contract.forbidden_actions.length > 0 && (
        <div style={{ marginTop: "10px" }}>
          <div
            style={{
              fontSize: "0.72rem",
              color: "var(--red)",
              fontWeight: 700,
              marginBottom: "4px",
            }}
          >
            Forbidden actions
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
            {contract.forbidden_actions.map((a) => (
              <div
                key={a}
                style={{
                  fontSize: "0.72rem",
                  color: "var(--red)",
                  padding: "3px 8px",
                  background: "rgba(239,68,68,0.07)",
                  border: "1px solid rgba(239,68,68,0.2)",
                  borderRadius: "6px",
                }}
              >
                ✕ {a}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Expected artifacts */}
      {contract.expected_artifacts.length > 0 && (
        <div style={{ marginTop: "10px" }}>
          <div style={{ fontSize: "0.72rem", color: "var(--muted-2)", marginBottom: "4px" }}>
            Expected artifacts
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
            {contract.expected_artifacts.map((a) => (
              <div
                key={a}
                style={{
                  fontSize: "0.72rem",
                  color: "var(--muted)",
                  padding: "3px 8px",
                  background: "rgb(255,255,255,0.025)",
                  border: "1px solid var(--border-soft)",
                  borderRadius: "6px",
                }}
              >
                {a}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Governance rules */}
      {contract.governance_rules.length > 0 && (
        <div style={{ marginTop: "10px" }}>
          <div style={{ fontSize: "0.72rem", color: "var(--muted-2)", marginBottom: "4px" }}>
            Governance rules
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
            {contract.governance_rules.map((r, i) => (
              <div
                key={i}
                style={{
                  fontSize: "0.72rem",
                  color: "var(--muted)",
                  padding: "3px 8px",
                  background: "rgb(255,255,255,0.025)",
                  border: "1px solid var(--border-soft)",
                  borderRadius: "6px",
                }}
              >
                {r}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}