"use client";

import type { TaskReviewEvidence, ValidatorResultSummary } from "../lib/types";

interface ValidatorSummaryCardProps {
  evidence: TaskReviewEvidence | null;
  loading?: boolean;
}

function ValidatorRow({ result }: { result: ValidatorResultSummary }) {
  const status = result.status ?? "unknown";
  const colorMap: Record<string, string> = {
    passed: "var(--green)",
    failed: "var(--red)",
    blocked: "var(--yellow)",
    skipped: "var(--muted-2)",
    unknown: "var(--muted)",
  };
  const color = colorMap[status] ?? "var(--muted)";

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "7px 10px",
        borderBottom: "1px solid var(--border-soft)",
        gap: "10px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <span
          style={{
            width: "8px",
            height: "8px",
            borderRadius: "999px",
            background: color,
            flexShrink: 0,
          }}
        />
        <code
          style={{
            fontSize: "0.78rem",
            color: "var(--text)",
            fontWeight: 600,
          }}
        >
          {result.validator ?? "unknown"}
        </code>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
        {result.exit_code !== null && result.exit_code !== undefined && (
          <span
            style={{
              fontSize: "0.7rem",
              color: "var(--muted-2)",
              fontFamily: "monospace",
            }}
          >
            exit {result.exit_code}
          </span>
        )}
        <span
          style={{
            fontSize: "0.72rem",
            color,
            fontWeight: 700,
            textTransform: "capitalize",
          }}
        >
          {status}
        </span>
      </div>
    </div>
  );
}

function PolicyBanner({ evidence }: { evidence: TaskReviewEvidence }) {
  const { policy_status, policy_warnings } = evidence;

  const colorMap: Record<string, string> = {
    passed: "var(--green)",
    failed: "var(--red)",
    not_run: "var(--yellow)",
    not_required: "var(--muted-2)",
    unknown: "var(--muted)",
  };
  const color = colorMap[policy_status] ?? "var(--muted)";

  return (
    <div
      style={{
        padding: "10px 14px",
        background: `${color}14`,
        border: `1px solid ${color}44`,
        borderRadius: "10px",
        marginBottom: "12px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "4px",
        }}
      >
        <span
          style={{
            fontSize: "0.75rem",
            fontWeight: 750,
            color: "var(--text)",
          }}
        >
          Policy check
        </span>
        <span
          style={{
            fontSize: "0.72rem",
            color,
            fontWeight: 700,
            textTransform: "capitalize",
          }}
        >
          {policy_status.replace(/_/g, " ")}
        </span>
      </div>

      {policy_warnings.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
          {policy_warnings.map((w, i) => (
            <div
              key={i}
              style={{
                fontSize: "0.76rem",
                color: color,
                padding: "4px 8px",
                background: `${color}0a`,
                borderRadius: "6px",
              }}
            >
              {w}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ValidatorSummaryCard({
  evidence,
  loading = false,
}: ValidatorSummaryCardProps) {
  if (loading) {
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
            height: "16px",
            width: "120px",
            background: "var(--border)",
            borderRadius: "6px",
            marginBottom: "12px",
            opacity: 0.5,
          }}
        />
        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              style={{
                height: "12px",
                background: "var(--border-soft)",
                borderRadius: "6px",
                width: `${60 + i * 10}%`,
              }}
            />
          ))}
        </div>
      </div>
    );
  }

  if (!evidence) {
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
        No validator evidence loaded yet.
      </div>
    );
  }

  const results = evidence.validator_results ?? [];
  const passed = results.filter((r) => r.status === "passed").length;
  const failed = results.filter((r) => r.status === "failed").length;
  const blocked = results.filter((r) => r.status === "blocked").length;
  const other = results.length - passed - failed - blocked;

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
        }}
      >
        <h3 style={{ margin: 0, fontSize: "0.9rem", fontWeight: 760 }}>
          Validator Results
        </h3>
        <div
          style={{
            display: "flex",
            gap: "6px",
            fontSize: "0.72rem",
          }}
        >
          {passed > 0 && (
            <span
              style={{
                padding: "2px 7px",
                background: "rgba(38,162,105,0.12)",
                border: "1px solid rgba(38,162,105,0.3)",
                borderRadius: "999px",
                color: "var(--green)",
                fontWeight: 700,
              }}
            >
              {passed} passed
            </span>
          )}
          {failed > 0 && (
            <span
              style={{
                padding: "2px 7px",
                background: "rgba(239,68,68,0.1)",
                border: "1px solid rgba(239,68,68,0.25)",
                borderRadius: "999px",
                color: "var(--red)",
                fontWeight: 700,
              }}
            >
              {failed} failed
            </span>
          )}
          {blocked > 0 && (
            <span
              style={{
                padding: "2px 7px",
                background: "rgba(234,179,8,0.1)",
                border: "1px solid rgba(234,179,8,0.2)",
                borderRadius: "999px",
                color: "var(--yellow)",
                fontWeight: 700,
              }}
            >
              {blocked} blocked
            </span>
          )}
          {other > 0 && (
            <span
              style={{
                padding: "2px 7px",
                background: "var(--panel-2)",
                border: "1px solid var(--border)",
                borderRadius: "999px",
                color: "var(--muted-2)",
                fontWeight: 700,
              }}
            >
              {other} other
            </span>
          )}
        </div>
      </div>

      {/* Policy banner */}
      <PolicyBanner evidence={evidence} />

      {/* Validator rows */}
      {results.length === 0 ? (
        <div
          style={{
            fontSize: "0.8rem",
            color: "var(--muted-2)",
            textAlign: "center",
            padding: "8px",
          }}
        >
          No validator results recorded yet.
        </div>
      ) : (
        results.map((r) => (
          <ValidatorRow key={r.validator ?? "unknown"} result={r} />
        ))
      )}
    </div>
  );
}