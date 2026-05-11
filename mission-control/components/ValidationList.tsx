import type { ValidationResult } from "../lib/types";
import { StatusBadge } from "./StatusBadge";

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  return String(value);
}

export function ValidationList({
  validations
}: {
  validations: ValidationResult[];
}) {
  if (validations.length === 0) {
    return <div className="empty">No validation results recorded.</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Validator</th>
            <th>Status</th>
            <th>Exit code</th>
            <th>Summary</th>
            <th>Log path</th>
          </tr>
        </thead>
        <tbody>
          {validations.map((validation, index) => (
            <tr key={`${validation.task_key}-${validation.validator ?? "validation"}-${index}`}>
              <td>{valueOrDash(validation.validator)}</td>
              <td>
                <StatusBadge status={validation.status} />
              </td>
              <td>{valueOrDash(validation.exit_code)}</td>
              <td>{valueOrDash(validation.summary)}</td>
              <td className="mono">{valueOrDash(validation.log_path)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
