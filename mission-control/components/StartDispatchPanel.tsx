"use client";

import { useState } from "react";
import { startTask } from "../lib/api";
import type { ActionResponse, ApiFailure, StartTaskRequest } from "../lib/types";
import { DEFAULT_VALIDATORS } from "../lib/taskState";
import { VALIDATOR_OPTIONS } from "./GovernanceWarningBox";

const DISPATCH_WARNING =
  "Dispatching asks the agent-taskflow backend to run the task executor. " +
  "The UI does NOT execute Pi, OpenCode, or Shell directly. " +
  "Workers cannot approve or push. Deterministic validators are always required. " +
  "Human approval is the final gate. " +
  "This does NOT push, merge, or cleanup.";

interface StartDispatchPanelProps {
  taskKey: string;
  currentStatus: string;
  currentExecutor?: string | null;
  currentModel?: string | null;
}

export function StartDispatchPanel({
  taskKey,
  currentStatus,
  currentExecutor,
  currentModel,
}: StartDispatchPanelProps) {
  const [showOptions, setShowOptions] = useState(false);
  const [executor, setExecutor] = useState(currentExecutor ?? "opencode");
  const [model, setModel] = useState(currentModel ?? "");
  const [selectedValidators, setSelectedValidators] = useState<string[]>(
    Array.from(DEFAULT_VALIDATORS)
  );
  const [dryRun, setDryRun] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{
    ok: boolean;
    message: string;
    status?: string | null | undefined;
  } | null>(null);

  const canStart =
    currentStatus === "queued" ||
    currentStatus === "blocked" ||
    currentStatus === "preparing";

  function toggleValidator(value: string) {
    setSelectedValidators((prev) =>
      prev.includes(value)
        ? prev.filter((v) => v !== value)
        : [...prev, value]
    );
  }

  async function handleDispatch() {
    if (!canStart) return;

    const confirmed = window.confirm(
      DISPATCH_WARNING + `\n\nProceed to dispatch task ${taskKey}?`
    );
    if (!confirmed) return;

    setSubmitting(true);
    setResult(null);

    try {
      const payload: StartTaskRequest = {
        executor: executor || undefined,
        model: model.trim() || undefined,
        validators:
          selectedValidators.length > 0 ? selectedValidators : undefined,
        dry_run: dryRun,
      };

      const response = await startTask(taskKey, payload);

      if (response.ok) {
        const ar = response.data as ActionResponse;
        setResult({
          ok: true,
          message: ar.message ?? "Task dispatched",
          status: ar.status ?? undefined,
        });
      } else {
        const err = response.error as ApiFailure;
        setResult({ ok: false, message: err.message });
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (!canStart) {
    if (currentStatus === "waiting_approval") {
      return (
        <div
          style={{
            padding: "12px 16px",
            background: "rgba(234,179,8,0.07)",
            border: "1px solid rgba(234,179,8,0.25)",
            borderRadius: "10px",
            fontSize: "0.82rem",
            color: "var(--yellow)",
          }}
        >
          <strong>Task is waiting for human review.</strong> Use the{" "}
          <strong>Approve / Reject</strong> action below to record your
          decision. Approval is the final gate — no worker can approve itself.
        </div>
      );
    }

    return (
      <div
        style={{
          padding: "10px 14px",
          background: "var(--panel)",
          border: "1px solid var(--border-soft)",
          borderRadius: "10px",
          fontSize: "0.8rem",
          color: "var(--muted-2)",
        }}
      >
        Dispatch is not available for tasks in{" "}
        <strong style={{ color: "var(--muted)" }}>{currentStatus}</strong> state.
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
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "10px",
          gap: "10px",
          flexWrap: "wrap",
        }}
      >
        <div>
          <h3 style={{ margin: "0 0 4px", fontSize: "0.92rem" }}>
            Start / Dispatch
          </h3>
          <p style={{ margin: 0, fontSize: "0.78rem", color: "var(--muted)" }}>
            Calls backend dispatcher. UI does not execute Pi/OpenCode/Shell.
          </p>
        </div>
        <button
          className="button"
          onClick={() => setShowOptions((v) => !v)}
          style={{ fontSize: "0.78rem", padding: "5px 10px" }}
        >
          {showOptions ? "Hide options" : "Options"}
        </button>
      </div>

      {/* Dispatch options */}
      {showOptions && (
        <div
          style={{
            padding: "12px",
            background: "rgb(255,255,255,0.025)",
            border: "1px solid var(--border-soft)",
            borderRadius: "10px",
            marginBottom: "12px",
            display: "flex",
            flexDirection: "column",
            gap: "10px",
          }}
        >
          <label style={{ fontSize: "0.8rem", color: "var(--muted)" }}>
            Executor
            <select
              onChange={(e) => setExecutor(e.target.value)}
              value={executor}
              style={{
                width: "100%",
                marginTop: "3px",
                minHeight: "36px",
                padding: "7px 9px",
                color: "var(--text)",
                background: "#0f1218",
                border: "1px solid var(--border)",
                borderRadius: "8px",
              }}
            >
              <option value="opencode">OpenCode</option>
              <option value="pi">Pi (mission contract)</option>
              <option value="shell">Shell</option>
              <option value="manual">Manual</option>
            </select>
          </label>

          <label style={{ fontSize: "0.8rem", color: "var(--muted)" }}>
            Model
            <input
              onChange={(e) => setModel(e.target.value)}
              placeholder="optional"
              value={model}
              style={{ marginTop: "3px" }}
            />
          </label>

          <div style={{ fontSize: "0.8rem", color: "var(--muted)" }}>
            Validators
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "5px",
                marginTop: "5px",
              }}
            >
              {VALIDATOR_OPTIONS.map((opt) => (
                <label
                  key={opt.value}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "7px",
                    cursor: "pointer",
                    fontSize: "0.78rem",
                    color: "var(--text)",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={selectedValidators.includes(opt.value)}
                    disabled={opt.required}
                    onChange={() => toggleValidator(opt.value)}
                  />
                  <code style={{ fontSize: "0.75rem" }}>{opt.label}</code>
                  {opt.required && (
                    <span
                      style={{
                        fontSize: "0.68rem",
                        color: "var(--muted-2)",
                        fontStyle: "italic",
                      }}
                    >
                      (default, always on)
                    </span>
                  )}
                  {!opt.required && (
                    <span
                      style={{
                        fontSize: "0.68rem",
                        color: "var(--muted-2)",
                      }}
                    >
                      — {opt.description}
                    </span>
                  )}
                </label>
              ))}
            </div>
          </div>

          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: "7px",
              cursor: "pointer",
              fontSize: "0.78rem",
              color: "var(--muted)",
            }}
          >
            <input
              type="checkbox"
              checked={dryRun}
              onChange={(e) => setDryRun(e.target.checked)}
            />
            Dry run
          </label>
        </div>
      )}

      {/* Result banner */}
      {result && (
        <div
          style={{
            padding: "10px 14px",
            background: result.ok
              ? "rgba(38,162,105,0.08)"
              : "rgba(239,68,68,0.08)",
            border: `1px solid ${
              result.ok ? "rgba(38,162,105,0.3)" : "rgba(239,68,68,0.3)"
            }`,
            borderRadius: "8px",
            fontSize: "0.8rem",
            color: result.ok ? "var(--green)" : "var(--red)",
            marginBottom: "10px",
          }}
        >
          <strong>{result.ok ? "Success" : "Failed"}:</strong> {result.message}
          {result.status && (
            <>
              {" "}· status: <code>{result.status}</code>
            </>
          )}
        </div>
      )}

      <button
        className="button"
        disabled={submitting}
        onClick={handleDispatch}
        style={{
          background: "var(--blue)",
          borderColor: "var(--blue)",
          fontWeight: 750,
        }}
      >
        {submitting ? "Dispatching…" : "Dispatch task"}
      </button>
    </div>
  );
}