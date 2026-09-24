# Agent Taskflow / Mission Control Instructions

You are working in the `agent-taskflow` repository. This file is the short entrypoint for every coding agent here: builders, reviewers, and the orchestrator. It summarizes and links. Where it disagrees with an authoritative source, the source wins, and you report the conflict.

The core principle is:

> Manage work, not agents.

Agent Taskflow is a Practical V1 control plane for AI-assisted GitHub work. Deterministic Python owns Tickets, Attempts, leases, worktrees, validator gates, integration evidence, and cleanup eligibility. Two roles are easy to confuse:

- **Inside the product**, an AI tool run by an executor adapter (Claude Code, Codex, Pi, OpenCode, or a future tool) is a bounded implementation worker in one Ticket worktree. It does not claim Tickets, change lifecycle state, decide validation, approve, push, merge, or clean up (SPEC §2.2, `WORKFLOW.md`, `docs/claude-code-bounded-implementer-executor.md`).
- **When developing this repository**, agent sessions such as Claude Code act as orchestrator, builder, or reviewer under the orchestrator execution contract below. Even then, a human reviews and merges every PR.

## Required Context and Sources of Truth

Authority order (AGENT-EXECUTION §0):

1. Current explicit human ruling. Entries marked OWNER in `RULINGS.md` are human rulings.
2. `AGENT-EXECUTION.md`, the orchestrator execution contract.
3. The Level 2 Roadmap, `agent-taskflow-shortest-level2-roadmap-v2.md`.
4. The Practical V1 Master Spec, `SPEC.md`, read together with `RULINGS.md` and `FOLLOWUPS.md`.
5. Code, tests, and migrations.
6. Historical notes, old issue text, and agent summaries.

A 2026-09-19 human ruling applies the Level 2 Roadmap only where it does not conflict with Practical V1. `README.md` says the same. As a result, the Roadmap's M5 does not authorize auto-merge today (V1 SPEC §34, §41), and nothing restores the legacy `waiting_approval` success path (RULINGS 53). When sources conflict, never silently pick one. Prefer the higher source and record the conflict in your evidence. If the conflict changes architecture, safety, lifecycle semantics, permissions, or merge behavior, stop that line of work as `BLOCKED_HUMAN_DECISION`.

None of the control documents are committed. On the VPS they live in `.agent-taskflow/control/` at the root of the dev checkout (for example `/home/ubuntu/agent-taskflow-dev/.agent-taskflow/control/`). That directory is gitignored, so worktrees under `.worktrees/` do not have it. `SPEC.md`, `RULINGS.md`, and `FOLLOWUPS.md` are also in `~/agent-taskflow-ops/v1/`. If they are not in your environment, say so and work from the committed documents:

- `README.md` / `README.zh-TW.md`: the current V1 flow, what is automated and what is manual, and a table mapping each safety boundary to its enforcement point.
- `WORKFLOW.md`: the repo-owned workflow contract. It covers worker boundaries, changed-files and path policy, proof-of-work, and the Integration Controller boundary. Read it before editing, not after, when a task touches execution workflow, executor or validator behavior, proof-of-work artifacts, workspace or path policy, approval/rejection/rerun/blocking behavior, Mission Control review semantics, or governance. Its "Task Lifecycle" section (`queued -> ... -> waiting_approval`) predates V1 and matches the legacy GitHub-issue path. For Tickets, use SPEC §12 and `agent_taskflow/status_vocab.py`.
- `docs/v1/handoff-*.md`: the implementation decisions and end-to-end evidence for each merged V1 step and F item.
- `docs/v1-step2-integration-controller.md`: the integration module map. `docs/script-map.md` and `docs/scheduler-module-map.md`: the script and scheduler inventories.
- `docs/m0-correctness-baseline-status.md`, `docs/m1-exit-gate-status.md`, `docs/m1-completion-inventory.md`: the Level 2 milestone records. `docs/resolved-launch-evidence.md`, `docs/validation-summary.md`, `docs/executor-process-lifecycle.md`: the M2 evidence pieces on `main`.
- Most other files in `docs/` are dated phase records, and several say so (for example `docs/current-architecture-boundary.md`). Treat them as history, not as current behavior.

## What `main` Does Today

Ticket lifecycle, using SPEC §12 display names. `agent_taskflow/status_vocab.py` is the only mapping to persisted spellings (`ready`=`created`, `running`=`implementing`, `needs_review`=`waiting_for_review`, `completed`=`cleaned`, `cancelled`=`canceled`):

