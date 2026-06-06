# P2 Architecture Checkpoint

This document is a documentation-only checkpoint taken after the P0,
Phase 6D, P2A, and P2B work landed on `main`. It records the current
state of the agent-taskflow orchestration architecture, evaluates
maintainability and safety properties, and recommends the next phase of
work.

It introduces no runtime changes. Its purpose is to keep the
"manage work, not agents" architecture coherent as the codebase grows.

## 1. Executive Summary

Recent landed work:

- **P0 — CORS API test stabilization.** Restored the green test baseline
  by fixing CORS API tests so they use isolated SQLite DBs and the
  TestClient lifespan correctly.
- **Phase 6D — Deterministic GitHub issue intake gate.** Added the
  explicit, dry-run-by-default intake runner that records only a queued
  `TaskRecord` and a `github_issue_ingested` event when
  `--confirm-intake` is supplied.
- **P2A — Draft PR confirmation helper extraction.** Moved pure
  `gh` command building, JSON parsing, PR verification dict assembly,
  and small string/list normalization helpers out of
  `agent_taskflow/draft_pr_confirm.py` into
  `agent_taskflow/draft_pr_confirm_helpers.py`.
- **P2B — Remote branch cleanup confirmation helper extraction.**
  Moved pure git command builders, branch normalization/validation,
  protected-branch constants, empty evidence dict builders, and the
  cleanup recommendation snapshot helper out of
  `agent_taskflow/remote_branch_cleanup_confirm.py` into
  `agent_taskflow/remote_branch_cleanup_confirm_helpers.py`.

Why this matters for the "manage work, not agents" architecture:

- The intake gate (Phase 6D) closed the last "AI worker triggers itself
  from a GitHub issue" risk by making issue ingestion a deterministic,
  human-confirmed operation that writes only a queued task.
- The two helper extractions (P2A and P2B) reduced the two highest-risk
  giant files in the cleanup/PR-handoff path. Pure validation,
  command-building, and evidence assembly logic now sits in importable
  helper modules with focused unit coverage, while the I/O-bearing,
  store-bearing, and subprocess-bearing orchestration code remains in
  the original modules.
- The result: the orchestration core is now both smaller per file and
  easier to reason about, without changing any externally observable
  behavior, CLI flag, confirmation gate, or safety property.

## 2. Current Pipeline State

The end-to-end safe flow is:

```
GitHub Issue
  → deterministic intake gate           (Phase 6D, implemented, dry-run + --confirm-intake)
  → SQLite TaskRecord(status=queued)    (implemented; written only after explicit confirm)
  → explicit operator runner / future scheduler
                                        (operator-driven runner implemented;
                                         no scheduler, no background polling)
  → worktree / executor / validator     (implemented; explicit workspace prep,
                                         deterministic validators, no self-validation)
  → waiting_approval                    (implemented; proof-of-work artifacts recorded)
  → branch push confirmation            (Phase 5C, implemented; dry-run + --confirm-branch-push)
  → draft PR confirmation               (Phase 5D + P2A, implemented;
                                         post-create gh pr view verification required)
  → human review / merge                (manual, GitHub-side; not automated here)
  → post-merge cleanup recommendation   (Phase 6A, implemented; recommendation only)
  → local cleanup confirmation          (Phase 6B, implemented; --confirm-local-cleanup)
  → remote branch cleanup confirmation  (Phase 6C + P2B, implemented;
                                         --confirm-remote-branch-delete)
  → task closeout / archive             (future / partially scaffolded)
```

What is implemented today:

- Deterministic intake gate with explicit `--confirm-intake`.
- Explicit operator runner that consumes prepared workspaces and
  approved task records (no scheduler-driven auto-dispatch).
- Workspace preparation, executor adapters (`manual`, `shell`,
  `opencode`, `pi`), deterministic validators, artifact recording,
  waiting-approval handoff package.
- Branch push confirmation, draft PR creation confirmation with
  post-create verification.
- Post-merge cleanup recommendation (Phase 6A), local cleanup
  confirmation (Phase 6B), remote branch cleanup confirmation (Phase 6C).
- Mission Control read-only API and frontend observability.

What is intentionally still future / manual:

- A queued-task scheduler or background dispatcher.
- An intake-to-runner handoff contract that picks the next queued task
  for explicit operator-run execution.
- Task closeout / archive confirmation in its full form.
- Any automatic post-merge action (no auto-merge, no auto-issue-close,
  no auto-remote-branch-cleanup).
