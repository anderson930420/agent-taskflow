# Evidence-Only / Superseded Task Archive

This document describes `scripts/archive_task_evidence_only.py`, an
operator-confirmed archive path for **evidence-only** and **superseded** tasks.

> Manage work, not agents. Archiving an evidence-only or superseded task is an
> explicit operator decision backed by preserved proof-of-work, never an
> automatic or worker-driven action.

## This is not merged-PR closeout

`archive_task_evidence_only.py` is **not** merged-PR closeout. It
**does not replace** `scripts/confirm_task_closeout.py`, and it does not weaken
it.

The two commands cover different situations and must not be confused:

| Situation | Command |
| --- | --- |
| A task whose draft PR was created, verified, pushed, and **merged**, with local + remote branch cleanup evidence | `scripts/confirm_task_closeout.py` (stricter) |
| An evidence-only / no-op / superseded / stale / manually-salvaged task that never produced the full draft PR pipeline evidence | `scripts/archive_task_evidence_only.py` |

### Use `confirm_task_closeout.py` for full draft PR pipeline closeout

`confirm_task_closeout.py` is intentionally **stricter**. It is the correct tool
for the real merged-PR lifecycle and requires, before it will mark a task
`completed`/`done`:

- a recorded **draft PR artifact** and a **draft PR event**;
- **verified** draft PR evidence (`pr_created` / `draft_pr_created` true,
  `pr_number` and `pr_url` present);
- local cleanup evidence and remote branch cleanup evidence;
- a **merged** GitHub PR (verified via `gh pr view` or an offline fixture).

That strictness is correct and **must not be weakened**. For example, when
`GH-9604` was manually reconstructed and merged as PR #78, a
`confirm_task_closeout.py` dry-run correctly **blocked** the original task,
because the original task lacks the draft PR pipeline evidence:

- the draft PR artifact record is missing;
- the draft PR event is missing;
- draft PR evidence must be verified;
- `pr_created` / `draft_pr_created` are not available.

That block is the intended behaviour. Do not route evidence-only or superseded
tasks through `confirm_task_closeout.py`, and do not relax its checks to make
them pass.

### Use `archive_task_evidence_only.py` for evidence-only / superseded tasks

Use `archive_task_evidence_only.py` for tasks that should be closed out for
evidence-only or superseded reasons, where the merged-PR pipeline does not
apply:

- smoke-only / no-op tasks;
- blocked tasks that were superseded by later work;
- stale branch-push tasks;
- tasks manually salvaged by a separately reconstructed PR.

It does **not** require draft PR evidence, does **not** require local cleanup
evidence, does **not** call GitHub, and does **not** inspect or delete
filesystem worktrees.

## Reason codes

A reason code is **required** and must be one of:

- `salvaged_by_pr` — the task's work was manually salvaged by a separately
  reconstructed PR.
- `smoke_evidence_only` — a smoke task that produced evidence only, with no
  publishable change.
- `superseded_by_later_smoke` — a smoke task superseded by a later smoke run.
- `no_op_evidence` — the task produced no-op evidence.
- `stale_policy_blocked` — the task is a stale, policy-blocked leftover.
- `stale_branch_push` — the task is a stale branch-push leftover.
- `obsolete_queued` — an obsolete queued task that should not be executed.

## Default behaviour: dry-run, no DB write

The command is **dry-run by default**. It performs **no DB write** unless
`--confirm-evidence-archive` is present. A run without `--dry-run` and without
`--confirm-evidence-archive` is **blocked** and changes nothing, so you can
preview safely.

On a confirmed run it:

- updates the task status to `--target-status` (default `archived`);
- writes a deterministic JSON evidence artifact under the artifact root;
- records a `task_evidence_archive` artifact;
- records a `task_evidence_archived` task event.

## Examples

Preview (dry-run) a manually salvaged task:

```bash
PYTHONPATH=. .venv/bin/python scripts/archive_task_evidence_only.py \
  --task-key GH-9604 \
  --reason-code salvaged_by_pr \
  --superseded-by-pr 78 \
  --dry-run
```

Confirm the same archive (`GH-9604` salvaged by PR #78):

```bash
PYTHONPATH=. .venv/bin/python scripts/archive_task_evidence_only.py \
  --task-key GH-9604 \
  --reason-code salvaged_by_pr \
  --superseded-by-pr 78 \
  --confirm-evidence-archive
```

Archive a smoke-only evidence task (`AT-GH-74`):

```bash
PYTHONPATH=. .venv/bin/python scripts/archive_task_evidence_only.py \
  --task-key AT-GH-74 \
  --reason-code smoke_evidence_only \
  --confirm-evidence-archive
```

Archive a smoke task superseded by a later smoke (`AT-GH-69`, superseded by
`AT-GH-74`):

```bash
PYTHONPATH=. .venv/bin/python scripts/archive_task_evidence_only.py \
  --task-key AT-GH-69 \
  --reason-code superseded_by_later_smoke \
  --superseded-by-task AT-GH-74 \
  --confirm-evidence-archive
```

Archive a stale policy-blocked task (`GH-9601`):

```bash
PYTHONPATH=. .venv/bin/python scripts/archive_task_evidence_only.py \
  --task-key GH-9601 \
  --reason-code stale_policy_blocked \
  --confirm-evidence-archive
```

## Safety boundaries

This command intentionally does **not**:

- mutate GitHub in any way (**no GitHub mutation**);
- close a GitHub issue;
- merge, approve, push, or create a PR;
- delete a local or remote branch (**no deletion**);
- inspect or remove filesystem worktrees (**no deletion**);
- run cleanup scripts or any cleanup automation (**no cleanup automation**);
- start an executor or a validator;
- modify cron, systemd, nginx, or deployment configuration;
- add automation, a scheduler loop, a background worker, a webhook, or a
  polling loop.

The recorded evidence carries explicit safety flags asserting all of the above
are `false`, that `db_written` is `true` only on a confirmed success, and that
the archive `is_merged_pr_closeout = false`.

Human review remains the final gate. A task is not finally approved or merged by
this command; it only records an operator-confirmed evidence-only / superseded
archive disposition.
