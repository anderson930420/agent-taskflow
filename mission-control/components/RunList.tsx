import type { ExecutorRun } from "../lib/types";
import { StatusBadge } from "./StatusBadge";

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  return String(value);
}

export function RunList({ runs }: { runs: ExecutorRun[] }) {
  if (runs.length === 0) {
    return <div className="empty">No executor runs recorded.</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Executor</th>
            <th>Model</th>
            <th>Status</th>
            <th>Exit code</th>
            <th>Summary</th>
            <th>Prompt path</th>
            <th>Log path</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run, index) => (
            <tr key={`${run.task_key}-${run.executor ?? "run"}-${index}`}>
              <td>{valueOrDash(run.executor)}</td>
              <td className="mono">{valueOrDash(run.model)}</td>
              <td>
                <StatusBadge status={run.status} />
              </td>
              <td>{valueOrDash(run.exit_code)}</td>
              <td>{valueOrDash(run.summary)}</td>
              <td className="mono">{valueOrDash(run.prompt_path)}</td>
              <td className="mono">{valueOrDash(run.log_path)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
