"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { realtimeStreamUrl } from "../lib/api";
import {
  BOARD_SECTIONS,
  NEEDS_DECISION_SECTION,
  sectionColor,
  sectionEmptyText,
  sectionTickets,
  ticketSubtitle,
  valueOrDash
} from "../lib/realtime";
import type { BoardProjection, BoardTicket } from "../lib/types";
import { ExecutionStepList } from "./ExecutionStepList";

type Connection = "connecting" | "live" | "retrying";

function TicketCard({ ticket }: { ticket: BoardTicket }) {
  return (
    <Link
      className="task-card"
      href={`/tasks/${encodeURIComponent(ticket.task_key)}`}
      key={ticket.task_key}
    >
      <div className="task-card-top">
        <span className="task-key">{ticket.task_key}</span>
        {ticket.running ? (
          <span style={{ fontSize: "0.66rem", color: "var(--blue)" }}>
            executing
          </span>
        ) : null}
        {ticket.eligible_for_execution ? (
          <span style={{ fontSize: "0.66rem", color: "var(--muted-2)" }}>
            awaiting a slot
          </span>
        ) : null}
        {ticket.awaiting_decision ? (
          <span style={{ fontSize: "0.66rem", color: "var(--purple)" }}>
            awaiting a human decision
          </span>
        ) : null}
        {ticket.pr.pr_number ? (
          <span style={{ fontSize: "0.66rem", color: "var(--muted-2)" }}>
            PR #{ticket.pr.pr_number}
          </span>
        ) : null}
      </div>

      <h3>{ticket.title ?? ticket.task_key}</h3>
      <p>{ticketSubtitle(ticket)}</p>

      {ticket.blocked || ticket.paused ? (
        <p className="muted" style={{ fontSize: "0.7rem" }}>
          {ticket.blocked ? "Blocked" : "Paused"} · not eligible to execute ·{" "}
          {valueOrDash(ticket.blocker_hint)}
        </p>
      ) : (
        <ExecutionStepList steps={ticket.steps} compact />
      )}

      <div className="task-card-meta">
        <span>{ticket.repository}</span>
        <span>
          {valueOrDash(ticket.current_phase)} ·{" "}
          {valueOrDash(ticket.worktree_path)}
        </span>
      </div>
    </Link>
  );
}

export function LiveBoard({ initial }: { initial: BoardProjection | null }) {
  const [board, setBoard] = useState<BoardProjection | null>(initial);
  const [connection, setConnection] = useState<Connection>("connecting");

  useEffect(() => {
    // SPEC §15.1 — every connection begins with a full snapshot, so a
    // reconnect just replaces local state. No replay, no Last-Event-ID, no
    // backfill of anything missed while the tab was away.
    const source = new EventSource(realtimeStreamUrl());

    const apply = (event: MessageEvent<string>) => {
      try {
        setBoard(JSON.parse(event.data) as BoardProjection);
        setConnection("live");
      } catch {
        // A malformed frame must never blank the board.
      }
    };

    source.addEventListener("snapshot", apply as EventListener);
    source.addEventListener("update", apply as EventListener);
    source.onerror = () => setConnection("retrying");

    return () => source.close();
  }, []);

  const unsectioned = board?.unsectioned ?? [];

  return (
    <main>
      <header className="header">
        <p>
          <Link href="/">← Back to dashboard</Link>
        </p>
        <h1>Live Board</h1>
        <p className="muted">
          Read-only view of orchestrator state. Mission Control renders
          lifecycle; it does not own it. Connection: {connection}. Snapshot
          generated {valueOrDash(board?.generated_at)}.
        </p>
      </header>

      <div className="board">
        {BOARD_SECTIONS.map((key) => {
          const tickets = sectionTickets(board, key);
          return (
            <section className="board-column" key={key}>
              <div className="column-header">
                <div className="column-title-wrap">
                  <span
                    className="column-status-dot"
                    style={{ background: sectionColor(key) }}
                  />
                  <span>{key}</span>
                </div>
                <span className="column-count">{tickets.length}</span>
              </div>

              {key === NEEDS_DECISION_SECTION ? (
                <p
                  className="muted"
                  style={{ fontSize: "0.7rem", margin: "0 12px 8px" }}
                >
                  Read-only · Mission Control offers no decision actions here.
                </p>
              ) : null}

              <div className="task-card-list">
                {tickets.length === 0 ? (
                  <div className="empty">{sectionEmptyText(key)}</div>
                ) : (
                  tickets.map((ticket) => (
                    <TicketCard key={ticket.task_key} ticket={ticket} />
                  ))
                )}
              </div>
            </section>
          );
        })}
      </div>

      {unsectioned.length > 0 ? (
        <section className="panel">
          <h2>Not on the board</h2>
          <p className="muted">
            These Tickets are in none of the board sections — closed work, and
            any status the projection could not map.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Ticket</th>
                  <th>Repository</th>
                  <th>Status</th>
                  <th>Current activity</th>
                </tr>
              </thead>
              <tbody>
                {unsectioned.map((ticket) => (
                  <tr key={ticket.task_key}>
                    <td className="mono">
                      <Link
                        href={`/tasks/${encodeURIComponent(ticket.task_key)}`}
                      >
                        {ticket.task_key}
                      </Link>
                    </td>
                    <td>{ticket.repository}</td>
                    <td>{ticket.status}</td>
                    <td>{valueOrDash(ticket.current_activity)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {board?.notes?.length ? (
        <section className="panel">
          <h2>Projection notes</h2>
          <ul>
            {board.notes.map((note) => (
              <li className="muted" key={note}>
                {note}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </main>
  );
}
