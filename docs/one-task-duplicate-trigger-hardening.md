# One-Task Duplicate Trigger Hardening

## Scope

This document defines the duplicate-trigger boundary for the existing one-task
watcher and task-to-draft-PR pipeline. It documents the single-use behavior
only. It does not introduce a continuous scheduler, multi-task queue, daemon,
cron job, webhook, task lease table, auto-approval, auto-merge, or cleanup
automation.

## Already Processed

A task counts as already processed for the one-task draft-PR path when the
local mirror has all of the evidence needed to prove the previous run reached
draft PR handoff:

- the task is in `waiting_approval`
- runtime execution evidence exists and validates for the task
- branch push evidence exists and validates for the task branch
- draft PR evidence exists and validates for the task branch

For the watcher explicit `task_key` resume path, the selection gate also
requires a `waiting_approval` task with existing `draft_pr` artifact evidence.
The downstream pipeline then re-validates and reuses the matching runtime,
handoff, branch-push, and draft-PR evidence before reporting success.

## Explicit Task Resume

An operator may rerun the watcher or `run_task_to_draft_pr_pipeline` with the
same explicit `task_key` and resume flags:

- `resume_existing=True`
- `resume_pr_preparation=True`

That rerun is a resume/readback operation over existing proof-of-work. It may
return `draft_pr_already_created`, `resume_already_processed=true`, and
`duplicate_trigger_suppressed=true`. It must not call the approved task runner,
push the branch again, or create another draft PR.

## Select-First Rerun

Confirmed first-candidate mode selects only `candidates[0]` from the current
preview. After a task has already reached `waiting_approval` with draft PR
evidence, it is no longer an eligible first candidate.

A second confirmed first-candidate invocation must therefore fail or no-op
safely, such as `no_eligible_candidates`. It must not silently reselect the
completed task and must not fall back to an explicit-task resume. The operator
must name the task with `task_key` when they intend to inspect or resume an
already processed task.

## Forbidden Duplicate Effects

Duplicate-trigger suppression forbids these effects on a rerun with completed
draft PR evidence:

- no approved task runner rerun
- no branch repush
- no duplicate draft PR
- no merge
- no approval
- no local or remote cleanup
- no branch deletion
- no worktree deletion
- no Mission Control write action

The result safety block reports whether new runner, branch push, or draft PR
work happened in the current invocation. On a suppressed duplicate trigger,
those fields must remain false even when prior evidence proves the earlier
successful run.

