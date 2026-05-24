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

  executor?: string | null;
  model?: string | null;
  provider?: string | null;
  tools?: string[] | null;
  pi_bin?: string | null;
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

export interface MissionContractSummary {
  exists: boolean;
  status: "present" | "missing" | "invalid";
  schema_version?: string | null;
  task_key?: string | null;
  goal?: string | null;
  executor?: string | null;
  required_validators: string[];
  forbidden_actions: string[];
  expected_artifacts: string[];
  human_approval_required?: boolean | null;
  governance_rules: string[];
  error?: string;
}

export interface ArtifactFileSummary {
  name: string;
  kind: string;
  size_bytes: number;
  preview_available: boolean;
  has_secret_warning: boolean;
  is_binary: boolean;
  is_validator_log: boolean;
  is_executor_log: boolean;
  is_mission_contract: boolean;
}

export interface ValidatorResultSummary {
  validator?: string | null;
  status?: string | null;
  exit_code?: number | null;
  summary?: string | null;
  log_path?: string | null;
  created_at?: string | null;
}

export interface TaskReviewEvidence {
  task_key: string;
  mission_contract: MissionContractSummary;
  artifacts: ArtifactFileSummary[];
  validator_results: ValidatorResultSummary[];
  policy_status: string;
  policy_warnings: string[];
}

export interface DogfoodEvidenceItem {
  name: string;
  artifact_type: string;
  kind: string;
  category: string;
  path?: string | null;
  exists: boolean;
  preview_available: boolean;
  size_bytes?: number | null;
  source: string;
  created_at?: string | null;
  validator?: string | null;
  status?: string | null;
  summary?: string | null;
}

export interface DogfoodEvidenceSummary {
  has_issue_spec: boolean;
  has_pr_handoff: boolean;
  has_branch_push: boolean;
  has_draft_pr: boolean;
  has_preflight: boolean;
  validation_statuses: Array<{
    validator?: string | null;
    status?: string | null;
    summary?: string | null;
  }>;
}

export interface DogfoodEvidenceSafety {
  read_only: boolean;
  push_available_from_this_endpoint: boolean;
  pr_creation_available_from_this_endpoint: boolean;
  merge_available_from_this_endpoint: boolean;
  cleanup_available_from_this_endpoint: boolean;
  approval_available_from_this_endpoint: boolean;
}

export interface TaskDogfoodEvidence {
  task_key: string;
  available: boolean;
  categories: Record<string, DogfoodEvidenceItem[]>;
  summary: DogfoodEvidenceSummary;
  safety: DogfoodEvidenceSafety;
}

export interface ArtifactPreview {
  name: string;
  content: string | null;
  truncated: boolean;
  size_bytes: number;
  preview_reason: string | null;
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

export interface RuntimeAuditEvent {
  id?: number | string | null;
  task_key: string;
  created_at?: string | null;
  source?: string | null;
  message?: string | null;
  kind: string;
  runtime_execution_id?: string | null;
  executor?: string | null;
  preflight_passed?: boolean | null;
  package_verified?: boolean | null;
  intake_runner_handoff_verified?: boolean | null;
  expiration_still_valid?: boolean | null;
  approved_task_runner_invoked?: boolean | null;
  runner_returned?: boolean | null;
  runner_ok?: boolean | null;
  runner_status?: string | null;
  runner_phase?: string | null;
  final_status?: string | null;
  runner_error?: string | null;
  verifier_run_id?: string | null;
  verifier_report_path?: string | null;
  intake_runner_handoff_artifact_path?: string | null;
  proposal_hash?: string | null;
  proposal_item_id?: string | null;
  item_hash?: string | null;
  confirmation_id?: string | null;
  runtime_execution_artifact_path?: string | null;
  not_action_evidence?: boolean;
  not_validation_authority?: boolean;
  [key: string]: unknown;
}

export interface TaskDetailBundle {
  task: Task;
  runs: ExecutorRun[];
  artifacts: Artifact[];
  validations: ValidationResult[];
  approvals: ApprovalDecision[];
  runtimeAudits: RuntimeAuditEvent[];
}

export interface TaskReviewBundle {
  item: TaskReviewEvidence;
}

export interface TaskDogfoodEvidenceBundle {
  item: TaskDogfoodEvidence;
}
