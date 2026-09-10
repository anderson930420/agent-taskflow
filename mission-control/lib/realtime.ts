/**
 * V1 Step 3 realtime presentation helpers (SPEC §14, §16).
 *
 * Pure presentation only — no fetching, no mutation, no lifecycle opinion.
 * Every value rendered here comes from the read-only board / Ticket
 * projection served by the Agent Taskflow API.
 *
 * SPEC §14.2: the UI shows the real lifecycle, never a claimed share of work
 * and never a countdown. Steps render as glyphs, not as numbers.
 */

import type { BoardProjection, BoardTicket, RuntimeObservedStep } from "./types";

/** Rendered in place of any null or missing value. */
export const DASH = "—";

/**
 * Board sections: NEEDS DECISION on top by human ruling 5, then the five
 * SPEC §16 sections in the order the spec lists them.
 */
export const BOARD_SECTIONS = [
  "NEEDS DECISION",
  "RUNNING",
  "READY",
  "BLOCKED",
  "PAUSED",
  "READY FOR REVIEW"
] as const;

export type BoardSectionKey = (typeof BOARD_SECTIONS)[number];

/** The read-only section for Tickets waiting on a human decision. */
export const NEEDS_DECISION_SECTION: BoardSectionKey = "NEEDS DECISION";

export const SECTION_EMPTY_TEXT: Record<string, string> = {
  "NEEDS DECISION": "No Ticket is waiting on a human decision.",
  RUNNING: "No Ticket is executing.",
  READY: "No Ticket is waiting for an executor slot.",
  BLOCKED: "No Ticket is blocked.",
  PAUSED: "No Ticket is paused.",
  "READY FOR REVIEW": "No Ticket is waiting for human review."
};

/** SPEC §14.1 first-level steps, in spec order. */
export const RUNTIME_STEPS = [
  "Prepare",
  "Scout",
  "Planner",
  "Implementer",
  "Reviewer",
  "Validator",
  "Integration"
] as const;

/** SPEC §14.2 renders shorter verbs than the §14.1 record names. */
export const RUNTIME_STEP_LABELS: Record<string, string> = {
  Prepare: "Prepare",
  Scout: "Scout",
  Planner: "Plan",
  Implementer: "Implement",
  Reviewer: "Review",
  Validator: "Validate",
  Integration: "Integrate"
};

/** SPEC §14.1 step statuses. */
export const RUNTIME_STEP_STATUSES = [
  "pending",
  "running",
  "passed",
  "failed",
  "blocked"
] as const;

/** Lifecycle glyphs (SPEC §14.2) — deliberately never a number. */
export const STEP_GLYPHS: Record<string, string> = {
  pending: "○",
  running: "●",
  passed: "✓",
  failed: "✗",
  blocked: "⊘"
};

export const STEP_COLORS: Record<string, string> = {
  pending: "var(--muted-2)",
  running: "var(--blue)",
  passed: "var(--green)",
  failed: "var(--red)",
  blocked: "var(--red)"
};

export const SECTION_COLORS: Record<string, string> = {
  "NEEDS DECISION": "var(--purple)",
  RUNNING: "var(--blue)",
  READY: "var(--muted)",
  BLOCKED: "var(--red)",
  PAUSED: "var(--muted-2)",
  "READY FOR REVIEW": "var(--yellow)"
};

export function valueOrDash(value?: string | number | boolean | null): string {
  if (value === undefined || value === null) return DASH;
  const text = String(value).trim();
  return text.length > 0 ? text : DASH;
}

export function stepGlyph(status: string): string {
  return STEP_GLYPHS[status] ?? STEP_GLYPHS.pending;
}

export function stepColor(status: string): string {
  return STEP_COLORS[status] ?? STEP_COLORS.pending;
}

export function stepLabel(step: RuntimeObservedStep): string {
  return step.label || RUNTIME_STEP_LABELS[step.name] || step.name;
}

export function sectionColor(key: string): string {
  return SECTION_COLORS[key] ?? "var(--muted)";
}

export function sectionEmptyText(key: string): string {
  return SECTION_EMPTY_TEXT[key] ?? "Nothing here.";
}

/** Tickets in one §16 section, in board order. */
export function sectionTickets(
  board: BoardProjection | null,
  key: string
): BoardTicket[] {
  if (!board) return [];
  return board.sections.find((section) => section.key === key)?.tickets ?? [];
}

/**
 * The one-line subtitle under a Ticket on the board.
 *
 * BLOCKED shows its blocker (SPEC §16: "Waiting for AT-101"). RUNNING shows
 * the current activity. Nothing here ever claims how much work is left.
 */
export function ticketSubtitle(ticket: BoardTicket): string {
  if (ticket.awaiting_decision) return "Awaiting a human decision";
  if (ticket.blocked || ticket.paused) {
    return valueOrDash(ticket.blocker_hint);
  }
  if (ticket.current_activity) return ticket.current_activity;
  if (ticket.current_phase) {
    return RUNTIME_STEP_LABELS[ticket.current_phase] ?? ticket.current_phase;
  }
  return valueOrDash(null);
}
