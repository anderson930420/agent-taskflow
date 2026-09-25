# V0 supported surface

Status: V0 Scope Freeze, batch 1 (owner RULINGS 74, 80 and 81; orchestrator ruling OR-12). This file is the one list of what V0 supports. Nothing reads it at runtime. `tests/test_v0_supported_surface.py` keeps it complete and pins the ACTIVE_V0 set.

## The V0 happy path

RULINGS 80 names one product path, and V0 has exactly one execution path:

```text
normal Ticket creation (POST /api/tickets)
-> formal execution tick (scripts/run_parallel_scheduler_tick.py -> scheduler_worker -> Dispatcher)
-> one real executor -> validators pass -> ready_for_integration
-> formal integration tick (scripts/run_integration_tick.py) -> PR
-> human merge on GitHub -> merge verification
-> explicit human-confirmed cleanup (run_integration_tick.py --confirm-cleanup) -> completed
```

Nothing on that path may be replaced by a manual DB edit, a manual status change, a manual enqueue, a fake or dry-run executor, a skipped validator, or a direct call to an internal function (RULINGS 80 §13).

## Labels

- **ACTIVE_V0**: the V0 happy path executes it, or it is an operator action the owner requires for V0: `set-capacity 1`, pausing legacy or frozen work, the explicit migrations, retrying a Ticket without editing the DB, and explicit human-confirmed cleanup.
- **FROZEN**: a V1 or Level 2 capability that Ticket #1 does not need. It is kept and not deleted (directive §3).
- **LEGACY**: the pre-V1 GitHub-issue, Hermes, and `waiting_approval` path, or PR-4/PR-5 compatibility. It is kept and not deleted (directive §0: disable or freeze first, delete later).

The `ticket_guard` column says how the entrypoint keeps V1 Tickets out:

