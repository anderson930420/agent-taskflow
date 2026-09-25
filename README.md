# Agent Taskflow

[English](README.md) | [繁體中文](README.zh-TW.md)

Agent Taskflow is a Practical V1 control plane for AI-assisted GitHub work. A
deterministic Python lifecycle owns Tickets, attempts, leases, isolated
worktrees, validator gates, integration evidence, and cleanup eligibility. AI
is a bounded implementation executor; it does not own the delivery lifecycle.

The product promise is deliberately narrow: turn a Ticket into a reviewable
GitHub pull request while preserving evidence and keeping the final merge under
human control.

## Current Practical V1 flow

```text
Create Ticket
  -> runtime admission, one worktree, bounded executor
  -> deterministic validation and persisted evidence
  -> ready_for_integration and per-repository FIFO queue
  -> when explicitly invoked: integrate against the latest target, validate,
     push/update a PR
  -> GitHub human review and human merge
  -> when explicitly invoked: detect and verify GitHub's merge result, then
     clean up
  -> completed
```

The implemented producer half is automatic: a successful Ticket skips the
legacy `waiting_approval` gate, transitions from validation to
`ready_for_integration`, and is enqueued once for its repository. This is the
current successful implementation path, not an extra approval step.

The public display vocabulary uses names such as `needs_review` and
`completed`; the existing persisted vocabulary has legacy spellings in places
(for example `waiting_for_review`, `cleaned`, and `canceled`).
`agent_taskflow/status_vocab.py` is the single mapping boundary.

### What is automated today

* Ticket admission enforces dependencies, leases, and configured concurrency.
* Each Ticket owns an isolated worktree; attempts and lifecycle evidence are
  persisted in SQLite.
* The executor path records runtime progress and runs deterministic validators.
* A successful implementation moves to `ready_for_integration` and enters its
  repository's FIFO integration queue exactly once.
* When explicitly confirmed by a caller, the integration controller can fetch
  the latest target, perform initial integration or non-force-push
  re-integration, rerun validators, and create or update the same GitHub PR.
* Components exist to detect stale PRs, poll PR outcomes, verify a GitHub merge
  commit in the target history, and perform gated cleanup.

### Current operator and human gates

F9, the integration-queue consumer tick, is not implemented yet. Consequently
no current non-test caller drains `ready_for_integration`; an operator must
explicitly invoke the integration controller with `dry_run=False` and
`confirm_integration=True`. This is an integration trigger, not a merge
approval. The controller defaults to dry-run/confirmation-required behavior.

After integration, Taskflow can push a task branch and create or update a draft
PR, but **a human reviews and merges it in GitHub**. Taskflow never calls
`gh pr merge`, cannot self-approve, and does not enable auto-merge.

F10's real end-to-end gate (ruling 56) passed on 2026-09-25 against a private
throwaway repository, with the two cron lines as the only drivers. F10 is the
rest of the V1 invocation surface (SPEC §47): one
`scripts/run_integration_tick.py` pass per repository polls PR
outcomes, runs verified-merge cleanup, polls target freshness, then drains the
queue; each phase is a read-only preview unless its own `--confirm-*` flag is
passed. Both tick scripts hold a non-overlap lock and log a `skipped_overlap`
result, and `deploy/cron/v1-execution-tick.cron.example` and
`deploy/cron/v1-integration-tick.cron.example` show the cron lines. Nothing is
installed: installing cron is a human operator action (SPEC §47.4), and which
confirmations cron passes is a human choice. The ticks must not be
described as an installed, autonomous service. Ruling 65 records the
missing-invoker history.

## Safety boundaries and enforcement

| Boundary | Enforcement point |
| --- | --- |
| AI is a bounded worker, not lifecycle authority | Runtime admission and dispatcher (`agent_taskflow/runtime_admission.py`, `agent_taskflow/dispatcher.py`) |
| One Ticket owns one worktree; retries preserve auditable Ticket context | Ticket worktree and attempt-resource services (`agent_taskflow/ticket_worktree.py`, `agent_taskflow/attempt_resources.py`) |
| Execution is bounded and dependency-safe | Atomic claim and capacity controls (`agent_taskflow/runtime_admission.py`, `agent_taskflow/runtime_capacity.py`) |
| A blocked or paused Ticket cannot run | Claim and transition guards (`agent_taskflow/runtime_admission.py`, `agent_taskflow/ticket_lifecycle.py`) |
| Integration is serialized per repository | Integration lock and queue (`agent_taskflow/integration_controller.py`, `agent_taskflow/integration_queue.py`) |
| Published PR branches are never force-pushed | Integration Git policy (`agent_taskflow/integration_git.py`) |
| Protected or target branches are never task-push targets | Branch normalization and push allowlist (`agent_taskflow/integration_git.py`) |
| Validators, rather than GitHub CI, gate `needs_review` | Integration validators (`agent_taskflow/integration_validators.py`) |
| Only GitHub human review can merge | Manual-merge invariant in `agent_taskflow/integration_controller.py`; no merge command is issued |
| Cleanup requires a verified merge (or explicit cancelled-work approval) | `agent_taskflow/integration_cleanup.py` |
| Lifecycle changes remain reviewable | SQLite task events and integration evidence |

