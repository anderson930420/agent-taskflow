# Script Map

This operator-facing map summarizes the scripts in `scripts/`. It is a
documentation index only. It does not add scheduler runtime consumption, auto-merge, self-approval,
PR creation automation, branch push automation, cleanup automation, or
background scheduler behavior.

Every script listed here must be treated as an explicit operator command or a
local smoke/check helper. None of these paths should be run as a daemon, cron
loop, webhook worker, or implicit scheduler.

## Intake / GitHub Issues

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/discover_github_issues.py` | Discover GitHub Issues eligible for later local intake. | Read-only local output; may call `gh issue list/view`. | No `--confirm-*`; discovery only. | No - no background scheduler behavior. |
| `scripts/ingest_github_issue.py` | Ingest one human-written issue/spec into the local task mirror. | May write local DB rows and issue/spec artifacts; dry-run available. | No `--confirm-*`; operator invokes a specific issue/spec. | No - no background scheduler behavior. |
| `scripts/ingest_selected_github_issues.py` | Ingest an explicitly selected set of GitHub Issues. | May call `gh issue view`; may write local DB rows and artifacts; dry-run available. | No `--confirm-*`; selected issue numbers are explicit input. | No - no background scheduler behavior. |
| `scripts/intake_github_issues.py` | Run deterministic selected issue intake gate. | May call `gh`; writes local DB only when confirmed. | Yes, `--confirm-intake`. | No - no background scheduler behavior. |

## Scheduler Proposal / Confirmation

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/list_task_recommendations.py` | List read-only per-task operator recommendations. | Read-only SQLite/artifact inspection. | No `--confirm-*`; analysis only. | No - no background scheduler behavior. |
| `scripts/recommend_next_tasks.py` | Rank queued tasks for operator review. | Read-only SQLite/artifact inspection. | No `--confirm-*`; analysis only. | No - no background scheduler behavior. |
| `scripts/discover_scheduler_candidates.py` | List read-only scheduler candidates with required next gate and operator action (Phase G — Level 1 discovery). | Read-only SQLite/artifact inspection; no proposal/confirmation/handoff/runtime side effects. | No `--confirm-*`; discovery only. Not execution permission. | No - no background scheduler behavior. |
| `scripts/create_scheduler_proposal.py` | Create a scheduler proposal artifact from recommendations. | Dry-run by default; may write proposal artifacts and DB metadata only when confirmed. | Yes, `--confirm-create-proposal`. | No - no background scheduler behavior. |
| `scripts/review_scheduler_proposal.py` | Review recorded scheduler proposal artifacts. | Read-only SQLite and artifact inspection. | No `--confirm-*`; review only. | No - no background scheduler behavior. |
| `scripts/create_scheduler_confirmation.py` | Record operator-attested selection of proposal items. | Dry-run by default; may write confirmation artifacts and DB metadata only when confirmed. | Yes, `--confirm-create-confirmation`. | No - no background scheduler behavior. |
| `scripts/verify_scheduler_confirmation.py` | Dry-run binding, revalidation, and expiration check for a confirmation item. | Read-only SQLite/artifact inspection; no consumption event or artifact. | No `--confirm-*`; verifier is dry-run only. | No - no background scheduler behavior. |

