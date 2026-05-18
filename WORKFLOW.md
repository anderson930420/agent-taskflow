# agent-taskflow Workflow Contract

`WORKFLOW.md` is the repo-owned workflow contract for agent-taskflow. It
describes how tasks should be executed by deterministic orchestration code and
bounded AI coding workers.

This file is a human-readable contract skeleton. It is not parsed or enforced
by runtime code in this phase.

## Purpose

agent-taskflow is deterministic Python orchestration code. It manages task
lifecycle, workspace selection, executor invocation, validation,
proof-of-work collection, and human review gates.

AI coding agents such as Pi, OpenCode, Codex, Claude Code, or future tools are
bounded implementation workers invoked through executor adapters. They do not
own scheduling, task selection, lifecycle state transitions, validation
decisions, approval decisions, merge behavior, push behavior, or cleanup
behavior.

Workflow policy may be included in prompts for context, but AI workers are not
trusted to enforce workflow policy by prompt adherence alone. Enforcement
belongs to deterministic code, validators, changed-files checks, workspace
checks, git checks, and human review.

## Component Ownership

| Component | Ownership |
| --- | --- |
| Dispatcher / orchestrator | Deterministic lifecycle and scheduling code |
| Workspace manager | Deterministic workspace setup foundation; cleanup policy remains deferred |
| Executor adapter | Deterministic CLI wrapper and result normalizer |
| AI coding agent | Bounded implementation worker |
| Validator | Deterministic proof-of-work checker |
| Human reviewer | Final approve / reject / rerun / block decision maker |

The workspace manager foundation can prepare isolated local git worktrees for
tasks. Cleanup policy remains deferred and must stay human-controlled or
deterministic-policy-controlled.
Explicit CLI and API entrypoints can request workspace preparation before
dispatcher execution. The dispatcher consumes recorded prepared workspaces; it
does not silently create them.

The prepared workspace golden-path smoke
(`scripts/run_prepared_workspace_golden_path_smoke.py`) proves the local flow
from explicit workspace preparation through dispatcher execution, validation,
artifact readback, and `waiting_approval`. It is local-only and does not add
GitHub Issue sync, PR creation, push, merge, cleanup automation, or dispatcher
auto-create behavior.

The GitHub Issue ingestion foundation
(`scripts/ingest_github_issue.py`) provides explicit, CLI-first, read-only
ingestion of one human-written GitHub Issue/spec into the local task mirror.
It records a local task, issue/spec artifact, and ingestion event only. It does
not dispatch, prepare workspaces, create PRs, push, merge, clean up, mutate
GitHub, or run as a webhook/background worker.

The issue-to-prepared-workspace golden-path smoke
(`scripts/run_issue_to_prepared_workspace_smoke.py`) proves the current
explicit local chain from offline issue ingestion to prepared workspace
dispatch and review evidence readback. It is local-only and does not add
GitHub sync automation, PR creation, push, merge, cleanup, webhooks,
background polling, frontend changes, or dispatcher auto-create behavior.

The PR Handoff Foundation (`scripts/create_pr_handoff.py`) generates local
handoff artifacts for a task that has already reached `waiting_approval`. It
summarizes task state, prepared worktree metadata, branch/base information,
changed files, executor evidence, validator evidence, artifact evidence,
review evidence, and proposed draft PR metadata. It is handoff evidence only:
it does not create PRs, push, merge, rebase, clean up, delete branches, remove
worktrees, mutate GitHub, dispatch tasks, prepare workspaces, run executors, or
run in the background. Human review remains the final gate before any GitHub
action.

The PR handoff golden-path smoke
(`scripts/run_pr_handoff_golden_path_smoke.py`) proves the current local chain
from offline issue ingestion through explicit workspace preparation,
dispatcher execution, validation, review evidence readback, and local PR
handoff package generation. It is local-only and does not create PRs, push,
merge, clean up, mutate GitHub, run webhooks/background polling, change the
frontend, or run real AI executors.

The Actual Draft PR Creation Foundation (`scripts/create_draft_pr.py`) provides
an explicit CLI-only path from existing local PR handoff evidence to a GitHub
draft PR. It is dry-run by default and requires `--confirm-create-pr` before it
may call `gh pr create --draft`. It creates draft PRs only. It does not push,
merge, approve, clean up, delete branches or worktrees, mutate issues/projects,
run background workers, or run automatically from dispatcher, ingestion,
workspace preparation, validators, or PR handoff smoke. Human review remains
the final gate.

The Draft PR Creation Fake-gh Golden Path Smoke
(`scripts/run_draft_pr_fake_gh_golden_path_smoke.py`) proves the current local
chain from offline issue ingestion through PR handoff and draft PR evidence
generation using a fake gh runner only. It does not create real PRs, push,
merge, approve, clean up, mutate GitHub, run webhooks/background polling, change
the frontend, or run real AI executors.