- Mission Control review actions beyond observation.

## 3. P0 Summary — CORS API Test Stabilization

- CORS API tests now use an isolated SQLite state DB per test (no shared
  global DB leakage) and rely on the FastAPI `TestClient` context manager
  so the application's lifespan startup/shutdown runs correctly during
  tests.
- The test baseline returned to green and remains green at this
  checkpoint, supporting subsequent refactor work with regression
  signal.

## 4. Phase 6D Summary — GitHub Issue Intake Gate

- Added a deterministic intake gate that reads a GitHub issue snapshot
  (or offline fixture) and decides whether it is safe to ingest.
- **Dry-run by default.** No SQLite writes occur unless
  `--confirm-intake` is passed.
- When confirmed, the runner records only:
  - a `TaskRecord` with `status="queued"`,
  - a `github_issue_ingested` event in the store.
- The intake gate explicitly does **not**:
  - prepare worktrees,
  - run executors,
  - run validators,
  - push branches,
  - create PRs,
  - merge PRs,
  - perform any cleanup,
  - mutate GitHub,
  - run as a webhook or background worker.
- This closes the gap between "human-written GitHub issue" and
  "queued local task" with an auditable, conservative, idempotent path.

## 5. P2A Summary — Draft PR Confirmation Helpers

- Created `agent_taskflow/draft_pr_confirm_helpers.py` to hold pure,
  side-effect-free pieces of the draft PR confirmation flow.
- Moved into the helper module:
  - `gh` command builders (`build_gh_create_command`,
    `build_gh_view_command`, `build_gh_list_open_pr_command`),
  - JSON parsing helpers (`parse_json_object`, `parse_json_array`,
    `parse_event_payload`),
  - PR verification dict assembly (`empty_verification_preview`,
    `empty_verification_result`, `build_verification_result`),
  - repo/branch normalization (`normalize_repo`,
    `normalize_branch_choice`),
  - small string/list helpers (`command_preview`, `body_preview`,
    `extract_pr_url`, `extract_pr_file_paths`, `extract_pr_commit_oids`,
    `stringify_list`, `dedupe_preserve_order`),
  - the `DraftPrConfirmError` exception and `PROTECTED_HEAD_BRANCHES`
    constant.
- Remained in `agent_taskflow/draft_pr_confirm.py`:
  - the orchestration entrypoint,
  - request/result dataclasses,
  - subprocess invocation,
  - SQLite store reads and writes,
  - artifact file writes,
  - result builders that compose the helpers with I/O-bearing pieces.
- **Safety behavior preserved.** The post-create `gh pr view`
  verification gate remains required: a draft PR is only treated as
  successful when the GitHub response matches the expected base/head,
  draft state, title, files, and commits.

## 6. P2B Summary — Remote Branch Cleanup Confirmation Helpers

- Created `agent_taskflow/remote_branch_cleanup_confirm_helpers.py` to
  hold pure pieces of the remote branch cleanup flow.
- Moved into the helper module:
  - `PROTECTED_BRANCHES` (`{"main", "master", "trunk"}`),
  - phase constants (`LOCAL_ARTIFACT_KIND`, `LOCAL_EVENT_TYPE`,
    `LOCAL_CONFIRM_FLAG`),
  - branch name helpers (`normalize_branch_name`, `validate_branch_name`),
  - list utility (`dedupe_preserve_order`),
  - git command builders (`build_git_ls_remote_heads_command`,
    `build_git_push_delete_command`),
  - safety block builder (`safety_block`),
  - empty evidence dict builders (`empty_cleanup_recommendation`,
    `empty_draft_pr_evidence`, `empty_local_cleanup_evidence`,
    `empty_remote_branch`),
  - the cleanup recommendation snapshot (`cleanup_recommendation_snapshot`),
  - safe JSON event parser (`latest_event_payload`).
- Remained in `agent_taskflow/remote_branch_cleanup_confirm.py`:
  - the orchestration entrypoint,
  - request/result dataclasses,
  - subprocess invocation (`_run_git`),
  - SQLite store reads and writes,
  - artifact file writes,
  - resolved-branch logic and readiness-check assembly,
  - result builders (`_blocked_result`, `_preview_result`,
    `_success_result`, `_not_found_result`).
