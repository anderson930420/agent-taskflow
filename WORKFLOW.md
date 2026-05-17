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
