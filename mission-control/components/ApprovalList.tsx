import type { ApprovalDecision } from "../lib/types";
import { StatusBadge } from "./StatusBadge";

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  return String(value);
}

export function ApprovalList({
  approvals
}: {
  approvals: ApprovalDecision[];
}) {
  if (approvals.length === 0) {
    return <div className="empty">No approval decisions recorded.</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Decision</th>
            <th>Decided by</th>
            <th>Reviewer</th>
            <th>Notes</th>
            <th>Reason</th>
            <th>PR</th>
            <th>Merged commit</th>
          </tr>
        </thead>
        <tbody>
          {approvals.map((approval, index) => (
            <tr key={`${approval.task_key}-${approval.decision ?? "approval"}-${index}`}>
              <td>
                <StatusBadge status={approval.decision} />
              </td>
              <td>{valueOrDash(approval.decided_by)}</td>
              <td>{valueOrDash(approval.reviewer)}</td>
              <td>{valueOrDash(approval.notes ?? approval.summary)}</td>
              <td>{valueOrDash(approval.reason)}</td>
              <td>
                {approval.pr_url ? (
                  <a href={approval.pr_url} rel="noreferrer" target="_blank">
                    PR {approval.pr_number ?? ""}
                  </a>
                ) : (
                  "—"
                )}
              </td>
              <td className="mono">{valueOrDash(approval.merged_commit)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
