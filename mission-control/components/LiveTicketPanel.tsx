"use client";

import { useCallback, useEffect, useState } from "react";
import { getTicketRealtime, ticketRealtimeStreamUrl } from "../lib/api";
import { valueOrDash } from "../lib/realtime";
import type { TicketProjection } from "../lib/types";
import { ExecutionStepList } from "./ExecutionStepList";

const EM_DASH = "—";

type Connection = "connecting" | "live" | "retrying";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <th>{label}</th>
      <td className="mono">{value}</td>
    </tr>
  );
}

/**
 * SPEC §17 live Ticket page.
 *
 * Read-only. Every value is a projection of persisted orchestrator state:
 * repository, priority, status, branch, worktree, execution steps, current
 * activity, artifact links, and the SPEC §31 review surface. The SPEC §32.1
 * PR fields are owned by the Step 2 watcher — each one may be absent or null
 * and renders as an em dash when it is.
 */
export function LiveTicketPanel({ taskKey }: { taskKey: string }) {
  const [projection, setProjection] = useState<TicketProjection | null>(null);
  const [attemptId, setAttemptId] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const result = await getTicketRealtime(taskKey, attemptId);
    if (result.ok) {
      setProjection(result.data);
      setError(null);
    } else {
      setError(result.error.message);
    }
  }, [taskKey, attemptId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // SPEC §15.1 — each connection opens with a full snapshot, so reconnect
    // simply replaces state. No replay and no missed-event backfill.
    const source = new EventSource(ticketRealtimeStreamUrl(taskKey, attemptId));

    const apply = (event: MessageEvent<string>) => {
      try {
        setProjection(JSON.parse(event.data) as TicketProjection);
        setConnection("live");
        setError(null);
      } catch {
        // A malformed frame must never blank the panel.
      }
    };

    source.addEventListener("snapshot", apply as EventListener);
    source.addEventListener("update", apply as EventListener);
    source.onerror = () => setConnection("retrying");

    return () => source.close();
  }, [taskKey, attemptId]);

  if (error && !projection) {
    return (
      <section className="panel">
        <h2>Live Runtime</h2>
        <div className="error">{error}</div>
      </section>
    );
  }

  if (!projection) {
    return (
      <section className="panel">
        <h2>Live Runtime</h2>
        <p className="muted">Connecting to the runtime stream…</p>
      </section>
    );
  }

  const { ticket, attempts, artifacts, validators, reviewer_hints } = projection;
  const pr = ticket.pr.display;

  return (
    <>
      <section className="panel">
        <h2>Live Runtime</h2>
        <p className="muted">
          Read-only projection of persisted state · connection: {connection} ·
          snapshot {valueOrDash(projection.generated_at)}
        </p>

        <div className="table-wrap">
          <table>
            <tbody>
              <Row label="Repository" value={ticket.display.repository} />
              <Row label="Priority" value={ticket.display.priority} />
              <Row label="Status" value={ticket.display.status} />
              <Row label="Board section" value={ticket.display.section} />
              <Row label="Branch" value={ticket.display.branch} />
              <Row label="Worktree" value={ticket.display.worktree_path} />
              <Row label="Current phase" value={ticket.display.current_phase} />
              <Row
                label="Current activity"
                value={ticket.display.current_activity}
              />
              <Row label="Blocker" value={ticket.display.blocker_hint} />
            </tbody>
          </table>
        </div>

        {ticket.blocked || ticket.paused ? (
          <p className="muted">
            {ticket.blocked ? "Blocked" : "Paused"} — this Ticket is not
            eligible to execute and is never shown as running.
          </p>
        ) : null}
      </section>

      <section className="panel">
        <h2>Execution</h2>
        {attempts.length > 1 ? (
          <p className="muted">
            <label htmlFor="attempt-select">Attempt </label>
            <select
              id="attempt-select"
              value={projection.selected_attempt_id ?? ""}
              onChange={(event) => setAttemptId(event.target.value || null)}
            >
              {attempts.map((item) => (
                <option key={item.attempt_id} value={item.attempt_id}>
                  Attempt {item.attempt_number}
                  {item.is_active ? " (active)" : ""}
                </option>
              ))}
            </select>{" "}
            · earlier attempts stay viewable
          </p>
        ) : (
          <p className="muted">
            Attempt {valueOrDash(ticket.attempt_number)} ·{" "}
            {valueOrDash(ticket.attempt_id)}
          </p>
        )}
        <ExecutionStepList steps={ticket.steps} />
      </section>

      <section className="panel">
        <h2>Review Surface</h2>
        <p className="muted">
          Read-only. GitHub Pull Request is the review surface (SPEC §31);
          Mission Control does not replace GitHub diff review, and no action
          here creates, updates, or merges a PR.
        </p>

        <div className="table-wrap">
          <table>
            <tbody>
              <Row label="PR number" value={pr.pr_number} />
              <Row label="PR URL" value={pr.pr_url} />
              <Row label="PR state" value={pr.pr_state} />
              <Row label="PR merged" value={pr.pr_merged} />
              <Row label="PR head SHA" value={pr.pr_head_sha} />
              <Row label="Merge commit SHA" value={pr.merge_commit_sha} />
              <Row label="Review decision" value={pr.review_decision} />
              <Row label="GitHub CI" value={pr.ci_status} />
              <Row label="Integrated base SHA" value={pr.integrated_base_sha} />
              <Row
                label="Re-integration count"
                value={pr.reintegration_count}
              />
              <Row
                label="Re-integration required"
                value={pr.reintegration_required}
              />
              <Row label="PR last polled" value={pr.pr_last_polled_at} />
            </tbody>
          </table>
        </div>

        {ticket.pr.available ? null : (
          <p className="muted">
            No PR state has been recorded for this Ticket yet — every field
            above reads {EM_DASH}.
          </p>
        )}

        <p className="muted">
          GitHub CI is displayed for information only.
          {" "}
          It is not a Taskflow lifecycle authority (SPEC §30): a red check does
          not move this Ticket out of review, and branch protection — not
          Taskflow — gates the human merge.
        </p>
      </section>

      <section className="panel">
        <h2>Taskflow validators</h2>
        <p className="muted">
          Deterministic Taskflow validator evidence, kept separate from the
          GitHub CI status above (SPEC §30).
        </p>
        {validators.length === 0 ? (
          <p className="muted">No validator evidence recorded.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Validator</th>
                  <th>Status</th>
                  <th>Summary</th>
                </tr>
              </thead>
              <tbody>
                {validators.map((item, index) => (
                  <tr key={`${String(item.validator)}-${index}`}>
                    <td className="mono">{valueOrDash(String(item.validator ?? ""))}</td>
                    <td>{valueOrDash(String(item.status ?? ""))}</td>
                    <td>{valueOrDash(String(item.summary ?? ""))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {reviewer_hints.length > 0 ? (
        <section className="panel">
          <h2>Reviewer hints</h2>
          <p className="muted">
            Attention routing only (SPEC §38) — never a correctness decision.
          </p>
          <ul>
            {reviewer_hints.map((hint) => (
              <li key={hint}>{hint}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="panel">
        <h2>Artifacts</h2>
        {artifacts.length === 0 ? (
          <p className="muted">No artifacts recorded.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Path</th>
                  <th>Recorded</th>
                </tr>
              </thead>
              <tbody>
                {artifacts.map((artifact) => (
                  <tr key={`${artifact.artifact_type}-${artifact.path}`}>
                    <td>{artifact.artifact_type}</td>
                    <td className="mono">{artifact.path}</td>
                    <td className="mono">{valueOrDash(artifact.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
