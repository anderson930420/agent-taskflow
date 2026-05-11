"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { createTask } from "../lib/api";
import type { ActionResponse, ApiFailure, CreateTaskRequest, Task } from "../lib/types";
import { ActionResultBanner, type ActionResultState } from "./ActionResultBanner";

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
  if (Number.isNaN(parsed)) {
    return undefined;
  }

  return parsed;
}

function isAbsolutePath(value: string): boolean {
  return value.startsWith("/");
}

function failure(message: string): ActionResultState {
  return {
    kind: "failure",
    error: { message }
  };
}

export function CreateTaskForm() {
  const router = useRouter();
  const [taskKey, setTaskKey] = useState("");
  const [project, setProject] = useState("agent-taskflow");
  const [repoPath, setRepoPath] = useState("/home/ubuntu/agent-taskflow");
  const [worktreePath, setWorktreePath] = useState("");
  const [artifactDir, setArtifactDir] = useState("");
  const [executor, setExecutor] = useState("opencode");
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

    if (!normalizedProject) {
      setResult(failure("project is required."));
      return;
    }

    if (!isAbsolutePath(normalizedRepoPath)) {
      setResult(failure("repo_path must be an absolute path."));
      return;
    }

    if (!isAbsolutePath(normalizedWorktreePath)) {
      setResult(failure("worktree_path must be an absolute path."));
      return;
    }

    if (!isAbsolutePath(normalizedArtifactDir)) {
      setResult(failure("artifact_dir must be an absolute path."));
      return;
    }

    const payload: CreateTaskRequest = {
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
      pr_number: parseOptionalInteger(prNumber)
    };

    const confirmed = window.confirm(
      `Create task ${normalizedTaskKey}? This only creates a local mirrored task record and does not start a worker.`
    );

    if (!confirmed) {
      return;
    }

    setSubmitting(true);

    try {
      const response = await createTask(payload);

      if (response.ok) {
        const actionResponse = response.data as ActionResponse<Task>;
        setResult({ kind: "success", response: actionResponse });

        if (actionResponse.task_key) {
          router.push(`/tasks/${encodeURIComponent(actionResponse.task_key)}`);
          router.refresh();
        }
      } else {
        setResult({
          kind: "failure",
          action: "create",
          error: response.error as ApiFailure
        });
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={handleSubmit}>
      <ActionResultBanner result={result} />

      <label>
        Task key *
        <input
          onChange={(event) => setTaskKey(event.target.value)}
          placeholder="AT-0011"
          required
          value={taskKey}
        />
      </label>

      <label>
        Project *
        <input
          onChange={(event) => setProject(event.target.value)}
          required
          value={project}
        />
      </label>

      <label>
        Repo path *
        <input
          onChange={(event) => setRepoPath(event.target.value)}
          required
          value={repoPath}
        />
      </label>

      <label>
        Worktree path *
        <input
          onChange={(event) => setWorktreePath(event.target.value)}
          placeholder="/home/ubuntu/agent-taskflow/.worktrees/AT-0011"
          required
          value={worktreePath}
        />
      </label>

      <label>
        Artifact dir *
        <input
          onChange={(event) => setArtifactDir(event.target.value)}
          placeholder="/home/ubuntu/.agent-taskflow/artifacts/AT-0011"
          required
          value={artifactDir}
        />
      </label>

      <label>
        Executor
        <input
          onChange={(event) => setExecutor(event.target.value)}
          placeholder="opencode"
          value={executor}
        />
      </label>

      <label>
        Model
        <input
          onChange={(event) => setModel(event.target.value)}
          placeholder="optional"
          value={model}
        />
      </label>

      <label>
        Validator
        <input
          onChange={(event) => setValidator(event.target.value)}
          placeholder="pytest"
          value={validator}
        />
      </label>

      <label>
        Title
        <input
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Optional title"
          value={title}
        />
      </label>

      <label>
        Board
        <input
          onChange={(event) => setBoard(event.target.value)}
          placeholder="Defaults to project"
          value={board}
        />
      </label>

      <label>
        Hermes task id
        <input
          onChange={(event) => setHermesTaskId(event.target.value)}
          placeholder="Optional"
          value={hermesTaskId}
        />
      </label>

      <label>
        Branch
        <input
          onChange={(event) => setBranch(event.target.value)}
          placeholder="Defaults to task/<task_key>"
          value={branch}
        />
      </label>

      <label>
        Base branch
        <input
          onChange={(event) => setBaseBranch(event.target.value)}
          placeholder="main"
          value={baseBranch}
        />
      </label>

      <label>
        PR URL
        <input
          onChange={(event) => setPrUrl(event.target.value)}
          placeholder="Optional metadata only"
          value={prUrl}
        />
      </label>

      <label>
        PR number
        <input
          inputMode="numeric"
          onChange={(event) => setPrNumber(event.target.value)}
          placeholder="Optional metadata only"
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
