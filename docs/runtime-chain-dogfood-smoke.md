# Runtime Chain Dogfood Smoke (Phase E)

## Purpose

Phase E does not introduce new runtime automation. It stabilizes the
runtime chain assembled by Phase A through Phase D and proves it is
reproducible end-to-end on a fresh queued task:

- Phase A — `intake_runner_handoff` confirmed mode persists the verifier
  report as a sibling artifact and stamps the handoff payload with
  `verifier_run_id` / `verifier_report_path`.
- Phase B — `queued_task_handoff` confirmed mode requires
  `intake_runner_handoff_artifact_path`, reopens the handoff + verifier
  report, and rechecks `proposal_hash` / `item_hash` / TTL before
  invoking `approved_task_runner`.
- Phase C — `queued_task_handoff` confirmed mode records
  `runtime_preflight_finished`, `runtime_execution_started`, and
  `runtime_execution_finished` events plus a `runtime_handoff_execution`
  artifact (schema `runtime_handoff_execution.v1`).
- Phase D — store / API / Mission Control expose the runtime audit
  evidence through `TaskMirrorStore.list_runtime_audit_events`,
  `GET /api/tasks/{task_key}/runtime-audits`, and the Mission Control
  Runtime Audit panel.

Phase E ties these together in a single, hermetic dogfood smoke that an
operator can run locally to verify the chain is stable.

## Chain

```
fresh queued TaskRecord
  └── real Task Execution Package
       └── scheduler proposal (confirmed)
            └── scheduler confirmation (confirmed)
                 └── intake-runner handoff (confirmed)
                      + persisted verifier report artifact
                      └── queued_task_handoff (confirmed)
                           ├── runtime_preflight_finished event
                           ├── runtime_execution_started event
                           ├── runtime_execution_finished event
                           └── runtime_handoff_execution artifact
                                └── approved_task_runner
                                     └── fake executor + fake validator
                                          └── task → waiting_approval
                                               └── read-only API readback
                                                    /runtime-audits
                                                    /artifacts
                                                    /validations
```

Every step uses the real production helper, including
`create_scheduler_proposal`, `create_scheduler_confirmation`,
`create_intake_runner_handoff`, and `run_queued_task_handoff`. The only
substitutions are the injected fake executor + fake validator at the
final dispatch step, so the smoke remains hermetic (no real Pi,
OpenCode, network, or GitHub access).

## Running the smoke

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_runtime_chain_dogfood_smoke.py --pretty
```

CLI options:

- `--workspace-root <abs>` — reuse a specific workspace (otherwise a
  `$TMPDIR/agent-taskflow-runtime-chain-dogfood-smoke-*` directory is
  created and cleaned up after the run unless `--keep-temp` is set).
- `--keep-temp` — keep the temp workspace for operator inspection.
- `--cleanup-temp` — force cleanup (default unless `--keep-temp`).
- `--task-key <key>` — task key to use (default `RUNTIME-SMOKE-0001`).
- `--base-branch <branch>` — base branch for the temp git repo.
- `--executor <name>` — executor slot (the smoke always uses an injected
  fake executor; this only changes the slot name).
- `--json` / `--pretty` — output format.

The smoke writes its SQLite state to
`<workspace-root>/runtime-chain-dogfood-smoke.db` and never touches the
real `~/.agent-taskflow/state.db`.

## Expected success signals

- `final_status = "waiting_approval"`.
- `runtime_audit.runtime_event_kinds` contains
  `runtime_preflight_finished`, `runtime_execution_started`, and
  `runtime_execution_finished`.
- `runtime_audit.runtime_execution_artifact_path` exists on disk and is
  the `runtime_handoff_execution.v1` artifact.
- `api_readback.runtime_audits_count >= 3` and every item advertises
  `not_action_evidence=true` and `not_validation_authority=true`.
- `api_readback.artifact_types` includes `runtime_handoff_execution`.
- `validation.validation_result_count >= 1` and at least one validator
  result reaches `status="passed"`.

## Safety boundary

The Phase E smoke is observation only. It deliberately enforces these
invariants and surfaces them in its `safety` block:

- No scheduler loop.
- No background worker.
- No automatic task picking.
- No batch execution.
- No GitHub mutation.
- No branch push.
- No PR creation.
- No merge.
- No approval or rejection.
- No cleanup of real branches or worktrees.
- No production DB mutation by default — every run uses a temp
  workspace + temp DB + temp artifact root.

Runtime audit evidence remains observation only. It is **not** action
evidence, and `runtime_execution_finished` is **not** validation
authority. `validation_result` events surfaced through
`GET /api/tasks/{task_key}/validations` and the Mission Control
Validation Results section remain the authoritative validator record.
Mission Control remains read-only; Phase E adds no UI controls.
