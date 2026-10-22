# Agent Taskflow — instructions for coding agents

This is the entrypoint for every coding agent in this repository. `AGENTS.md` is a byte-identical copy for tools that read that name; edit `CLAUDE.md` and copy it over.

> Manage work, not agents.

Agent Taskflow runs AI coding work as **Tickets**. A Ticket runs one bounded AI executor in its own git worktree, passes deterministic validators, is integrated with the latest target branch and opened as a GitHub PR. A human reviews and merges on GitHub; Taskflow then verifies the merge and cleans up.

Two roles are easy to confuse:

- **Inside the product**, the AI tool an executor adapter runs is a bounded worker in one Ticket worktree. It does not claim Tickets, change lifecycle state, decide validation, push, merge or clean up. A session started by an executor adapter is always this worker, even when its Ticket targets this repository.
- **When developing this repository**, an agent session works on a task branch in its own clone or worktree. The owner reviews and merges every PR.

## The Ticket path

There is one execution path. Each hop names its module (under `agent_taskflow/` unless shown):

```text
create:     POST /api/tickets (api/tickets.py -> ticket_creation.py)
execute:    cron, every minute: scripts/run_parallel_scheduler_tick.py -> parallel_scheduler.py
            picks a Ticket (ready_queue.py), prepares its worktree (ticket_worktree.py), starts scheduler_worker.py
claim:      dispatcher.py claims through runtime_admission.py (dependencies, lease, capacity 1)
run:        the executor named by the project's execution policy (execution_policy.py, config/projects.yaml)
            -> implementation validators -> control-plane commit (ticket_success_gate.py)
            -> ready_for_integration, enqueued once (ticket_lifecycle.py, integration_handoff.py)
integrate:  cron, every minute: scripts/run_integration_tick.py -> integration_tick.py, one repository per pass
            phases: PR outcomes -> verified-merge cleanup -> target freshness -> FIFO drain
            drain: integration_controller.integrate_task: rebase on the latest target, integration validators,
            push the task branch (integration_git.py), create or update the PR (github_pr_adapter.py) -> needs_review
review:     the owner reviews and merges on GitHub (never Taskflow)
close:      the next pass records the merge (integration_watcher.py), verifies it in target history
            (merge_verification.py), removes the worktree (integration_cleanup.py) -> completed
```

A stale `needs_review` branch goes back to `ready_for_integration` and is integrated again. Both ticks hold a non-overlap lock (`tick_lock.py`); an overlapping run exits 75.

Statuses use SPEC §12 display names; `status_vocab.py` is the only mapping to the persisted spellings (`ready`=`created`, `running`=`implementing`, `needs_review`=`waiting_for_review`, `completed`=`cleaned`, `cancelled`=`canceled`):

```text
ready -> preparing -> running -> validating -> ready_for_integration -> integrating -> needs_review -> completed
needs_decision: a validator failed, a conflict could not be resolved, or changes were requested.
failed: a runtime or infrastructure failure.   cancelled: includes a PR closed without merging.
blocked, paused: never executed.   Retry a stopped Ticket with scripts/reset_task_status.py.
```

The agent-taskflow execution policy runs the `claude-code` executor and the `compile` validator in both phases (`python -I -m compileall -f`, a few seconds). The full test suite is not a per-Ticket gate.

## Repository map

- `agent_taskflow/`: the package.
  - Tickets and state: `store.py` (SQLite mirror and task events), `status_vocab.py`, `ticket_*.py`, the `*_schema.py` modules.
  - Admission and scheduling: `runtime_admission.py`, `runtime_capacity.py`, `runtime_reaper.py`, `ready_queue.py`, `parallel_scheduler.py`, `scheduler_worker.py`, `tick_lock.py`.
  - Execution: `dispatcher.py`, `execution_policy.py`, `ticket_success_gate.py`, `executors/`, `validators/`, `executor_launch.py`.
  - Integration: `integration_*.py`, `merge_verification.py`, `github_pr_adapter.py`.
  - Evidence: `artifacts.py`, `launch_evidence.py`, `launch_provenance.py`, `outcome_ledger.py`, `validation_summary.py`.
  - `api/`: the FastAPI app (`main.py`, `tickets.py`, `realtime.py`, `review.py`), started by `scripts/run_api.py`.
