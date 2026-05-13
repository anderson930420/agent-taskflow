"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { createTask } from "../lib/api";
import type { ActionResponse, ApiFailure, CreateTaskRequest, Task } from "../lib/types";
import { ActionResultBanner, type ActionResultState } from "./ActionResultBanner";
import {
  DefaultValidatorsNote,
  EXECUTOR_OPTIONS,
  GovernanceWarningBox,
  VALIDATOR_OPTIONS,
} from "./GovernanceWarningBox";

function emptyToUndefined(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

function parseOptionalInteger(value: string): number | undefined {
  const trimmed = value.trim();
  if (trimmed === "") {
    return undefined;
  }
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isNaN(parsed) ? undefined : parsed;
}

function isAbsolutePath(value: string): boolean {
  return value.startsWith("/");
}

function failure(message: string): ActionResultState {
  return { kind: "failure", error: { message } };
}

export function CreateTaskForm() {
  const router = useRouter();
  const [taskKey, setTaskKey] = useState("");
  const [project, setProject] = useState("agent-taskflow");
  const [repoPath, setRepoPath] = useState("/home/ubuntu/agent-taskflow");
  const [worktreePath, setWorktreePath] = useState("");
  const [artifactDir, setArtifactDir] = useState("");
  const [executor, setExecutor] = useState("pi");
  const [model, setModel] = useState("");
  const [validator, setValidator] = useState("pytest");
  const [title, setTitle] = useState("");
  const [board, setBoard] = useState("");
  const [hermesTaskId, setHermesTaskId] = useState("");
  const [branch, setBranch] = useState("");
  const [baseBranch, setBaseBranch] = useState("main");
  const [prUrl, setPrUrl] = useState("");
  const [prNumber, setPrNumber] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ActionResultState | null>(null);

  // Auto-generate worktree_path when task_key or repo_path changes
  useEffect(() => {
    if (taskKey && repoPath && !worktreePath) {
      setWorktreePath(`${repoPath}/.worktrees/${taskKey}`);
    }
  }, [taskKey, repoPath, worktreePath]);

  // Auto-generate artifact_dir when task_key or project changes
  useEffect(() => {
    if (taskKey && project && !artifactDir) {
      setArtifactDir(`/tmp/agent-taskflow-${project}-artifacts/${taskKey}`);
    }
  }, [taskKey, project, artifactDir]);

  // Auto-generate branch when task_key changes
  useEffect(() => {
    if (taskKey && !branch) {
      setBranch(`task/${taskKey}`);
    }
  }, [taskKey, branch]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedTaskKey = taskKey.trim();
    const normalizedProject = project.trim();
    const normalizedRepoPath = repoPath.trim();
    const normalizedWorktreePath = worktreePath.trim();
    const normalizedArtifactDir = artifactDir.trim();

    if (!normalizedTaskKey) {
      setResult(failure("task_key is required."));
      return;
    }
    if (!/^[A-Za-z0-9_-]+$/.test(normalizedTaskKey)) {
      setResult(
        failure(
          "task_key must contain only letters, digits, hyphens, and underscores."
        )
      );
      return;
    }
    if (!normalizedProject) {
      setResult(failure("project is required."));
      return;
    }
    if (!isAbsolutePath(normalizedRepoPath)) {
      setResult(failure("repo_path must be an absolute path (starts with /)."));
      return;
    }
    if (!isAbsolutePath(normalizedWorktreePath)) {
      setResult(
        failure("worktree_path must be an absolute path (starts with /).")
      );
      return;
    }
    if (!isAbsolutePath(normalizedArtifactDir)) {
      setResult(
        failure("artifact_dir must be an absolute path (starts with /).")
      );
      return;
    }

    const confirmed = window.confirm(
      `Create task ${normalizedTaskKey}?\n\n` +
        `This only creates a local mirrored task record.\n` +
        `It does NOT start a worker, push, merge, or cleanup.\n` +
        `Workers run through the backend dispatcher after you call Start/Dispatch.`
    );
    if (!confirmed) {
      return;
    }

    setSubmitting(true);
    try {
      const response = await createTask({
        task_key: normalizedTaskKey,
        project: normalizedProject,
        repo_path: normalizedRepoPath,
        worktree_path: normalizedWorktreePath,
        artifact_dir: normalizedArtifactDir,
        executor: emptyToUndefined(executor),
        model: emptyToUndefined(model),
        validator: emptyToUndefined(validator),
        title: emptyToUndefined(title),
        board: emptyToUndefined(board),
        hermes_task_id: emptyToUndefined(hermesTaskId),
        branch: emptyToUndefined(branch),
        base_branch: emptyToUndefined(baseBranch),
        pr_url: emptyToUndefined(prUrl),
        pr_number: parseOptionalInteger(prNumber),
      });

      if (response.ok) {
        const actionResponse = response.data as ActionResponse<Task>;
        setResult({ kind: "success", response: actionResponse });
        const tk = actionResponse.task_key;
        if (tk) {
          setTimeout(() => {
            router.push(`/tasks/${encodeURIComponent(tk)}`);
            router.refresh();
          }, 1200);
        }
      } else {
        setResult({
          kind: "failure",
          action: "create",
          error: response.error as ApiFailure,
        });
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={handleSubmit}>
      <ActionResultBanner result={result} />

      <GovernanceWarningBox variant="warning" />

      {/* Required fields */}
      <label>
        Task key *
        <input
          onChange={(e) => setTaskKey(e.target.value)}
          placeholder="AT-0011"
          required
          value={taskKey}
        />
      </label>

      <label>
        Project *
        <input onChange={(e) => setProject(e.target.value)} required value={project} />
      </label>

      <label>
        Repo path *
        <input
          onChange={(e) => setRepoPath(e.target.value)}
          placeholder="/home/ubuntu/agent-taskflow"
          required
          value={repoPath}
        />
      </label>

      <label>
        Worktree path *
        <input
          onChange={(e) => setWorktreePath(e.target.value)}
          placeholder="/home/ubuntu/agent-taskflow/.worktrees/AT-0011"
          required
          value={worktreePath}
        />
      </label>

      <label>
        Artifact dir *
        <input
          onChange={(e) => setArtifactDir(e.target.value)}
          placeholder="/home/ubuntu/.agent-taskflow/artifacts/AT-0011"
          required
          value={artifactDir}
        />
      </label>

      {/* Executor */}
      <label>
        Executor
        <select
          onChange={(e) => setExecutor(e.target.value)}
          value={executor}
          style={{
            width: "100%",
            minHeight: "40px",
            padding: "9px 11px",
            color: "var(--text)",
            background: "#0f1218",
            border: "1px solid var(--border)",
            borderRadius: "10px",
          }}
        >
          {EXECUTOR_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
        <span className="field-hint">
          {executor === "pi"
            ? "Pi uses mission contract / mission plan when available. Does not self-approve."
            : "Executor backend. Worker runs after Start/Dispatch action."}
        </span>
      </label>

      {/* Model */}
      <label>
        Model
        <input
          onChange={(e) => setModel(e.target.value)}
          placeholder="optional"
          value={model}
        />
      </label>

      {/* Validators */}
      <label>
        Validator
        <select
          onChange={(e) => setValidator(e.target.value)}
          value={validator}
          style={{
            width: "100%",
            minHeight: "40px",
            padding: "9px 11px",
            color: "var(--text)",
            background: "#0f1218",
            border: "1px solid var(--border)",
            borderRadius: "10px",
          }}
        >
          {VALIDATOR_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
              {opt.required ? " (default)" : " (optional)"}
            </option>
          ))}
        </select>
        <span className="field-hint">
          {VALIDATOR_OPTIONS.find((o) => o.value === validator)?.description}
        </span>
      </label>

      <DefaultValidatorsNote />

      {/* Title */}
      <label>
        Title
        <input
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Optional title"
          value={title}
        />
      </label>

      <label>
        Board
        <input
          onChange={(e) => setBoard(e.target.value)}
          placeholder="Defaults to project"
          value={board}
        />
      </label>

      <label>
        Hermes task id
        <input
          onChange={(e) => setHermesTaskId(e.target.value)}
          placeholder="Optional"
          value={hermesTaskId}
        />
      </label>

      <label>
        Branch
        <input
          onChange={(e) => setBranch(e.target.value)}
          placeholder="Defaults to task/<task_key>"
          value={branch}
        />
      </label>

      <label>
        Base branch
        <input
          onChange={(e) => setBaseBranch(e.target.value)}
          placeholder="main"
          value={baseBranch}
        />
      </label>

      <label>
        PR URL
        <input
          onChange={(e) => setPrUrl(e.target.value)}
          placeholder="Optional metadata only"
          value={prUrl}
        />
      </label>

      <label>
        PR number
        <input
          inputMode="numeric"
          onChange={(e) => setPrNumber(e.target.value)}
          placeholder="Optional"
          value={prNumber}
        />
      </label>

      <div className="form-actions">
        <button className="button" disabled={submitting} type="submit">
          {submitting ? "Creating..." : "Create task"}
        </button>
      </div>
    </form>
  );
}