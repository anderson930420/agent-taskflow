import Link from "next/link";
import { ActionPanel } from "../../../components/ActionPanel";
import { ApprovalList } from "../../../components/ApprovalList";
import { ArtifactList } from "../../../components/ArtifactList";
import { DogfoodEvidencePanel } from "../../../components/DogfoodEvidencePanel";
import { RunList } from "../../../components/RunList";
import { RuntimeAuditPanel } from "../../../components/RuntimeAuditPanel";
import { RuntimeExecutionPanel } from "../../../components/RuntimeExecutionPanel";
import { ReviewEvidenceSection } from "../../../components/ReviewEvidenceSection";
import { TaskSchedulerCandidatePanel } from "../../../components/SchedulerCandidatePanel";
import { TaskSchedulerConfirmationPanel } from "../../../components/SchedulerConfirmationPanel";
import { TaskSchedulerProposalPanel } from "../../../components/SchedulerProposalPanel";
import { StartDispatchPanel } from "../../../components/StartDispatchPanel";
import { StatusBadge } from "../../../components/StatusBadge";
import { TaskEvidencePanel } from "../../../components/TaskEvidencePanel";
import { TaskStateTimeline } from "../../../components/TaskStateTimeline";
import { ValidationList } from "../../../components/ValidationList";
import { API_BASE_URL, getTaskDetailBundle } from "../../../lib/api";

export const dynamic = "force-dynamic";

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  return String(value);
}

function toolsOrDash(value?: string[] | null): string {
  if (!value || value.length === 0) {
    return "—";
  }
  return value.join(", ");
}

export default async function TaskDetailPage({
  params,
}: {
  params: Promise<{ taskKey: string }>;
}) {
  const { taskKey } = await params;
  const decodedTaskKey = decodeURIComponent(taskKey);
  const result = await getTaskDetailBundle(decodedTaskKey);

  if (!result.ok) {
    return (
      <main>
        <header className="header">
          <p>
            <Link href="/">← Back to dashboard</Link>
          </p>
          <h1>Task {decodedTaskKey}</h1>
          <p className="muted">
            API base URL: <span className="mono">{API_BASE_URL}</span>
          </p>
        </header>

        <div className="error">{result.error.message}</div>
      </main>
    );
  }

  const {
    task,
    runs,
    artifacts,
    validations,
    approvals,
    runtimeAudits,
    schedulerCandidate,
    schedulerProposals,
    schedulerConfirmations
  } = result.data;

  return (
    <main>
      <header className="header">
        <p>
          <Link href="/">← Back to dashboard</Link>
        </p>
        <h1>{task.task_key}</h1>
        <p>{task.title ?? "Task detail"}</p>
        <p className="muted">
          API base URL: <span className="mono">{API_BASE_URL}</span>
        </p>
      </header>

      <section className="panel">
        <h2>Task Metadata</h2>
        <div className="table-wrap">
          <table>
            <tbody>
              <tr>
                <th>Task key</th>
                <td className="mono">{task.task_key}</td>
              </tr>
              <tr>
                <th>Project</th>
                <td>{task.project}</td>
              </tr>
              <tr>
                <th>Board</th>
                <td>{valueOrDash(task.board)}</td>
              </tr>
              <tr>
                <th>Hermes task id</th>
                <td className="mono">{valueOrDash(task.hermes_task_id)}</td>
              </tr>
              <tr>
                <th>Status</th>
                <td>
                  <StatusBadge status={task.status} />
                </td>
              </tr>
              <tr>
                <th>Repo path</th>
                <td className="mono">{valueOrDash(task.repo_path)}</td>
              </tr>
              <tr>
                <th>Artifact dir</th>
                <td className="mono">{valueOrDash(task.artifact_dir)}</td>
              </tr>
              <tr>
                <th>Blocked reason</th>
                <td>{valueOrDash(task.blocked_reason)}</td>
              </tr>
              <tr>
                <th>Created</th>
                <td className="mono">{valueOrDash(task.created_at)}</td>
              </tr>
              <tr>
                <th>Updated</th>
                <td className="mono">{valueOrDash(task.updated_at)}</td>
              </tr>
              <tr>
                <th>Last synced</th>
                <td className="mono">{valueOrDash(task.last_synced_at)}</td>
              </tr>
              <tr>
                <th>PR</th>
                <td>
                  {task.pr_url ? (
                    <a href={task.pr_url} rel="noreferrer" target="_blank">
                      PR {task.pr_number ?? ""}
                    </a>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section className="section panel">
        <h2>Executor Metadata</h2>
        <div className="table-wrap">
          <table>
            <tbody>
              <tr>
                <th>Executor</th>
                <td>{valueOrDash(task.executor)}</td>
              </tr>
              <tr>
                <th>Model</th>
                <td className="mono">{valueOrDash(task.model)}</td>
              </tr>
              <tr>
                <th>Provider</th>
                <td>{valueOrDash(task.provider)}</td>
              </tr>
              <tr>
                <th>Tools</th>
                <td className="mono">{toolsOrDash(task.tools)}</td>
              </tr>
              <tr>
                <th>Pi binary</th>
                <td className="mono">{valueOrDash(task.pi_bin)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <StartDispatchPanel
        taskKey={task.task_key}
        currentStatus={task.status}
        currentExecutor={task.executor}
        currentModel={task.model}
      />

      <div className="task-detail-body">
        <div className="task-detail-main">
          <section className="section panel">
            <h2>Task State Timeline</h2>
            <TaskStateTimeline
              currentStatus={task.status}
              blockedReason={task.blocked_reason}
            />
          </section>

          <section className="section panel">
            <h2>Review Evidence</h2>
            <ReviewEvidenceSection taskKey={decodedTaskKey} />
          </section>

          <section className="section panel">
            <h2>Evidence Summary</h2>
            <DogfoodEvidencePanel taskKey={decodedTaskKey} />
          </section>

          <TaskEvidencePanel taskKey={decodedTaskKey} />

          <section className="section panel">
            <h2>Executor Runs</h2>
            <RunList runs={runs} />
          </section>

          <section className="section panel">
            <h2>Artifacts</h2>
            <ArtifactList artifacts={artifacts} />
          </section>

          <section className="section panel">
            <h2>Validation Results</h2>
            <ValidationList validations={validations} />
          </section>

          <section className="section panel">
            <h2>Runtime Audit</h2>
            <RuntimeAuditPanel events={runtimeAudits} />
          </section>

          <section className="section panel">
            <h2>Runtime Execution</h2>
            <RuntimeExecutionPanel events={runtimeAudits} />
          </section>

          <section className="section panel">
            <h2>Scheduler Candidate</h2>
            <TaskSchedulerCandidatePanel bundle={schedulerCandidate} />
          </section>

          <section className="section panel">
            <h2>Scheduler Proposals</h2>
            <TaskSchedulerProposalPanel bundle={schedulerProposals} />
          </section>

          <section className="section panel">
            <h2>Scheduler Confirmations</h2>
            <TaskSchedulerConfirmationPanel bundle={schedulerConfirmations} />
          </section>

          <section className="section panel">
            <h2>Approval Decisions</h2>
            <ApprovalList approvals={approvals} />
          </section>
        </div>

        <aside className="task-detail-sidebar">
          <ActionPanel task={task} />
        </aside>
      </div>
    </main>
  );
}