- `n/a`: it never changes a Ticket, or it is on the V0 path itself.
- `d1_refusal`: it refuses a V1 Ticket through D1's `legacy_entrypoint_ticket_refusal` (RULINGS 67).
- `status_gated`: it acts only from a legacy status (`waiting_approval`, legacy `queued`) that a post-F8 Ticket never holds. It has no explicit refusal.
- `unsupported_in_v0`: it calls `agent_taskflow.v0_surface.require_supported_in_v0` (guards G1-G14), which raises `UnsupportedInV0`.
- `owner_decision`: the owner ruled on it separately (see the row's note).

## The entrypoint guards are the invariant

Every `unsupported_in_v0` entrypoint checks whether its task is a V1 Ticket (`tasks.prompt IS NOT NULL`) **before any write, git/gh subprocess or artifact write, in every mode, dry-run included**. It refuses in its own native form, with reason code `v1_ticket_legacy_entrypoint_refused`:

- a script prints one JSON result and exits 2;
- an API route returns HTTP 409 with `ok: false`;
- a module function returns its blocked result, or raises the error it already uses for refusals.

A legacy task (no `prompt`) keeps its prior behaviour.

These guards are what isolate V0. The live database holding no V1 Ticket today (OR-12 F7) is migration-state evidence, not an invariant (RULINGS 81). No guard may be skipped or weakened because "no Ticket can be in that state".

The check reuses D1 unchanged, so it fails closed the same way. With no database path, or a database that exists but cannot be read, the entrypoint refuses. A database file that does not exist yet holds no Ticket, so the check passes.

## `POST /api/tasks/{task_key}/start` and the Start button

`/start` refuses every V1 Ticket with 409 (G5, RULINGS 80 and 81). The Mission Control Start button therefore shows a 409 for a Ticket. The only acceptable future design for a Ticket Start button is:

```text
Start button -> canonical domain command -> the Ticket enters a scheduler-claimable state -> the scheduler tick executes it
```

The HTTP endpoint never launches an executor.

## V0 operator surface

These are the ACTIVE_V0 operator commands, besides the three happy-path entrypoints:

- `scripts/runtime_control.py`: `set-capacity 1`; pause or kill at task, project or global scope. This is how a legacy, frozen or old Ticket is kept out of the execution tick (Part 2 §6). A pause stops future claims but not a Ticket that is already running; a kill request stops it. It replaces the `/block` action for Tickets.
- `scripts/reset_task_status.py`: retries a stopped Ticket without a DB edit (§7.12).
- `scripts/reap_stale_runtime.py`: the standalone lease reaper. The execution tick also reaps.
- `scripts/terminate_executor_process.py`: operator kill of a registered executor or validator process group.
- The explicit `scripts/migrate_*.py` migrations listed below, run through the owner-approved runbook (FOLLOWUPS F3).
- `scripts/run_integration_tick.py --confirm-cleanup`: the explicit, human-confirmed cleanup after merge verification.

Old Tickets in `created` or `queued` would become execution-tick candidates once their project gets an `execution:` policy. Pause them with `runtime_control.py`; the ready queue and the claim both honour it.

## Surface table

Routes are the `APIRoute`s of `create_app()`. FastAPI's generated `/openapi.json`, `/docs`, `/docs/oauth2-redirect` and `/redoc` are not listed. Installers are the `install_*()` calls in `agent_taskflow/__init__.py`, in call order. Their labels come from the V0 inventory (import order sets the class bases; see `tests/test_v0_installed_runtime_chain.py`).

| surface | kind | label | ticket_guard | note |
|---|---|---|---|---|
| `scripts/run_api.py` | script | ACTIVE_V0 | n/a | Serves `POST /api/tickets` and the routes below. |
| `scripts/run_parallel_scheduler_tick.py` | script | ACTIVE_V0 | n/a | The execution tick: the only V0 execution path. |
| `scripts/run_integration_tick.py` | script | ACTIVE_V0 | n/a | The integration tick; `--confirm-cleanup` is the explicit human-confirmed cleanup. |
| `scripts/runtime_control.py` | script | ACTIVE_V0 | n/a | `set-capacity 1`; task/project/global pause and kill. |
| `scripts/reset_task_status.py` | script | ACTIVE_V0 | n/a | Retries a stopped Ticket without a DB edit. |
| `scripts/reap_stale_runtime.py` | script | ACTIVE_V0 | n/a | Standalone lease reaper. |
| `scripts/terminate_executor_process.py` | script | ACTIVE_V0 | n/a | Operator kill of a registered process group. |
| `scripts/migrate_ticket_fields.py` | script | ACTIVE_V0 | n/a | Explicit migration; the execution tick fails closed without it. |
| `scripts/migrate_ticket_worktree_resources.py` | script | ACTIVE_V0 | n/a | Explicit migration; the execution tick fails closed without it. |
| `scripts/migrate_runtime_progress.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/migrate_runtime_admission.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/migrate_canonical_runtime_admission.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/migrate_task_attempt_lifecycle.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/migrate_attempt_resources.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/migrate_lifecycle_control.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/migrate_project_class_controls.py` | script | ACTIVE_V0 | n/a | Explicit migration; backs project-scope pause. |
| `scripts/migrate_executor_process_lifecycle.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/migrate_reset_lineage.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/migrate_validator_process_lifecycle.py` | script | ACTIVE_V0 | n/a | Explicit migration (F3 runbook). |
| `scripts/ticket_dependency.py` | script | FROZEN | n/a | Ticket-native `blocked_by` in the tick's own domain, audited. Not guarded: it is the formal way to undo an accidental `blocked_by`. |
| `scripts/run_concurrency_rehearsal.py` | script | FROZEN | n/a | Disposable fixtures; the >1 capacity gate is suspended at capacity 1. |
| `scripts/audit_m1_exit_gate.py` | script | FROZEN | n/a | Level 2 M1; read-only / DB copies. |
| `scripts/run_m1_canonical_execution_path_rehearsal.py` | script | FROZEN | n/a | Level 2 M1 rehearsal on disposable state. |
| `scripts/run_m1_db_copy_rehearsal.py` | script | FROZEN | n/a | Level 2 M1 rehearsal on a DB copy. |
| `scripts/run_m1_dual_write_observation.py` | script | FROZEN | n/a | Level 2 M1; not a V0 gate. |
| `scripts/run_m1_project_class_control_rehearsal.py` | script | FROZEN | n/a | Level 2 M1 rehearsal on disposable state. |
| `scripts/run_real_executor_preflight.py` | script | FROZEN | n/a | Read-only report. |
| `scripts/summarize_atomic_temp_orphans.py` | script | FROZEN | n/a | Read-only report. |
| `scripts/summarize_local_workspace_inventory.py` | script | FROZEN | n/a | Read-only report. |
| `scripts/summarize_workflow_policy.py` | script | FROZEN | n/a | Read-only report. |
| `scripts/validate_workflow_contract.py` | script | FROZEN | n/a | Read-only validator of `WORKFLOW.md`. |
| `scripts/validate_workflow_policy.py` | script | FROZEN | n/a | Read-only validator of the workflow policy. |
| `scripts/write_workflow_policy_summary_artifact.py` | script | FROZEN | n/a | Writes a policy summary artifact only. |
| `scripts/report_workflow_policy_review_evidence.py` | script | FROZEN | n/a | Read-only report. |
| `scripts/run_dispatcher.py` | script | LEGACY | d1_refusal | Refuses a Ticket (exit 2). |
| `scripts/run_approved_task.py` | script | LEGACY | d1_refusal | `run_approved_task` refuses a Ticket. |
| `scripts/run_queued_task_handoff.py` | script | LEGACY | d1_refusal | `run_queued_task_handoff` refuses a Ticket. |
| `scripts/run_execution_engine_approved_task.py` | script | LEGACY | d1_refusal | The ExecutionEngine adapter refuses a Ticket. |
| `scripts/run_runtime_handoff_execution_from_handoff.py` | script | LEGACY | d1_refusal | Refused transitively by `run_approved_task`. |
| `scripts/run_one_shot_task_pipeline.py` | script | LEGACY | d1_refusal | Refused transitively by `run_approved_task`. |
| `scripts/run_github_issue_one_task_automation.py` | script | LEGACY | d1_refusal | Refused transitively; its ingestion step is also G12. |
| `scripts/run_github_issue_one_task_scheduler_tick.py` | script | LEGACY | d1_refusal | The ExecutionEngine adapter refuses a Ticket. |
| `scripts/run_scheduler_watcher_one_task.py` | script | LEGACY | status_gated | Acts from `waiting_approval` only. |
| `scripts/run_task_to_draft_pr_pipeline.py` | script | LEGACY | status_gated | Acts from `waiting_approval` only. |
| `scripts/run_pr_preparation_pipeline.py` | script | LEGACY | status_gated | Acts from `waiting_approval` only. |
| `scripts/confirm_branch_push.py` | script | LEGACY | status_gated | Acts from `waiting_approval` only. |
| `scripts/confirm_draft_pr.py` | script | LEGACY | status_gated | Acts from `waiting_approval` only. |
| `scripts/create_draft_pr.py` | script | LEGACY | status_gated | Acts from `waiting_approval` only. |
| `scripts/create_pr_handoff.py` | script | LEGACY | status_gated | Acts from `waiting_approval` only. |
| `scripts/confirm_task_closeout.py` | script | LEGACY | status_gated | Mutates only from `waiting_approval`. |
| `scripts/create_task_execution_package.py` | script | LEGACY | status_gated | Acts from legacy `queued`; writes an artifact and an event, never a status. |
| `scripts/archive_task_evidence_only.py` | script | LEGACY | unsupported_in_v0 | G6: could write `cleaned`/`canceled`/`archived`/`rejected` on a Ticket. |
| `scripts/push_task_branch.py` | script | LEGACY | unsupported_in_v0 | G7 (`branch_push.push_task_branch`): only `integration_git` pushes a Ticket branch. |
| `scripts/record_existing_draft_pr.py` | script | LEGACY | unsupported_in_v0 | G8 (`draft_pr_record.record_existing_draft_pr`): Step 2 is the only PR-evidence writer. |
| `scripts/confirm_local_cleanup.py` | script | LEGACY | unsupported_in_v0 | G9 (`local_cleanup_confirm.confirm_local_cleanup`): would bypass merge verification. |
| `scripts/confirm_remote_branch_cleanup.py` | script | LEGACY | unsupported_in_v0 | G10 (`remote_branch_cleanup_confirm.confirm_remote_branch_cleanup`): V1 never deletes a remote branch. |
| `scripts/prepare_task_workspace.py` | script | LEGACY | unsupported_in_v0 | G11: only `ensure_ticket_worktree` owns a Ticket worktree. |
| `scripts/ingest_github_issue.py` | script | LEGACY | unsupported_in_v0 | G12 (`github_issue_ingestion.ingest_github_issue`): an explicit `--task-key` could rewrite a Ticket row. |
| `scripts/create_pi_smoke_task.py` | script | LEGACY | unsupported_in_v0 | G13: an explicit `--task-key` could overwrite a Ticket row and worktree record. |
| `scripts/retry_advisory_evidence_transition.py` | script | LEGACY | unsupported_in_v0 | G14 (`advisory_evidence_retry.run_advisory_evidence_retry`): would write legacy `waiting_approval`. |
| `scripts/ingest_selected_github_issues.py` | script | LEGACY | n/a | `github_issue_intake`: writes only derived `AT-GH-<n>` keys and refuses an existing key; not reached by G12. |
| `scripts/intake_github_issues.py` | script | LEGACY | n/a | `github_issue_intake_gate`: writes only derived `GH-<n>` keys; not reached by G12. |
| `scripts/kanban_accept_cleanup.py` | script | LEGACY | owner_decision | FROZEN for V0 (RULINGS 81): a label only, with no code guard. It reads no Taskflow DB, so it cannot tell a Ticket; `--confirm` removes `<worktrees_dir>/<task-key>`, the same path as a Ticket worktree. Never run it on a Ticket key. |
| `scripts/kanban_create.py` | script | LEGACY | n/a | No Taskflow DB; `git worktree add` fails on an existing Ticket worktree path. |
| `scripts/create_pr_handoff_package.py` | script | LEGACY | n/a | Artifact only; push only as a dry-run preview. |
| `scripts/create_scheduler_proposal.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/create_scheduler_proposal_from_candidate.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/create_scheduler_confirmation.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/create_scheduler_confirmation_from_proposal.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/create_scheduler_confirmation_verifier_report.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/verify_scheduler_confirmation.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/review_scheduler_proposal.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/create_intake_runner_handoff.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/create_intake_runner_handoff_from_verifier_report.py` | script | LEGACY | n/a | Artifacts and events, no status. |
| `scripts/run_codex_advisory_review.py` | script | LEGACY | n/a | Advisory artifacts only. |
| `scripts/run_scheduler_watcher_preview.py` | script | LEGACY | n/a | Read-only dry-run preview. |
| `scripts/discover_github_issues.py` | script | LEGACY | n/a | Read-only. |
| `scripts/discover_scheduler_candidates.py` | script | LEGACY | n/a | Read-only. |
| `scripts/list_task_recommendations.py` | script | LEGACY | n/a | Read-only. |
| `scripts/recommend_next_tasks.py` | script | LEGACY | n/a | Read-only. |
| `scripts/recommend_post_merge_cleanup.py` | script | LEGACY | n/a | Read-only. |
| `scripts/summarize_waiting_approval.py` | script | LEGACY | n/a | Read-only. |
| `scripts/summarize_real_scheduled_execution.py` | script | LEGACY | n/a | Read-only. |
| `scripts/summarize_execution_observability_payload.py` | script | LEGACY | n/a | Read-only. |
| `scripts/kanban_workflow_regression.py` | script | LEGACY | n/a | Disposable fixtures. |
| `scripts/run_local_validation.py` | script | LEGACY | n/a | Local checks; no Ticket state. |
| `scripts/run_draft_pr_fake_gh_golden_path_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_intake_runner_handoff_hardening_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_issue_to_prepared_workspace_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_issue_to_waiting_approval_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_minimal_runtime_handoff_execution_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_mission_control_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_one_shot_task_pipeline_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_pi_executor_golden_path_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_pr_handoff_golden_path_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_pr_preparation_pipeline_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_prepared_workspace_golden_path_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_real_approved_task_runner_integration_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_runtime_chain_dogfood_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_scheduler_confirmation_preparation_hardening_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_scheduler_confirmation_verifier_report_hardening_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_scheduler_proposal_creation_hardening_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_scheduler_watcher_one_task_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_scheduler_watcher_preview_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_task_to_draft_pr_pipeline_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_workflow_policy_artifact_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_workflow_policy_pow_package_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `scripts/run_workflow_policy_review_evidence_smoke.py` | script | LEGACY | n/a | Smoke on a fixture DB. |
| `POST /api/tickets` | api | ACTIVE_V0 | n/a | The V0 entry: creates a Ticket. |
| `GET /api/tickets` | api | ACTIVE_V0 | n/a | Read. |
| `GET /api/tickets/{task_key}` | api | ACTIVE_V0 | n/a | Read. |
| `GET /api/repositories` | api | ACTIVE_V0 | n/a | Read: the registry for Ticket creation. |
| `GET /health` | api | ACTIVE_V0 | n/a | Read. |
| `GET /api/realtime/board` | api | ACTIVE_V0 | n/a | SSE progress (SPEC §14), read. |
| `GET /api/realtime/stream` | api | ACTIVE_V0 | n/a | SSE progress, read. |
| `GET /api/tasks/{task_key}/realtime` | api | ACTIVE_V0 | n/a | SSE progress, read. |
| `GET /api/tasks/{task_key}/realtime/stream` | api | ACTIVE_V0 | n/a | SSE progress, read. |
| `GET /api/tasks/{task_key}/attempts` | api | ACTIVE_V0 | n/a | Read-only evidence (§7.11). |
| `GET /api/tasks/{task_key}/runtime-audits` | api | ACTIVE_V0 | n/a | Read-only evidence. |
| `GET /api/tasks/{task_key}/evidence` | api | ACTIVE_V0 | n/a | Read-only evidence. |
| `GET /api/tasks/{task_key}/review-evidence` | api | ACTIVE_V0 | n/a | Read-only evidence. |
| `GET /api/tasks/{task_key}/validations` | api | ACTIVE_V0 | n/a | Read-only evidence. |
| `GET /api/tasks/{task_key}/runs` | api | ACTIVE_V0 | n/a | Read-only evidence. |
| `GET /api/tasks/{task_key}/artifacts` | api | ACTIVE_V0 | n/a | Read-only evidence. |
| `GET /api/tasks/{task_key}/artifacts/{artifact_name}` | api | ACTIVE_V0 | n/a | Read-only evidence. |
| `GET /api/tasks` | api | FROZEN | n/a | Read; lists Tickets too. |
| `GET /api/tasks/{task_key}` | api | FROZEN | n/a | Read. |
| `GET /api/projects` | api | FROZEN | n/a | Read. |
| `GET /api/tasks/{task_key}/approvals` | api | LEGACY | n/a | Read. |
| `GET /api/scheduler/candidates` | api | LEGACY | n/a | Read. |
| `GET /api/scheduler/proposals` | api | LEGACY | n/a | Read. |
| `GET /api/scheduler/confirmations` | api | LEGACY | n/a | Read. |
| `GET /api/tasks/{task_key}/scheduler-candidate` | api | LEGACY | n/a | Read. |
| `GET /api/tasks/{task_key}/scheduler-proposals` | api | LEGACY | n/a | Read. |
| `GET /api/tasks/{task_key}/scheduler-confirmations` | api | LEGACY | n/a | Read. |
| `POST /api/tasks` | api | LEGACY | n/a | Creates a prompt-less legacy row; any existing key, a Ticket included, gets 409. |
| `POST /api/tasks/{task_key}/validate` | api | LEGACY | n/a | Always 501. |
| `POST /api/tasks/{task_key}/start` | api | LEGACY | unsupported_in_v0 | G5: a Ticket runs only through the execution tick (see above). |
| `POST /api/tasks/{task_key}/block` | api | LEGACY | unsupported_in_v0 | G1: use `runtime_control.py` pause or kill instead. |
| `POST /api/tasks/{task_key}/reject` | api | LEGACY | unsupported_in_v0 | G2: legacy `rejected` on a Ticket. |
| `POST /api/tasks/{task_key}/approve` | api | LEGACY | unsupported_in_v0 | G4: legacy approval on a Ticket. |
| `POST /api/tasks/{task_key}/prepare-workspace` | api | LEGACY | unsupported_in_v0 | G3: only `ensure_ticket_worktree` owns a Ticket worktree. |
| `agent-taskflow-local-validation` | cli | FROZEN | n/a | Local checks; no Ticket state. |
| `agent-taskflow-github-issue-one-task-automation` | cli | LEGACY | d1_refusal | Refused through `run_approved_task`; its ingestion step is also G12. |
| `agent-taskflow-github-issue-one-task-scheduler-tick` | cli | LEGACY | d1_refusal | The ExecutionEngine adapter refuses a Ticket. |
| `agent-taskflow-codex-advisory-review` | cli | LEGACY | n/a | Advisory artifacts only. |
| `install_canonical_runtime_path` | installer | ACTIVE_V0 | n/a | The atomic claim and lease owner. Its `run_approved_task` half is LEGACY and dormant. |
| `install_attempt_scoped_runtime_path` | installer | ACTIVE_V0 | n/a | Attempt/evidence binding and Ticket failure mapping. Its runner half is LEGACY and dormant. |
| `install_attempt_scoped_runtime_compat` | installer | LEGACY | n/a | PR-4 public symbol and a non-git fallback that V0 does not reach (F2, a later §6.4 item). Load-bearing for 4 tests; not changed. |
| `install_lifecycle_reason_compat` | installer | ACTIVE_V0 | n/a | `canonical_runtime_*` reason codes the V0 claim records. |
| `install_lifecycle_runtime_path` | installer | ACTIVE_V0 | n/a | Terminal outcome and lease release. |
| `install_lifecycle_entrypoint_controls` | installer | ACTIVE_V0 | n/a | Pause/kill check at dispatch. Its runner half is LEGACY and dormant. |
| `install_executor_process_reason_compat` | installer | ACTIVE_V0 | n/a | Executor failure-path reason codes. |
| `install_executor_process_runtime_path` | installer | ACTIVE_V0 | n/a | Managed executor launch bound to the Attempt. |
| `install_reset_runtime_path` | installer | ACTIVE_V0 | n/a | Reset-reserved retry and the claim's runtime-control gate. Its advisory-retry part is LEGACY. |
| `install_validator_process_reason_compat` | installer | ACTIVE_V0 | n/a | Validator failure-path reason codes. |
| `install_validator_process_runtime_path` | installer | ACTIVE_V0 | n/a | Final store: managed validator processes bound to the Attempt. |
