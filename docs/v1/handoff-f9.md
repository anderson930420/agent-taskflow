# V1 F9: explicit integration queue tick

`scripts/run_integration_tick.py` visits one repository's initial queue snapshot
in the existing FIFO order (`enqueued_at`, then sequence). Priority does not
change this order. The invocation returns after that bounded pass; entries
added or re-enqueued during the pass wait for the next invocation.

The database, GitHub repository key, local repository path and validator
configuration are explicit. The database must already exist and have the
operator-installed Ticket fields. The tick verifies each genuine Ticket's
repository, target branch and registered worktree against its stored binding
before calling the existing integration controller. It never initializes a new
database or migrates Ticket fields.

## Run a preview

Create a JSON validator configuration with a nonempty array of named argv
commands. Commands run in each Ticket's worktree through the existing validator
runner; choose meaningful project checks. For example:

```json
[
  {"name": "unit-tests", "command": ["python3", "-m", "unittest", "discover", "-s", "tests"], "timeout_seconds": 600},
  {"name": "diff-check", "command": ["git", "diff", "--check"], "timeout_seconds": 30}
]
```

```bash
python3 scripts/run_integration_tick.py \
  --db-path /absolute/disposable/state.db \
  --repo owner/repository \
  --repo-path /absolute/disposable/repository \
  --validator-config /absolute/disposable/validators.json
```

Use the exact repository key stored by the queue producer. `--target-branch`
defaults to `main`; `--remote` defaults to `origin`. The default mode is dry-run.
Add `--confirm-integration` to execute the pass. `--dry-run` explicitly selects
the default and cannot be combined with confirmation.

## Results and safety boundaries

The JSON result records configuration, initial queue size, per-Ticket sequence,
priority, controller result or refusal reason, and remaining queue keys. Exit
code 0 means all visited outcomes succeeded (including a successful preview or
an empty queue); 1 means a reported refusal/failure; 2 means invalid input or an
uncaught setup error. Invalid CLI syntax uses the standard argument-parser error.

The unchanged controller owns integration status transitions, validators,
normal branch push, draft PR creation/update, dequeue and its per-repository
lock. The tick never acquires an outer integration lock. `lock_unavailable`
ends the pass immediately, leaving the current Ticket queued. An unexpected
exception also ends the pass with an observable error. Neither case retries.

Other controller refusals and validator failures are recorded once and later
snapshot entries may proceed. Paused, failed and nonready Tickets retain the
controller's existing refusal semantics. Completed entries are dequeued by the
controller and are not replayed by a later tick. Validator failure retains the
existing `needs_decision` boundary. No lock is retained across human review.

This command does not schedule itself. Watcher polling, target-freshness
production, PR outcome polling, cleanup, cron and service wiring remain separate
F10/deployment work. It never merges or approves a PR or force-pushes.

## Acceptance and handoff

Focused tests exercise real disposable local Git worktrees with a fake GitHub
adapter: FIFO versus priority, repository isolation, confirmation, validator
failure, controller refusal, binding checks, lock contention and repeated ticks.
Validation evidence must identify the exact source/base and terminal command
results. Local tests alone do not satisfy the standing real GitHub gate.

That gate uses a fresh throwaway GitHub repository and two genuine Tickets:
enqueue LOW priority first and HIGH priority second, make independent changes
with meaningful validators, then drain both to `needs_review` through this tick.
After a human merges the first draft PR, explicitly run the existing freshness
producer for the second Ticket and invoke this tick again. Verify the same
second PR, updated base provenance and a normal push. Producer invocation is
fixture setup at the F10 boundary; it is not wired into this tick. Human merge
is required and must remain a recorded pending gate until it happens.

Rollback is a human-reviewed revert of these additive tick, CLI, test and
handoff files. Existing controller and lifecycle implementation remain intact.