- Still present from the earlier GitHub-issue path, and next in line to consolidate: `approved_task_runner.py`, the runtime installers that `__init__.py` applies to the dispatcher (several of them carry behaviour the Ticket path relies on), and the API's legacy `/api/tasks/...` action routes. `docs/v0-supported-surface.md` labels every entrypoint ACTIVE_V0, FROZEN or LEGACY.
- `config/projects.yaml`: the repository registry and each project's `execution:` policy.
- `scripts/`: explicit operator commands. The Ticket path uses `run_api.py`, `run_parallel_scheduler_tick.py` and `run_integration_tick.py`; operators also use `runtime_control.py` (capacity, pause, kill), `reset_task_status.py`, `reap_stale_runtime.py`, `terminate_executor_process.py`, `ticket_dependency.py` and the `migrate_*.py` migrations.
- `tests/`: one flat suite, mostly `test_<module>.py`.
- `deploy/cron/v1-*.cron.example`: the two tick lines. `ops/`: the read-only Discord notifier.
- `docs/`: `docs/v0-supported-surface.md` (the entrypoint labels) and `docs/v1/` (the handoff and end-to-end record of each V1 step). The few other files there are historical records kept only because code docstrings or `WORKFLOW.md` cite them. Older documents are in git history.
- The operator UI is the Taskflow module of the owner's Personal AI Workspace, a separate app that calls this API server-side.

## Deployment (VPS)

- The control plane runs from `~/agent-taskflow-runtime/current`, a symlink to `releases/<sha>`, each with its own `.venv`. Deploying means: clone at the merged SHA, `python3 -m venv .venv && .venv/bin/pip install -e . -c constraints.txt`, then switch `current` atomically (`ln -sfn` to a temp link, `mv -T`).
- Cron runs both ticks every minute against the live database; the integration tick passes `--confirm-pr-poll --confirm-freshness --confirm-integration --confirm-cleanup`. The API runs `scripts/run_api.py --db-path <live db>` on `127.0.0.1:8100`.
- `/home/ubuntu/agent-taskflow` is the **managed repository** for agent-taskflow Tickets: Ticket worktrees are cut from its local `main`. Do not develop, switch branches or run tests there; use your own clone or worktree.
- Live state is under `~/.agent-taskflow/`. Never run tests, scripts or experiments against it; use a disposable database. Changing cron, systemd or the deployed release is an owner action unless the owner asks an agent to do it.

## Rules

Safety boundaries, which no change may weaken:

- Taskflow never merges a PR. Its only push is `git push origin <task-branch>`: never a force push, never `main`/`master`/`trunk`, never a remote branch deletion.
- The owner reviews and merges every PR on GitHub. No agent merges, enables auto-merge or pushes to `main`.
- Every lifecycle change is an auditable task event. Never edit the live database or a Ticket's status by hand.
- Do not touch secrets, `.env` files, SSH keys, tokens or credentials.

Working rules:

- Inspect the relevant code before editing, prefer small reviewable changes, and reuse existing patterns.
- Keep the executor, validator, store, API and integration boundaries clean. Do not add dependencies unless the task needs them.
- Agents commit to task branches and open PRs when the owner asks; the owner squash-merges. Write commit messages in English as `area: summary`, with a body that says what changed and why.
- Do not fake success, validation or evidence. Report a failed or skipped command as failed or skipped.

Older planning documents (the Practical V1 SPEC, RULINGS, FOLLOWUPS, the Level 2 roadmap and the orchestrator execution contract) are background. They are not authority; the owner's current instructions are.

## Validation

- CI (`.github/workflows/ci.yml`) installs the package, compiles the sources and imports the Ticket path's entrypoints.
- Locally, run from your clone's root with `PYTHONPATH=.` and a venv built with `pip install -e . -c constraints.txt` plus `pytest pytest-xdist`.
  - Run the tests of what you changed and of its direct callers.
  - For a change to the dispatcher, admission, integration, store, the ticks or the installers in `__init__.py`, run the full suite: `python -m pytest tests -q -p no:cacheprovider -n 4` (about 5 minutes).
  - Also run `python -m compileall -q agent_taskflow scripts tests` and `git diff --check`.
- Never claim a command passed unless you ran it and saw it pass.

## Final report

End an implementation task with: starting branch and status; what changed; files changed; each validation command with its result; final status and whether a commit or PR was made; open follow-ups.
