import Link from "next/link";
import { CreateTicketForm } from "../../../components/CreateTicketForm";
import { API_BASE_URL } from "../../../lib/api";
import { getRepositories } from "../../../lib/tickets";

export const dynamic = "force-dynamic";

export default async function NewTicketPage() {
  const repositories = await getRepositories();

  return (
    <main>
      <header className="header">
        <p>
          <Link href="/">← Back to dashboard</Link>
        </p>
        <h1>Create Ticket</h1>
        <p>
          Pick a repository, describe the work, set a priority. Agent Taskflow
          derives the Task ID, branch, worktree path and artifact directory.
        </p>
        <p className="muted">
          Creating a Ticket records state only. It does not create a worktree
          or branch, start a worker, push, open a PR, merge, or clean up.
        </p>
        <p className="muted">
          API base URL: <span className="mono">{API_BASE_URL}</span>
        </p>
      </header>

      <section className="section panel">
        <h2>New Ticket</h2>
        {repositories.ok ? (
          <CreateTicketForm repositories={repositories.data} />
        ) : (
          <div className="error">{repositories.error.message}</div>
        )}
      </section>
    </main>
  );
}
