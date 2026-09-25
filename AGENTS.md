# Agent Taskflow / Mission Control Instructions

You are working in the `agent-taskflow` repository. This file is the short entrypoint for every coding agent here: builders, reviewers, and the orchestrator. It summarizes and links. Where it disagrees with an authoritative source, the source wins, and you report the conflict.

The core principle is:

> Manage work, not agents.

Agent Taskflow is a Practical V1 control plane for AI-assisted GitHub work. Deterministic Python owns Tickets, Attempts, leases, worktrees, validator gates, integration evidence, and cleanup eligibility. Two roles are easy to confuse:

- **Inside the product**, an AI tool run by an executor adapter (Claude Code, Codex, Pi, OpenCode, or a future tool) is a bounded implementation worker in one Ticket worktree. It does not claim Tickets, change lifecycle state, decide validation, approve, push, merge, or clean up (SPEC §2.2, `WORKFLOW.md`, `docs/claude-code-bounded-implementer-executor.md`). A session started by an executor adapter is always this in-product worker, even when its Ticket targets this repository.
- **When developing this repository**, agent sessions such as Claude Code act as orchestrator, builder, or reviewer under the orchestrator execution contract below. Even then, a human reviews and merges every PR.

## Big Picture / Start Here

**What it does.** By design, a user picks a registered repository and creates a Ticket, and Taskflow runs a bounded AI executor in the Ticket's own git worktree and gates the result with deterministic validators. It then integrates the result with the latest target branch and opens or updates a GitHub PR. A human reviews and merges on GitHub. Taskflow then verifies the merge, cleans up, and marks the Ticket `completed` (SPEC §0, §1, §46).