## Task Execution Package / Queued Handoff

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/create_task_execution_package.py` | Build deterministic task execution package artifacts for a queued task. | Dry-run by default; may write local package artifacts and DB metadata only when confirmed. | Yes, `--confirm-create-package`. | No - no background scheduler behavior. |
| `scripts/run_queued_task_handoff.py` | Hand a queued task package to the approved task runner. | Dry-run by default; confirmed mode may invoke local runner behavior. | Yes, `--confirm-handoff`. | No - no background scheduler behavior. |
| `scripts/run_approved_task.py` | Run one explicitly approved queued task through execution and validation. | Dry-run available; confirmed mode may run executor and validators locally. | Yes, `--confirm-approved-task`. | No - no background scheduler behavior. |
| `scripts/run_dispatcher.py` | Dispatch one task through the dispatcher lifecycle. | Dry-run available; may run configured executor/validators locally. | No `--confirm-*`; explicit task command with dry-run support. | No - no background scheduler behavior. |
| `scripts/prepare_task_workspace.py` | Prepare an isolated task worktree for an existing task. | Writes local worktree and DB worktree metadata. | No `--confirm-*`; explicit task/worktree command. | No - no background scheduler behavior. |
| `scripts/create_pi_smoke_task.py` | Seed a local task for Pi executor smoke testing. | May write local DB/artifacts for smoke setup. | No `--confirm-*`; local smoke setup only. | No - no background scheduler behavior. |

## Executor Smoke / Golden Path

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/run_prepared_workspace_golden_path_smoke.py` | Smoke explicit workspace preparation through dispatcher validation and review evidence. | Creates temporary local repo/DB/artifacts; may run local subprocess git commands. | No `--confirm-*`; local smoke only. | No - no background scheduler behavior. |
| `scripts/run_issue_to_prepared_workspace_smoke.py` | Smoke offline issue ingestion to prepared workspace dispatch. | Creates temporary local repo/DB/artifacts; may run local subprocess git commands. | No `--confirm-*`; local smoke only. | No - no background scheduler behavior. |
| `scripts/run_issue_to_waiting_approval_smoke.py` | Smoke issue intake through package, handoff, execution, validation, and waiting approval. | Creates temporary local repo/DB/artifacts; uses local fake executor/validator behavior. | Uses in-flow explicit confirmation calls for package/handoff steps. | No - no background scheduler behavior. |
| `scripts/run_pi_executor_golden_path_smoke.py` | Exercise golden path with the existing Pi executor. | Dry-run/fake by default; real Pi path may call external Pi CLI. | Yes for real Pi, `--confirm-real-pi`. | No - no background scheduler behavior. |
| `scripts/run_draft_pr_fake_gh_golden_path_smoke.py` | Smoke draft PR creation path using a fake `gh` runner. | Creates local evidence using fake external tool responses only. | No real GitHub confirmation; fake-gh smoke only. | No - no background scheduler behavior. |
| `scripts/run_pr_handoff_golden_path_smoke.py` | Smoke issue ingestion through local PR handoff package generation. | Creates temporary local repo/DB/artifacts; no real push or PR. | No `--confirm-*`; local smoke only. | No - no background scheduler behavior. |
| `scripts/run_real_executor_preflight.py` | Check local dependencies before real executor dogfood. | Read-only environment/tool availability checks; may inspect command availability. | No `--confirm-*`; report only. | No - no background scheduler behavior. |

## PR Handoff / Draft PR / Branch Push Helpers

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/create_pr_handoff.py` | Generate local PR handoff evidence from waiting-approval state. | Dry-run available; writes local handoff artifacts when not dry-run. | No `--confirm-*`; handoff artifact only, not PR creation. | No - no background scheduler behavior. |
| `scripts/create_pr_handoff_package.py` | Generate the newer waiting-approval PR handoff package. | Dry-run available; may write local package artifacts and DB metadata. | No `--confirm-*`; package artifact only. | No - no background scheduler behavior. |
| `scripts/create_draft_pr.py` | Explicit draft PR creation from handoff evidence. | Dry-run by default; confirmed mode may call `gh pr create --draft`. | Yes, `--confirm-create-pr`. | No - no background scheduler behavior. |
| `scripts/confirm_draft_pr.py` | Confirm draft PR creation after branch push evidence. | Dry-run available; confirmed mode may call `gh pr create --draft`. | Yes, `--confirm-draft-pr`. | No - no background scheduler behavior. |
| `scripts/record_existing_draft_pr.py` | Record evidence for a pre-existing draft PR. | Dry-run by default; confirmed mode may call `gh pr view` and write local evidence. | Yes, `--confirm-record-existing-pr`. | No - no background scheduler behavior. |
| `scripts/push_task_branch.py` | Explicitly publish a prepared task branch. | Dry-run by default; confirmed mode may call `git push --set-upstream`. | Yes, `--confirm-push`. | No - no background scheduler behavior. |
| `scripts/confirm_branch_push.py` | Confirm branch push after waiting-approval handoff and branch-readiness checks. | Dry-run available; confirmed mode may call `git push`. | Yes, `--confirm-branch-push`. | No - no background scheduler behavior. |
| `scripts/summarize_waiting_approval.py` | Summarize waiting-approval evidence for human review. | Read-only SQLite/artifact inspection. | No `--confirm-*`; summary only. | No - no background scheduler behavior. |

## Validation / Policy / Proof-of-Work

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/run_local_validation.py` | Run local validation commands selected by policy. | May call local validation subprocesses such as tests or compile checks. | No `--confirm-*`; validation command only. | No - no background scheduler behavior. |
| `scripts/validate_workflow_contract.py` | Validate workflow contract content. | Read-only file validation. | No `--confirm-*`; validator only. | No - no background scheduler behavior. |
| `scripts/validate_workflow_policy.py` | Validate workflow policy JSON. | Read-only file validation. | No `--confirm-*`; validator only. | No - no background scheduler behavior. |
| `scripts/summarize_workflow_policy.py` | Summarize workflow policy without runtime integration. | Read-only policy parsing and stdout output. | No `--confirm-*`; summary only. | No - no background scheduler behavior. |
| `scripts/write_workflow_policy_summary_artifact.py` | Write workflow policy summary artifact. | Writes local artifact output path. | No `--confirm-*`; explicit artifact command. | No - no background scheduler behavior. |
| `scripts/run_workflow_policy_artifact_smoke.py` | Smoke workflow policy summary artifact generation/readback. | Writes temporary local artifacts. | No `--confirm-*`; local smoke only. | No - no background scheduler behavior. |
| `scripts/run_workflow_policy_pow_package_smoke.py` | Smoke proof-of-work package generation for workflow policy. | Writes temporary local artifacts and reports proof-of-work. | No `--confirm-*`; local smoke only. | No - no background scheduler behavior. |
| `scripts/run_workflow_policy_review_evidence_smoke.py` | Smoke workflow policy evidence through store/review readback. | Writes temporary local DB/artifacts. | No `--confirm-*`; local smoke only. | No - no background scheduler behavior. |
| `scripts/report_workflow_policy_review_evidence.py` | Report workflow policy review evidence. | Read-only evidence inspection/reporting. | No `--confirm-*`; report only. | No - no background scheduler behavior. |

