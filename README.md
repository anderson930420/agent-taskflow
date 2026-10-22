# Agent Taskflow

[English](README.md) | [繁體中文](README.zh-TW.md)

Agent Taskflow runs AI coding work as **Tickets**. Each Ticket runs one bounded
AI executor in its own git worktree, passes deterministic validators, is
integrated with the latest target branch and opened as a GitHub pull request.
The owner reviews and merges it on GitHub; Taskflow then verifies the merge and
cleans up.

Deterministic Python owns the lifecycle: claims, leases, worktrees, validators,
integration, evidence and cleanup. The AI tool is only a worker inside one
worktree. It does not change lifecycle state, push, merge or clean up.

## The Ticket path

```text
create     POST /api/tickets
execute    execution tick: claim the Ticket, prepare its worktree, run the project's
           executor and validators, commit the result -> ready_for_integration
integrate  integration tick, one repository per pass: rebase on the latest target,
           rerun validators, push the task branch, create or update the PR -> needs_review
review     the owner reviews and merges the PR on GitHub
close      the next integration pass records the merge, verifies it in the target
           history and removes the worktree -> completed
```

Each project's executor and validators come from its `execution:` policy in
`config/projects.yaml`; agent-taskflow itself runs the `claude-code` executor and
the `compile` validator. A stale `needs_review` branch returns to
`ready_for_integration` and is integrated again. A Ticket that cannot go on stops
at `needs_decision` (a validator failed, a conflict, changes requested), `failed`
(a runtime failure) or `cancelled` (for example a PR closed without merging).

## Running it

The operator UI is the Taskflow module of the owner's Personal AI Workspace, a
separate app that calls this API server-side; this repository has no UI.

```bash
python scripts/run_api.py --db-path /abs/path/state.db      # API on 127.0.0.1:8100
python scripts/run_parallel_scheduler_tick.py --db-path /abs/path/state.db --jsonl
python scripts/run_integration_tick.py --db-path /abs/path/state.db \
  --repo owner/name --repo-path /abs/path/checkout --jsonl
```

Both ticks do one bounded pass and exit; cron runs each on a short interval.
`deploy/cron/v1-execution-tick.cron.example` and
`deploy/cron/v1-integration-tick.cron.example` show the cron lines and explain
every flag and exit code. Each integration phase is a read-only preview unless
its own `--confirm-*` flag is passed. Both ticks hold a non-overlap lock: an
overlapping run logs `skipped_overlap` and exits 75. Schema changes are explicit
`scripts/migrate_*.py` commands. Installing or changing cron is an owner action.

## Safety boundaries

- Taskflow never merges a pull request: `agent_taskflow/github_pr_adapter.py`
  refuses `gh pr merge` and its API equivalents.
- Its only push is `git push origin <task-branch>`. Taskflow never force-pushes,
  never pushes `main`, `master`, `trunk` or the target branch, and never deletes
  a remote branch (`agent_taskflow/integration_git.py`).
- The owner reviews and merges every PR on GitHub. No agent merges or enables
  auto-merge.
- Cleanup runs only after the merge is verified in the target history, and only
  on the Ticket's own clean worktree (`agent_taskflow/merge_verification.py`,
  `agent_taskflow/integration_cleanup.py`).
- Every lifecycle change is an auditable task event in the SQLite store.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e . -c constraints.txt
.venv/bin/pip install pytest pytest-xdist
PYTHONPATH=. .venv/bin/python -m pytest tests -q -n 4
```

Use a disposable database; never run tests or scripts against live state.
Coding agents start with `CLAUDE.md`. `docs/v1/` records how each V1 step was
built and tested end to end, and `docs/v0-supported-surface.md` labels every
entrypoint.