- **Safety behavior preserved.** Protected branches still cannot be
  deleted, `--confirm-remote-branch-delete` is still required for
  actual deletion, dry-run remains the default for previews, and
  evidence is recorded only after the actual `git push --delete`
  succeeds and a follow-up `git ls-remote --heads` verifies the branch
  is gone. There is still no auto-cleanup.

## 7. Current Architecture Assessment

- **State layer / SQLite mirror (`store.py`).** Authoritative
  orchestrator state. Records `TaskRecord`, `TaskWorktreeRecord`,
  artifacts, and events. All phase transitions write evidence here.
  No worker writes directly to the store outside the deterministic
  Python code.
- **Intake layer (`github_issue_ingestion.py`,
  `github_issue_intake.py`, `github_issue_intake_gate.py`).**
  Read-only ingestion of GitHub issues with an explicit confirmation
  gate (Phase 6D). Writes only `queued` task records; never dispatches.
- **Workspace / executor / validator layer (`workspace_manager.py`,
  `worktree.py`, `executors/`, `validators/`, `dispatcher.py`,
  `approved_task_runner.py`).** Explicit operator-driven dispatch.
  Workspaces are isolated git worktrees. Executors are deterministic
  CLI wrappers. Validators are deterministic proof-of-work gates that
  must pass before `waiting_approval`.
- **PR handoff / draft PR verification layer (`pr_handoff.py`,
  `pr_handoff_package.py`, `branch_push_confirm.py`, `draft_pr.py`,
  `draft_pr_confirm.py`, `draft_pr_confirm_helpers.py`).** Builds a
  reviewable handoff package, then requires explicit confirmation for
  branch push and draft PR creation, with post-create verification.
- **Cleanup confirmation layer
  (`post_merge_cleanup_recommendation.py`, `local_cleanup_confirm.py`,
  `remote_branch_cleanup_confirm.py`,
  `remote_branch_cleanup_confirm_helpers.py`).** Recommendation-first,
  confirm-by-default-off. Each cleanup step is gated by its own
  `--confirm-*` flag; protected branches are off-limits.
- **Mission Control / API observability layer (`api/`,
  `mission-control/`).** Read-only serialization of task state,
  evidence, and review artifacts. Mission Control does not execute,
  validate, approve, merge, or clean up.

## 8. Maintainability Assessment

Based on the current line counts captured at this checkpoint, the
largest Python files are:

| Lines | File | Assessment |
| ----- | ---- | ---------- |
| 1659  | `agent_taskflow/draft_pr_confirm.py` | Should be split soon. Post-P2A the helpers are out, but the orchestration core, request/result dataclasses, store I/O, and result builders together still produce the largest module in the repo. A P2C result-builder extraction is the natural next refactor target — but only after Phase 6E, per the recommendation below. |
| 1518  | `tests/test_dispatcher.py` | Acceptable for now. Test files are allowed to be large when they cover a single subsystem end-to-end; splitting test files just for size adds harness sprawl. Reconsider only if a true subsystem boundary appears. |
| 1517  | `agent_taskflow/task_closeout_confirm.py` | Should not be touched yet. Closeout is partially scaffolded and behavior is still being finalized; refactoring before the contract stabilizes risks freezing the wrong shape. |
| 1206  | `agent_taskflow/local_cleanup_confirm.py` | Acceptable for now. Similar pattern to `remote_branch_cleanup_confirm.py` pre-P2B; a P2D-style helper extraction is plausible later but is not the highest-value next move. |
| 1137  | `agent_taskflow/post_merge_cleanup_recommendation.py` | Acceptable for now. Recommendation-only module; risk is low and the structure already separates pure recommendation logic from store reads. |
| 1121  | `agent_taskflow/remote_branch_cleanup_confirm.py` | Acceptable for now (down from 1295 pre-P2B). Further extraction (result builders) is plausible but not urgent. |
| 1097  | `agent_taskflow/waiting_approval_summary.py` | Acceptable for now. Centralized summary builder; splitting risks duplicating data shapes across modules. |
| 1089  | `agent_taskflow/pr_handoff_package.py` | Acceptable for now. Stable contract that the rest of the PR handoff path depends on; touching it is high-blast-radius. |
| 1084  | `tests/test_workflow_policy_read_only_api_contract.py` | Acceptable for now. Single-contract test file. |
| 1048  | `agent_taskflow/branch_push_confirm.py` | Acceptable for now. Could benefit from a future helper split, but is not currently on the critical path. |
| 1019  | `agent_taskflow/store.py` | Should not be touched yet for size. Splitting the store is a structural change that should follow, not precede, the queued-task handoff work. |
| 1015  | `agent_taskflow/approved_task_runner.py` | Acceptable for now, and likely to grow as Phase 6E lands. Defer refactor until after the handoff contract is in. |

