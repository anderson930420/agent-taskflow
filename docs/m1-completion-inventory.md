# M1 Completion Inventory

> Read-only gap analysis. No code, branch, or state was modified to produce it.
> Working directory: `/home/ubuntu/agent-taskflow`
> Branch: `main` @ `de633a9aa7193bcd940b36e23b2ce36c431a1471` (pulled before analysis)
> Audited database: `~/.agent-taskflow/state/github_issue_scheduler.sqlite3`
> Date: 2026-08-15

Scope: what remains to take Milestone 1 from its current state to closed, against
the **existing** M1 definition in `docs/m1-exit-gate-status.md` and
`docs/canonical-execution-engine-authority.md`. No new architecture is proposed.

## 0. How the current state was established

1. Read `docs/m1-exit-gate-status.md`, `docs/canonical-execution-engine-authority.md`,
   `docs/execution-engine-contract.md`, `docs/execution-engine-approved-task-adapter.md`,
   and `docs/codex-advisory-review.md` (§ "Operator recovery for codex_advisory_evidence blocking",
   lines 634-769).
2. Read the runtime path: `scheduler_execution_engine_request_builder.py`,
   `scheduler_execution_engine_authority.py`, `execution_engine_approved_task_adapter.py`,
   `approved_task_runner.py`, `attempt_store.py`, `runtime_admission.py`,
   `canonical_runtime_path.py`, `runtime_handoff_execution_from_handoff.py`,
   `pr_preparation_pipeline.py`, `advisory_evidence_retry.py`, `task_status_reset.py`,
   `level2_execution_authority.py`, `m1_exit_gate.py`.
3. Ran the documented read-only audit twice (no pytest was run):

```bash
python3 scripts/audit_m1_exit_gate.py --db-path ~/.agent-taskflow/state/github_issue_scheduler.sqlite3 --repo-root "$PWD"
python3 scripts/audit_m1_exit_gate.py --db-path ... --repo-root "$PWD" \
  --evidence-dir ~/.agent-taskflow/rehearsals/m1-c-merged-main-20260809-cd032aa
```

4. Read production state read-only (`file:...?mode=ro`): `schema_migrations`, `attempts`,
   `attempt_resources`, `lifecycle_events`, `task_events`, `lifecycle_allowed_transitions`,
   plus the 7 `artifacts/github-issue-scheduler/runtime_handoff_executions/*/runtime_handoff_execution.json`
   evidence files.

**Important operational note:** run without `--evidence-dir` the audit reports
5 blocked / 2 partial / 3 passed, because the rehearsal evidence lives outside the repo at
`~/.agent-taskflow/rehearsals/`. The real state is the second run below.

---

## A. Current-state matrix

Audit output at HEAD `de633a9` with `--evidence-dir ~/.agent-taskflow/rehearsals/m1-c-merged-main-20260809-cd032aa`:
`m1_exit_gate=blocked`, counts `{passed: 7, partial: 1, blocked: 2}`.

