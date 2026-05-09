# Agent Taskflow

Reusable human-gated task workflow tooling for AI coding agents.

Agent Taskflow provides scripts, templates, and documentation for running repo-backed AI worker tasks with explicit human approval gates.

## Purpose

This project is designed for workflows where:

1. A human creates a task with a clear spec.
2. An AI worker implements the task in an isolated worktree.
3. The worker produces a completion report and PR.
4. The task waits for human approval.
5. The human accepts, rejects, or requests revision.
6. Accepted tasks are merged and cleaned up by the human-controlled workflow.

## Core principles

- AI workers must not merge into main.
- AI workers must not self-approve.
- Every repo task should use a verified worktree.
- Human approval is required before acceptance.
- Task artifacts should remain auditable.
- The workflow should be reusable across multiple repositories.

## Current status

This repository is being extracted from the original `bullet_journal_app` workflow experiment and is now generalized into a reusable agent task orchestration toolkit.

## Quick start

### 1. Configure your projects

Copy the example config and edit for your repositories:

```bash
cp examples/project_config.yaml config/projects.yaml
# Edit config/projects.yaml with your project paths
```

Each project in `config/projects.yaml` requires these fields:

| Field | Description |
|-------|-------------|
| `project_slug` | Unique identifier (used with `--project`) |
| `task_key_prefix` | Prefix for task keys (e.g. `AT`, `BJ`) |
| `repo_path` | Absolute path to the git repository |
| `github_repo` | GitHub owner/repo (e.g. `owner/repo`) |
| `artifacts_root` | Where task artifacts are stored |
| `worktrees_dir` | Where git worktrees are created |
| `default_branch` | Main branch name (default: `main`) |
| `branch_prefix` | Prefix for worktree branches (default: `worktree/`)

### 2. Create a task

```bash
python3 scripts/kanban_create.py \
  --config config/projects.yaml \
  --project agent-taskflow \
  --task-key AT-0001 \
  --title "My task title" \
  --body-file /tmp/task.md \
  --assignee my-profile \
  --priority 1
```

### 3. Accept / cleanup after merge

```bash
python3 scripts/kanban_accept_cleanup.py \
  --config config/projects.yaml \
  --project agent-taskflow \
  --task-key AT-0001 \
  --task-id t_xxx \
  --decision accepted \
  --merged-commit abc1234 \
  --confirm
```

### 4. Audit workflow state

```bash
python3 scripts/kanban_workflow_regression.py \
  --config config/projects.yaml \
  --project agent-taskflow \
  --task-key AT-0001 \
  --task-id t_xxx \
  --phase review
```