Helper modules in the repo at this checkpoint:

- `agent_taskflow/draft_pr_confirm_helpers.py`
- `agent_taskflow/remote_branch_cleanup_confirm_helpers.py`

Test baseline at this checkpoint: **2130 passed, 191 subtests passed**
under `pytest -q`.

## 9. Governance / Safety Checkpoint

The current architecture still preserves all of the following invariants:

- **No self-approval.** AI workers cannot mark their own work
  `approved`. Approval is a separate human-gated step.
- **No auto-merge.** Nothing in the codebase calls `gh pr merge` or
  equivalent. Merge remains a manual GitHub-side action.
- **No destructive cleanup without explicit confirmation.** Local
  cleanup requires `--confirm-local-cleanup`; remote branch cleanup
  requires `--confirm-remote-branch-delete`; both default to dry-run.
- **No executor outside a prepared worktree.** Dispatch consumes
  recorded `TaskWorktreeRecord` entries created by explicit workspace
  preparation; executors do not run in the main working tree.
- **Proof-of-work evidence before human review.** Validators write
  reviewable artifacts (executor logs, validation reports,
  changed-files audits, handoff packages) before a task reaches
  `waiting_approval`.
- **Deterministic validators before `waiting_approval`.** The
  validator suite (`pytest`, `policy`, `changed-files`, optional
  `openspec`, etc.) is invoked by deterministic Python, not by the AI
  worker, and its results gate the lifecycle transition.
- **Mission Control is observability/review, not the engine.** The
  API/UI exposes state and artifacts; it does not orchestrate, dispatch,
  validate, approve, merge, or clean up.
- **Protected branches stay protected.** `PROTECTED_BRANCHES` is set in
  both `branch_push.py`/`branch_push_confirm.py` and
  `remote_branch_cleanup_confirm_helpers.py`. Force push, mirror push,
  and protected-branch deletion are explicitly rejected paths.
- **No background loops.** No webhook handler, no scheduler, no polling
  daemon ships in this checkpoint. Every state-changing action is
  triggered explicitly by an operator command.

## 10. Recommended Next Steps

Candidate next phases:

- **Option A — P2C result-builder extraction.** Continue the helper
  refactor by extracting the result-builder layer (`_blocked_result`,
  `_preview_result`, `_success_result`, `_not_found_result`) from
  `draft_pr_confirm.py`, `remote_branch_cleanup_confirm.py`,
  `branch_push_confirm.py`, and `local_cleanup_confirm.py`. Pure
  mechanical refactor, low risk, modest line-count gain.
- **Option B — Phase 6E intake-to-runner handoff contract.** Define
  the explicit, deterministic contract that takes a `status=queued`
  `TaskRecord` produced by the Phase 6D intake gate and hands it to the
  explicit operator runner for workspace preparation and dispatch. No
  scheduler, no polling — a single explicit handoff with its own
  confirmation gate and audit trail.
- **Option C — Queued-task listing / operator selection.** Add a
  read-only listing of queued tasks (CLI and/or Mission Control read
  view) so operators can pick which queued task to run next.
- **Option D — Cleanup watcher recommendation-only mode.** A
  recommendation-only observer that surfaces tasks eligible for local
  or remote cleanup. Read-only, no mutation, no scheduling.

**Recommendation: pause broad refactor and proceed to Option B
(Phase 6E — intake-to-runner handoff contract).**

Reasoning:

- P2A and P2B already reduced the highest-risk giant-file pressure in
  the modules with the most destructive-adjacent behavior (PR creation,
  remote branch deletion). The remaining large files are either
  test-coverage files, stable contracts, or modules whose shape will
  change once the handoff contract lands. Further mechanical refactor
  now is low-value churn.
- The current architectural gap is not file size — it is that the
  Phase 6D intake gate produces `queued` task records, but the contract
  for moving those records into explicit operator-run execution is
  still implicit. Specifying that contract explicitly is the next
  meaningful step in the "manage work, not agents" architecture.
- Option C and Option D both depend on the handoff contract being
  explicit, so they naturally follow Option B rather than precede it.
- Option A can be picked back up after Phase 6E without losing context;
  the helper-extraction pattern established by P2A/P2B is now a known,
  repeatable refactor.

