# Active Cron Observability Post-Rollout Validation (P4-k)

This is the **post-rollout validation** record for the **active cron
observability rollout** (P4-j). It is a **documentation only** / **evidence
record only** document: it captures the observed smoke evidence after the
operator manually applied the rollout runbook
(`docs/active-cron-observability-rollout.md`) to the active real `opencode`
cron line.

> **This phase modifies nothing.** The **active crontab is not modified by this
> phase** — the actual crontab update was a separate, explicit human operator
> action that was already completed before this record was written. This
> document only records what was observed afterwards.

See also:

- `docs/active-cron-observability-rollout.md` — the P4-j rollout runbook
  (including the rollback procedure).
- `docs/real-scheduled-execution-observability.md` — the P4-h read-only
  dashboard / summarizer whose output is quoted below.
- `docs/execution-observability-summary.md` — the normalized
  `UnifiedExecutionSummary` schema carried in `observability_summary`.

## Purpose

This record exists to:

- record the **post-rollout validation** evidence for the active cron
  observability rollout;
- prove that live cron JSONL scheduler tick lines now carry a top-level
  `observability_summary` (the normalized `UnifiedExecutionSummary`,
  `schema_version=execution_observability_summary.v1`,
  `source=scheduler_tick`);
- prove the dashboard / summarizer
  (`scripts/summarize_real_scheduled_execution.py`) reads the unified
  summaries successfully;
- confirm the old-log **legacy fallback** remains safe: lines that predate the
  rollout (without `observability_summary`) remain readable via the legacy
  scheduler tick payload, and malformed lines are safely skipped.

## Scope

- This is a **documentation / evidence record only**.
- It **does not modify the active crontab** — the active crontab is not
  modified by this phase.
- It does not modify runtime behavior.
- It does not change scheduler execution semantics.
- The **scheduler tick is not migrated to ExecutionEngine** — whether to start
  an ExecutionEngine-backed scheduler migration is a possible future-phase
  decision, not part of P4-k.

## Rollout state

The validated post-rollout state of the live schedule:

- The active cron line includes `--include-observability-summary --json`.
- The runtime worktree `/home/ubuntu/agent-taskflow-cron` is synced to
  `origin/main`.
- The log path remains
  `/home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl`.
- The active runner remains executor `opencode` + model
  `minimax-coding-plan/MiniMax-M2.7` + validator `policy`.
- Publication remains execution-only: `publish_after_execution=False`,
  `mode=execution_only`.

## Latest JSONL tick evidence

The latest valid JSON tick line in the live log, observed after the operator
applied the rollout:

- `json_line` 342
- status `no_eligible_issues`
- ok `True`
- `has_observability_summary` `True`
- schema_version `execution_observability_summary.v1`
- source `scheduler_tick`
- executor `opencode`
- model `minimax-coding-plan/MiniMax-M2.7`
- validators `['policy']`

This proves the live cron tick now emits the unified `observability_summary`
alongside the legacy scheduler tick payload.

## Dashboard / summarizer evidence

The read-only dashboard / summarizer output after the rollout:

- last tick status `no_eligible_issues`
- ok `True`
- lock acquired `True` / contended `False` / released `True`
- failures 0
- `lock_contention` 0
- observability summaries read 20
- `waiting_approval` 1
- blocked 0
- queued 0
- `ingestion_failure_count` 0
- quarantined 0

The summarizer parsed 20 recent ticks, all ok, all `no_eligible_issues`, and
read a unified observability summary from every one of them (observability
summaries read 20). This proves the dashboard / summarizer reads the unified
summaries successfully, with zero failures and zero lock contention.

## Historical malformed lines

The dashboard also reported:

```text
malformed lines skipped: 26
```

These 26 lines are **historical residue** from the first rollout attempt: the
active cron command already had the new flag, but the runtime worktree still
pointed at an old commit that did not recognize
`--include-observability-summary`, so those ticks wrote non-JSON error output
into the log. The runtime worktree was subsequently synced to `origin/main`,
after which later ticks emit valid JSON with `observability_summary`.

This is historical log residue, **not a current runtime failure**, and it is
not treated as a current failure once valid summary-bearing ticks are present
(the latest valid tick above and the 20 summary-bearing recent ticks). The
dashboard safely skips malformed lines by design — it counts and skips them
without crashing or corrupting the rest of the summary.

## Safety boundaries

This validation record, and the rollout it validates, involve:

- **no GitHub mutation**
- **no approval**
- **no merge**
- **no cleanup**
- **no archive** disposition
- **no closeout** disposition
- **no PR publication**
- **no issue close**
- **no branch deletion**
- **no worktree deletion**
- **no daemon**
- **no webhook**
- **no background worker**
- **no scheduler loop**
- **no multi-task behavior**

The **scheduler tick is not migrated to ExecutionEngine**.

## Rollback reference

- A backup crontab exists under `/home/ubuntu/agent-taskflow-cron-backups/`.
- The rollback procedure remains documented in
  `docs/active-cron-observability-rollout.md` (Step 5 — Rollback).
- No rollback was needed or performed; this phase executed no rollback and
  records the reference for operators only.

## Conclusion

- The active cron observability rollout is **validated**: live cron JSONL
  carries `observability_summary`, and the dashboard / summarizer reads the
  unified summaries successfully with the legacy fallback intact.
- The **P4 observability chain is complete** (P4-a through P4-k).
- A future phase may decide whether to start an ExecutionEngine-backed
  scheduler migration, but that is **not part of P4-k** — the scheduler tick is
  not migrated to ExecutionEngine.
