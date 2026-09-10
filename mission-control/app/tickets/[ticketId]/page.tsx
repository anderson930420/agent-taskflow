import Link from "next/link";
import { API_BASE_URL } from "../../../lib/api";
import { getTicketDetail } from "../../../lib/tickets";

export const dynamic = "force-dynamic";

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  return String(value);
}

export default async function TicketDetailPage({
  params
}: {
  params: Promise<{ ticketId: string }>;
}) {
  const { ticketId } = await params;
  const decodedTicketId = decodeURIComponent(ticketId);
  const result = await getTicketDetail(decodedTicketId);

  if (!result.ok) {
    return (
      <main>
        <header className="header">
          <p>
            <Link href="/tickets/new">← Create another Ticket</Link>
          </p>
          <h1>Ticket {decodedTicketId}</h1>
          <p className="muted">
            API base URL: <span className="mono">{API_BASE_URL}</span>
          </p>
        </header>

        <div className="error">{result.error.message}</div>
      </main>
    );
  }

  const { item: ticket, events } = result.data;

  return (
    <main>
      <header className="header">
        <p>
          <Link href="/tickets/new">← Create another Ticket</Link>
        </p>
        <h1>{ticket.ticket_id}</h1>
        <p>{ticket.title}</p>
        <p className="muted">
          API base URL: <span className="mono">{API_BASE_URL}</span>
        </p>
      </header>

      <section className="panel">
        <h2>Ticket</h2>
        <div className="table-wrap">
          <table>
            <tbody>
              <tr>
                <th>Repository</th>
                <td>{ticket.repository}</td>
              </tr>
              <tr>
                <th>Priority</th>
                <td>{ticket.priority}</td>
              </tr>
              <tr>
                <th>Status</th>
                <td className="mono">{ticket.status}</td>
              </tr>
              <tr>
                <th>Blocked by</th>
                <td className="mono">{valueOrDash(ticket.blocked_by)}</td>
              </tr>
              <tr>
                <th>Title source</th>
                <td>{ticket.title_source}</td>
              </tr>
              <tr>
                <th>Created</th>
                <td className="mono">{valueOrDash(ticket.created_at)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section className="section panel">
        <h2>Prompt</h2>
        <p>{ticket.prompt}</p>
      </section>

      <section className="section panel">
        <h2>Derived metadata</h2>
        <p className="muted">
          Derived by Agent Taskflow. These are recorded strings: the branch and
          worktree do not exist on disk yet.
        </p>
        <div className="table-wrap">
          <table>
            <tbody>
              <tr>
                <th>Repo path</th>
                <td className="mono">{ticket.repo_path}</td>
              </tr>
              <tr>
                <th>GitHub repo</th>
                <td className="mono">{valueOrDash(ticket.github_repo)}</td>
              </tr>
              <tr>
                <th>Base branch</th>
                <td className="mono">{ticket.base_branch}</td>
              </tr>
              <tr>
                <th>Branch</th>
                <td className="mono">{ticket.branch}</td>
              </tr>
              <tr>
                <th>Worktree</th>
                <td className="mono">{ticket.worktree_path}</td>
              </tr>
              <tr>
                <th>Artifact dir</th>
                <td className="mono">{ticket.artifact_dir}</td>
              </tr>
              <tr>
                <th>Commit message suggestion</th>
                <td className="mono">
                  {valueOrDash(ticket.commit_message_suggestion)}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section className="section panel">
        <h2>Audit trail</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Event</th>
                <th>Actor</th>
                <th>Recorded</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.event_id}>
                  <td className="mono">{event.event_type}</td>
                  <td>{event.actor}</td>
                  <td className="mono">{valueOrDash(event.created_at)}</td>
                  <td>{valueOrDash(event.message)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="section panel">
        <h2>Execution</h2>
        <p className="muted">
          No execution has run. Runtime progress, validator evidence and PR
          state are not part of Ticket creation and appear once the executor,
          integration and realtime steps land.
        </p>
      </section>
    </main>
  );
}
