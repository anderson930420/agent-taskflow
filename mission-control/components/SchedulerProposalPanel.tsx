import Link from "next/link";
import type {
  SchedulerProposalReadback,
  SchedulerProposalReadbackItem
} from "../lib/types";

const READ_ONLY_NOTE =
  "Read-only proposal readback. NOT execution permission. " +
  "Proposal is not confirmation. Human/operator confirmation required. " +
  "Mission Control remains read-only.";

function valueOrDash(value?: string | number | null): string {
  if (value === undefined || value === null || value === "") {
    return "-";
  }
  return String(value);
}

function listOrDash(value?: string[] | null): string {
  if (!value || value.length === 0) {
    return "-";
  }
  return value.join(", ");
}

function shortHash(value?: string | null): string {
  if (!value) return "-";
  return value.length > 12 ? `${value.slice(0, 12)}...` : value;
}

function safetyLabels(
  safety?: Record<string, unknown> | null
): string[] {
  if (!safety) return [];
  const labels: string[] = [];
  for (const [key, value] of Object.entries(safety)) {
    if (value === true) {
      labels.push(key);
    }
  }
  return labels;
}

function itemSafetyLabels(item: SchedulerProposalReadbackItem): string[] {
  const labels = safetyLabels(item.safety);
  if (item.not_execution_permission === true) {
    labels.push("not_execution_permission");
  }
  if (item.not_confirmation === true) {
    labels.push("not_confirmation");
  }
  if (item.requires_human_confirmation === true) {
    labels.push("requires_human_confirmation");
  }
  return [...new Set(labels)];
}

function SafetyBanner() {
  return (
    <div className="muted" style={{ marginBottom: "0.75rem" }}>
      Read-only proposal readback. NOT execution permission.
      Proposal is not confirmation. Human/operator confirmation required.
      Mission Control remains read-only.
    </div>
  );
}

export function SchedulerProposalSummary({
  bundle
}: {
  bundle: SchedulerProposalReadback | null;
}) {
  if (!bundle) {
    return (
      <div className="empty">
        Scheduler proposal readback unavailable. {READ_ONLY_NOTE}
      </div>
    );
  }

  const safety = safetyLabels(bundle.safety);

  return (
    <div>
      <SafetyBanner />
      <div className="table-wrap" style={{ marginBottom: "1rem" }}>
        <table>
          <tbody>
            <tr>
              <th>Recorded proposals</th>
              <td>{bundle.count ?? bundle.items?.length ?? 0}</td>
            </tr>
            <tr>
              <th>Mode</th>
              <td className="mono">{valueOrDash(bundle.mode)}</td>
            </tr>
            <tr>
              <th>Readback note</th>
              <td className="muted">
                {valueOrDash(bundle.readback_note ?? null)}
              </td>
            </tr>
            <tr>
              <th>Safety flags</th>
              <td className="muted">
                {safety.length === 0 ? "read-only" : safety.join("; ")}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function SchedulerProposalList({
  bundle
}: {
  bundle: SchedulerProposalReadback | null;
}) {
  if (!bundle) {
    return null;
  }

  const proposals = bundle.items ?? [];
  if (proposals.length === 0) {
    return (
      <div className="empty">
        No scheduler proposals recorded. {READ_ONLY_NOTE}
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            <th>Proposal id</th>
            <th>Proposal hash</th>
            <th>Proposal item id</th>
            <th>Item hash</th>
            <th>Recommended command kind</th>
            <th>Artifact path</th>
            <th>Event created</th>
            <th>Artifact created</th>
            <th>Readback warnings</th>
            <th>Missing evidence</th>
            <th>Safety</th>
          </tr>
        </thead>
        <tbody>
          {proposals.map((proposal, index) => {
            const safety = itemSafetyLabels(proposal);
            return (
              <tr
                key={`${proposal.task_key ?? "proposal"}-${proposal.proposal_item_id ?? index}`}
              >
                <td className="mono">
                  {proposal.task_key ? (
                    <Link
                      href={`/tasks/${encodeURIComponent(proposal.task_key)}`}
                    >
                      {proposal.task_key}
                    </Link>
                  ) : (
                    "-"
                  )}
                </td>
                <td className="mono">{valueOrDash(proposal.proposal_id)}</td>
                <td className="mono" title={proposal.proposal_hash ?? ""}>
                  {shortHash(proposal.proposal_hash)}
                </td>
                <td className="mono">
                  {valueOrDash(proposal.proposal_item_id)}
                </td>
                <td className="mono" title={proposal.item_hash ?? ""}>
                  {shortHash(proposal.item_hash)}
                </td>
                <td className="mono">
                  {valueOrDash(proposal.recommended_command_kind)}
                </td>
                <td className="mono">{valueOrDash(proposal.artifact_path)}</td>
                <td className="mono">
                  {valueOrDash(proposal.event_created_at)}
                </td>
                <td className="mono">
                  {valueOrDash(proposal.artifact_created_at)}
                </td>
                <td>{listOrDash(proposal.readback_warnings)}</td>
                <td>{listOrDash(proposal.missing_evidence)}</td>
                <td className="muted">
                  {safety.length === 0 ? "read-only" : safety.join("; ")}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function TaskSchedulerProposalPanel({
  bundle
}: {
  bundle: SchedulerProposalReadback | null;
}) {
  if (!bundle) {
    return (
      <div className="empty">
        Scheduler proposal readback unavailable. {READ_ONLY_NOTE}
      </div>
    );
  }

  if ((bundle.items ?? []).length === 0) {
    return (
      <div>
        <SafetyBanner />
        <div className="empty">
          No scheduler proposals recorded for this task. {READ_ONLY_NOTE}
        </div>
      </div>
    );
  }

  return (
    <div>
      <SchedulerProposalSummary bundle={bundle} />
      <SchedulerProposalList bundle={bundle} />
    </div>
  );
}

export function SchedulerProposalPanel({
  bundle
}: {
  bundle: SchedulerProposalReadback | null;
}) {
  return (
    <div>
      <SchedulerProposalSummary bundle={bundle} />
      <SchedulerProposalList bundle={bundle} />
    </div>
  );
}
