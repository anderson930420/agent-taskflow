import { postJson, requestJson } from "./api";
import type { ApiResult, ListResponse } from "./types";

/** SPEC §7. Highest precedence first. */
export const TICKET_PRIORITIES = ["critical", "high", "normal", "low"] as const;

export type TicketPriority = (typeof TICKET_PRIORITIES)[number];

/** SPEC §12 display vocabulary (§12.2). V1 never writes `queued`. */
export type TicketDisplayStatus =
  | "queued"
  | "ready"
  | "blocked"
  | "paused"
  | "preparing"
  | "running"
  | "validating"
  | "ready_for_integration"
  | "integrating"
  | "needs_review"
  | "needs_decision"
  | "completed"
  | "failed"
  | "cancelled"
  | string;

export type AiTitleStatus = "generated" | "fallback" | "not_attempted";

export type BranchSlugSource = "ai" | "fallback";

/** SPEC §11: one entry of the read-only repository registry. */
export interface TicketRepository {
  repository: string;
  repo_path: string;
  worktrees_dir: string;
  artifacts_root: string;
  base_branch: string;
  branch_prefix: string;
  github_repo?: string | null;
}

/**
 * A prompt-first row of the canonical `tasks` table. `status` is the
 * persisted TASK_STATUSES value; `display_status` is its §12 name.
 */
export interface Ticket {
  task_key: string;
  repository: string;
  prompt: string;
  title: string;
  ai_title_status: AiTitleStatus;
  priority: TicketPriority;
  status: string;
  display_status: TicketDisplayStatus;
  blocked_by?: string | null;
  repo_path: string;
  github_repo?: string | null;
  base_branch: string;
  branch: string;
  branch_slug_source: BranchSlugSource;
  worktree_path: string;
  artifact_dir: string;
  commit_message_suggestion?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** One `task_events` audit record. */
export interface TicketEvent {
  task_key: string;
  event_type: string;
  source: string;
  message?: string | null;
  payload_json?: string | null;
  created_at?: string | null;
}

export interface TicketDetail {
  item: Ticket;
  events: TicketEvent[];
}

/** SPEC §10: the entire user-supplied creation surface. */
export interface CreateTicketRequest {
  repository: string;
  prompt: string;
  priority: TicketPriority;
}

export interface TicketResponse {
  ok: boolean;
  task_key?: string | null;
  status?: string | null;
  display_status?: TicketDisplayStatus | null;
  message: string;
  item?: Ticket | null;
}

export async function getRepositories(): Promise<
  ApiResult<TicketRepository[]>
> {
  const result = await requestJson<ListResponse<TicketRepository>>(
    "/api/repositories"
  );
  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

/** `status` is a §12 display name; it matches every persisted alias. */
export async function getTickets(params?: {
  repository?: string;
  status?: TicketDisplayStatus;
  priority?: string;
}): Promise<ApiResult<Ticket[]>> {
  const search = new URLSearchParams();
  if (params?.repository) search.set("repository", params.repository);
  if (params?.status) search.set("status", params.status);
  if (params?.priority) search.set("priority", params.priority);

  const suffix = search.toString() ? `?${search.toString()}` : "";
  const result = await requestJson<ListResponse<Ticket>>(
    `/api/tickets${suffix}`
  );
  if (!result.ok) return result;
  return { ok: true, data: result.data.items };
}

export async function getTicketDetail(
  taskKey: string
): Promise<ApiResult<TicketDetail>> {
  return requestJson<TicketDetail>(
    `/api/tickets/${encodeURIComponent(taskKey)}`
  );
}

export async function createTicket(
  payload: CreateTicketRequest
): Promise<ApiResult<TicketResponse>> {
  return postJson<TicketResponse>("/api/tickets", payload);
}
