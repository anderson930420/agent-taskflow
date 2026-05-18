# Operator Issue-to-Draft-PR Dogfood Runbook

This runbook describes the current semi-automatic dogfood path for moving one
human-written issue/spec through local agent-taskflow evidence and, when the
operator explicitly chooses, toward draft PR creation.

The core rule remains: manage work, not agents. The operator chooses the issue,
starts each deterministic step, inspects the evidence, and decides whether any
GitHub action is appropriate. Human review remains the final gate.

## Purpose

The current operator path is:

```text
human writes GitHub Issue/spec
-> operator explicitly ingests the issue
-> operator explicitly prepares the workspace
-> operator explicitly runs the dispatcher
-> operator inspects review evidence
-> operator creates local PR handoff evidence
-> operator may explicitly push the clean committed task branch
-> operator runs draft PR dry-run or fake-gh smoke
-> operator may create a real draft PR only after all preconditions pass
-> human reviews the PR and decides merge, reject, or rerun
```

This runbook is a human-triggered procedure. It is not automatic issue polling,
automatic workspace preparation, dispatcher-driven PR creation, auto-merge, or
cleanup automation.

## Current Capability

The current system can prove the local/fake-gh chain through:

- issue ingestion
- `issue_spec` artifact and `github_issue_ingested` event
- explicit workspace preparation
- `TaskWorktreeRecord` with `base_sha`
- dispatcher execution
- deterministic validation
- review evidence readback
- final task status `waiting_approval`
- local PR handoff package
- fake draft PR evidence

The fake-gh smoke proves the draft PR command path and evidence writing without
calling real GitHub.

## Current Limitation

Explicit Branch Push Foundation is implemented as an operator-triggered CLI path.
It is dry-run by default and requires `--confirm-push` before the recorded task
branch may be published.

The system still does not push branches automatically. The push foundation only
publishes the existing clean committed task branch recorded in
`TaskWorktreeRecord`. It does not create commits, stage files, stash changes,
force push, merge, clean up, create PRs, or approve PRs.

Real draft PR creation should remain optional/manual and should happen only
after branch push dry-run, optional confirmed branch push, PR handoff review,
and draft PR dry-run all pass.

## Preconditions

- The repository is clean before starting.
- Latest `main` is pulled.
- `.venv` is activated.
- No unreviewed local changes are present.
- The target GitHub Issue exists, or an offline issue JSON fixture exists.
- `gh` is authenticated only if the operator intends to create a real draft PR.
- The operator does not expect auto-merge, auto-approval, or automatic cleanup.
- The operator understands branch publication is explicit and dry-run-first.

## Safe Command Sequence: Local/Fake-gh Proof Path

Use placeholders consistently:

```bash
REPO_PATH=/home/ubuntu/agent-taskflow
TASK_KEY=AT-DOGFOOD-001
ISSUE_NUMBER=<issue number>
DB_PATH=<absolute db path>
ARTIFACT_ROOT=<absolute artifact root>
WORKSPACE_ROOT=<absolute workspace root>
REPO=anderson930420/agent-taskflow
```

Activate the virtual environment and establish a local validation baseline:

```bash
cd "$REPO_PATH"
source .venv/bin/activate
python3 scripts/run_local_validation.py
```

Ingest one issue into the local task mirror. For a real issue, omit
`--issue-json-path`. For an offline fixture, provide an absolute JSON path:

```bash
python3 scripts/ingest_github_issue.py \
  --repo "$REPO" \
  --issue-number "$ISSUE_NUMBER" \
  --db-path "$DB_PATH" \
  --local-repo-path "$REPO_PATH" \
  --artifact-root "$ARTIFACT_ROOT" \
  --task-key "$TASK_KEY"
```

Prepare the isolated worktree explicitly:

```bash
python3 scripts/prepare_task_workspace.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --base-branch main
```

Run the dispatcher explicitly. Choose the executor and validators deliberately
for the dogfood task:

```bash
python3 scripts/run_dispatcher.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --executor manual \
  --validators pytest,openspec
```

Inspect the task and review evidence through the local API or direct store-backed
artifacts. The task must reach `waiting_approval` before PR handoff:

```bash
python3 scripts/create_pr_handoff.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --repo "$REPO"
```

Preview branch publication. This only checks the recorded worktree and prints
the inert command preview; it must not publish anything:

```bash
python3 scripts/push_task_branch.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --dry-run
```

Run draft PR creation in dry-run mode first. This must not call `gh`:

