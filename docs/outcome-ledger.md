# Attempt Outcome Ledger

Level 2 Roadmap Milestone 2 §2.3 requires one closeout record per Attempt.
`agent_taskflow/outcome_ledger.py` writes that record as an immutable JSON
artifact inside the Attempt's own artifact root, indexed through the existing
`task_artifacts` and `task_events` tables. There is no new table, no migration
and no new mutation endpoint.

## What is written, and when

Every terminal Attempt route calls the same writer **after** its lifecycle
transaction has committed:

| Route (`closeout_route`) | Call site |
| --- | --- |
| `runtime_admission_release` | `RuntimeAdmissionStore.release` |
| `runtime_lease_expiry` | `RuntimeAdmissionStore.expire_stale_leases` |
| `attempt_store_close` | `AttemptStore.close_attempt` |
| `task_status_trigger` | `TaskMirrorStore.update_task_status`, for the compatibility SQLite trigger `runtime_terminal_status_releases_lease` |

`LifecycleRuntimeTaskStore._release` is deliberately **not** hooked: it
delegates to `RuntimeAdmissionStore.release`, so hooking it as well would write
the same Attempt twice.

The artifact is `outcome-ledger-<attempt_id>.json` under
`attempts.artifact_root`. The file name is the idempotency key. An Attempt id
that is not a single safe path component is hashed into
`outcome-ledger-sha256-<digest>.json` instead; the mapping is deterministic, so
a repeat publication still collides on the same name.

## Recorded fields

Each of the fourteen §2.3 fields is stored as
`{"value", "provenance", "source", "reason", ...}`, where `provenance` is
`observed`, `unknown` or `not_applicable`.

| Field | Source when observed |
| --- | --- |
| `final_status` | `attempts.status` for this exact Attempt |
| `phase_durations` | consecutive status transitions in this Attempt's own `lifecycle_events`, closed by `attempts.ended_at` |
| `retry_count` | Attempts of this Task with `attempt_number` **less than** this Attempt |
| `first_pass_success` | whether Attempt 1 of this Task itself reached `completed` or `waiting_approval` |
| `human_intervention_count` | this Attempt's lifecycle events whose reason code is in the documented operator set |
| `diff_size` | `changed-files-audit.json` in this Attempt's artifact root |
| `task_class` | `tasks.task_class` |
| `policy_version` | `attempts.policy_version` |
| `model_snapshot` | the configured Attempt executor/model/base/config; the backend model actually served is a separate, unattested fact |
| `canonical_execution_path` | the reason code of this Attempt's own recorded creation/claim event |
| `merge_recommendation` | `attempts.merge_recommendation` |
| `human_decision` | not observable at closeout — see later observations |
| `post_merge_result` | not observable at closeout — see later observations |
| `rollback_result` | not observable at closeout — see later observations |

Facts that have not happened yet are `null` with provenance `unknown` and
reason `not_observed_at_attempt_closeout`. They are never defaulted to zero,
false or "success". A source that exists but cannot be read (missing, corrupt,
substituted by a symlink, too large) also becomes `unknown`, with the concrete
reason recorded.

The Attempt's final status and the Task status observed at closeout are stored
as separate facts (`fields.final_status` and `task_status_at_closeout`).

## Later observations

A merge decision, post-merge result or rollback result that is actually
observed later is appended as its own artifact,
`outcome-ledger-<attempt_id>-observation-<id>.json`, which names the base
ledger and its sha256. The base ledger is never rewritten.

`scripts/confirm_task_closeout.py --attempt-id <attempt>` performs that
linkage. Closeout itself is task-scoped, so the flag is optional: **omit it
when the Attempt is not known.** An unbound merge outcome stays unbound rather
than being attached to the newest Attempt by inference. The same rule applies
to integration runs, which carry an `integration_run_id` and no Attempt
binding.

## Failure behaviour

Evidence failures are reported, never hidden and never escalated:

- A missing, unsafe or symlinked artifact root, an unresolvable Attempt, a
  still-active Attempt, a wrong task identity, a failed publication or a failed
  artifact index each record an explicit `incomplete` observation as a `note`
  task event with the exact Attempt id and reason.
- A repeat publication returns the original artifact and records a `duplicate`
  observation.
- An existing artifact whose identity disagrees with the new snapshot records a
  `conflict` observation; the existing bytes are kept and the new snapshot is
  rejected.
- No failure path rolls back, retries or reinterprets the committed lifecycle
  result, and nothing in the ledger is a lifecycle, approval or merge
  authority.

## Reading it back

```bash
python3 - <<'PY'
from agent_taskflow.store import TaskMirrorStore
store = TaskMirrorStore("/absolute/path/state.db")
for record in store.list_task_artifacts("AT-GH-42"):
    if "outcome-ledger-" in record.path.name:
        print(record.path)
PY
```

The matching `note` events with source `outcome_ledger` carry the publication
reference, including the artifact sha256 and any incomplete-evidence reason.