```text
ready --runtime admission (dependencies, lease, capacity)--> preparing -> running -> validating
  -> ready_for_integration  enqueued once in the repository's FIFO integration queue
  -> integrating            latest target, Taskflow validators, push task branch, create/update PR
  -> needs_review           human review on GitHub; a stale branch returns to ready_for_integration
  -> human merge on GitHub -> merge verified in target history -> cleanup -> completed
blocked, paused: never executed.   needs_decision: validator red, unresolvable conflict, changes requested.
failed: runtime or infrastructure failure.   cancelled: includes a PR closed unmerged, whose worktree is kept until cleanup is confirmed.
```

- Since F8 (#203), a successful Ticket skips the legacy `waiting_approval` gate and is enqueued exactly once (`ticket_lifecycle.py`, `integration_handoff.py`). The legacy GitHub-issue path (`approved_task_runner.py` and the one-task automation) still ends at `waiting_approval` and fails to `blocked` (FOLLOWUPS F5).
- Nothing in the repository schedules work. Every tick and queue consumer runs only when someone explicitly invokes it, and the parallel execution tick (`scripts/run_parallel_scheduler_tick.py`) is no exception. Target-freshness polling, PR-outcome polling, and cleanup have no non-test caller (FOLLOWUPS F10). SPEC §47 names short, idempotent cron ticks as the future invoker. Installing cron is a human action.
- F9 (#211) added `scripts/run_integration_tick.py`: one explicit, dry-run-by-default pass over one repository's FIFO queue snapshot that calls `integrate_task` per Ticket and exits (`--confirm-integration` executes). It does not schedule itself; cron and F10's pollers and cleanup remain separate (`docs/v1/handoff-f9.md`).
- Integration runs only when explicitly triggered. `integrate_task` does a dry run unless it is called with `dry_run=False` and `confirm_integration=True` (`integration_controller.py`). That flag triggers integration; it does not approve a merge.
- Taskflow never merges: it never runs `gh pr merge` or an API equivalent (`github_pr_adapter.py`, README). The Integration Controller's only push is `git push origin <task-branch>`, optionally with `-u`. It never force-pushes, never pushes `main`/`master`/`trunk`, and never deletes a remote branch in V1 (`integration_git.py`, `WORKFLOW.md`). The older explicit operator commands for pushing and remote-branch cleanup are described in `WORKFLOW.md`, and each requires its own confirmation flag.
- GitHub CI is shown for review but has no authority over the Taskflow lifecycle. Taskflow validators gate `needs_review` (SPEC §30).
- The runtime limit `max_concurrent_tasks` defaults to 1. It can be raised only with rehearsal evidence bound to the deployed commit (`scripts/runtime_control.py set-capacity --evidence-path`, SPEC §19). This is a different limit from the orchestrator's cap of 2 active implementation tasks (AGENT-EXECUTION §5).

The Level 2 Roadmap is the path to the first safe auto-merge. Its promotion unit is `(project, task_class, policy_version)`:

```text
M0 correctness baseline -> M1 Attempt model + canonical ExecutionEngine path
-> M2 constrained execution, runner evidence, outcome ledger, docs-debt backlog
-> M3 shadow mode (frozen cohort, no auto-merge) -> M4 credential separation, canary, rollback drills
-> M5 controlled docs-only-safe auto-merge
```

A milestone is complete only when every one of its Exit Gates is evidenced (AGENT-EXECUTION §18). Check the status records listed above; do not infer progress from merged PRs. The M2 pieces on `main` include resolved launch evidence (#205), runner validation summaries (#208), and launch provenance (#212). Confirmed Level 2 execution goes through `SchedulerExecutionEngineAuthority` (README). Describe only what is merged on `main`. Open or draft PRs are not current behavior.

## Architecture Boundaries

Use these interpretations consistently:

- The SQLite store is orchestrator state storage. Every lifecycle mutation is an auditable task event (SPEC §44).
- Runtime admission (`runtime_admission.py`, `runtime_capacity.py`) owns atomic claims, leases, capacity, and the dependency gate. CLI, scheduler, and API entrypoints must all reach the same domain logic rather than each having its own semantics (AGENT-EXECUTION §20.4).
- `parallel_scheduler.py` picks eligible Tickets. `dispatcher.py` runs one task through one executor and its validators. It never approves, merges, pushes, or cleans worktrees.
- Executor adapters (`agent_taskflow/executors/`: `manual`, `noop`, `shell`, `opencode`, `pi`, `claude-code`) are deterministic CLI wrappers and result normalizers. The AI tool an adapter runs is the bounded worker.
- Validators (`agent_taskflow/validators/`, `integration_validators.py`) are deterministic proof-of-work gates.
- The Integration Controller (`integration_controller.py`, `integration_queue.py`, `integration_git.py`, `github_pr_adapter.py`, `merge_verification.py`, `integration_cleanup.py`) owns the per-repository queue and lock, PR create/update, merge verification, and cleanup. Step 2 is the only writer of the SPEC §32.1 PR fields.
- FastAPI (`agent_taskflow/api/`) serves Tickets, state, SSE progress, and proof-of-work. Mission Control (`mission-control/`, Next.js) creates Tickets and shows progress. It is not the execution core, and the GitHub PR is the only code-review surface (SPEC §31).
- Artifact metadata is the index of attempt-scoped proof-of-work. Approval metadata is the human gate of the legacy path only.
- Golden-path smoke tests are local acceptance tests. The final gate for a V1 Step or F item is a real end-to-end run against a throwaway GitHub repository with real `git` and `gh`, and the owner performs the merge (RULINGS 43, 56).

## Operating Rules

- Prefer small, reviewable changes.
- Before editing, inspect relevant files and explain intended changes.
- Reuse existing project patterns before introducing new abstractions.
- Keep executor, validator, store, API, and frontend boundaries clean.
- Do not edit unrelated files.
- Do not perform cosmetic rewrites unrelated to the task.
- Do not introduce new dependencies unless explicitly required.
- Do not touch secrets, `.env` files, SSH keys, API keys, tokens, or system credentials.
- Do not weaken tests, validators, governance checks, or safety policies.
- Do not fake success, fake validation, or fabricate artifacts.

## Governance Rules

Do not do any of the following unless the human explicitly asks:

- create commits
- push
- open or update pull requests
- merge
- rebase shared branches
- delete branches
- delete worktrees
- run destructive cleanup
- close issues
- approve tasks
- mark work as finally complete
- bypass validators
- change approval records to imply human approval
- change deployment, systemd, nginx, or cron configuration

There is one exception, and it covers only the first three items. The orchestrator is the session that owns execution state and dispatches builders and reviewers under `AGENT-EXECUTION.md`. It may commit to and push *task branches*, and open or update *draft* PRs, once required validation and independent review have passed. It then stops at `NEEDS_HUMAN_REVIEW` (AGENT-EXECUTION §1, §13, §21, and a 2026-09-19 human ruling). Builders and reviewers do not commit, push, or open PRs. After a human merges, the orchestrator may record the internal task as `COMPLETED` (AGENT-EXECUTION §2.3, §17). That record reconciles state; it is not final approval.

Unless a newer explicit human ruling says otherwise, no agent may merge or enable auto-merge, approve a PR as the final human gate, push to or directly modify `main`, bypass required review or validation gates, weaken acceptance criteria so a task passes, or rewrite history or overwrite evidence to hide a failed attempt (AGENT-EXECUTION §1, §11, §20.3).

These gates are human-only (AGENT-EXECUTION §14): final PR approval and merge to `main`; changing acceptance criteria after implementation begins; accepting an architectural deviation from the spec; weakening a required safety gate; changing credential boundaries; enabling a new autonomy level or task class; overriding a failed rollback or safety drill; and declaring a blocked architectural conflict resolved without code or spec evidence. Installing or changing a cron entry is also a human operator action (SPEC §47.4).

Never run tests, scripts, migrations, or experiments against a production database or the production checkout, and never touch production services. On the VPS, live state is under `~/.agent-taskflow/`. Use a disposable database. If you need production-like data, restore a consistent backup into an isolated location (README, 2026-09-19 human ruling). Production migrations and capacity changes are owner-approved runbook steps (FOLLOWUPS F3).

Subagents may do read-only work only. They never write files, run state-changing git commands, push, run migrations, or touch a database. The main session verifies anything it acts on (RULINGS 25).

Human review remains the final gate. Every merge is a human action on GitHub.

## Builder, Reviewer, and Evidence Contract

Builders (AGENT-EXECUTION §6-7) work from a delegation packet that sets the goal, sources, allowed and forbidden scope, acceptance criteria, required validation and evidence, and definition of done.

- Inspect the existing implementation before editing, and state any conflict with the task's assumptions.
- Implement only within the allowed scope. Add or update tests, run the required validation, and write evidence. Summarize exactly what changed and what is still unresolved.
- Never approve your own work, hide or weaken required validation, change acceptance criteria, broaden scope, change policy semantics, or treat a failed criterion as optional. A builder saying "all tests pass" is advisory until someone inspects the command output.

Reviewers (§9) run in a separate session from the builder. They check that the task is satisfied and scope held, and that the tests cover the changed semantics. They check that the evidence supports the claims, and they examine error and rollback paths, lifecycle and migration invariants, security and permission boundaries, and conflicts with the downstream roadmap. The verdict is one of `APPROVE`, `APPROVE_WITH_NITS`, `REPAIR_REQUIRED`, `BLOCKED_ARCHITECTURE`, or `BLOCKED_SPEC_CONFLICT`, and any non-approval lists concrete findings. A documentation-class finding does not block on its own (RULINGS 33). Reviewers confirm CI or recorded validation on the exact head, then run the delta's tests and reproductions instead of repeating the full suite. A repair round gets a narrow review of the delta (RULINGS 36).

Rounds and statuses (§8, §10, §15): by default, a task gets at most 2 builder rounds and 2 review rounds. If blocking issues remain after the last round, the task is `BLOCKED_HUMAN_DECISION`. If required validation cannot run because of tooling, the task is `BLOCKED_TOOLING`, never a success. If the implementation is wrong but the intended architecture is clear, the task is `NEEDS_REPAIR`.

Evidence (§11-12): repository state plus runner evidence is the authority; an agent's narrative is not. Save commands, exit codes, base and result SHAs, changed files, a scope audit, the decision, and remaining risks wherever the packet says. In the dev checkout that is `.agent-taskflow/evidence/<task>/<attempt>/`, which is gitignored. Every new attempt gets a new identity. Never overwrite a failed attempt's evidence.

## Validation Expectations

CI (`.github/workflows/ci.yml`) runs on every pull request and every push to `main`, using Python 3.12:

```bash
python -m pip install -e . -c constraints.txt
PYTHONPATH=. python -m unittest discover -s tests
PYTHONPATH=. python -m compileall agent_taskflow scripts tests
```

Green CI on the exact PR head is part of the merge gate (RULINGS 9, 36).

Locally, run commands from the repository root with `PYTHONPATH=.`. Use the checkout's `.venv` if it has one, or an isolated venv built with `python -m pip install -r requirements.txt -c constraints.txt`. `pytest` and `pytest-xdist` are not declared dependencies, so install them into that venv when you need them. Choose a lane from the actual diff (2026-09-19 human ruling on a single full runner):

- **Docs-only:** run the tests that pin the changed docs, the workflow validators, and `git diff --check`. Never claim a runtime suite passed.
  - `grep -rln "<changed file name>" tests/` finds the pinned tests. Run each with `PYTHONPATH=. python -m unittest tests.<module> -v`. For example: `tests.test_readme_current_architecture` for the READMEs, `tests.test_docs_maps` for the script and scheduler maps, and `tests.test_workflow_contract` for `WORKFLOW.md`.
  - `PYTHONPATH=. python scripts/validate_workflow_contract.py` and `PYTHONPATH=. python scripts/validate_workflow_policy.py`.
- **Local runtime change:** run the smallest relevant test first. Then run the direct and adjacent caller tests, `compileall`, the workflow validators, and import or collection checks where relevant. Record the selection and why it is enough.
- **High-risk change, unknown impact, CI/test-infrastructure/dependency/discovery change, or a failed, flaky, or uncertain fast gate:** a full pytest run is mandatory: `PYTHONPATH=. python -m pytest tests -q -p no:cacheprovider`. Add `-n 4` when pytest-xdist is installed (RULINGS 40). Keep any failure visible; a different passing gate cannot hide it.
- Run a full local `unittest discover` only when the diff affects unittest runner or discovery compatibility and targeted checks cannot cover it. Record the reason. Do not add an unconditional full unittest run after pytest.
- **Mission Control:** `cd mission-control && npm run build`. For a type-check only, run `npm run typecheck`.
- **Smoke scripts:** run the smoke (`scripts/run_*_smoke.py`) and, when one exists, its unit test (`tests/test_run_*_smoke.py`).
- **Schema or migration changes:** use an explicit `scripts/migrate_*.py`, write rollback notes, and dry-run on a restored database copy before any production use (Roadmap M1 and §6).

Never claim a command passed unless it was actually run and observed to pass. Record each command, its exit code, and a short result.

If a command was not run, say it was not run and explain why.

If a command failed, report the failure clearly.

## Final Report Format

End each implementation task with:

```text
Final Report

1. Starting state
- Branch:
- Git status:

2. Implementation summary
- ...

3. Files changed
- ...

4. Validation
- Command:
  Result:

5. Artifacts
- ...

6. Final state
- Git status:
- Commit created: yes/no

7. Blockers / follow-ups
- ...
```

## Completion Standard

A task is implementation-complete only when:

- the requested change is implemented
- relevant tests pass or failures are clearly reported
- proof-of-work is available
- the final report is accurate
- the work is ready for human review

The task is not finally approved until a human reviewer approves it. For code, that means a human reviews and merges the PR on GitHub.
