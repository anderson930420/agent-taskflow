import type {
  ActionResponse,
  ApiResult,
  ApprovalDecision,
  ApprovalRequest,
  Artifact,
  ArtifactPreview,
  BlockTaskRequest,
  CreateTaskRequest,
  DetailResponse,
  ExecutorRun,
  ListResponse,
  Project,
  RejectRequest,
  RuntimeAuditEvent,
  SchedulerCandidateDiscovery,
  SchedulerConfirmationReadback,
  SchedulerProposalReadback,
  StartTaskRequest,
  Task,
  TaskDetailBundle,
  TaskReviewBundle,
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

function formatUnknownDetail(detail: unknown): string | null {
  if (typeof detail === "string") {
    return detail;
  }

  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (
          item &&
          typeof item === "object" &&
          "msg" in item &&
          typeof item.msg === "string"
        ) {
          return item.msg;
        }
        return JSON.stringify(item);
      })
      .join("; ");
  }

  if (detail && typeof detail === "object") {
    return JSON.stringify(detail);
  }

  return null;
}

async function responseFailure<T>(
  response: Response,
  url: string
): Promise<ApiResult<T>> {
  let detail = response.statusText;

  try {
    const body = (await response.json()) as {
      detail?: unknown;
      message?: unknown;
    };
    const bodyDetail =
      formatUnknownDetail(body.detail) ?? formatUnknownDetail(body.message);

    if (bodyDetail) {
      detail = bodyDetail;
    }
  } catch {
    // Keep the HTTP status text if the body is not JSON.
  }

  return failure(`Request failed for ${url}: ${detail}`, response.status);
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
      return responseFailure<T>(response, url);
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

export async function postJson<T>(
  path: string,
  payload?: unknown
): Promise<ApiResult<T>> {
  const url = endpoint(path);

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload ?? {})
    });

    if (!response.ok) {
      return responseFailure<T>(response, url);
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

export async function getRuntimeAudits(
  taskKey: string
): Promise<ApiResult<RuntimeAuditEvent[]>> {
  const result = await requestJson<ListResponse<RuntimeAuditEvent>>(
    `/api/tasks/${encodeURIComponent(taskKey)}/runtime-audits`
  );

  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

export async function getSchedulerCandidates(params?: {
  task_key?: string;
  project?: string;
  status?: string;
  include_not_ready?: boolean;
  include_no_action?: boolean;
  limit?: number;
  completed_limit?: number;
}): Promise<ApiResult<SchedulerCandidateDiscovery>> {
  const search = new URLSearchParams();
  if (params?.task_key) search.set("task_key", params.task_key);
  if (params?.project) search.set("project", params.project);
  if (params?.status) search.set("status", params.status);
  if (params?.include_not_ready) search.set("include_not_ready", "true");
  if (params?.include_no_action) search.set("include_no_action", "true");
  if (typeof params?.limit === "number") {
    search.set("limit", String(params.limit));
  }
  if (typeof params?.completed_limit === "number") {
    search.set("completed_limit", String(params.completed_limit));
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return requestJson<SchedulerCandidateDiscovery>(
    `/api/scheduler/candidates${suffix}`
  );
}

export async function getTaskSchedulerCandidate(
  taskKey: string
): Promise<ApiResult<SchedulerCandidateDiscovery>> {
  return requestJson<SchedulerCandidateDiscovery>(
    `/api/tasks/${encodeURIComponent(taskKey)}/scheduler-candidate`
  );
}

export async function getSchedulerProposals(params?: {
  task_key?: string;
  limit?: number;
}): Promise<ApiResult<SchedulerProposalReadback>> {
  const search = new URLSearchParams();
  if (params?.task_key) search.set("task_key", params.task_key);
  if (typeof params?.limit === "number") {
    search.set("limit", String(params.limit));
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return requestJson<SchedulerProposalReadback>(
    `/api/scheduler/proposals${suffix}`
  );
}

export async function getTaskSchedulerProposals(
  taskKey: string
): Promise<ApiResult<SchedulerProposalReadback>> {
  return requestJson<SchedulerProposalReadback>(
    `/api/tasks/${encodeURIComponent(taskKey)}/scheduler-proposals`
  );
}

export async function getSchedulerConfirmations(params?: {
  task_key?: string;
  limit?: number;
}): Promise<ApiResult<SchedulerConfirmationReadback>> {
  const search = new URLSearchParams();
  if (params?.task_key) search.set("task_key", params.task_key);
  if (typeof params?.limit === "number") {
    search.set("limit", String(params.limit));
  }
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return requestJson<SchedulerConfirmationReadback>(
    `/api/scheduler/confirmations${suffix}`
  );
}

export async function getTaskSchedulerConfirmations(
  taskKey: string
): Promise<ApiResult<SchedulerConfirmationReadback>> {
  return requestJson<SchedulerConfirmationReadback>(
    `/api/tasks/${encodeURIComponent(taskKey)}/scheduler-confirmations`
  );
}

export async function getTaskDetailBundle(
  taskKey: string
): Promise<ApiResult<TaskDetailBundle>> {
  const [
    task,
    runs,
    artifacts,
    validations,
    approvals,
    runtimeAudits,
    schedulerCandidate,
    schedulerProposals,
    schedulerConfirmations
  ] = await Promise.all([
    getTask(taskKey),
    getExecutorRuns(taskKey),
    getArtifacts(taskKey),
    getValidations(taskKey),
    getApprovals(taskKey),
    getRuntimeAudits(taskKey),
    getTaskSchedulerCandidate(taskKey),
    getTaskSchedulerProposals(taskKey),
    getTaskSchedulerConfirmations(taskKey)
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
    const runtimeAuditEvents = runtimeAudits.ok ? runtimeAudits.data : [];
    const candidateBundle = schedulerCandidate.ok
      ? schedulerCandidate.data
      : null;
    const proposalBundle = schedulerProposals.ok
      ? schedulerProposals.data
      : null;
    const confirmationBundle = schedulerConfirmations.ok
      ? schedulerConfirmations.data
      : null;

    return {
      ok: true,
      data: {
        task: task.data,
        runs: runs.data,
        artifacts: artifacts.data,
        validations: validations.data,
        approvals: approvals.data,
        runtimeAudits: runtimeAuditEvents,
        schedulerCandidate: candidateBundle,
        schedulerProposals: proposalBundle,
        schedulerConfirmations: confirmationBundle
      }
    };
  }

  return failure("Unable to load task detail bundle.");
}

export async function getTaskReviewEvidence(
  taskKey: string
): Promise<ApiResult<TaskReviewBundle>> {
  return requestJson<TaskReviewBundle>(
    `/api/tasks/${encodeURIComponent(taskKey)}/review-evidence`
  );
}

export async function getArtifactPreview(
  taskKey: string,
  artifactName: string
): Promise<ApiResult<ArtifactPreview>> {
  return requestJson<ArtifactPreview>(
    `/api/tasks/${encodeURIComponent(taskKey)}/artifacts/${encodeURIComponent(artifactName)}`
  );
}

export async function createTask(
  payload: CreateTaskRequest
): Promise<ApiResult<ActionResponse<Task>>> {
  return postJson<ActionResponse<Task>>("/api/tasks", payload);
}

export async function startTask(
  taskKey: string,
  payload?: StartTaskRequest
): Promise<ApiResult<ActionResponse>> {
  return postJson<ActionResponse>(
    `/api/tasks/${encodeURIComponent(taskKey)}/start`,
    payload ?? {}
  );
}

export async function approveTask(
  taskKey: string,
  payload: ApprovalRequest
): Promise<ApiResult<ActionResponse<Task>>> {
  return postJson<ActionResponse<Task>>(
    `/api/tasks/${encodeURIComponent(taskKey)}/approve`,
    payload
  );
}

export async function rejectTask(
  taskKey: string,
  payload: RejectRequest
): Promise<ApiResult<ActionResponse<Task>>> {
  return postJson<ActionResponse<Task>>(
    `/api/tasks/${encodeURIComponent(taskKey)}/reject`,
    payload
  );
}

export async function blockTask(
  taskKey: string,
  payload: BlockTaskRequest
): Promise<ApiResult<ActionResponse<Task>>> {
  return postJson<ActionResponse<Task>>(
    `/api/tasks/${encodeURIComponent(taskKey)}/block`,
    payload
  );
}
