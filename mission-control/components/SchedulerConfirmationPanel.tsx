import Link from "next/link";
import type {
  SchedulerConfirmationReadback,
  SchedulerConfirmationReadbackItem
} from "../lib/types";

const READ_ONLY_NOTE =
  "Read-only confirmation evidence. NOT execution permission. " +
  "Confirmation is not verifier report. Confirmation is not handoff. " +
  "Confirmation is not runtime execution. Next gate still required. " +
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

function itemSafetyLabels(
  item: SchedulerConfirmationReadbackItem
): string[] {
  const labels = safetyLabels(item.safety);
  if (item.not_execution_permission === true) {
    labels.push("not_execution_permission");
  }
  if (item.not_verifier_report === true) {
    labels.push("not_verifier_report");
  }
  if (item.not_handoff === true) {
    labels.push("not_handoff");
  }
  if (item.not_runtime === true) {
    labels.push("not_runtime");
  }
  if (item.requires_next_gate === true) {
    labels.push("requires_next_gate");
  }
  return [...new Set(labels)];
}

function SafetyBanner() {
  return (
    <div className="muted" style={{ marginBottom: "0.75rem" }}>
      Read-only confirmation evidence. NOT execution permission.
      Confirmation is not verifier report. Confirmation is not handoff.
      Confirmation is not runtime execution. Next gate still required.
      Mission Control remains read-only.
    </div>
  );
}

export function SchedulerConfirmationSummary({
  bundle
}: {
  bundle: SchedulerConfirmationReadback | null;
}) {
  if (!bundle) {
    return (
      <div className="empty">
        Scheduler confirmation readback unavailable. {READ_ONLY_NOTE}
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
              <th>Recorded confirmations</th>
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

export function SchedulerConfirmationList({
  bundle
}: {
  bundle: SchedulerConfirmationReadback | null;
}) {
  if (!bundle) {
    return null;
  }

  const confirmations = bundle.items ?? [];
  if (confirmations.length === 0) {
    return (
      <div className="empty">
        No scheduler confirmations recorded. {READ_ONLY_NOTE}
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>task_key</th>
            <th>confirmation_id</th>
            <th>proposal_id</th>
            <th>proposal_hash</th>
            <th>proposal_item_id</th>
            <th>item_hash</th>
            <th>recommended_command_kind</th>
            <th>proposal_artifact_path</th>
            <th>artifact_path</th>
            <th>event_created_at</th>
            <th>artifact_created_at</th>
            <th>readback_warnings</th>
            <th>missing_evidence</th>
            <th>Safety</th>
          </tr>
        </thead>
        <tbody>
          {confirmations.map((confirmation, index) => {
            const safety = itemSafetyLabels(confirmation);
            return (
              <tr
                key={`${confirmation.task_key ?? "confirmation"}-${confirmation.confirmation_id ?? index}`}
              >
                <td className="mono">
                  {confirmation.task_key ? (
                    <Link
                      href={`/tasks/${encodeURIComponent(confirmation.task_key)}`}
                    >
                      {confirmation.task_key}
                    </Link>
                  ) : (
                    "-"
                  )}
                </td>
                <td className="mono">
                  {valueOrDash(confirmation.confirmation_id)}
                </td>
                <td className="mono">
                  {valueOrDash(confirmation.proposal_id)}
                </td>
                <td
                  className="mono"
                  title={confirmation.proposal_hash ?? ""}
                >
                  {shortHash(confirmation.proposal_hash)}
                </td>
                <td className="mono">
                  {valueOrDash(confirmation.proposal_item_id)}
                </td>
                <td className="mono" title={confirmation.item_hash ?? ""}>
                  {shortHash(confirmation.item_hash)}
                </td>
                <td className="mono">
                  {valueOrDash(confirmation.recommended_command_kind)}
                </td>
                <td className="mono">
                  {valueOrDash(confirmation.proposal_artifact_path)}
                </td>
                <td className="mono">
                  {valueOrDash(confirmation.artifact_path)}
                </td>
                <td className="mono">
                  {valueOrDash(confirmation.event_created_at)}
                </td>
                <td className="mono">
                  {valueOrDash(confirmation.artifact_created_at)}
                </td>
                <td>{listOrDash(confirmation.readback_warnings)}</td>
                <td>{listOrDash(confirmation.missing_evidence)}</td>
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

export function TaskSchedulerConfirmationPanel({
  bundle
}: {
  bundle: SchedulerConfirmationReadback | null;
}) {
  if (!bundle) {
    return (
      <div className="empty">
        Scheduler confirmation readback unavailable. {READ_ONLY_NOTE}
      </div>
    );
  }

  if ((bundle.items ?? []).length === 0) {
    return (
      <div>
        <SafetyBanner />
        <div className="empty">
          No scheduler confirmations recorded for this task. {READ_ONLY_NOTE}
        </div>
      </div>
    );
  }

  return (
    <div>
      <SchedulerConfirmationSummary bundle={bundle} />
      <SchedulerConfirmationList bundle={bundle} />
    </div>
  );
}

export function SchedulerConfirmationPanel({
  bundle
}: {
  bundle: SchedulerConfirmationReadback | null;
}) {
  return (
    <div>
      <SchedulerConfirmationSummary bundle={bundle} />
      <SchedulerConfirmationList bundle={bundle} />
    </div>
  );
}
