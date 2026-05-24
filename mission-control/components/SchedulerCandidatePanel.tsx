import Link from "next/link";
import type {
  SchedulerCandidate,
  SchedulerCandidateDiscovery
} from "../lib/types";

const READ_ONLY_NOTE =
  "Scheduler candidate readback is NOT execution permission. " +
  "Read-only discovery. Human/operator confirmation required.";

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

function listOrDash(value?: string[] | null): string {
  if (!value || value.length === 0) {
    return "—";
  }
  return value.join(", ");
}

function safetyLabels(
  safety?: Record<string, unknown> | null
): string[] {
  if (!safety) return [];
  const labels: string[] = [];
  for (const [key, value] of Object.entries(safety)) {
    if (value === true) {
      labels.push(key);
    } else if (value === false) {
      labels.push(`${key}=false`);
    }
  }
  return labels;
}

function SafetyBanner() {
  return (
    <div className="muted" style={{ marginBottom: "0.75rem" }}>
      NOT execution permission. Read-only discovery.
      Human/operator confirmation required. Mission Control remains read-only.
    </div>
  );
}

export function TaskSchedulerCandidatePanel({
  bundle
}: {
  bundle: SchedulerCandidateDiscovery | null;
}) {
  if (!bundle) {
    return (
      <div className="empty">
        Scheduler candidate readback unavailable. {READ_ONLY_NOTE}
      </div>
    );
  }

  const candidate: SchedulerCandidate | undefined = bundle.candidates?.[0];
  if (!candidate) {
    return (
      <div>
        <SafetyBanner />
        <div className="empty">
          No scheduler candidate available for this task. {READ_ONLY_NOTE}
        </div>
      </div>
    );
  }

  const safety = safetyLabels(candidate.safety);

  return (
    <div>
      <SafetyBanner />
      <div className="table-wrap" style={{ marginBottom: "1rem" }}>
        <table>
          <tbody>
            <tr>
              <th>Candidate ready</th>
              <td>{boolOrDash(candidate.candidate_ready)}</td>
            </tr>
            <tr>
              <th>Recommended command kind</th>
              <td className="mono">
                {valueOrDash(candidate.recommended_command_kind)}
              </td>
            </tr>
            <tr>
              <th>Current phase label</th>
              <td>{valueOrDash(candidate.current_phase_label)}</td>
            </tr>
            <tr>
              <th>Required next gate</th>
              <td className="mono">
                {valueOrDash(candidate.required_next_gate)}
              </td>
            </tr>
            <tr>
              <th>Required operator action</th>
              <td className="mono">
                {valueOrDash(candidate.required_operator_action)}
              </td>
            </tr>
            <tr>
              <th>Missing evidence</th>
              <td>{listOrDash(candidate.missing_evidence)}</td>
            </tr>
            <tr>
              <th>Consistency warnings</th>
              <td>{listOrDash(candidate.consistency_warnings)}</td>
            </tr>
            <tr>
              <th>Reason</th>
              <td>{valueOrDash(candidate.reason)}</td>
            </tr>
            <tr>
              <th>Blocked reason</th>
              <td>{valueOrDash(candidate.blocked_reason)}</td>
            </tr>
            <tr>
              <th>Severity</th>
              <td>{valueOrDash(candidate.severity)}</td>
            </tr>
            <tr>
              <th>Confidence</th>
              <td>{valueOrDash(candidate.confidence)}</td>
            </tr>
            <tr>
              <th>Discovery note</th>
              <td className="muted">
                {valueOrDash(
                  candidate.discovery_note ?? bundle.discovery_note ?? null
                )}
              </td>
            </tr>
            <tr>
              <th>Safety flags</th>
              <td className="muted">
                {safety.length === 0 ? "—" : safety.join("; ")}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function SchedulerCandidateSummary({
  bundle
}: {
  bundle: SchedulerCandidateDiscovery | null;
}) {
  if (!bundle) {
    return (
      <div className="empty">
        Scheduler candidate readback unavailable. {READ_ONLY_NOTE}
      </div>
    );
  }
  const total = bundle.candidate_count ?? bundle.candidates?.length ?? 0;
  const readyCount = (bundle.candidates ?? []).filter(
    (item) => item.candidate_ready === true
  ).length;
  return (
    <div>
      <SafetyBanner />
      <div className="table-wrap" style={{ marginBottom: "1rem" }}>
        <table>
          <tbody>
            <tr>
              <th>Total candidates</th>
              <td>{total}</td>
            </tr>
            <tr>
              <th>Ready candidates</th>
              <td>{readyCount}</td>
            </tr>
            <tr>
              <th>Discovery note</th>
              <td className="muted">
                {valueOrDash(bundle.discovery_note ?? null)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function SchedulerCandidateList({
  bundle
}: {
  bundle: SchedulerCandidateDiscovery | null;
}) {
  if (!bundle) {
    return null;
  }
  const candidates = bundle.candidates ?? [];
  if (candidates.length === 0) {
    return (
      <div className="empty">
        No scheduler candidates discovered. {READ_ONLY_NOTE}
      </div>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            <th>Status</th>
            <th>Recommended command kind</th>
            <th>Required next gate</th>
            <th>Ready</th>
            <th>Safety</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((candidate, index) => {
            const safety = safetyLabels(candidate.safety);
            return (
              <tr
                key={`${candidate.task_key ?? "candidate"}-${index}`}
              >
                <td className="mono">
                  {candidate.task_key ? (
                    <Link
                      href={`/tasks/${encodeURIComponent(candidate.task_key)}`}
                    >
                      {candidate.task_key}
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
                <td>{valueOrDash(candidate.status)}</td>
                <td className="mono">
                  {valueOrDash(candidate.recommended_command_kind)}
                </td>
                <td className="mono">
                  {valueOrDash(candidate.required_next_gate)}
                </td>
                <td>{boolOrDash(candidate.candidate_ready)}</td>
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

export function SchedulerCandidatePanel({
  bundle
}: {
  bundle: SchedulerCandidateDiscovery | null;
}) {
  return (
    <div>
      <SchedulerCandidateSummary bundle={bundle} />
      <SchedulerCandidateList bundle={bundle} />
    </div>
  );
}