| # | M1 exit gate | Status | Evidence / code |
| - | --- | --- | --- |
| 1 | Production DB-copy migration, integrity, rollback rehearsal | **done** | `production-db-copy-rehearsal.json` present in 6 rehearsal dirs; gate logic `agent_taskflow/m1_exit_gate.py` (`_audit_*` chain); artifact carries no `repo_sha`, so it is not invalidated by HEAD moving |
| 2 | Dual-write consistency, zero mismatch in a bounded window | **done** | `~/.agent-taskflow/rehearsals/m1-b-20260808T162339Z/dual-write-consistency.json`; contract `docs/m1-dual-write-consistency-observation.md:1-60`; not SHA-bound |
| 3 | One task produces ≥3 non-overwriting Attempts | **partially done** | Schema constraints installed (`agent_taskflow/attempt_resources_schema.py`; triggers `attempt_resources_identity_guard`, `attempt_resources_immutable_paths` present in prod). Audit: *"no task in this database proves three isolated Attempt resources"* — prod `attempt_resources` holds 7 rows, all `attempt_number=1`, one per task |
| 4 | Timeout/abort clears PID, releases lock, applies worktree policy, verifies exit | **done** | `timeout-abort-cleanup.json` in `m1-process-cleanup-20260808T171034Z/`; gate passes |
| 5 | Lifecycle timeline reconstructable from events | **done (with a caveat)** | Audit: *"All 8 Attempt timelines replay to their persisted status."* Caveat: the gate replays **Attempt** chains only. `attempt-3e1b6593…` (AT-GH-159) has a continuous chain `queued→…→blocked` (`lifecycle_events` 39-57) while the **Task** was later moved to `waiting_approval` and `archived` by operator recovery with no corresponding lifecycle event — see gap G3 |
| 6 | Illegal lifecycle transition rejected | **done** | `lifecycle_attempt_transition_guard` + `lifecycle_allowed_transitions` installed in prod (37 attempt rows; `blocked` is terminal — there is no `blocked → *` row) |
| 7 | Pause prevents new pickup | **done** | `pause-admission-rehearsal.json` in `m1-pause-admission-20260808T173210Z/`; gate passes |
| 8 | Project admission pause + task-class kill switch immediately disable | **not started (in production)** | Audit: *"Project/class control implementation is not deployed: project scope, task_class scope, level2_project_class_controls_v1"*. Prod `schema_migrations` contains 11 rows, none of them `level2_project_class_controls_v1`. Code + rehearsal exist (`agent_taskflow/project_class_control_schema.py`, `m1_project_class_control_rehearsal.py`); newest evidence is SHA-bound to `eaf2c30`, not HEAD |
| 9 | ExecutionEngine parity + repository-wide Level 2 authority | **partially done** | Authority wired at the tick (`github_issue_one_task_scheduler_tick.py:224-234`, `_attach_execution_authority` :323-346); Level-2 guards at `approved_task_runner.py:214`, `queued_task_handoff.py:1176`, `dispatcher.py:139`, `api/main.py:312`, `scripts/run_approved_task.py:207`, `one_shot_task_pipeline.py:209`, `runtime_handoff_execution_from_handoff.py:454-462`, `pr_handoff.py:174-183`, `pr_preparation_pipeline.py:336-347`. Evidence `canonical-execution-path.json` (`m1-c-merged-main-20260809-cd032aa`) satisfies **every** required semantic and adversarial check; its **only** failure is `repo_sha=cd032aa ≠ HEAD` (`m1_exit_gate.py:554`). Separately, no production tick has ever produced a bound Attempt — see gaps G1/G2 |
| 10 | Legacy schema and reader retained until M1 closes | **done** | `tasks.is_legacy` present (`attempt_schema.py:59-64`), fallback reader `agent_taskflow/real_scheduled_execution_observability.py` + `scripts/summarize_real_scheduled_execution.py` present; gate passes |

### A.1 The gate-vs-production divergence

Gate 9 can pass on a **deterministic fixture** rehearsal (`m1_canonical_execution_path_rehearsal.py`
uses a disposable DB and a fixture runner; `docs/canonical-execution-engine-authority.md:64-75`)
while every real scheduler tick reports `canonical_attempt_bound=false`. Both are true today
and they are not contradictory: **the M1 exit gate as written does not require a real
production Attempt binding.** Gaps G1-G3 below are required by the stated M1 goal
("canonical attempt binding end-to-end") but are *not* what currently blocks the audit.
This distinction drives the sequencing in section C and decision D1.

---

## B. Gap list

### G1 — Canonical Attempt reserve/bind/associate in the tick execution path

**What exists today**

- Attempt rows *are* created in production, but by the runtime-admission claim, not by the engine:
  `CanonicalRuntimeTaskStore._claim()` (`canonical_runtime_path.py:150-195`) fires on the
  `queued → preparing` transition inside `run_approved_task`, via the import-time monkeypatch chain
  in `agent_taskflow/__init__.py:11-89` (`install_canonical_runtime_path` wraps `run_approved_task`
  to canonicalize its store — `canonical_runtime_path.py:420-457`).
- Terminal mapping is correct: `_terminal_attempt_status()` (`canonical_runtime_path.py:197-205`)
  maps `waiting_approval → (waiting_approval, completed, passed)`, which is exactly what
  `verify_canonical_attempt` requires (`level2_execution_authority.py:280-296`).
- The engine does **not** reserve or pass an Attempt id. `ApprovedTaskRunnerExecutionEngineAdapter`
  snapshots ids before the call (`execution_engine_approved_task_adapter.py:125`) and *infers* the
  Attempt afterwards by set difference (`:149`, `:239-273`) — and only when `mapped.ok` is true
  (`:136-147` returns `canonical_attempt_bound: False` for any non-ok result).
