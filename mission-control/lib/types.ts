export type TaskStatus =
  | "queued"
  | "preparing"
  | "implementing"
  | "validating"
  | "waiting_approval"
  | "waiting_for_review"
  | "blocked"
  | "accepted"
  | "rejected"
  | "cleaned"
  | "completed"
  | "canceled"
  | string;

export interface ListResponse<T> {
  items: T[];
  count: number;
}

export interface DetailResponse<T> {
  item: T;
}

export interface Project {
  project: string;
  task_count?: number;
  active_count?: number;
  blocked_count?: number;
  waiting_approval_count?: number;
  [key: string]: unknown;
}

export interface Task {
  task_key: string;
  project: string;
  board?: string | null;
  hermes_task_id?: string | null;
  title?: string | null;
  status: TaskStatus;
  repo_path?: string | null;
  artifact_dir?: string | null;
  blocked_reason?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  last_synced_at?: string | null;

  // Reserved for future API enrichment. Current Phase 8/9 API may not expose these.
  executor?: string | null;
  model?: string | null;
  validator?: string | null;
  pr_url?: string | null;
  pr_number?: number | null;
}

export interface ExecutorRun {
  task_key: string;
  executor?: string | null;
  model?: string | null;
  status?: string | null;
  exit_code?: number | null;
  summary?: string | null;
  prompt_path?: string | null;
  log_path?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  artifacts?: Record<string, string>;
  [key: string]: unknown;
}

export interface Artifact {
  task_key: string;
  artifact_type: string;
  path: string;
  created_at?: string | null;
}

export interface ValidationResult {
  task_key: string;
  validator?: string | null;
  status?: string | null;
  exit_code?: number | null;
  summary?: string | null;
  log_path?: string | null;
  created_at?: string | null;
  artifacts?: Record<string, string>;
  [key: string]: unknown;
}

export interface ApprovalDecision {
  task_key: string;
  decision?: string | null;
  decided_by?: string | null;
  reviewer?: string | null;
  notes?: string | null;
  summary?: string | null;
  reason?: string | null;
  pr_url?: string | null;
  pr_number?: number | null;
  merged_commit?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface ActionResponse<T = unknown> {
  ok: boolean;
  action: string;
  task_key?: string | null;
  status?: string | null;
  message: string;
  item?: T | null;
}

export interface CreateTaskRequest {
  task_key: string;
  project: string;
  repo_path: string;
  worktree_path: string;
  artifact_dir: string;
  executor?: string | null;
  model?: string | null;
  validator?: string | null;
  pr_url?: string | null;
  pr_number?: number | null;
  title?: string | null;
  board?: string | null;
  hermes_task_id?: string | null;
  branch?: string | null;
  base_branch?: string | null;
}

export interface StartTaskRequest {
  validators?: string[] | null;
  executor?: string | null;
  model?: string | null;
  dry_run?: boolean;
}

export interface ApprovalRequest {
  decided_by: string;
  notes?: string | null;
}

export interface RejectRequest {
  decided_by: string;
  notes?: string | null;
}

export interface BlockTaskRequest {
  blocked_reason: string;
}

export interface ApiFailure {
  message: string;
  status?: number;
}

export type ApiResult<T> =
  | {
      ok: true;
      data: T;
    }
  | {
      ok: false;
      error: ApiFailure;
    };

export interface TaskDetailBundle {
  task: Task;
  runs: ExecutorRun[];
  artifacts: Artifact[];
  validations: ValidationResult[];
  approvals: ApprovalDecision[];
}