GitHub CI is visible review information; it is not a separate Taskflow lifecycle
authority. In the Ticket execution path, validation failures stop for
`needs_decision` and runtime failures use `failed`; integration failures,
including integration-validator failures, stop for `needs_decision`. The legacy
GitHub-issue path retains its older `blocked`/`waiting_approval` vocabulary.
A closed-but-unmerged PR is cancelled and its worktree is retained until an
explicit cleanup confirmation.

## Operational facts and source records

The recorded production capacity is **2 active executor leases**, not an assumed
configuration-file value. The operator deployment record is the read-only
`~/agent-taskflow-ops/v1/RULINGS.md` 52; it also records the default as 1 and
enforcement at the claim transaction. Raising capacity above 1 requires
commit-bound rehearsal evidence. The production preparation recorded in rulings
48 and 50 ran these explicit migrations against a backed-up database:

```text
scripts/migrate_ticket_fields.py
scripts/migrate_runtime_progress.py
scripts/migrate_ticket_worktree_resources.py
```

Those records are deployment evidence, not instructions to run migrations
against an arbitrary database. Migration scripts are explicit and idempotent;
startup does not silently apply the Ticket-field migration.

The implementation and real-GitHub evidence are documented in:

* `docs/v1/handoff-step1.md` — Ticket fields and creation.
* `docs/v1/handoff-step3.md` — runtime progress.
* `docs/v1/handoff-step4.md` and `docs/v1/handoff-step5.md` — capacity,
  admission, attempts, and parallel execution.
* `docs/v1/handoff-step2.md` and `docs/v1/handoff-step2-e2e.md` — integration,
  PR handling, merge verification, and cleanup components.
* `docs/v1/handoff-f4.md`, `docs/v1/handoff-f8.md`, and
  `docs/v1/handoff-f8-e2e.md` — automatic handoff to
  `ready_for_integration`, queue evidence, and the remaining manual trigger.

The governing read-only control records are
`~/agent-taskflow-ops/v1/SPEC.md`, `~/agent-taskflow-ops/v1/RULINGS.md`, and
`~/agent-taskflow-ops/v1/FOLLOWUPS.md`. The higher-level Master Spec and Level 2
Roadmap remain design inputs only where they do not conflict with Practical V1
or later human rulings. In particular, they do not authorize auto-merge or
restoration of the legacy `waiting_approval` success path.

## Canonical ExecutionEngine authority

Confirmed Level 2 scheduler execution routes through
`SchedulerExecutionEngineAuthority` and its canonical `ExecutionEngine` result.
`--use-execution-engine` remains a compatibility flag; it does not restore a
legacy scheduler fallback. This authority remains canonical wherever confirmed
Level 2 execution is invoked; it does not alter F8's specified Ticket success
status or the human GitHub merge gate.

## What remains before an unattended V1 round

* **F7 (this documentation update):** reconciles the public description with
  the merged F4/F8 behavior.
* **F9:** build one idempotent integration tick that drains each repository
  queue in FIFO order. It must call the existing controller; it must not change
  the controller's lock or merge semantics.
* **F10:** call the other existing consumer components from the integration
  tick, add non-overlap locks to both ticks, and ship uninstalled cron examples.
  Its real end-to-end run is the gate; installing cron stays a human action.
* **F2:** complete the deferred repository-wide status vocabulary and explicit
  migration cleanup.
* **After F9/F10:** run the first real Ticket. Its observed friction, rather
  than this documentation, prioritizes Step 6, F6, F5, and later work. An open
  PR remains a human-review dependency until it is actually merged.

## Development validation

For a disposable development database and worktree, run the repository
validators rather than treating this README as an operator runbook:

```bash
PYTHONPATH=. .venv/bin/python scripts/validate_workflow_contract.py
PYTHONPATH=. .venv/bin/python scripts/validate_workflow_policy.py
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```

Do not use a production database for local validation or experimentation.