- Consequences visible in production (7 runtime-handoff evidence files, e.g.
  `artifacts/github-issue-scheduler/runtime_handoff_executions/runtime-execution-20260815T070400-9b52830a15d5/runtime_handoff_execution.json`):
  - every run ends `runner_status=blocked` at the `codex_advisory_evidence` gate, so the binding
    branch is never entered;
  - top-level `"execution_authority": null` in **every** file. That field is only populated when
    `level2_execution` is true at `runtime_handoff_execution_from_handoff.py:451`. `tasks.is_legacy`
    defaults to `1` (`attempt_schema.py:59-64`), and the task is promoted to non-legacy only *later
    in the same tick* by `ensure_level2_task_identity` inside the authority
    (`scheduler_execution_engine_authority.py:144-147`). So on a task's first run the handoff-level
    Level 2 enforcement — the engine-authority-callback requirement (`:454-462`) and the canonical
    Attempt verification block (`:568-601`) — is **inert**, while downstream consumers
    (`pr_preparation_pipeline.py:336`) see the same task as Level 2 and demand a bound Attempt.

**What M1-complete looks like**

The Attempt id is reserved/bound before the executor starts, travels through the runner into the
runtime evidence artifact, and the runtime-handoff Level 2 decision is made against an identity that
is already settled (identity promoted at ingestion/admission, or the check re-evaluated after
promotion). `canonical_attempt_bound=true` + `canonical_attempt_store_verified=true` on a real tick.

**Files:** `agent_taskflow/scheduler_execution_engine_authority.py`,
`execution_engine_approved_task_adapter.py`, `canonical_runtime_path.py`,
`runtime_handoff_execution_from_handoff.py`, `level2_execution_authority.py`,
`github_issue_one_task_scheduler_tick.py`, `github_issue_ingestion.py` (identity registration point).

**Effort: L** — touches the ordering of identity promotion, the adapter's binding contract, and the
evidence payload, and every one of those has adversarial tests to keep green.

**Dependencies:** none upstream. Blocks G2 (PR preparation has nothing to verify until a real tick
binds) and is a precondition for any *production* claim about end-to-end binding.

---

### G2 — `pr_preparation` verification against bound Attempts, including the post-recovery case

**What exists today**

- Level 2 preflight requires an exact Attempt: `pr_preparation_pipeline.py:334-347` adds
  `canonical_attempt_id_required_for_level2` when none is supplied and delegates to
  `verify_canonical_attempt`.
- Runtime evidence cross-check: `_runtime_evidence()` (`:437-497`) adds
  `runtime_runner_not_ok` when **any** runtime payload carries `runner_ok=false` (`:476-477`), and
  when a verified attempt id is present it additionally requires each payload to carry
  `execution_authority=execution_engine`, the same `canonical_attempt_id`,
  `canonical_attempt_bound=true`, and `canonical_attempt_store_verified=true` (`:478-489`).
  The PR-handoff stage repeats the exact-binding check at `:1157-1164`.
- The post-recovery case therefore fails on four independent counts. For AT-GH-159, after the audited
  advisory-evidence recovery: the Attempt row is `status=blocked, execution_result=blocked,
  validation_result=NULL` (so `verify_canonical_attempt` fails at
  `level2_execution_authority.py:284-296`), the persisted runtime evidence has `runner_ok=false`,
  `canonical_attempt_id=null`, `canonical_attempt_bound=false`, and `execution_authority=null`.
  Nothing in the recovery path updates any of that (see G3).

**What M1-complete looks like**

A task that reached `waiting_approval` — by a clean run or by audited recovery — carries exactly one
verifiable canonical Attempt id, and `pr_preparation` consumes *that* id rather than rejecting the
task or reselecting the latest Attempt. Whether recovery is allowed to produce a PR-eligible Attempt
at all is decision D1.

**Files:** `agent_taskflow/pr_preparation_pipeline.py`, `pr_handoff.py`,
`advisory_evidence_retry.py`, `level2_execution_authority.py`.

**Effort: M** if recovery is made Attempt-aware (G3) and PR prep simply reads the recovered binding;
**L** if the advisory gate is moved so the Attempt never terminalizes as `blocked`.

**Dependencies:** requires G1 (a bound Attempt on the happy path) and G3 (a bound Attempt on the
recovery path).

---

