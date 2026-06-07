# Runtime Logs / Artifacts Retention Policy (P2-c)

This document defines the retention policy for the local runtime logs and
artifact directories produced by the real scheduled execution path. It is the
third phase (**P2-c**) of the local workspace cleanup effort, following the
read-only inventory ([P2-a](local-workspace-cleanup-inventory.md)) and the
operator-confirmed cleanup runbook ([P2-b](operator-cleanup-and-backlog-triage.md)).

> Manage work, not agents. Runtime logs and artifacts are preserved
> **proof-of-work**. Removing them is an explicit, reviewed, recorded operator
> decision — never an automatic, cron-driven, or worker-driven action.

**P2-c is documentation and tests only.** It defines what each runtime path is
for, how long it is retained, and which actions are forbidden in this phase. It
does **not** implement cleanup automation, and it does **not** delete, rotate,
compress, move, prune, reset, or clean anything. No runtime code, cron example,
scheduler, executor, validator, archive, closeout, or inventory behavior is
changed by this phase.

## Retention categories

Every retained path falls into one of five categories. The category drives the
default policy and the safety rules below.

| Category | Meaning | Default policy |
| --- | --- | --- |
| Active operational logs | Live observability / recent-tick audit trail | Keep active log in place |
| Runtime execution evidence | Per-task proposal/confirmation/verifier/handoff/runtime evidence | Retain while the task is live or recent |
| Disposition evidence | Permanent record of superseded / stale / smoke-only dispositions | Preserve long-term |
| Closeout evidence | Proof that merged PR + cleanup + status closeout happened | Preserve long-term |
| Manual backup evidence | Recovery snapshots taken before a reset/cleanup | Preserve until manually reviewed |

## Retained paths and their policies

### A. Cron JSONL logs — active operational logs

- **Path:** `/home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl`
- **Category:** active operational logs.
- **Purpose:** operational observability and recent tick audit. This is the
  live append log the installed cron tick writes to; it backs the dashboard's
  recent-tick view and is **operational evidence**, not scratch output.
- **Default policy:** keep the active log in place. Do not truncate or rotate it
  in this phase.
