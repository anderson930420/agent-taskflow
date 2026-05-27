# Real Approved Task Runner Integration Smoke (Level 6C)

## Purpose

Level 6C real approved_task_runner integration smoke proves that the Level 6A
runtime handoff path can invoke the real `approved_task_runner` interface in a
controlled, isolated, non-destructive fixture.

The smoke exercises the actual `run_approved_task` function — not a fake — using
a safe shell executor running `true` (a POSIX no-op command) inside an isolated
temporary git repository. It proves end-to-end that the runtime preflight →
approved_task_runner invocation → runtime audit evidence chain works with the
real interface.

## What it exercises

The smoke creates an isolated workspace (temporary directory), an isolated
SQLite DB, an isolated artifact root, and an isolated fixture git repository,
then:

- seeds a queued `TaskRecord` with `repo_path` pointing to the fixture repo
- creates a `scheduler_proposal` artifact/event
- creates a `scheduler_confirmation` artifact/event
- creates a `scheduler_confirmation_verifier_report` artifact/event
- creates an `intake_runner_handoff` artifact/event
- runs `check_runtime_handoff_preflight` against the handoff
- invokes the real `approved_task_runner` interface (via `_RealApprovedTaskRunnerAdapter`)
  with:
  - `executor="shell"`, `command=("true",)` — safe POSIX no-op
  - `validators=("smoke-noop",)` — trivially passing noop validator
  - `preflight=False` — preflight already done by runtime handoff path
- writes a `runtime_handoff_execution` artifact and three runtime audit events:
  `runtime_preflight_finished`, `runtime_execution_started`,
  `runtime_execution_finished`
- reads runtime audit events back via `TaskMirrorStore.list_runtime_audit_events`
- asserts that no forbidden artifacts, events, or payload markers exist

## Confirmation requirement

**--confirm-real-runner is required** to call the real `approved_task_runner`
interface. Default mode does not call approved_task_runner.

Without `--confirm-real-runner`:
- The script exits with a nonzero status.
- No workspace is created.
- No runner is called.

This gate is intentional. Level 6C touches the real runner interface, so
explicit operator confirmation is required before invocation.

## What it does not do

- no scheduler loop
- no background worker
- no automatic task picking
- no approval / merge / cleanup
- no GitHub mutation in the controlled fixture
- no Mission Control action UI
- no runtime trigger API
- no real AI executor (the shell executor runs `true`, a POSIX no-op)
- no worktree writes outside the isolated fixture repo

## Commands

Dry confirmation check (no runner called, exits nonzero):

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_real_approved_task_runner_integration_smoke.py
```

Confirmed smoke (calls the real approved_task_runner interface):

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_real_approved_task_runner_integration_smoke.py --confirm-real-runner
```

Optional: keep workspace after run for inspection:

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_real_approved_task_runner_integration_smoke.py --confirm-real-runner --keep-workspace
```

## Expected summary shape

```json
{
  "ok": true,
  "task_key": "AT-L6C-REAL-RUNNER-SMOKE",
  "workspace_root": "/tmp/agent-taskflow-l6c-real-runner-...",
  "db_path": "...",
  "artifact_root": "...",
  "real_runner_confirmed": true,
  "real_approved_task_runner_called": true,
  "proposal": {"...": "..."},
  "confirmation": {"...": "..."},
  "verifier_report": {"...": "..."},
  "handoff": {"...": "..."},
  "runtime_execution": {
    "runtime_execution_id": "runtime-execution-...",
    "approved_task_runner_called": true,
    "runner_returned": true,
    "runner_ok": true,
    "runner_status": "waiting_approval",
    "runner_phase": "waiting_approval",
    "artifact_path": "..."
  },
  "readbacks": {
    "runtime_audit_event_count": 3,
    "runtime_execution_artifact_count": 1
  },
  "safety": {
    "scheduler_loop_started": false,
    "background_worker_started": false,
    "automatic_task_picking_started": false,
    "approved": false,
    "merged": false,
    "cleanup_performed": false,
    "github_mutated": false
  },
  "forbidden_side_effect_counts": {
    "artifacts": 0,
    "events": 0,
    "payload_markers": 0
  }
}
```

## Safety boundary

- **Real runner smoke is isolated.** The fixture uses a temporary directory, an
  isolated SQLite DB, and an isolated git repository that is created fresh for
  each run and cleaned up afterwards. No writes occur to the main repository.

- **Real approved_task_runner is called only with --confirm-real-runner.** Without
  that flag the script exits with a nonzero status and does not create any
  workspace or call any runner.

- **Runtime audit evidence is not approval.** The `runtime_handoff_execution`
  artifact and the three runtime audit events (`runtime_preflight_finished`,
  `runtime_execution_started`, `runtime_execution_finished`) are audit evidence
  only. They do not approve the task.

- **Runtime audit evidence is not merge.** No branch is pushed, no PR is created,
  and no merge occurs as part of this smoke. The shell executor runs `true` inside
  the isolated fixture worktree.

- **Runtime audit evidence is not cleanup.** No worktree cleanup, no local branch
  deletion, no remote branch deletion.

- **Human review remains required after runtime.** The smoke exists to exercise the
  real runner interface boundary, not to advance a task through workflow approval.
  The task reaches `waiting_approval` status inside the isolated fixture DB only,
  which is discarded after the run.

- **No GitHub mutation in the controlled fixture.** The `true` command does not
  push, create PRs, mutate issues, or call any GitHub API. If `github_mutated`
  were unexpectedly true, the smoke would fail with a safety assertion error.