### G3 — Engine-authority alignment of `retry_advisory_evidence_transition` and `reset_task_status`

**What exists today**

- `reset_task_status` **is** Attempt-aware: it reserves the next Attempt and records lineage
  (`task_status_reset.py:178-183`, `reset_lineage.py:397`), and enforces `blocked → queued` only.
  It does **not** consult `level2_direct_execution_error` — defensible, since it does not execute.
- `retry_advisory_evidence_transition` is **neither** Attempt-aware nor authority-aware:
  `advisory_evidence_retry.py:52` imports `TaskMirrorStore` directly (the un-canonicalized class;
  the monkeypatches at `__init__.py:11-89` rewrite `run_approved_task` and `Dispatcher`, not this
  import), and `:529-553` performs a bare `update_task_status(blocked → waiting_approval)` plus a
  `task_events` audit record.
- Production consequence (AT-GH-159, `task_events` 109-110 at `2026-08-14T21:34:05Z`): the Task moved
  to `waiting_approval`, but `lifecycle_events` for `attempt-3e1b6593…` still end at event 57
  (`validating → blocked`, `runtime_governance_blocked`), and the Attempt row still reads
  `status=blocked / execution_result=blocked / validation_result=NULL`. Task state and Attempt state
  are divergent, and the divergence is invisible to gate 5 because that gate replays Attempt chains only.
- Note the constraint any fix must respect: `lifecycle_allowed_transitions` has **no**
  `blocked → waiting_approval` Attempt row, so the current guard would reject that transition outright.

**What M1-complete looks like**

Both operator transitions go through the canonical runtime store, append an Attempt-bound
`lifecycle_events` row with an operator-recovery reason code, and leave Task status and Attempt status
consistent — without weakening the v0.2.5 advisory gate or the v0.2.4 contract validator
(`docs/codex-advisory-review.md:753-769` is explicit that the recovery path must not relax them).

**Files:** `agent_taskflow/advisory_evidence_retry.py`,
`scripts/retry_advisory_evidence_transition.py`, `canonical_runtime_path.py`,
`lifecycle_control_schema.py` / `lifecycle_allowed_transitions` seed, `task_status_reset.py`.

**Effort: M** — small code surface, but it touches the transition allowlist, which is a governance
artifact and needs operator sign-off (decision D1).

**Dependencies:** independent of G1; blocks G2's post-recovery half.

---

### G4 — Exit-gate evidence runs still missing

| Evidence | State | What is needed | Effort |
| --- | --- | --- | --- |
| `canonical-execution-path.json` | Semantically complete, **SHA-stale** (`cd032aa` vs HEAD `de633a9`). All 9 required semantics and all 3 adversarial checks are `true` | Re-run `scripts/run_m1_canonical_execution_path_rehearsal.py` at the **final closing SHA** (`m1_exit_gate.py:554` compares to `git rev-parse HEAD`) | **S** — one command, but it must be the last step before the audit |
| `project-class-control-rehearsal.json` | Bound to `eaf2c30`; and the **production DB does not have `level2_project_class_controls_v1`** | Deploy the migration to the production database, then re-run `scripts/run_m1_project_class_control_rehearsal.py` at the closing SHA. The gate checks both the target DB *and* the disposable evidence DB (`m1_exit_gate.py:430-456`) | **M** — a production schema deploy, reviewed separately |
| Three-Attempt isolation | `partial` — prod has 7 `attempt_resources` rows, all `attempt_number=1` | One disposable task driven through three Attempts, in whichever DB is audited at closeout (see decision D3) | **M** |
| `production-db-copy-rehearsal.json` | **passing**, not SHA-bound | nothing | — |
| `dual-write-consistency.json` | **passing**, not SHA-bound | nothing | — |
| `timeout-abort-cleanup.json` | **passing**, not SHA-bound | nothing | — |
| `pause-admission-rehearsal.json` | **passing**, not SHA-bound | nothing | — |

**Would a direct-cutover decision waive any of these?** No. The four already-passing artifacts are
satisfied and are not SHA-bound, so they survive further commits. The dual-write artifact is a
disposable-target observation of `claim()`/`release()` (`docs/m1-dual-write-consistency-observation.md:16-35`),
not a live production dual-write window, so choosing "direct cutover" over "another observation
window" changes nothing that the audit reads. The only genuinely outstanding evidence is the three
rows above, and none of them is waivable by a cutover decision — two are SHA-bound by construction
and one is a database-content fact.

