/**
 * Frontend read-only task state metadata.
 * Derived from backend dispatcher state machine and task statuses.
 *
 * UI rules enforced here:
 * - No push/merge/cleanup/delete actions in UI
 * - No direct Pi/OpenCode/Shell execution from UI
 * - Human approval is the final gate — worker cannot self-approve
 * - All UI actions go through backend API endpoints only
 */

export type TaskStateCategory =
  | "not_started"   // queued
  | "running"       // preparing, implementing, validating
  | "review"        // waiting_approval
  | "terminal_success"  // accepted, completed
  | "terminal_failure"  // rejected, failed
  | "terminal_blocked"  // blocked
  | "terminal_skipped"  // cleaned, canceled, waiting_for_review
  | "unknown";

export interface TaskStateInfo {
  readonly label: string;
  readonly category: TaskStateCategory;
  readonly description: string;
  readonly allowedActions: readonly string[];
  readonly terminal: boolean;
}

export const TASK_STATE_MAP: Readonly<Record<string, TaskStateInfo>> = {
  queued: {
    label: "Queued",
    category: "not_started",
    description: "Task is registered and waiting to be dispatched. Executor has not started.",
    allowedActions: ["start", "block"],
    terminal: false,
  },
  preparing: {
    label: "Preparing",
    category: "running",
    description: "Dispatcher is setting up worktree, artifact directory, and mission contract.",
    allowedActions: [],
    terminal: false,
  },
  implementing: {
    label: "Implementing",
    category: "running",
    description: "Executor backend is actively running inside the assigned worktree.",
    allowedActions: [],
    terminal: false,
  },
  validating: {
    label: "Validating",
    category: "running",
    description: "Deterministic validators (e.g., pytest, openspec) are running against the output.",
    allowedActions: [],
    terminal: false,
  },
  waiting_approval: {
    label: "Waiting Approval",
    category: "review",
    description: "Validation passed. Task requires human review and approval before it can be accepted.",
    allowedActions: ["approve", "reject", "block"],
    terminal: false,
  },
  waiting_for_review: {
    label: "Waiting for Review",
    category: "terminal_skipped",
    description: "Task has paused for review. No action pending.",
    allowedActions: [],
    terminal: true,
  },
  accepted: {
    label: "Approved",
    category: "terminal_success",
    description: "Human approval has accepted the task output. Task is complete.",
    allowedActions: [],
    terminal: true,
  },
  completed: {
    label: "Completed",
    category: "terminal_success",
    description: "Task completed successfully.",
    allowedActions: [],
    terminal: true,
  },
  rejected: {
    label: "Rejected",
    category: "terminal_failure",
    description: "Human review rejected the task output. Task did not pass governance.",
    allowedActions: [],
    terminal: true,
  },
  failed: {
    label: "Failed",
    category: "terminal_failure",
    description: "Executor or validator reported failure. Task did not succeed.",
    allowedActions: ["block"],
    terminal: true,
  },
  blocked: {
    label: "Blocked",
    category: "terminal_blocked",
    description: "Task was manually blocked or hit a governance error. No executor action will run.",
    allowedActions: [],
    terminal: true,
  },
  cleaned: {
    label: "Cleaned",
    category: "terminal_skipped",
    description: "Task was cleaned up. No further action.",
    allowedActions: [],
    terminal: true,
  },
  canceled: {
    label: "Canceled",
    category: "terminal_skipped",
    description: "Task was canceled. No further action.",
    allowedActions: [],
    terminal: true,
  },
};

export const ALL_STATES = Object.keys(TASK_STATE_MAP);

export function getStateInfo(status: string): TaskStateInfo {
  return TASK_STATE_MAP[status] ?? {
    label: status,
    category: "unknown" as TaskStateCategory,
    description: "Unknown task status.",
    allowedActions: [],
    terminal: true,
  };
}

export function isActionAllowed(status: string, action: string): boolean {
  const info = getStateInfo(status);
  return info.allowedActions.includes(action);
}

export function isTerminal(status: string): boolean {
  return getStateInfo(status).terminal;
}

export function stateCategoryColor(category: TaskStateCategory): string {
  switch (category) {
    case "not_started":     return "var(--muted)";
    case "running":         return "var(--blue)";
    case "review":          return "var(--yellow)";
    case "terminal_success":return "var(--green)";
    case "terminal_failure":return "var(--red)";
    case "terminal_blocked":return "var(--red)";
    case "terminal_skipped":return "var(--muted-2)";
    default:                return "var(--muted)";
  }
}

export const APPROVE_WARNING =
  "Approving accepts the task output for this governance workflow. " +
  "This does NOT push, merge, or cleanup any branch or worktree. " +
  "Human approval is the final gate.";

export const REJECT_WARNING =
  "Rejecting marks the task output as not accepted. " +
  "Provide a reason so the task can be reworked. " +
  "This does NOT delete any artifacts.";

export const BLOCK_WARNING =
  "Blocking stops the task immediately. " +
  "Provide a clear reason. " +
  "This does NOT cleanup or delete any files.";

/** States that are safe to show the full action panel for */
export function showActionPanel(status: string): boolean {
  return !isTerminal(status) || status === "waiting_approval" || status === "failed";
}

/** States that should show review evidence before approval */
export function needsReviewBeforeAction(status: string): boolean {
  return status === "waiting_approval";
}