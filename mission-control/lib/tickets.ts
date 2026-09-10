import { postJson, requestJson } from "./api";
import type { ApiResult, ListResponse } from "./types";

/** SPEC §7. Highest precedence first. */
export const TICKET_PRIORITIES = ["critical", "high", "normal", "low"] as const;

export type TicketPriority = (typeof TICKET_PRIORITIES)[number];

/** SPEC §12. V1 never writes `queued`. */
export type TicketStatus =
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

export type TicketMetadataSource = "ai" | "fallback";

/** SPEC §11: one entry of the read-only repository registry. */
export interface TicketRepository {
  repository: string;
  repo_path: string;
  worktrees_dir: string;
  artifacts_root: string;
  base_branch: string;
  branch_prefix: string;
  ticket_prefix: string;
  github_repo?: string | null;
}

export interface Ticket {
  ticket_id: string;
  repository: string;
  prompt: string;
  title: string;
  title_source: TicketMetadataSource;
  priority: TicketPriority;
  status: TicketStatus;
  blocked_by?: string | null;
  repo_path: string;
  github_repo?: string | null;
  base_branch: string;
  branch: string;
  branch_slug_source: TicketMetadataSource;
  worktree_path: string;
  artifact_dir: string;
  commit_message_suggestion?: string | null;
  ticket_prefix: string;
  ticket_sequence: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TicketEvent {
  event_id: number;
  ticket_id: string;
  event_type: string;
  actor: string;
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
  ticket_id?: string | null;
  status?: TicketStatus | null;
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

export async function getTickets(params?: {
  repository?: string;
  status?: string;
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
  ticketId: string
): Promise<ApiResult<TicketDetail>> {
  return requestJson<TicketDetail>(
    `/api/tickets/${encodeURIComponent(ticketId)}`
  );
}

export async function createTicket(
  payload: CreateTicketRequest
): Promise<ApiResult<TicketResponse>> {
  return postJson<TicketResponse>("/api/tickets", payload);
}
