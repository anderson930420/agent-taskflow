import Link from "next/link";
import { CreateTaskForm } from "../../../components/CreateTaskForm";
import { GovernanceWarningBox } from "../../../components/GovernanceWarningBox";
import { API_BASE_URL } from "../../../lib/api";

export const dynamic = "force-dynamic";

export default function NewTaskPage() {
  return (
    <main>
      <header className="header">
        <p>
          <Link href="/">← Back to dashboard</Link>
        </p>
        <h1>Create Task</h1>
        <p>
          Create a local mirrored Agent Taskflow task. This page does not start
          a worker, create a PR, push, merge, or clean up worktrees.
        </p>
        <p className="muted">
          API base URL: <span className="mono">{API_BASE_URL}</span>
        </p>
      </header>

      <section className="panel">
        <GovernanceWarningBox variant="critical" />
      </section>
      <section className="section panel">
        <h2>Task Metadata</h2>
        <CreateTaskForm />
      </section>
    </main>
  );
}