## Cleanup / Closeout

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/recommend_post_merge_cleanup.py` | Recommend post-merge cleanup actions from evidence. | Read-only local DB/artifact and optional Git/GitHub state inspection. | No `--confirm-*`; recommendation only. | No - no background scheduler behavior. |
| `scripts/confirm_local_cleanup.py` | Confirm local worktree and local branch cleanup. | Dry-run available; confirmed mode may remove local worktree and delete local branch. | Yes, `--confirm-local-cleanup`. | No - no background scheduler behavior. |
| `scripts/confirm_remote_branch_cleanup.py` | Confirm remote branch cleanup after local cleanup evidence. | Dry-run available; confirmed mode may call `git push --delete`. | Yes, `--confirm-remote-branch-delete`. | No - no background scheduler behavior. |
| `scripts/confirm_task_closeout.py` | Confirm local task closeout after PR and cleanup evidence. | Dry-run available; confirmed mode may update local task status/evidence. | Yes, `--confirm-task-closeout`. | No - no background scheduler behavior. |
| `scripts/kanban_accept_cleanup.py` | Legacy Hermes/Kanban accept and cleanup helper. | Dry-run available; confirmed mode may call `gh` and `git` cleanup commands. | Yes, `--confirm`. | No - no background scheduler behavior. |

## Mission Control / API Smoke

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/run_api.py` | Run the Mission Control FastAPI server. | Starts a foreground `uvicorn` server process. | No `--confirm-*`; explicit foreground server command. | No - no background scheduler behavior. |
| `scripts/run_mission_control_smoke.py` | Exercise Mission Control API/readback path. | Uses local API/store paths and smoke fixtures. | No `--confirm-*`; local smoke only. | No - no background scheduler behavior. |

## Release / Documentation Checks

| Path | Purpose | Effects and external tools | Explicit confirmation | Background scheduler behavior |
| --- | --- | --- | --- | --- |
| `scripts/kanban_create.py` | Legacy helper to create Kanban task/worktree scaffolding. | Dry-run available; may call git worktree and task tooling when run for real. | No `--confirm-*`; dry-run flag available. | No - no background scheduler behavior. |
| `scripts/kanban_workflow_regression.py` | Regression checks for the legacy Kanban workflow. | Creates temporary local repositories/worktrees and calls git subprocesses. | No `--confirm-*`; regression check only. | No - no background scheduler behavior. |

## Operator Boundary Summary

- These scripts are explicit commands, not a background scheduler.
- Confirmation artifacts are not runtime consumption.
- Proposal artifacts are not action evidence.
- There is no auto-merge and no self-approval in this script layer.
- No script should be treated as permission for auto-merge or self-approval.
- Existing command-specific `--confirm-*` gates remain the only mutation gates
  for the matching operator action.