**Where it is heading.** Practical V1 comes first: a real Ticket must run the whole flow with nothing triggered by hand except the human merge. F10 (#220) gave every hop below a non-test invoker, so what remains is owner decisions and deployment: executor policy, integration-lock liveness, the F10 follow-ups due before cron, the deployment decisions H1–H7, F6 under RULINGS 54, and a human installing SPEC §47's cron invokers. After V1 comes the Level 2 Roadmap, and its evidence work already runs alongside V1. It goes to shadow mode first, where the system recommends and a human still decides every merge, and then to controlled auto-merge for one `docs-only-safe` task class. Level 2 applies only where it does not conflict with V1, so nothing auto-merges today. See "Roadmap and Remaining Scope".

**End-to-end flow on `main`.** Each hop names its owning module, under `agent_taskflow/` unless shown. `[gap]` marks what is still missing:

```text
Ticket created: POST /api/tickets (api/tickets.py -> ticket_creation.py); Mission Control calls this route
-> execution tick: scripts/run_parallel_scheduler_tick.py -> parallel_scheduler.py picks (ready_queue.py), prepares the worktree (ticket_worktree.py), starts one scheduler_worker.py per Ticket   [gap: not scheduled; cron not installed]
-> claim: the worker's dispatcher.py claims atomically via runtime_admission.py (dependencies, lease, capacity)
-> run: dispatcher.py -> executors/<adapter> -> validators/   [gap: default executor is `manual`, V1-EXECUTOR-CONTRACT]
-> ready_for_integration (ticket_lifecycle.py), enqueued once (integration_handoff.py) in the repo's FIFO queue (integration_queue.py)
-> integration tick, one repository per pass: scripts/run_integration_tick.py -> integration_tick.py   [gap: not scheduled; cron not installed]
   phases in PHASE_ORDER: PR outcomes -> verified-merge cleanup -> target freshness -> FIFO drain
-> drain: integration_controller.integrate_task: latest target + integration_validators.py, push task branch (integration_git.py), create/update PR (github_pr_adapter.py) -> needs_review
-> human review and merge on GitHub (never Taskflow)
-> next pass, PR-outcome phase: integration_watcher.poll_pr_outcomes records the merge
-> cleanup phase: integration_cleanup.py -> merge_verification.py -> completed
(freshness phase: integration_watcher.poll_target_freshness returns a stale needs_review Ticket to ready_for_integration)
```

Both ticks hold a non-overlap lock (`tick_lock.py`). Cron examples for both are in `deploy/cron/`, but nothing is installed, and installing cron is a human action (SPEC §47.4).

**Read these first:** (1) `README.md` for the V1 flow and its table of safety boundaries; (2) `WORKFLOW.md`, the workflow contract; (3) if present, `.agent-taskflow/control/SPEC.md` §1, §12, §44, and §47 for the goal, statuses, safety invariants, and execution vehicle; (4) if present, `.agent-taskflow/control/AGENT-EXECUTION.md`, the orchestrator contract; (5) the docstrings of `agent_taskflow/dispatcher.py` and `agent_taskflow/integration_controller.py`, which cover the two halves of the flow, and for the integration side `docs/v1/handoff-f10.md`, which describes the integration tick's phases, flags, exit codes, and fail-closed cleanup.

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

- `README.md` / `README.zh-TW.md`: the current V1 flow, what is automated and what is manual, and a table mapping each safety boundary to its enforcement point. Both READMEs describe F10, but they still say F9 is not implemented; that sentence is out of date. "What `main` Does Today" below describes F9 and F10.
- `WORKFLOW.md`: the repo-owned workflow contract. It covers worker boundaries, changed-files and path policy, proof-of-work, and the Integration Controller boundary. Read it before editing, not after, when a task touches execution workflow, executor or validator behavior, proof-of-work artifacts, workspace or path policy, approval/rejection/rerun/blocking behavior, Mission Control review semantics, or governance. Its "Task Lifecycle" section (`queued -> ... -> waiting_approval`) predates V1 and matches the legacy GitHub-issue path. For Tickets, use SPEC §12 and `agent_taskflow/status_vocab.py`.
- The rest of `docs/` is described under "Repository Map".

## Repository Map

Top level:

- `agent_taskflow/`: the Python package, grouped by function below. Importing it installs the layered runtime paths (`*_runtime_path.py`) onto the dispatcher and the legacy runner (`agent_taskflow/__init__.py`). Runtime admission is not patched: the claim paths use `CanonicalRuntimeAdmissionStore` subclasses instead (`canonical_runtime_path.py`, `lifecycle_runtime_path.py`, `reset_runtime_path.py`). `agent_taskflow/cli/` holds the console scripts declared in `pyproject.toml`.
- `agent_taskflow/api/`: the FastAPI app (`main.py`), Ticket routes (`tickets.py`), SSE progress (`realtime.py`), and read-only review evidence (`review.py`). `scripts/run_api.py` starts it.
- `agent_taskflow/executors/`: executor adapters and their registry (`registry.py`).
- `agent_taskflow/validators/`: deterministic validators and their registry (`registry.py`).
- `scripts/`: explicit operator commands over the package. It holds the two V1 ticks, `runtime_control.py`, `ticket_dependency.py`, `reset_task_status.py`, the `migrate_*.py` migrations, rehearsals, smokes, and many legacy-path commands. Nothing in it runs by itself.
- `tests/`: one flat suite, mostly `test_<module>.py`, plus shared fixtures (`v1_step2_fixtures.py`, `step5_support.py`).
- `docs/`: mostly dated phase records (the pre-V1 pipeline, Level 2 milestones, release notes), and several say they predate V1 (for example `docs/current-architecture-boundary.md`). Treat them as history, not as current behavior, unless this file points to one.
- `docs/v1/`: a handoff for each merged V1 Step or F item (`handoff-step1.md` to `handoff-f10.md`, plus the E2E records), with its decisions and its real end-to-end evidence.
- `mission-control/`: the Next.js UI (`app/`, `components/`, `lib/`). It creates Tickets through the API and shows progress.
- `config/projects.yaml`: the repository registry (SPEC §11), read by Ticket creation and the scripts.
- `deploy/`: *examples* only; nothing here is installed. `deploy/cron/v1-execution-tick.cron.example` and `deploy/cron/v1-integration-tick.cron.example` are the V1 ticks' cron lines. The other cron and systemd files are for the legacy one-task tick.
- `AGENTS.md`: a byte-identical copy of this file, for tools that read `AGENTS.md`. The orchestrator copies it from the reviewed `CLAUDE.md` (owner ruling, 2026-09-24), so edit `CLAUDE.md`, not the copy.
- `.pi/`: `.pi/settings.json`, which holds only `quietStartup: false`. That it configures the Pi coding agent is an inference from the name.
- `ops/`: versioned copies of the VPS operator scripts, for example the notifier, the `codex-as-pi.sh` shim, and the manual legacy tick. The live copies run from `~/agent-taskflow-ops/`.
- `examples/`: example project-config and workflow-policy files.
- `.github/workflows/ci.yml`: the only CI workflow.
- `.agent-taskflow/`: gitignored, and present only in the dev checkout on the VPS. It holds `control/` (the control documents), `execution-state.yaml` (the orchestrator's state), and `evidence/` (builder and reviewer evidence).

`agent_taskflow/` by function (key modules only):

- **Lifecycle, state, and store:** `store.py` (the SQLite mirror and task events), `status_vocab.py`, `ticket_models.py`, `ticket_creation.py`, `ticket_store.py`, `ticket_lifecycle.py`, `ticket_dependencies.py`, `ticket_worktree.py`, `ticket_retry.py`, plus the schema modules (`*_schema.py`).
- **Admission and scheduling:** `runtime_admission.py`, `runtime_capacity.py`, `runtime_reaper.py`, `ready_queue.py`, `parallel_scheduler.py`, `scheduler_worker.py`, `tick_lock.py` (both ticks' non-overlap locks), and for the §19 evidence gate `concurrency_gate.py` and `concurrency_rehearsal.py`.
- **Execution and dispatch:** `dispatcher.py`, `executors/`, `validators/`, `executor_launch.py` (managed process groups), and the `runtime_progress*.py` modules (§14 progress).
- **Evidence and artifacts:** `atomic_write.py`, `artifacts.py`, `validation_summary.py`, `evidence_coverage.py`, `launch_evidence.py`, `launch_provenance.py`, `outcome_ledger.py`, `attempt_failure_class.py`, `integration_evidence_root.py`.
- **Integration:** `integration_handoff.py`, `integration_queue.py`, `integration_tick.py`, `integration_controller.py`, `integration_git.py`, `integration_validators.py`, `integration_watcher.py`, `merge_verification.py`, `integration_cleanup.py`, `integration_store.py`, `reviewer_hints.py`.
- **GitHub adapters:** `github_pr_adapter.py` is the Integration Controller's adapter: PR create/update/read through `gh`, the `gh api` allowlist, and the merge guard. The legacy path reaches GitHub through `github_issue_*.py`, `draft_pr*.py`, and `branch_push*.py`.
- **Level 2 / ExecutionEngine:** `attempt_models.py`, `attempt_store.py`, `attempt_resources.py`, `canonical_runtime_path.py`, `lifecycle_control.py`, `level2_execution_authority.py`, `execution_engine_contract.py`, `execution_engine_approved_task_adapter.py`, `scheduler_execution_engine_authority.py`, and `m1_*.py` (the M1 rehearsals and exit gate).
- **Legacy GitHub-issue path:** `approved_task_runner.py`, `github_issue_one_task_*.py`, `scheduler_proposals.py`, `scheduler_confirmation*.py`, `scheduler_watcher_*.py`, `task_execution_package.py`, `pr_handoff*.py`, `*_confirm.py`, `waiting_approval_summary.py`, `codex_advisory_*.py`. This path ends at `waiting_approval`.

Detailed maps: `docs/v1-step2-integration-controller.md` covers the integration modules and §32.1 field ownership. `docs/script-map.md` is the operator script inventory. `docs/scheduler-module-map.md` covers the legacy scheduler proposal, confirmation, and ExecutionEngine surfaces. The last two predate the V1 ticks: neither lists `run_parallel_scheduler_tick.py` or `run_integration_tick.py`.

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
- Nothing schedules the V1 Ticket-path ticks. The execution tick (`scripts/run_parallel_scheduler_tick.py`) and the integration tick (`scripts/run_integration_tick.py`) run only when invoked. SPEC §47 names short, idempotent cron ticks as the invoker. `deploy/cron/v1-*.cron.example` show those cron lines, but installing or changing a cron entry is a human action (SPEC §47.4), and so is choosing which confirmations cron passes. Any installed cron entry lives outside the repository.
- One integration-tick pass serves one repository and then exits. Its phases run in `PHASE_ORDER` (`integration_tick.py`): PR outcomes (`poll_pr_outcomes`), then verified-merge cleanup (`run_integration_cleanup`), then target freshness (`poll_target_freshness`), then F9's FIFO drain. Each phase is a read-only preview unless its own flag is passed: `--confirm-pr-poll`, `--confirm-cleanup`, `--confirm-freshness`, or `--confirm-integration`. The drain (F9, #211) calls `integrate_task` only for queue entries that are unchanged and whose stored Ticket/worktree binding verifies. `lock_unavailable` or an unexpected error (`integration_error`) ends the drain. The tick never deletes a remote branch and never automates cleanup of cancelled work (`docs/v1/handoff-f9.md`, `docs/v1/handoff-f10.md`).
- F10 (#220) also added the following. Cleanup fails closed: it refuses any target that is not the registered, clean task worktree at its recorded path and branch, and it never deletes a directory itself. Both ticks hold a non-overlap `flock` beside the database (`tick_lock.py`). An overlapping run prints `skipped_overlap` and exits 75. The integration tick reports three conditions without repairing them: a held per-repository lock row (read-only, in `integration_lock_at_start`), `ready_for_integration` Tickets missing from the queue, and verified merges whose cleanup did not complete. The last two make it exit 1. A held lock row does so only indirectly, when a queued entry makes the drain hit `lock_unavailable`; a held row with an empty queue and nothing else wrong exits 0. F10's real E2E passed on 2026-09-25 with the two cron lines as the only drivers.
- The parallel scheduler's worker builds its dispatcher with the default executor `manual`, which returns `skipped` and runs nothing. Ticket creation (`POST /api/tickets`) sets no executor, so a Ticket the worker starts can reach `ready_for_integration` without an implementation (V1-EXECUTOR-CONTRACT, below).
- Integration runs only when explicitly triggered. `integrate_task` does a dry run unless it is called with `dry_run=False` and `confirm_integration=True` (`integration_controller.py`). That flag triggers integration; it does not approve a merge.
- Taskflow never merges: it never runs `gh pr merge` or an API equivalent (`github_pr_adapter.py`, README). The Integration Controller's only push is `git push origin <task-branch>`, optionally with `-u`. It never force-pushes, never pushes `main`/`master`/`trunk`, and never deletes a remote branch in V1 (`integration_git.py`, `WORKFLOW.md`). The older explicit operator commands for pushing and remote-branch cleanup are described in `WORKFLOW.md`, and each requires its own confirmation flag.
- GitHub CI is shown for review but has no authority over the Taskflow lifecycle. Taskflow validators gate `needs_review` (SPEC §30).
- The runtime limit `max_concurrent_tasks` defaults to 1. It can be raised only with rehearsal evidence bound to the deployed commit (`scripts/runtime_control.py set-capacity --evidence-path`, SPEC §19). This is a different limit from the orchestrator's cap of 2 active implementation tasks (AGENT-EXECUTION §5).
- Level 2: the M2 pieces on `main` include resolved launch evidence (#205, `docs/resolved-launch-evidence.md`), runner validation summaries (#208, `docs/validation-summary.md`), launch provenance (#212, `docs/executor-process-lifecycle.md`), the Attempt outcome ledger (#213, `docs/outcome-ledger.md`), and shared runner evidence coverage with producer-Attempt binding (#214, `docs/m2-evidence-coverage-and-producer-binding.md`), a persisted failure class for each failed Attempt (`execution_failure`, `validation_failure`, or `tool_error`, else `unknown`; #218, `attempt_failure_class.py`), and integration and validator evidence written under the producer Attempt's root (#219, `integration_evidence_root.py`). Confirmed Level 2 execution goes through `SchedulerExecutionEngineAuthority` (README).

Describe only what is merged on `main`. Open or draft PRs are not current behavior.

## Roadmap and Remaining Scope

This section lists planned work: its scope, its order, and where a human is involved. It carries no progress status beyond marking merged items done, because status goes stale in a committed file. Status lives in the orchestrator's `.agent-taskflow/execution-state.yaml` (if present) and in the milestone status records. Everything below that is not marked done is planned. Nothing here is current behavior unless "What `main` Does Today" says so. The control documents are the authority if present, and each line names its own source. Where sources disagree on order, the higher source in the authority order wins, and the conflict is named.

The V1 and Level 2 tracks run in parallel, within the orchestrator's cap of 2 active implementation tasks (AGENT-EXECUTION §5; 2026-09-19 ruling). Every item ends with a human PR review and merge on GitHub. The lines below note only the extra human gates.

**Practical V1.** The goal is a real Ticket that runs end to end with nothing triggered by hand except the human merge (RULINGS 64, FOLLOWUPS F10):

- **F10, the rest of the consumer side: done (#220).** See "What `main` Does Today". Sources: SPEC §47, FOLLOWUPS F10, orchestrator rulings OR-1 to OR-3, `docs/v1/handoff-f10.md`.
- **V1-EXECUTOR-CONTRACT, executor policy.** The worker's `manual` default would send an unimplemented Ticket to `ready_for_integration`. The owner must first decide how each repository's executor is selected; a normal PR follows. This blocks the first real Ticket. Source: orchestrator ruling OR-4.
- **V1-INTEGRATION-LOCK-LIVENESS, lock recovery.** The per-repo `integration_locks` row has no TTL or reaper, so a killed `integrate_task` wedges its repository. OR-4 reads RULINGS 62 as reserving Step 2 lock semantics to the owner, so an owner decision comes first. It must be settled before cron is installed. Source: OR-4.
- **F10-FU1 to F10-FU3, before cron is installed.** FU1 will record `pr_state_polled` only when a polled field changes. FU2 will bound the evidence file and row that a persistent confirmed cleanup refusal adds on every tick. FU3 will write `merge_verified_at` only after removal, so an incomplete cleanup heals itself. FU3 reorders Step 2's writes and needs an owner ruling. Sources: the orchestrator rulings' F10 follow-ups; `docs/v1/handoff-f10.md`.
- **Deployment decisions H1–H7 (human-only).** These cover whether and when to install cron (H1), what "deployed" means and which checkout cron runs (H2), production capacity (H3), and which `--confirm-*` flags cron passes, a new autonomy class (H4). They also cover an optional temporary crontab rehearsal (H5-b; H5 itself was settled for the E2E by OR-2), which repositories and validators the production integration tick serves (H6), and the credentials cron runs with (H7). The owner answers them before cron is installed. Sources: the F10 human-decision draft (`.agent-taskflow/evidence/V1-F10/opus55-20260925-01/prep/human-decisions.md`, if present; the committed cron examples cite H2 and H4); SPEC §47.4; AGENT-EXECUTION §14.
- **F6, crash-rehearsal robustness.** The §19.3 crash checks will wait on a condition with a generous deadline, or use a longer lease TTL on slow machines, instead of a fixed sleep. A blind retry is not a fix. Order conflict: the owner's RULINGS 54, restated in FOLLOWUPS F9, puts F6 before the first real Ticket. The later RULINGS 64, `README.md`, and the execution state put it after. RULINGS 54 is the only explicit human ruling on this point, so it governs (AGENT-EXECUTION §0) until the owner rules again. Source: FOLLOWUPS F6.
- **First real Ticket.** An isolated real workload runs the whole flow unattended, keeping the human merge and deployment steps. It waits on owner decisions: V1-EXECUTOR-CONTRACT, and before cron is installed, V1-INTEGRATION-LOCK-LIVENESS, F10-FU1 to F10-FU3, and H1–H7. Under RULINGS 54 it also waits on F6. Its observed friction, not a plan, decides the F5 and Step 6 work, and SPEC §47.2's cadence is revisited after it. Sources: RULINGS 54, 64; OR-4; SPEC §47.2.
- **F5, one failure vocabulary.** Will fold the legacy GitHub-issue path, which still writes `blocked`, into SPEC §29's `needs_decision`/`failed`, together with F2's status migration. It comes after the first real Ticket, and only if real friction calls for it. Sources: FOLLOWUPS F5, RULINGS 54.
- **F2, vocabulary and migration cleanup.** Will migrate `TASK_STATUSES` to SPEC §12 names and move the legacy `store.init_db()` migrations, including Step 2's startup-created tables, to explicit scripts. The `TASK_STATUSES` migration goes with F5 once V1 is running (FOLLOWUPS F5, which outranks `README.md`, where F2 comes before the first real Ticket). Step 2 keeps creating its own tables at startup for V1 (RULINGS 16), so moving them also comes after V1. Sources: FOLLOWUPS F2, F5; SPEC §12.2; RULINGS 16.
- **Step 6, practical UX.** Drag priority, mobile refinement, pause/resume, retry and dependency UX, integration-queue and re-integration visibility, reviewer-hint presentation, and better logs. Only what real workflow friction shows is in scope, after the first real Ticket. Sources: SPEC §42 Step 6, RULINGS 54.
- **F3 deployment constraints (standing).** Every production migration or capacity change follows the owner-approved runbook: back up the live database, run the explicit migrations, produce Step 4 rehearsal evidence at the exact deployed commit, then run `set-capacity`. A limit above 1 is refused unless that evidence passes the gate and its `repo_sha` matches the checkout's `HEAD`. Evidence from another commit does not count. Sources: FOLLOWUPS F3; SPEC §19.4; RULINGS 8, 15, 24, 52; `runtime_capacity.py`, `concurrency_gate.py`.

**Level 2.** The Roadmap's promotion unit is `(project, task_class, policy_version)`. M0 (correctness baseline) and M1 (Attempt model and canonical ExecutionEngine path) come first. Then the order is M2 → M3 → M4 → M5. The Attempt model, canonical path, shadow mode, cohort freeze, and rollback drill can never be skipped (Roadmap §5). A milestone is complete only when every one of its Exit Gates is evidenced (AGENT-EXECUTION §18). For M0/M1, check `docs/m0-correctness-baseline-status.md`, `docs/m1-exit-gate-status.md`, and `docs/m1-completion-inventory.md`. Do not infer progress from merged PRs.

- **M2, constrained execution and evidence.** Its items are M2.1 through M2.4, and several already have pieces on `main` (listed above). M2.1 is the executor launch contract: a resolved launch spec (prompt, spec file, executor, model snapshot, permission profile, allowed tools, environment allowlist, network policy, preflight, timeout, base commit, policy version, canonical path), recorded as launch provenance with anything unobserved marked unknown, plus pre-launch checks (worktree write preflight, root policy, Git hooks disabled or isolated, a secret environment allowlist, symlink and path-escape checks, a spec size cap, and delimited untrusted issue text). M2.2 is shared runner evidence coverage and producer-Attempt binding. M2.3 is the per-Attempt outcome ledger. M2.4 is the docs-debt inventory, which follows M2.4A/M2.4B's documentation clarifications. M2 closes only when all seven exit-gate rows are evidenced: (1) the executor cannot obtain a merge token; (2) the runner tells execution failure, validation failure, and tool error apart; (3) all required evidence is Attempt-scoped; (4) an operator can judge results from the CLI and artifacts alone; (5) at least 5 human-reviewed tasks have a complete ledger and evidence; (6) there is a human-confirmed backlog of 20-30 real docs-debt issues; (7) that backlog has no duplicates, padding, or non-`docs-only-safe` work. Remaining scope: row 1 (executor environment allowlist and credential isolation, L2-M2-B5, after an owner ruling on the credential boundary); row 4's read-only show-attempt CLI and runbook (B3, not merged); outcome-ledger additions (B4, not merged); row 5's five real runs, which need the V1-EXECUTOR-CONTRACT decision and owner authorization; and rows 6 and 7, which need human confirmation of the backlog and a threshold ruling. Human gates: the credential-boundary ruling (AGENT-EXECUTION §14), executor selection, a human reviewing the five tasks, and a human confirming the backlog. Sources: Roadmap M2 (§2.1-§2.4, Exit Gate); 2026-09-19 ruling, items 2-5; the M2 exit-gate audit (`.agent-taskflow/evidence/L2-M2/opus55-20260925-01/exit-gate-audit/exit-gate-audit.md`, if present).
- **M3, shadow mode (Level 1.5).** Will cover `docs-only-safe` only, with intake through a dedicated GitHub Issue Form. The cohort configuration will be frozen before it starts (model snapshot, prompt, validators, permission profile, policy version, canonical path). The system will record an `auto_merge | reject | needs_human` recommendation but never merge, and a human will decide every PR with a reason code. Exit: 20 eligible attempts that meet the thresholds (first-pass success at least 90%, disagreement at most 5%, zero validation misses, reverts, or critical security incidents), with every disagreement root-caused. It depends on M2.1-M2.4. Source: Roadmap M3.
- **M4, recovery before autonomy.** Will add separate executor, merger, and release identities; branch protection; a post-merge canary; and on canary failure an automatic class disable and revert. At least two recovery drills must succeed: a canary failure and a scope/policy failure. It depends on M3. Human gates: credential-boundary changes (AGENT-EXECUTION §14), and the incident review before a class re-enters shadow mode. Source: Roadmap M4.
- **M5, controlled `docs-only-safe` auto-merge.** Will allow one project (`agent-taskflow`), one class, one frozen policy version, the ExecutionEngine path, and concurrency 1. A human will post-audit all of the first 10 auto-merges. Any incident will disable the class, roll it back, and return it to shadow mode. Level 2 will be declared after at least 10 clean controlled auto-merges. It depends on M4. Human gate: enabling a new autonomy level (AGENT-EXECUTION §14). Under the 2026-09-19 ruling V1 wins where they conflict (SPEC §34 human merge, §41 auto-merge out of scope), so M5 authorizes no auto-merge today, and enabling it needs a new explicit human ruling. Sources: Roadmap M5 and §5; the 2026-09-19 ruling.

**Prompt hygiene:**

- **PROMPT-AUDIT-APPLY: done (#217).** The owner-ordered prompt-audit findings 1, 3, 4, and 5 tidied the Claude Code implementer prompt (`agent_taskflow/executors/claude_code.py`) and the `--model` example in the legacy one-task tick's CLI help and doc. Finding 2 went into this file. Source: human steering, 2026-09-24.

**Validation infrastructure:**

- **CI-VALIDATION-STRATEGY (#210), layered CI validation.** Planned, and parked by the owner since 2026-09-24. PR #210 is a draft and is not on `main`, so CI still runs the workflow shown under "Validation Expectations". Its scope follows the single-full-runner ruling: deterministic, diff-based lane selection with recorded reasons, hashes, and exit codes. Docs-only diffs run the relevant docs tests, link checks, and validators. High-risk, unknown-impact, and CI/dependency/discovery diffs run full pytest, as does any diff whose fast gate failed or was flaky. Full unittest runs only with a recorded runner or discovery reason, and main, release, and nightly runs keep full pytest. Sources: `human-ruling-single-full-runner.md` (path under "Validation Expectations"), PR #210.

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
- Inspect the relevant files before editing.
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

There is one exception, and it covers only the first three items. The orchestrator is the session that a human has designated as orchestrator and as the single canonical writer of execution state. It dispatches builders and reviewers under `AGENT-EXECUTION.md`. A session does not become the orchestrator by describing itself that way. The orchestrator may commit to and push *task branches*, and open or update *draft* PRs, once required validation and independent review have passed. It then stops at `NEEDS_HUMAN_REVIEW` (AGENT-EXECUTION §1, §13, §21; the 2026-09-19 human ruling and later human steering). Builders and reviewers do not commit, push, or open PRs. After a human merges, the orchestrator may record the internal task as `COMPLETED` (AGENT-EXECUTION §2.3, §17). That record reconciles state; it is not final approval.

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

Reviewers (§9) run in a separate session from the builder. They check that the task is satisfied and scope held, and that the tests cover the changed semantics. They check that the evidence supports the claims, and they examine error and rollback paths, lifecycle and migration invariants, security and permission boundaries, and conflicts with the downstream roadmap. The verdict is one of `APPROVE`, `APPROVE_WITH_NITS`, `REPAIR_REQUIRED`, `BLOCKED_ARCHITECTURE`, or `BLOCKED_SPEC_CONFLICT`, and any non-approval lists concrete findings. A documentation-class finding does not block on its own (RULINGS 33). Reviewers confirm green CI on the exact head, or recorded runner evidence (command, exit code, source hash) for the exact final source, and then run the delta's tests and reproductions instead of repeating the full suite. A repair round gets a narrow review of the delta (RULINGS 36).

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

Locally, run commands from the repository root with `PYTHONPATH=.`. Use the checkout's `.venv` if it has one, or an isolated venv built with `python -m pip install -r requirements.txt -c constraints.txt`. `pytest` and `pytest-xdist` are not declared dependencies, so install them into that venv when you need them. Choose a lane from the actual diff, following the 2026-09-19 human ruling on a single full runner. If present, it is at `.agent-taskflow/evidence/reconciliation/vps-native-20260919-advance01/coordination/human-ruling-single-full-runner.md`.

- **Docs-only:** run the tests that pin the changed docs, check the links and paths you changed, and run the workflow validators and `git diff --check`. Never claim a runtime suite passed.
  - `grep -rln "<changed file name>" tests/` finds the pinned tests. Run each with `PYTHONPATH=. python -m unittest tests.<module> -v`. For example: `tests.test_readme_current_architecture` for the READMEs, `tests.test_docs_maps` for the script and scheduler maps, and `tests.test_workflow_contract` for `WORKFLOW.md`.
  - `PYTHONPATH=. python scripts/validate_workflow_contract.py` and `PYTHONPATH=. python scripts/validate_workflow_policy.py`.
- **Local runtime change:** run the smallest relevant test first. Then run the direct and adjacent caller tests, `compileall`, the workflow validators, a dependency-integrity check (`python -m pip check`), and import or collection checks where relevant. Record the selection and why it is enough.
- **High-risk change, unknown impact, CI/test-infrastructure/dependency/discovery change, or a failed, flaky, or uncertain fast gate:** a full pytest run is mandatory: `PYTHONPATH=. python -m pytest tests -q -p no:cacheprovider`. Add `-n 4` when pytest-xdist is installed (RULINGS 40). Keep any failure visible; a different passing gate cannot hide it.
- **unittest:** by default, run only the discovery and import checks you need, plus specific valuable tests that are proven not to be covered by pytest or that genuinely need unittest runner behavior. Run a full local `unittest discover` only when the diff affects unittest runner or discovery compatibility and targeted checks cannot cover it. Record the reason. Do not add an unconditional full unittest run after pytest.
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
