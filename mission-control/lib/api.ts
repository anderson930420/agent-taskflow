import type {
  ApiResult,
  ApprovalDecision,
  Artifact,
  DetailResponse,
  ExecutorRun,
  ListResponse,
  Project,
  Task,
  TaskDetailBundle,
  ValidationResult
} from "./types";

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_AGENT_TASKFLOW_API_BASE_URL?.replace(/\/+$/, "") ??
  "http://127.0.0.1:8100";

function endpoint(path: string): string {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

function failure<T>(message: string, status?: number): ApiResult<T> {
  return {
    ok: false,
    error: {
      message,
      status
    }
  };
}

export async function requestJson<T>(path: string): Promise<ApiResult<T>> {
  const url = endpoint(path);

  try {
    const response = await fetch(url, {
      method: "GET",
      headers: {
        Accept: "application/json"
      },
      cache: "no-store"
    });

    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") {
          detail = body.detail;
        }
      } catch {
        // Keep the HTTP status text if the body is not JSON.
      }

      return failure(`Request failed for ${url}: ${detail}`, response.status);
    }

    return {
      ok: true,
      data: (await response.json()) as T
    };
  } catch (error) {
    const message =
      error instanceof Error
        ? error.message
        : "Unknown network error while contacting Agent Taskflow API";

    return failure(
      `Agent Taskflow API unavailable at ${API_BASE_URL}. ${message}`
    );
  }
}

export async function getProjects(): Promise<ApiResult<Project[]>> {
  const result = await requestJson<ListResponse<Project>>("/api/projects");
  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

export async function getTasks(params?: {
  status?: string;
  project?: string;
}): Promise<ApiResult<Task[]>> {
  const search = new URLSearchParams();

  if (params?.status) {
    search.set("status", params.status);
  }

  if (params?.project) {
    search.set("project", params.project);
  }

  const suffix = search.toString() ? `?${search.toString()}` : "";
  const result = await requestJson<ListResponse<Task>>(`/api/tasks${suffix}`);

  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

export async function getTask(taskKey: string): Promise<ApiResult<Task>> {
  const result = await requestJson<DetailResponse<Task>>(
    `/api/tasks/${encodeURIComponent(taskKey)}`
  );

  if (!result.ok) return result;
  return { ok: true, data: result.data.item };
}

export async function getExecutorRuns(
  taskKey: string
): Promise<ApiResult<ExecutorRun[]>> {
  const result = await requestJson<ListResponse<ExecutorRun>>(
    `/api/tasks/${encodeURIComponent(taskKey)}/runs`
  );

  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

export async function getArtifacts(
  taskKey: string
): Promise<ApiResult<Artifact[]>> {
  const result = await requestJson<ListResponse<Artifact>>(
    `/api/tasks/${encodeURIComponent(taskKey)}/artifacts`
  );

  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

export async function getValidations(
  taskKey: string
): Promise<ApiResult<ValidationResult[]>> {
  const result = await requestJson<ListResponse<ValidationResult>>(
    `/api/tasks/${encodeURIComponent(taskKey)}/validations`
  );

  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

export async function getApprovals(
  taskKey: string
): Promise<ApiResult<ApprovalDecision[]>> {
  const result = await requestJson<ListResponse<ApprovalDecision>>(
    `/api/tasks/${encodeURIComponent(taskKey)}/approvals`
  );

  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

export async function getTaskDetailBundle(
  taskKey: string
): Promise<ApiResult<TaskDetailBundle>> {
  const [task, runs, artifacts, validations, approvals] = await Promise.all([
    getTask(taskKey),
    getExecutorRuns(taskKey),
    getArtifacts(taskKey),
    getValidations(taskKey),
    getApprovals(taskKey)
  ]);

  const failed = [task, runs, artifacts, validations, approvals].find(
    (result) => !result.ok
  );

  if (failed && !failed.ok) {
    return failed;
  }

  if (
    task.ok &&
    runs.ok &&
    artifacts.ok &&
    validations.ok &&
    approvals.ok
  ) {
    return {
      ok: true,
      data: {
        task: task.data,
        runs: runs.data,
        artifacts: artifacts.data,
        validations: validations.data,
        approvals: approvals.data
      }
    };
  }

  return failure("Unable to load task detail bundle.");
}