The Operator Issue-to-Draft-PR Dogfood Runbook
(`docs/operator-issue-to-draft-pr-dogfood.md`) documents the current
human-triggered semi-automatic procedure for issue ingestion, workspace
preparation, dispatcher execution, review evidence, PR handoff, draft PR
dry-run, and fake-gh proof. It is documentation and safety guidance only; it
does not add GitHub mutation automation, push, merge, approval, cleanup,
background workers, webhooks, frontend actions, or real AI executor changes.

The First Real Executor Dogfood Report
(`docs/first-real-executor-dogfood-report.md`) records the first real Pi
executor dogfood run from GitHub Issue ingestion through workspace preparation,
deterministic validation, review evidence, branch push evidence, draft PR
handoff, and human review gate. It is historical documentation only and does
not change dispatcher, workspace, push, PR, approval, merge, cleanup, or
Mission Control behavior.

The Explicit Branch Push Foundation (`scripts/push_task_branch.py`) provides an
explicit CLI-only path to publish the task branch recorded in
`TaskWorktreeRecord` from the prepared worktree. It is dry-run by default and
requires `--confirm-push` before it may run `git push --set-upstream`. It blocks
dirty worktrees, protected/base branches, and task branches with no commits
beyond `base_sha`. It does not force push, create commits, create PRs, merge,
approve, clean up, delete branches or worktrees, run background workers, or run
automatically from dispatcher, ingestion, workspace preparation, validators, PR
handoff, or draft PR creation.

## Task Lifecycle

The intended task lifecycle is:

```text
queued
-> running
-> validating
-> waiting_approval
-> approved / rejected / blocked
```

Workers cannot self-approve, self-merge, push, or clean up workspaces. Human
review and deterministic orchestration policy decide what happens after
validation.

## Workspace Policy

- Each task or run should use an isolated workspace.
- Direct writes to the main working tree should be avoided for agent runs.
- The workspace path must be recorded.
- The workspace manager foundation prepares task worktrees under
  `<repo>/.worktrees/<task-key>` from a resolved base ref and records the base
  commit SHA when integrated with the local store.
- Failed runs should preserve the workspace where useful for debugging.
- Cleanup should be human-controlled or deterministic policy-controlled, not
  worker-controlled.

This foundation does not implement GitHub Issue sync, PR creation, merge, push,
remote worker scheduling, frontend expansion, or automatic cleanup/delete
behavior.

## Executor Policy

Allowed executor adapters currently include:

- `manual`
- `shell`
- `opencode`
- `pi`

Future adapters, such as `codex` or `claude-code`, may be added later.

Executor adapters are deterministic wrappers. They are responsible for command
construction, workspace selection, environment setup, log capture, artifact
routing, and standardized run results. The external AI coding tool invoked by
an adapter is a bounded worker.

## Validation Policy

Validators are proof-of-work gates. They decide whether evidence passes. AI
worker claims alone are insufficient.

Current or intended validator categories include:

- `pytest`
- `openspec`, optional if available
- `policy`
- `changed-files`
- `typecheck`
- `lint`
- smoke tests
- future contract/workflow validators

Validation results should be recorded as reviewable proof-of-work artifacts.

## Changed-Files / Path Policy

The changed-files validator follows the Phase 79 path policy semantics:

- `forbidden_paths` wins.
- `allowed_paths` constrains changed files when non-empty.
- Untracked, modified, deleted, renamed, and copied files must be auditable.
- Artifact outputs outside the repo should be treated separately from repo diff
  audit.

The changed-files validator is a deterministic scope guard for executor output.

## Proof-of-Work Artifacts

The target contract includes these artifact categories:

- run summary
- implementation prompt
- mission contract
- executor log
- validation report
- changed-files audit
- artifact index
- handoff / review decision metadata

Not all artifact categories are required to exist immediately. This section
defines the target contract for future hardening.

## Human Review Gate

Human review decides:

- approve
- reject
- rerun
- block

Approval does not imply automatic merge, automatic push, or automatic cleanup.
Those actions remain outside worker authority and require separate deterministic
or human-controlled policy.

## Non-Goals

agent-taskflow should not provide:

- AI self-orchestration loops
- self-selected tasks
- self-validation
- self-approval
- automatic merge
- automatic push
- automatic cleanup/delete
- production GitHub issue sync yet
- remote worker pools yet
- multi-host scheduling yet

## Future Machine-Readable Contract

This Markdown file may later be paired with a machine-readable workflow schema.
That future schema could support deterministic validation of workflow policy,
executor permissions, workspace rules, and artifact requirements.

This phase only establishes the human-readable contract skeleton.