## 11. Non-Goals (What Must NOT Be Done Next)

- **No auto-merge.** `gh pr merge` and equivalents stay out of the
  codebase. Merge is a human action on GitHub.
- **No automatic remote branch deletion.** Remote branch deletion
  continues to require `--confirm-remote-branch-delete`, merged-PR
  evidence, local cleanup evidence, and a successful `git ls-remote`
  re-check after the push.
- **No background polling loop yet.** No webhook handler, no scheduler
  daemon, no `while True` watcher. Every state-changing operation is
  operator-triggered.
- **No broad helper extraction just for style.** Future helper splits
  should target a specific risk-reduction or testability goal, not
  uniform module size.
- **No scheduler before the queued-task handoff contract is clear.**
  Building a scheduler on top of an implicit handoff is exactly the
  inversion the project explicitly rejects. Define the contract first;
  consider automation later, if at all.

## 13. Pi Agent parity

This section documents the Pi Agent parity proof for the local
self-dogfood chain, mirroring the OpenCode path (GH-9604) using the
Pi Agent as the bounded coder executor.

### 13.1 Chain overview

The end-to-end self-dogfood chain is:

```
offline issue/spec
  -> deterministic intake
  -> Task Execution Package
  -> explicit queued-task handoff
  -> approved_task_runner
  -> Pi executor
  -> deterministic validators (pytest, policy, changed-files)
  -> waiting_approval
```

### 13.2 Explicit governance statements

- **Pi is used only as the bounded coder executor.**
  Pi does not own scheduling, task selection, lifecycle state
  transitions, validation decisions, approval decisions, merge
  behavior, push behavior, or cleanup behavior.
- **Validators remain deterministic.**
  The validator suite (pytest, policy, changed-files, optional
  openspec) is invoked by deterministic Python orchestration code,
  not by the Pi Agent. Its results gate the lifecycle transition.
- **Pi does not approve, merge, push, create PRs, or cleanup.**
  These actions remain outside Pi's authority and require separate
  human-controlled or deterministic-policy-controlled gates.
- **Human review remains the final gate.**
  Only a designated human approver can approve. Pi cannot
  self-approve or mark work finally complete.

### 13.3 Strict non-goals (enforced by governance)


- no auto-push
- no auto-PR
- no auto-merge
- no auto-cleanup

### 13.4 Required strings for regression testing

The following strings are required to be present in this section to
pass the deterministic changed-files validator regression test:

- Pi Agent
- Task Execution Package
- queued-task handoff
- approved_task_runner
- deterministic validators
- waiting_approval
- no auto-push
- no auto-PR
- no auto-merge
- no auto-cleanup

## 12. Local Self-Dogfood Chain

After the Phase 6E+3 no-op handoff guard (commit e78fbdf), the local
self-dogfood chain exercises the full orchestration stack without any
GitHub mutation. The chain is:

```
offline issue/spec
  -> deterministic intake
  -> Task Execution Package
  -> explicit queued-task handoff
  -> approved_task_runner
  -> deterministic validators (pytest, policy, changed-files)
  -> waiting_approval
```

Key properties of this chain:

- **Task Execution Package.** The deterministic package creation step
  consumes a queued `TaskRecord` plus recorded issue/spec evidence and
  writes `implementation_prompt.md` and `task_execution_package.json`.
  This package is the executor input contract; it exists before the
  queued-task handoff starts the runner.
- **queued-task handoff.** The handoff verifies the Task Execution Package
  and then explicitly invokes `approved_task_runner`. The runner prepares
  the isolated workspace, starts the executor, runs deterministic
  validators, and records proof-of-work evidence. No scheduler, polling,
  or background loop is involved.
- **waiting_approval.** After all validators pass, the task reaches
  `waiting_approval` as a proof-of-work gate. Human review is the only
  subsequent action.
- **no auto-push.** Branch push requires an explicit `--confirm-branch-push`
  confirmation. No auto-push exists in the chain.
- **no auto-PR.** Draft PR creation requires an explicit `--confirm-draft-pr`
  confirmation. No auto-PR exists in the chain.
- **no auto-merge.** Merge is a manual GitHub-side action. Nothing in
  the chain calls `gh pr merge` or equivalent.
- **no auto-cleanup.** Local and remote cleanup each require their own
  `--confirm-*` flags and explicit evidence. No auto-cleanup exists
  in the chain.