```bash
python3 scripts/create_draft_pr.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --repo "$REPO" \
  --dry-run
```

For the fully local fake-gh proof path, run the smoke script. This creates a
temporary local workspace, generates PR handoff evidence, injects a fake gh
runner, writes `draft_pr.json`, and records `draft_pr_created` without real
GitHub mutation:

```bash
python3 scripts/run_draft_pr_fake_gh_golden_path_smoke.py
```

Inspect the generated artifacts named in command output, including
`issue_spec.md`, `mission_contract.json`, validator logs, `pr_handoff.json`,
`pr_handoff.md`, and, for fake/real draft PR creation, `draft_pr.json`.

## Real Draft PR Creation Caution Path

Real draft PR creation is optional and should not be the default dogfood path.

Only consider it when all of these are true:

- The task exists.
- The task status is `waiting_approval`.
- Review evidence has been inspected by a human/operator.
- `pr_handoff.json` exists and remains conservative.
- The prepared worktree and branch are present.
- Branch push dry-run has passed.
- The branch has been published by the explicit branch push command, or the
  remote head branch already exists.
- Draft PR dry-run has been run first.
- The operator explicitly chooses draft PR creation.

Branch push dry-run first:

```bash
python3 scripts/push_task_branch.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --dry-run
```

Only if the worktree is clean, the current branch matches the recorded task
branch, the branch is not a protected/base branch, and the branch has committed
work beyond `base_sha`, the operator may publish the branch:

```bash
python3 scripts/push_task_branch.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --confirm-push
```

That command only publishes an existing clean committed task branch. It does not
create commits, force push, merge, clean up, create PRs, approve PRs, delete
branches, or delete worktrees.

Draft PR dry-run next:

```bash
python3 scripts/create_draft_pr.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --repo "$REPO" \
  --dry-run
```

Only if every check passes, the operator may run the explicit confirmation
command:

```bash
python3 scripts/create_draft_pr.py \
  --task-key "$TASK_KEY" \
  --db-path "$DB_PATH" \
  --repo "$REPO" \
  --confirm-create-pr
```

That command creates draft PRs only. It does not push, merge, approve, clean up,
delete branches, delete worktrees, edit issues, or mutate GitHub Projects.

## Expected Evidence

- `issue_spec` artifact
- `github_issue_ingested` event
- `TaskWorktreeRecord` with `base_sha`
- `mission_contract` artifact
- executor artifact
- validation result
- review evidence
- `pr_handoff.json`
- `pr_handoff.md`
- `pr_handoff_created` event
- `branch_push.json`, only after confirmed branch publication
- `branch_pushed` event, only after confirmed branch publication
- `draft_pr.json`, only after fake or real draft PR creation
- `draft_pr_created` event, only after fake or real draft PR creation

## Failure Handling

Common blockers:

- Dirty repository before starting.
- Missing `.venv` activation.
- Missing `fastapi` or `uvicorn` because the virtual environment is not active.
- Missing `gh` authentication for real draft PR creation.
- Missing issue or inaccessible offline issue fixture.
- Task is not `waiting_approval`.
- Missing prepared worktree.
- Missing `pr_handoff` artifact.
- Dirty worktree; commit or handle changes before pushing.
- Task branch has no commits beyond `base_sha`.
- Head branch is unavailable remotely.
- Validators failed.
- Review evidence is unavailable.
- `openspec` is skipped because it is not installed.

When a blocker occurs, stop and preserve evidence. Do not reinterpret failed
validators as approval. Do not mark human approval on behalf of the reviewer.

## Human Gates

- Human chooses the issue/spec.
- Human decides whether to run the executor.
- Human reviews validation and artifacts.
- Human decides whether to create a draft PR.
- Human reviews the PR.
- Human decides merge, reject, rerun, or block.
- There is no auto-merge.
- There is no auto-approve.
- There is no cleanup automation.

## What This Runbook Is Not

This runbook is not:

- a background worker
- a GitHub issue poller
- a webhook or polling loop
- a queue scheduler
- an auto PR creator
- a dispatcher auto-PR creation path
- an auto-merge system
- a cleanup system
- a real AI executor dogfood proof

## Next Phases

- First real operator branch publication dogfood using the explicit push CLI.
- First real dogfood task with Pi/OpenCode.
- Mission Control read-only exposure for PR handoff and draft PR evidence.
- Future issue queue/polling only after the semi-automatic path is stable.
