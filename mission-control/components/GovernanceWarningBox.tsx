"use client";

interface GovernanceWarningBoxProps {
  variant?: "info" | "warning" | "critical";
}

const MESSAGES = {
  info: "Mission Control is a governance control and review surface. It does not execute workers directly.",
  warning:
    "Creating a task does not start a worker. Workers run after a Start/Dispatch action is called through the backend API. Human approval is the final gate. No push, merge, or cleanup is performed by the UI.",
  critical:
    "The UI does not execute Pi, OpenCode, or Shell directly. Workers run in isolated backends. Deterministic validators remain required. Human approval cannot be replaced by AI review.",
};

export function GovernanceWarningBox({
  variant = "info",
}: GovernanceWarningBoxProps) {
  const colorMap = {
    info: {
      border: "rgba(90,100,220,0.25)",
      background: "rgba(90,100,220,0.08)",
      text: "var(--blue)",
    },
    warning: {
      border: "rgba(234,179,8,0.3)",
      background: "rgba(234,179,8,0.07)",
      text: "var(--yellow)",
    },
    critical: {
      border: "rgba(239,68,68,0.25)",
      background: "rgba(239,68,68,0.08)",
      text: "var(--red)",
    },
  };

  const style = colorMap[variant];

  return (
    <div
      style={{
        padding: "12px 16px",
        background: style.background,
        border: `1px solid ${style.border}`,
        borderRadius: "10px",
        fontSize: "0.8rem",
        color: style.text,
        lineHeight: 1.55,
      }}
    >
      <strong style={{ display: "block", marginBottom: "4px" }}>
        {variant === "critical" ? "⚠ Governance policy" : "ℹ Note"}
      </strong>
      {MESSAGES[variant]}
    </div>
  );
}

export const EXECUTOR_OPTIONS = [
  { value: "opencode", label: "OpenCode" },
  { value: "pi", label: "Pi (governance mission contract)" },
  { value: "shell", label: "Shell" },
  { value: "manual", label: "Manual" },
] as const;

export const VALIDATOR_OPTIONS = [
  {
    value: "pytest",
    label: "pytest",
    description: "Default — runs project test suite",
    required: true,
  },
  {
    value: "openspec",
    label: "openspec",
    description: "Default — checks spec consistency",
    required: true,
  },
  {
    value: "policy",
    label: "policy",
    description: "Optional — checks governance artifacts/logs",
    required: false,
  },
  {
    value: "typecheck",
    label: "typecheck",
    description: "Optional — runs mypy/typescript type checks",
    required: false,
  },
  {
    value: "lint",
    label: "lint",
    description: "Optional — runs ruff/flake8 checks",
    required: false,
  },
] as const;

export function DefaultValidatorsNote() {
  return (
    <div
      style={{
        padding: "8px 12px",
        background: "rgb(255,255,255,0.025)",
        border: "1px solid var(--border-soft)",
        borderRadius: "8px",
        fontSize: "0.75rem",
        color: "var(--muted)",
        marginTop: "4px",
      }}
    >
      <strong style={{ color: "var(--muted-2)" }}>Note:</strong>{" "}
      <code style={{ fontSize: "0.72rem" }}>pytest</code> and{" "}
      <code style={{ fontSize: "0.72rem" }}>openspec</code> are default
      deterministic validators. Optional validators (policy, typecheck, lint)
      are opt-in. No validator can replace human approval.
    </div>
  );
}