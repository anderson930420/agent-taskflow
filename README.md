# Agent Taskflow

Agent Taskflow is a Python-native, GitHub-oriented, Symphony-style agent
orchestration system.

Its core principle is:

> Manage work, not agents.

AI coding tools such as Pi, OpenCode, Codex, Claude Code, or future tools are
bounded implementation workers. Agent Taskflow manages task state, workspace
selection, executor invocation, validation, proof-of-work collection, and the
handoff to human review.

## Current Architecture

Agent Taskflow uses human-authored GitHub Issues or specs as task input. The
current ingestion path mirrors one issue into a local SQLite store and records
the issue/spec as reviewable evidence. Ingestion is explicit and local-first; it
does not run as a webhook, background poller, or automatic GitHub sync loop.

For implementation, Agent Taskflow prepares isolated git worktrees and records
their branch, base ref, and base commit in the local store. Bounded executor
adapters such as Pi and OpenCode run inside those prepared workspaces. They are
implementation workers only; they do not own orchestration, validation,
approval, push, PR creation, merge, or cleanup policy.

After an executor run, deterministic validators produce proof-of-work. Current
validator paths include pytest, optional openspec, policy checks, changed-files
checks, and smoke tests. Validator results, executor logs, changed-file
evidence, issue/spec artifacts, handoff metadata, branch publication evidence,
and draft PR evidence are recorded as reviewable artifacts.

Mission Control is a review and evidence dashboard. It surfaces task state,
executor metadata, validator results, artifacts, and dogfood evidence readback.
It is not the execution core, and the dogfood evidence readback surface is
read-only.

Human review remains the final gate. Agent Taskflow does not auto-merge, does
not let workers self-approve, and does not perform automatic cleanup.

## Current Semi-Automatic Dogfood Loop

The current dogfood loop is operator-driven and semi-automatic:

1. A human writes or selects a GitHub Issue/spec.
2. The operator explicitly ingests the issue into the local SQLite mirror.
3. The operator explicitly prepares an isolated worktree.
4. The operator runs the dispatcher with a selected bounded executor and
   validators.
5. Deterministic validators record proof-of-work.
6. The task reaches `waiting_approval` for human review when validation passes.
7. The operator may generate local PR handoff evidence.
8. Branch push is explicit through `scripts/push_task_branch.py`.
9. Draft PR creation is explicit through `scripts/create_draft_pr.py`.
10. A human reviews the evidence and decides what happens next.

The explicit branch push and draft PR creation scripts are dry-run by default
and require confirmation flags before they mutate GitHub. They do not merge,
approve, clean up, delete branches, delete worktrees, or run automatically from
the dispatcher.

## Deferred Automation

The following are intentionally deferred:

- Queue or polling automation for selecting and starting new tasks.
- Webhook/background GitHub issue sync.
- Dispatcher-driven workspace creation.
- Dispatcher-driven branch push or PR creation.
- Automatic merge after approval.
- Automatic cleanup, branch deletion, or worktree deletion.
- Remote worker pools and multi-host scheduling.

These are governance and lifecycle decisions, not executor behavior.

## Operator Flow

Run the local validation baseline before dogfood work:

```bash
source .venv/bin/activate
python3 scripts/run_local_validation.py
```

Ingest one GitHub Issue into the local mirror:

```bash
python3 scripts/ingest_github_issue.py \
  --repo owner/repo \
  --issue-number 123 \
  --db-path /absolute/path/to/state.db \
  --local-repo-path /absolute/path/to/repo \
  --artifact-root /absolute/path/to/artifacts \
  --task-key AT-123
```

Prepare an isolated worktree:

```bash
python3 scripts/prepare_task_workspace.py \
  --task-key AT-123 \
  --db-path /absolute/path/to/state.db \
  --base-branch main
```

Run executor preflight before a real executor path:

```bash
python3 scripts/run_real_executor_preflight.py \
  --executor opencode \
  --validators pytest,openspec
```

Dispatch the task explicitly:

```bash
python3 scripts/run_dispatcher.py \
  --task-key AT-123 \
  --db-path /absolute/path/to/state.db \
  --executor opencode \
  --validators pytest,openspec
```

Generate local PR handoff evidence after the task reaches `waiting_approval`:

```bash
python3 scripts/create_pr_handoff.py \
  --task-key AT-123 \
  --db-path /absolute/path/to/state.db \
  --repo owner/repo
```

Preview branch publication:

```bash
python3 scripts/push_task_branch.py \
  --task-key AT-123 \
  --db-path /absolute/path/to/state.db \
  --dry-run
```

Preview draft PR creation:

```bash
python3 scripts/create_draft_pr.py \
  --task-key AT-123 \
  --db-path /absolute/path/to/state.db \
  --dry-run
```

## Safety Boundaries

- Executors are bounded implementation workers.
- Validators are deterministic proof-of-work gates.
- SQLite is orchestrator state storage.
- FastAPI exposes state and evidence for review.
- Mission Control is observability and review, not the execution core.
- Approval metadata is a human review gate.
- Workers cannot self-approve, push, merge, or clean up.
- No automatic merge, automatic push, automatic PR creation, or automatic
  cleanup is implied by validation success.

## Historical Note

Older Hermes/Kanban extraction scripts and docs may still exist as historical
context, but they are not the current primary architecture. The current system
is the local SQLite, explicit worktree, bounded executor, deterministic
validator, proof-of-work, PR handoff, and human review loop described above.
