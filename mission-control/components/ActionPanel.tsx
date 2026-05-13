"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  approveTask,
  blockTask,
  rejectTask,
  startTask
} from "../lib/api";
import type { Task } from "../lib/types";
import {
  ActionResultBanner,
  type ActionResultState
} from "./ActionResultBanner";
import { ConfirmActionButton } from "./ConfirmActionButton";
import { StatusBadge } from "./StatusBadge";
import { APPROVE_WARNING, REJECT_WARNING, BLOCK_WARNING } from "../lib/taskState";

const STARTABLE_STATUSES = new Set(["queued", "blocked", "preparing"]);
const APPROVABLE_STATUSES = new Set(["waiting_approval"]);
const REJECTABLE_STATUSES = new Set(["waiting_approval", "blocked"]);
const BLOCKABLE_STATUSES = new Set([
  "queued",
  "preparing",
  "implementing",
  "validating",
  "waiting_approval"
]);

function isAllowed(status: string, allowed: Set<string>): boolean {
  return allowed.has(status);
}

export function ActionPanel({ task }: { task: Task }) {
  const router = useRouter();
  const [result, setResult] = useState<ActionResultState | null>(null);
  const [notes, setNotes] = useState("");
  const [blockedReason, setBlockedReason] = useState("");
  const [executor, setExecutor] = useState("");
  const [model, setModel] = useState("");
  const [validators, setValidators] = useState("pytest");
  const [dryRun, setDryRun] = useState(false);

  const status = task.status;
  const canStart = isAllowed(status, STARTABLE_STATUSES);
  const canApprove = isAllowed(status, APPROVABLE_STATUSES);
  const canReject = isAllowed(status, REJECTABLE_STATUSES);
  const canBlock = isAllowed(status, BLOCKABLE_STATUSES);

  function handleResult(nextResult: ActionResultState) {
    setResult(nextResult);
    if (nextResult.kind === "success") {
      router.refresh();
    }
  }

  function splitValidators(): string[] | undefined {
    const items = validators
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);

    return items.length > 0 ? items : undefined;
  }

  return (
    <section className="section panel">
      <h2>Task Actions</h2>
      <p className="muted">
        Controlled actions only. This UI does not execute Pi, OpenCode, or Shell directly.
        No push, merge, cleanup, worktree deletion, or branch deletion is available here.
      </p>

      <div className="action-status-row">
        <span>Current status:</span>
        <StatusBadge status={task.status} />
      </div>

      <ActionResultBanner result={result} />

      <div className="action-grid">
        <div className="action-card">
          <h3>Start task</h3>
          <p className="muted">
            Dispatch through the backend action API. Disabled for terminal or
            review states.
          </p>

          <label>
            Executor
            <input
              onChange={(event) => setExecutor(event.target.value)}
              placeholder="default backend executor"
              value={executor}
            />
          </label>

          <label>
            Model
            <input
              onChange={(event) => setModel(event.target.value)}
              placeholder="default backend model"
              value={model}
            />
          </label>

          <label>
            Validators, comma-separated
            <input
              onChange={(event) => setValidators(event.target.value)}
              placeholder="pytest"
              value={validators}
            />
          </label>

          <label className="checkbox-label">
            <input
              checked={dryRun}
              onChange={(event) => setDryRun(event.target.checked)}
              type="checkbox"
            />
            Dry run
          </label>

          <ConfirmActionButton
            confirmMessage={`Start task ${task.task_key} through the backend action API?`}
            disabled={!canStart}
            label="Start task"
            onConfirm={() =>
              startTask(task.task_key, {
                validators: splitValidators(),
                executor: executor.trim() || undefined,
                model: model.trim() || undefined,
                dry_run: dryRun
              })
            }
            onResult={handleResult}
          />

          {!canStart ? (
            <p className="muted">Start is disabled for status: {status}</p>
          ) : null}
        </div>

        <div className="action-card">
          <h3>Approve / Reject</h3>
          <p className="muted">
            Records a human decision through the backend action API.
          </p>

          <div
            style={{
              padding: "8px 12px",
              marginBottom: "10px",
              background: "rgba(234,179,8,0.08)",
              border: "1px solid rgba(234,179,8,0.25)",
              borderRadius: "8px",
              fontSize: "0.78rem",
              color: "var(--yellow)",
            }}
          >
            <strong>Human approval is the final gate.</strong> Worker cannot self-approve.
            Approving does not push, merge, or cleanup.
          </div>

          <label>
            Notes
            <textarea
              onChange={(event) => setNotes(event.target.value)}
              placeholder="Optional review notes"
              value={notes}
            />
          </label>

          <div className="button-row">
            <ConfirmActionButton
              confirmMessage={APPROVE_WARNING + `\n\nProceed to approve ${task.task_key}?`}
              disabled={!canApprove}
              label="Approve task"
              onConfirm={() =>
                approveTask(task.task_key, {
                  decided_by: "human",
                  notes: notes.trim() || undefined
                })
              }
              onResult={handleResult}
            />

            <ConfirmActionButton
              confirmMessage={REJECT_WARNING + `\n\nProceed to reject ${task.task_key}?`}
              danger
              disabled={!canReject || notes.trim() === ""}
              label="Reject task"
              onConfirm={() =>
                rejectTask(task.task_key, {
                  decided_by: "human",
                  notes: notes.trim() || undefined
                })
              }
              onResult={handleResult}
            />
          </div>

          {!canApprove && !canReject ? (
            <p className="muted">
              Approve/reject is disabled for status: {status}
            </p>
          ) : null}
        </div>

        <div className="action-card">
          <h3>Block task</h3>
          <p className="muted">
            Manually blocks a non-terminal task with an explicit reason.
          </p>

          <div
            style={{
              padding: "8px 12px",
              marginBottom: "10px",
              background: "rgba(239,68,68,0.08)",
              border: "1px solid rgba(239,68,68,0.25)",
              borderRadius: "8px",
              fontSize: "0.78rem",
              color: "var(--red)",
            }}
          >
            Blocking does not delete or clean up any artifacts or files.
          </div>

          <label>
            Blocked reason
            <textarea
              onChange={(event) => setBlockedReason(event.target.value)}
              placeholder="Required reason"
              value={blockedReason}
            />
          </label>

          <ConfirmActionButton
            confirmMessage={BLOCK_WARNING + `\n\nProceed to block ${task.task_key}?`}
            danger
            disabled={!canBlock || blockedReason.trim() === ""}
            label="Block task"
            onConfirm={() =>
              blockTask(task.task_key, {
                blocked_reason: blockedReason.trim()
              })
            }
            onResult={handleResult}
          />

          {!canBlock ? (
            <p className="muted">Block is disabled for status: {status}</p>
          ) : null}
        </div>
      </div>
    </section>
  );
}