---

### G5 — Legacy / shadow / compat removal scope

| Module | Status today | Removal belongs to |
| --- | --- | --- |
| `scheduler_execution_engine_opt_in.py` | **Partly live.** `build_scheduler_tick_execution_engine_request` is imported by the authority (`scheduler_execution_engine_authority.py:38-40`). `route_scheduler_tick_through_execution_engine` is the historical P5-d opt-in path and now fail-closes for Level 2 (`:207-222`). 13 referencing files, 8 test files | Later milestone (split, don't delete) |
| `scheduler_execution_engine_fallback.py` | Historical classifier; doc header marks it non-authoritative (`docs/scheduler-execution-engine-fallback-hardening.md:1-7`). 5 files, 3 test files | Later milestone |
| `scheduler_execution_engine_shadow_compare.py` | **Live but diagnostic** — called by `SchedulerExecutionEngineAuthority.evidence()` (`:236-245`), explicitly non-overriding (`shadow_result_can_override_authority: false`) | Later milestone |
| `execution_engine_manual_runtime.py` | Manual opt-in facade; 9 files, 7 test files | Later milestone |
| `attempt_scoped_runtime_compat.py`, `lifecycle_reason_compat.py`, `executor_process_reason_compat.py`, `validator_process_reason_compat.py` | Installed unconditionally at import (`__init__.py:28-89`); reason-string back-compat for old evidence readers | Later milestone |
| `real_scheduled_execution_observability.py` + `scripts/summarize_real_scheduled_execution.py` | **Must be retained.** Gate 10 reads them (`m1_exit_gate.py:587-589`) and the canonical rehearsal asserts `legacy_reader_compatibility_retained` | Retain past M1 |
| `--use-execution-engine` flag | Documented compatibility no-op (`docs/canonical-execution-engine-authority.md:58-60`); still surfaced in authority evidence (`:261-263`) | Later milestone |
| Import-time monkeypatch chain in `agent_taskflow/__init__.py:7-89` (11 installers) | The mechanism by which the canonical runtime store reaches `run_approved_task` — i.e. **load-bearing**, not dead compat | Later milestone; needs its own design review |

**Recommendation: none of this belongs to M1 close.** `docs/m1-exit-gate-status.md:67` requires the
legacy schema and reader to remain available "until final M1 closeout", and
`docs/canonical-execution-engine-authority.md:50-61` deliberately keeps the P5 opt-in/fallback
helpers import-compatible. Removing them before the canonical rehearsal is regenerated would risk the
`legacy_reader_compatibility_retained` semantic that the gate requires. Schedule the sweep as the
first M2 item, after `m1_exit_gate=passed` is recorded.

**Effort if/when taken: L** (≈35 referencing files including 22 test files).

---

## C. Proposed issue breakdown

Ordered. Each is one reviewable PR.

**1. Settle Level 2 task identity before the runtime-handoff authority check**
Register the canonical non-legacy `TaskIdentityRecord` at ingestion/admission, or re-evaluate
`is_level2_task` after `ensure_level2_task_identity`, so the handoff-level Level 2 enforcement in
`runtime_handoff_execution_from_handoff.py:451-462` is not inert on a task's first run. Fixes the
`"execution_authority": null` seen in all seven production runtime-handoff evidence files. No change
to the engine contract. *(Depends on: nothing. Effort: M.)*

**2. Reserve and bind the canonical Attempt inside the ExecutionEngine path**
Replace the adapter's post-hoc "diff the Attempt list" inference
(`execution_engine_approved_task_adapter.py:125,149,239-273`) with an Attempt reserved before the
executor starts and carried through the runner, so `canonical_attempt_id` is present in the runtime
evidence artifact regardless of terminal status. Keep the existing store verification in
`scheduler_execution_engine_authority.py:187-224` as the acceptance check. *(Depends on: 1.
Effort: L.)*

**3. Make audited operator recovery Attempt-aware**
Route `advisory_evidence_retry` through the canonical runtime store, append an Attempt-bound
`lifecycle_events` row with an operator-recovery reason code, and seed the corresponding
`lifecycle_allowed_transitions` entry (or reserve a recovery Attempt via reset lineage — decision D1).
Aligns `reset_task_status` and `retry_advisory_evidence_transition` on one lifecycle authority without
weakening the v0.2.5 gate. *(Depends on: D1. Effort: M.)*

**4. Verify PR preparation against the bound Attempt, including recovered tasks**
Make `pr_preparation_pipeline` preflight consume the bound Attempt id and accept audited-recovery
evidence per D2, instead of failing on `runtime_runner_not_ok` +
`runtime_canonical_attempt_id_mismatch` for every recovered task. Extend the exact-binding assertion
in the PR-handoff stage (`:1157-1164`) to the recovery shape. *(Depends on: 2, 3. Effort: M.)*

**5. Deploy project/class controls to production and produce the two missing evidence runs**
Apply `level2_project_class_controls_v1` to the production database (governance-reviewed schema
deploy), re-run the M1-D rehearsal, and drive one disposable task through three Attempts to satisfy
the three-Attempt isolation gate. Auto-merge stays disabled throughout. *(Depends on: nothing; can run
in parallel with 1-4. Effort: M.)*

**6. M1 final closeout**
At the final closing SHA, re-run `scripts/run_m1_canonical_execution_path_rehearsal.py`, then
`scripts/audit_m1_exit_gate.py --evidence-dir … --require-passed`, and record
`m1_exit_gate=passed` in `docs/m1-exit-gate-status.md`. Must be last: the canonical and project/class
artifacts are invalidated by any subsequent commit. *(Depends on: 1-5. Effort: S.)*

---

## D. Open decisions for the operator

**D1 — How does an advisory-evidence recovery reach a PR-eligible Attempt?**
- *(a)* Add an audited `blocked → waiting_approval` Attempt transition to
  `lifecycle_allowed_transitions`, restricted to the operator-recovery reason code, and have the
  recovery write it. Smallest change; keeps the evidence in the original Attempt directory, which is
  what the recovery contract requires (`docs/codex-advisory-review.md:673-706`). Cost: it widens the
  lifecycle allowlist, which is a governance artifact.
- *(b)* Have the recovery reserve a new Attempt via reset lineage. Keeps `blocked` terminal, but the
  advisory evidence lives in the *previous* Attempt's directory — the exact loop the recovery path
  was built to break.
- *(c)* Reorder the advisory gate so the Attempt never terminalizes as `blocked`. Structurally
  cleanest, largest change, and closest to new architecture.
- **Recommendation: (a).** It is the only option that keeps the evidence, the gate, and the "no new
  Attempt" contract all intact. It needs your explicit sign-off because it edits the transition
  allowlist.

**D2 — May a recovered task produce a PR at all, or must it be re-run cleanly?**
- *(a)* PR preparation accepts audited-recovery evidence (recovery reason code + the operator audit
  event) as an alternative to `runner_ok=true`.
- *(b)* Recovered tasks are review-only; a PR requires a clean run that reaches `waiting_approval`
  through the runner.
- **Recommendation: (a),** narrowly — accept it only when the recovery audit event is present and the
  Attempt binding verifies. Otherwise the advisory gate makes the first run of every task
  permanently PR-ineligible, which is the loop already documented in
  `docs/codex-advisory-review.md:640-672`.

**D3 — Which database is audited at closeout?**
The three-Attempt isolation gate reads whatever `--db-path` is given. Options: (a) production
`github_issue_scheduler.sqlite3` — then a disposable task must be driven through three Attempts *in
production*; (b) a production copy. **Recommendation: (a)** with a disposable task key, since gate 8
already requires the migration deployed to the real production database, and auditing two different
databases for one closeout weakens the result.

**D4 — Direct cutover vs another dual-write observation window.**
No evidence is waived either way; the dual-write artifact already passes and is not SHA-bound.
**Recommendation: direct cutover** — do not schedule another window; spend the effort on G1-G3, which
is where the real end-to-end binding gap is.

**D5 — Compat/shadow removal scope.**
**Recommendation: defer all of G5 to the first M2 item.** M1 explicitly requires legacy retention
until closeout, and the canonical rehearsal asserts legacy-reader compatibility.

---

## Not run

- `pytest` / `python3 -m unittest` — excluded by the task instructions.
- `scripts/run_m1_canonical_execution_path_rehearsal.py` and
  `scripts/run_m1_project_class_control_rehearsal.py` — these write evidence artifacts; regenerating
  them is issue 5/6 work, not inventory work.
- No file in the repository was modified except this document; nothing was committed.
