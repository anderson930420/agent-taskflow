import type { RuntimeAuditEvent } from "../lib/types";

const SAFETY_LINES = [
  "Runtime audit evidence is not approval.",
  "Runtime audit evidence is not merge.",
  "Runtime audit evidence is not cleanup.",
  "Human review remains required after runtime.",
  "Mission Control remains read-only.",
];

const RUNTIME_KINDS = [
  "runtime_preflight_finished",
  "runtime_execution_started",
  "runtime_execution_finished",
];

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  return String(value);
}

function boolOrDash(value?: boolean | null): string {
  if (value === undefined || value === null) {
    return "—";
  }
  return value ? "yes" : "no";
}

function findLatest(
  events: RuntimeAuditEvent[],
  kind: string
): RuntimeAuditEvent | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].kind === kind) {
      return events[index];
    }
  }
  return undefined;
}

function pickFirst<T>(...values: (T | null | undefined)[]): T | null {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== "") {
      return value;
    }
  }
  return null;
}

export function RuntimeExecutionPanel({
  events,
}: {
  events: RuntimeAuditEvent[];
}) {
  const safetyNote = (
    <div className="muted" style={{ marginBottom: "0.75rem" }}>
      {SAFETY_LINES.join(" ")}
    </div>
  );

  if (events.length === 0) {
    return (
      <div>
        {safetyNote}
        <div className="empty">
          No runtime execution evidence recorded. Runtime audit evidence is
          read-only readback; it is not action evidence and not validation
          authority.
        </div>
      </div>
    );
  }

  const latestPreflight = findLatest(events, "runtime_preflight_finished");
  const latestStarted = findLatest(events, "runtime_execution_started");
  const latestFinished = findLatest(events, "runtime_execution_finished");
  const latestEvent = events[events.length - 1];

  const runtimeExecutionId = pickFirst(
    latestFinished?.runtime_execution_id,
    latestStarted?.runtime_execution_id,
    latestPreflight?.runtime_execution_id,
    latestEvent.runtime_execution_id
  );

  const runtimeArtifactPath = pickFirst(
    latestFinished?.runtime_execution_artifact_path,
    latestStarted?.runtime_execution_artifact_path,
    latestPreflight?.runtime_execution_artifact_path,
    latestEvent.runtime_execution_artifact_path
  );

  const handoffArtifactPath = pickFirst(
    latestFinished?.intake_runner_handoff_artifact_path,
    latestStarted?.intake_runner_handoff_artifact_path,
    latestPreflight?.intake_runner_handoff_artifact_path
  );

  const verifierReportPath = pickFirst(
    latestFinished?.verifier_report_path,
    latestStarted?.verifier_report_path,
    latestPreflight?.verifier_report_path
  );

  const verifierRunId = pickFirst(
    latestFinished?.verifier_run_id,
    latestStarted?.verifier_run_id,
    latestPreflight?.verifier_run_id
  );

  const proposalHash = pickFirst(
    latestFinished?.proposal_hash,
    latestStarted?.proposal_hash,
    latestPreflight?.proposal_hash
  );

  const proposalItemId = pickFirst(
    latestFinished?.proposal_item_id,
    latestStarted?.proposal_item_id,
    latestPreflight?.proposal_item_id
  );

  const itemHash = pickFirst(
    latestFinished?.item_hash,
    latestStarted?.item_hash,
    latestPreflight?.item_hash
  );

  const confirmationId = pickFirst(
    latestFinished?.confirmation_id,
    latestStarted?.confirmation_id,
    latestPreflight?.confirmation_id
  );

  const runnerInvoked = pickFirst(
    latestFinished?.approved_task_runner_invoked,
    latestStarted?.approved_task_runner_invoked,
    latestPreflight?.approved_task_runner_invoked
  );

  return (
    <div>
      {safetyNote}

      <div className="table-wrap" style={{ marginBottom: "1rem" }}>
        <table>
          <tbody>
            <tr>
              <th>Runtime audit event count</th>
              <td>{events.length}</td>
            </tr>
            <tr>
              <th>runtime_execution_id</th>
              <td className="mono">{valueOrDash(runtimeExecutionId)}</td>
            </tr>
            <tr>
              <th>runtime_preflight_finished</th>
              <td>
                preflight_passed:{" "}
                {boolOrDash(latestPreflight?.preflight_passed)} · time:{" "}
                <span className="mono">
                  {valueOrDash(latestPreflight?.created_at)}
                </span>
              </td>
            </tr>
            <tr>
              <th>runtime_execution_started</th>
              <td>
                approved_task_runner_invoked:{" "}
                {boolOrDash(
                  latestStarted?.approved_task_runner_invoked ?? runnerInvoked
                )}{" "}
                · time:{" "}
                <span className="mono">
                  {valueOrDash(latestStarted?.created_at)}
                </span>
              </td>
            </tr>
            <tr>
              <th>runtime_execution_finished</th>
              <td>
                runner_returned:{" "}
                {boolOrDash(latestFinished?.runner_returned)} · runner_ok:{" "}
                {boolOrDash(latestFinished?.runner_ok)} · time:{" "}
                <span className="mono">
                  {valueOrDash(latestFinished?.created_at)}
                </span>
              </td>
            </tr>
            <tr>
              <th>runner_status</th>
              <td>{valueOrDash(latestFinished?.runner_status)}</td>
            </tr>
            <tr>
              <th>runner_phase</th>
              <td>{valueOrDash(latestFinished?.runner_phase)}</td>
            </tr>
            <tr>
              <th>final_status</th>
              <td>{valueOrDash(latestFinished?.final_status)}</td>
            </tr>
            <tr>
              <th>runner_error</th>
              <td className="mono">
                {valueOrDash(latestFinished?.runner_error)}
              </td>
            </tr>
            <tr>
              <th>runtime_execution_artifact_path</th>
              <td className="mono">{valueOrDash(runtimeArtifactPath)}</td>
            </tr>
            <tr>
              <th>Handoff artifact path</th>
              <td className="mono">{valueOrDash(handoffArtifactPath)}</td>
            </tr>
            <tr>
              <th>verifier_report_path</th>
              <td className="mono">{valueOrDash(verifierReportPath)}</td>
            </tr>
            <tr>
              <th>verifier_run_id</th>
              <td className="mono">{valueOrDash(verifierRunId)}</td>
            </tr>
            <tr>
              <th>proposal_hash</th>
              <td className="mono">{valueOrDash(proposalHash)}</td>
            </tr>
            <tr>
              <th>proposal_item_id</th>
              <td className="mono">{valueOrDash(proposalItemId)}</td>
            </tr>
            <tr>
              <th>item_hash</th>
              <td className="mono">{valueOrDash(itemHash)}</td>
            </tr>
            <tr>
              <th>confirmation_id</th>
              <td className="mono">{valueOrDash(confirmationId)}</td>
            </tr>
            <tr>
              <th>Last runtime event time</th>
              <td className="mono">{valueOrDash(latestEvent.created_at)}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Kind</th>
              <th>Source</th>
              <th>Message</th>
              <th>Runner status/phase</th>
              <th>Safety</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event, index) => {
              const safetyTag = RUNTIME_KINDS.includes(event.kind)
                ? "Read-only runtime evidence"
                : "Read-only audit evidence";
              return (
                <tr
                  key={`${event.id ?? "runtime-execution"}-${
                    event.created_at ?? index
                  }-${index}`}
                >
                  <td className="mono">{valueOrDash(event.created_at)}</td>
                  <td>{event.kind}</td>
                  <td>{valueOrDash(event.source)}</td>
                  <td>{valueOrDash(event.message)}</td>
                  <td>
                    {valueOrDash(
                      event.runner_status ??
                        event.runner_phase ??
                        event.final_status
                    )}
                  </td>
                  <td className="muted">{safetyTag}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
