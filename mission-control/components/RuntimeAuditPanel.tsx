import type { RuntimeAuditEvent } from "../lib/types";

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

export function RuntimeAuditPanel({
  events
}: {
  events: RuntimeAuditEvent[];
}) {
  if (events.length === 0) {
    return (
      <div className="empty">
        No runtime audit events recorded. Runtime audit readback is observation
        only; it is not action evidence and not validation authority.
      </div>
    );
  }

  const latestPreflight = findLatest(events, "runtime_preflight_finished");
  const latestStarted = findLatest(events, "runtime_execution_started");
  const latestFinished = findLatest(events, "runtime_execution_finished");
  const latestEvent = events[events.length - 1];
  const latestRuntimeExecutionId =
    latestFinished?.runtime_execution_id ??
    latestStarted?.runtime_execution_id ??
    latestPreflight?.runtime_execution_id ??
    latestEvent.runtime_execution_id ??
    null;

  return (
    <div>
      <div className="muted" style={{ marginBottom: "0.75rem" }}>
        Runtime audit only. Not action evidence. Not validation authority.
        Validator results remain authoritative under Validation Results.
      </div>

      <div className="table-wrap" style={{ marginBottom: "1rem" }}>
        <table>
          <tbody>
            <tr>
              <th>Latest runtime execution id</th>
              <td className="mono">
                {valueOrDash(latestRuntimeExecutionId)}
              </td>
            </tr>
            <tr>
              <th>Latest preflight passed</th>
              <td>{boolOrDash(latestPreflight?.preflight_passed)}</td>
            </tr>
            <tr>
              <th>Runner invoked</th>
              <td>
                {boolOrDash(
                  latestStarted?.approved_task_runner_invoked ??
                    latestPreflight?.approved_task_runner_invoked ??
                    null
                )}
              </td>
            </tr>
            <tr>
              <th>Runner status</th>
              <td>{valueOrDash(latestFinished?.runner_status)}</td>
            </tr>
            <tr>
              <th>Runner phase</th>
              <td>{valueOrDash(latestFinished?.runner_phase)}</td>
            </tr>
            <tr>
              <th>Last runtime event time</th>
              <td className="mono">{valueOrDash(latestEvent.created_at)}</td>
            </tr>
            <tr>
              <th>Runtime execution artifact</th>
              <td className="mono">
                {valueOrDash(
                  latestFinished?.runtime_execution_artifact_path ??
                    latestStarted?.runtime_execution_artifact_path ??
                    latestPreflight?.runtime_execution_artifact_path ??
                    null
                )}
              </td>
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
              <th>Preflight</th>
              <th>Runner invoked</th>
              <th>Executor</th>
              <th>Status / phase</th>
              <th>Safety</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event, index) => (
              <tr
                key={`${event.id ?? "runtime-audit"}-${event.created_at ?? index}-${index}`}
              >
                <td className="mono">{valueOrDash(event.created_at)}</td>
                <td>{event.kind}</td>
                <td>{boolOrDash(event.preflight_passed)}</td>
                <td>
                  {boolOrDash(event.approved_task_runner_invoked)}
                </td>
                <td>{valueOrDash(event.executor)}</td>
                <td>
                  {valueOrDash(
                    event.runner_status ?? event.runner_phase ?? event.final_status
                  )}
                </td>
                <td className="muted">
                  Not action evidence; not validation authority
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