- **Rotation** may be considered later, but only by **explicit human-confirmed**
  action in a dedicated phase (see [P2-e](#recommended-future-phases)). It
  **must not be auto-deleted** by cron or by any generic cleanup.

### B. Scheduler tick artifacts — runtime execution evidence

- **Path:** `/home/ubuntu/agent-taskflow-cron/artifacts/scheduler-tick/`
- **Category:** runtime execution evidence.
- **Purpose:** proposal / confirmation / verifier / handoff / runtime execution
  evidence captured per scheduler tick.
- **Default policy:** retain while the task is `active`, `waiting_approval`,
  `blocked`, or recently completed.
- Deletion is **forbidden** unless a later **explicit human-confirmed**
  retention command first verifies the task's disposition and records evidence
  of that verification. These artifacts **must not be auto-deleted**.

### C. Evidence archive artifacts — disposition evidence

- **Path:** `/home/ubuntu/agent-taskflow-cron/artifacts/evidence-archive/`
- **Category:** disposition evidence.
- **Purpose:** permanent **disposition evidence** — the durable disposition
  record for superseded / stale / smoke-only tasks (see
  [evidence-only task archive](evidence-only-task-archive.md)).
- **Default policy:** preserve long-term. These are **disposition evidence**
  records, not disposable scratch files, and **must not be auto-deleted**.
- They **may be backed up or copied**, but deletion requires explicit human
  review and a separate retention phase.

### D. Task closeout artifacts — closeout evidence

- **Path:** `/home/ubuntu/agent-taskflow-cron/artifacts/task-closeout/`
- **Category:** closeout evidence.
- **Purpose:** **closeout evidence** — proof that a merged PR, cleanup evidence,
  and a task status closeout actually happened (see
  [task closeout confirmation](../scripts/confirm_task_closeout.py)).
- **Default policy:** preserve long-term. Closeout records are the audit trail
  for completed work and **must not be auto-deleted**.

### E. Main local `.agent-taskflow/artifacts/` — historical local evidence

- **Path:** `/home/ubuntu/agent-taskflow/.agent-taskflow/artifacts/`
- **Category:** runtime execution evidence (historical).
- **Purpose:** historical local task evidence from earlier manual workflows, in
  the known manual checkout.
- **Default policy:** preserve until migrated or explicitly reviewed. It **must
  not be deleted** by generic workspace cleanup and **must not be auto-deleted**.

### F. Dirty checkout backups — manual backup evidence

- **Path:** `/home/ubuntu/agent-taskflow-backups/`
- **Category:** manual backup evidence.
- **Purpose:** recovery evidence captured **before** a reset/cleanup of a dirty
  checkout.
- **Default policy:** preserve for a conservative period or until manually
  reviewed. There is **no automatic deletion** in this phase, and these backups
  **must not be auto-deleted**.

## Allowed actions

In and around P2-c, the following non-destructive actions are allowed:

- **inspect** — read logs and artifacts for audit and observability;
- **copy / backup** — duplicate evidence to a backup location (the original
  copy stays in place);
- **compress** — only in a future explicit phase, on reviewed, confirmed input;
- **rotate** — only in a future explicit phase, by **explicit human-confirmed**
  action.

Inspecting and copying never remove the source. Compression and rotation are
deferred to the future phases below and are out of scope for P2-c.

## Forbidden actions in P2-c

The following are **forbidden** in this phase:

- **automatic deletion** of any log or artifact — these paths **must not be
  auto-deleted**;
- **cron cleanup** — no cron job may prune, rotate, or delete these paths;
- **DB mutation** — no orchestrator database write;
- **GitHub mutation** — no issue/PR/branch change on GitHub;
- **branch / worktree deletion** — including `git worktree remove`;
- **`git clean`** — never run `git clean` against a runtime worktree;
- **`git reset`** — never run `git reset` against a runtime worktree;
- **`git worktree prune`** — never run `git worktree prune` in this phase;
- **executor / validator execution** — no `executor` run and no `validator` run
  is triggered by retention work.

There is no `git clean`, no `git reset`, no `git worktree prune`, no `cron
cleanup`, no `DB mutation`, no `GitHub mutation`, and no `executor` or
`validator` invocation in P2-c.

## Recommended future phases

Cleanup, rotation, and migration are deliberately split into later, separately
reviewed phases:

- **P2-d — retention inventory.** A read-only inventory that classifies each
  retained path against this policy and reports candidates, without deleting
  anything.
- **P2-e — human-confirmed log rotation.** Rotate the active cron JSONL log only
  by **explicit human-confirmed** action, preserving the rotated segments as
  evidence.
- **P2-f — artifact migration / backup.** Migrate or back up historical and
  disposition artifacts to durable storage before any deletion is ever
  considered.

Each future phase remains explicit, reviewed, and recorded. None of them is
enabled by this document.

## Safety principles

- **Retain proof-of-work before cleanup.** Preserve the relevant
  **proof-of-work** before any cleanup is even considered.
- **Never delete the only copy of evidence.** Copy or back up first; deletion of
  a sole copy is never acceptable.
- **Runtime logs are operational evidence.** The cron JSONL log is live
  observability evidence, not a disposable scratch file.
- **Archive and closeout artifacts are disposition records.** Evidence-archive
  and task-closeout artifacts are **disposition evidence** and **closeout
  evidence** respectively — durable disposition records, not disposable scratch
  files.
- **Cleanup must be explicit, reviewed, and recorded.** Any future removal must
  be an **explicit human-confirmed** action that is reviewed and recorded.

Human review remains the final gate. This document records a retention policy
only; it does not authorize, schedule, or perform any deletion, rotation,
compression, move, prune, reset, crontab change, DB write, GitHub call, executor
run, or validator run.
