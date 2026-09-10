"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import {
  TICKET_PRIORITIES,
  createTicket,
  type TicketPriority,
  type TicketRepository
} from "../lib/tickets";

const SELECT_STYLE = {
  width: "100%",
  minHeight: "40px",
  padding: "9px 11px",
  color: "var(--text)",
  background: "#0f1218",
  border: "1px solid var(--border)",
  borderRadius: "10px"
};

const PRIORITY_LABELS: Record<TicketPriority, string> = {
  critical: "Critical",
  high: "High",
  normal: "Normal",
  low: "Low"
};

/**
 * SPEC §10: the user fills in Repository, Task and Priority. Task ID, branch,
 * worktree path and artifact directory are derived by the backend and are
 * deliberately absent from this form.
 */
export function CreateTicketForm({
  repositories
}: {
  repositories: TicketRepository[];
}) {
  const router = useRouter();
  const [repository, setRepository] = useState(
    repositories[0]?.repository ?? ""
  );
  const [prompt, setPrompt] = useState("");
  const [priority, setPriority] = useState<TicketPriority>("normal");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selected = repositories.find(
    (candidate) => candidate.repository === repository
  );

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (!repository) {
      setError("Select a repository.");
      return;
    }
    if (!prompt.trim()) {
      setError("Describe what you want done.");
      return;
    }

    setSubmitting(true);
    try {
      const response = await createTicket({ repository, prompt, priority });

      if (!response.ok) {
        setError(response.error.message);
        return;
      }

      const ticketId = response.data.ticket_id;
      if (!ticketId) {
        setError(response.data.message);
        return;
      }

      router.push(`/tickets/${encodeURIComponent(ticketId)}`);
      router.refresh();
    } finally {
      setSubmitting(false);
    }
  }

  if (repositories.length === 0) {
    return (
      <div className="error" role="alert">
        No repositories are registered. Add one to the project registry
        (<span className="mono">config/projects.yaml</span>) first.
      </div>
    );
  }

  return (
    <form className="form-grid" onSubmit={handleSubmit}>
      {error ? (
        <div className="error" role="alert">
          {error}
        </div>
      ) : null}

      <label>
        Repository
        <select
          onChange={(event) => setRepository(event.target.value)}
          style={SELECT_STYLE}
          value={repository}
        >
          {repositories.map((option) => (
            <option key={option.repository} value={option.repository}>
              {option.repository}
            </option>
          ))}
        </select>
        {selected ? (
          <span className="field-hint">
            Base branch <span className="mono">{selected.base_branch}</span> ·
            branches <span className="mono">{selected.branch_prefix}</span> ·
            task IDs <span className="mono">{selected.ticket_prefix}-…</span>
          </span>
        ) : null}
      </label>

      <label>
        Task
        <textarea
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="Describe what you want done..."
          required
          rows={6}
          style={{
            ...SELECT_STYLE,
            minHeight: "120px",
            fontFamily: "inherit",
            resize: "vertical"
          }}
          value={prompt}
        />
        <span className="field-hint">
          The Ticket title falls back to the first 60 characters of this prompt
          when AI metadata is unavailable.
        </span>
      </label>

      <label>
        Priority
        <select
          onChange={(event) =>
            setPriority(event.target.value as TicketPriority)
          }
          style={SELECT_STYLE}
          value={priority}
        >
          {TICKET_PRIORITIES.map((option) => (
            <option key={option} value={option}>
              {PRIORITY_LABELS[option]}
            </option>
          ))}
        </select>
        <span className="field-hint">
          Priority orders eligible Tickets. It never overrides a dependency.
        </span>
      </label>

      <div className="form-actions">
        <button className="button" disabled={submitting} type="submit">
          {submitting ? "Creating..." : "Create Ticket"}
        </button>
      </div>
    </form>
  );
}
