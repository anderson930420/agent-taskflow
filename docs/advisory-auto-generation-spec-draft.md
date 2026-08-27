# Spec draft: runner auto-generation of Codex advisory evidence (post-M1)

> Read-only design study produced on `main` @ `b5cca7f`, committed as-is.
> Implementation is tracked by #189, which resolves every open decision in §7 —
> read the issue, not §7, for the decisions that are actually in force.
> Implements the item #184 explicitly listed as out of scope: *"Auto-invoking the
> advisory review from inside the runner."*

## Problem

`run_approved_task` requires contract-valid Codex advisory evidence before
`waiting_approval` (v0.2.5 gate), but nothing produces that evidence. The
artifact is generated *from* Attempt evidence, so on a first run it is always
missing and the run always ends `blocked` at the `codex_advisory_evidence`
phase. PR #180 records the consequence: *"Every production tick ends `blocked`
at the `codex_advisory_evidence` gate"* — all seven persisted
`runtime_handoff_execution.json` files carry `execution_authority: null`.

Recovery today is a mandatory manual two-step: run
`scripts/run_codex_advisory_review.py --confirm-run` against the existing
Attempt artifact dir, then `scripts/retry_advisory_evidence_transition.py
--confirm-transition`. This milestone makes the runner produce the evidence
itself so that path becomes exceptional rather than routine.

The core semantic:

```text
Generate the evidence. Do not become the gate.
```

## 1. Insertion point

Auto-generation runs inside `run_approved_task`
(`agent_taskflow/approved_task_runner.py:181`), in the gap between the end of
the validator loop (`:467-551`) and the v0.2.5 gate comment (`:553`) — i.e. the
new call goes at **line 552**, after every deterministic validator has returned
non-`failed`/non-`blocked`, before `_check_codex_advisory_evidence`
(`:558`, helper at `:996-1017`).

```text
validator loop ends (:551)
  -> _generate_codex_advisory_evidence(request, effective_task, worktree_path=...)   # NEW
  -> _check_codex_advisory_evidence(request, effective_task)                          # :558, UNCHANGED
  -> gate blocks (:559-580) or update_task_status(waiting_approval) (:589)
```

The new module-level helper in `approved_task_runner.py`:

```python
def _generate_codex_advisory_evidence(
    request: ApprovedTaskRunRequest,
    task: TaskRecord,
    *,
    worktree_path: Path | None,
) -> dict[str, Any] | None:
```

It returns `None` when disabled, and otherwise a JSON-safe generation record
(see §3). It calls `generate_codex_advisory_review`
(`agent_taskflow/codex_advisory_review.py:1254`) with
`CodexAdvisoryReviewRequest(..., dry_run=False, confirm_run=True)` — never
`dry_run=True` (§5) — passing `artifact_dir=task.artifact_dir`,
`repo_path=request.repo_path`, `worktree_path=workspace_result.worktree_path`.
`build_confirm_run_payload` (`:1150`) already prefers the worktree as the Codex
`cwd`, so the reviewer reads the code the executor actually changed.

**Attempt artifact-dir threading (critical).** The gate does not read
`task.artifact_dir` directly. `install_attempt_scoped_runtime_path`
(`agent_taskflow/attempt_scoped_runtime_path.py:409-481`) monkey-patches the
module attribute `_check_codex_advisory_evidence` with `attempt_check_codex`
(`:453-456`), which rebinds the task to the Attempt artifact root via
`store.bind_task(task)` (`:178-182`) before delegating. The new helper **must**
be threaded identically:

- call it as a bare module-global name inside `run_approved_task`, so the patch
  is visible at call time (same pattern as `:558`);
- add `original_generate_codex = approved_task_runner_module._generate_codex_advisory_evidence`
  and an `attempt_generate_codex(request, task, **kwargs)` wrapper that calls
  `store.bind_task(task)` first, then rebind the module attribute alongside the
  existing seven at `attempt_scoped_runtime_path.py:475-481`.

Without this, generation writes into `artifact_root/<TASK_KEY>`
(`_effective_artifact_dir`, `approved_task_runner.py:1134-1140`) while the gate
reads `.../attempt-<id>/`, reproducing the #184 directory-mismatch loop in a new
form. This is the single highest-risk detail in the milestone.

Every artifact returned by `CodexAdvisoryReviewResult.artifact_paths()` and
`.codex_output_paths()` (`codex_advisory_review.py:293-303`) is recorded with
`_record_artifact(current_store, task_key, "other", path)`
(`approved_task_runner.py:1142`). No `TASK_ARTIFACT_TYPES`
(`agent_taskflow/models.py:78-103`) change is needed.

## 2. Configuration

New fields on `ApprovedTaskRunRequest` (`approved_task_runner.py:69-155`),
validated in `__post_init__` exactly like the `claude_code_*` block (`:139-155`):

| field | default | notes |
| --- | --- | --- |
| `auto_generate_codex_advisory_evidence` | `False` | opt-in, like `claude_code_enable_invocation` |
| `codex_advisory_command` | `DEFAULT_CODEX_COMMAND` (`codex exec`, `codex_advisory_review.py:56`) | `shlex.split`, `shell=False`, must be non-empty |
| `codex_advisory_timeout_seconds` | `DEFAULT_TIMEOUT_SECONDS` (`300`, `:57`) | must be a positive int |

Enabling is rejected when `request.dry_run` is true (dry-run never spawns a
subprocess) and when `require_codex_advisory_evidence` is false (generating
evidence nobody consumes is a no-op, not a feature).

**Threading from the tick CLI** — five hops, mirroring `preflight` verbatim:

1. `agent_taskflow/cli/github_issue_one_task_scheduler_tick.py:53-176` — add
   `--auto-generate-codex-advisory-evidence` (`store_true`, requires
   `--confirmed`), `--codex-advisory-command`, `--codex-advisory-timeout-seconds`;
   pass them in the request build at `:199-230`.
2. `GitHubIssueOneTaskSchedulerTickRequest`
   (`agent_taskflow/github_issue_one_task_scheduler_tick.py:48-199`) — three new
   fields; surface all three in `_runner_config_payload` (`:560-573`).
3. `build_scheduler_tick_execution_engine_request`
   (`agent_taskflow/scheduler_execution_engine_opt_in.py:129-160`) →
   `SchedulerExecutionEngineRequestBuildInput`
   (`scheduler_execution_engine_request_builder.py:81`).
4. `build_scheduler_execution_engine_request` (`:184-248`) → three new
   **top-level** `ExecutionEngineRequest` fields
   (`agent_taskflow/execution_engine_contract.py:236-250`), beside `preflight`.
   Not the executor profile: this is runner behaviour, not executor selection.
   Add them to `scheduler_execution_engine_request_to_json_dict` (`:252`).
5. `ExecutionEngineApprovedTaskAdapter._build_approved_request`
   (`agent_taskflow/execution_engine_approved_task_adapter.py:267-294`) — pass
   through to `ApprovedTaskRunRequest`.

Also thread the three flags onto `scripts/run_approved_task.py` (`:76-175`,
`:190-205`) for the manual one-shot path.

**Smoke safety.** The enforcing test is
`tests/test_run_issue_to_prepared_workspace_smoke.py:254`
`test_smoke_generates_advisory_evidence_without_invoking_codex`, which asserts
`advisory["dry_run"] is True`, `advisory["confirm_run"] is False`, and
`advisory["cli_invoked"] is False` on the payload built at
`scripts/run_issue_to_prepared_workspace_smoke.py:644-649` (`cli_invoked` is
`bool(codex_review_result.codex_output_paths())`). Because the runner default is
`False`, no smoke changes and no smoke behaviour changes: the three smokes that
pre-generate dry-run evidence
(`run_issue_to_prepared_workspace_smoke.py:468`,
`run_issue_to_waiting_approval_smoke.py:679`,
`run_runtime_chain_dogfood_smoke.py:473`) keep satisfying the gate with their own
dry-run artifact, and the runner finds a contract-valid artifact already present
and skips generation entirely (§3, idempotence). The sibling text-scan test
`test_smoke_does_not_use_real_ai_executors` (`:239`) is likewise unaffected — it
forbids the literal `"codex"` token, which none of this introduces.

## 3. Failure semantics

Generation invokes Codex **exactly once per `run_approved_task` call**. There is
no loop, no backoff, and no second attempt; a retry requires a new run
(`reset_task_status.py`, `blocked -> queued`, new Attempt) or the operator
recovery path (§4). `invoke_codex_cli`
(`codex_advisory_review.py:853-915`) is already bounded by
`timeout_seconds` and normalizes every failure instead of raising.

`build_confirm_run_payload` (`:1150-1249`) downgrades all six failure classes to
a contract-valid `tool_error` artifact via `_apply_tool_error` (`:1121-1147`):
`codex_cli_timeout`, `codex_cli_not_found`, `codex_cli_error`,
`codex_cli_nonzero_exit`, `codex_output_parse_error`,
`codex_output_invariant_violation`. An auth failure surfaces as
`codex_cli_nonzero_exit` (or a parse error on a non-JSON diagnostic).

Written on failure, in the Attempt artifact dir:

```text
codex-advisory-review.json    review_status=tool_error, risk_level=unknown,
                              validation_authority=false, human_review_required=true,
                              tool_error={category, message},
                              review_checklist = all 8 areas status=unknown,
                              human_review_priorities = 1 fallback entry,
                              codex_invocation={command, cwd, timeout_seconds,
                                                duration_seconds, timed_out, exit_code}
codex-advisory-review.md      rendered summary incl. checklist + priorities
codex-advisory-review-prompt.md
codex-advisory-review-stdout.txt / -stderr.txt   captured verbatim
```

Ordering at the insertion point:

1. Generate (once).
2. `_check_codex_advisory_evidence` — **unchanged**. If the artifact is
   contract-invalid, the existing block path (`:559-580`) fires with the
   existing contract errors. No new behaviour.
3. **New:** if generation ran and produced `review_status == "tool_error"`,
   block even though the contract passed.

Statuses on that new block: task status `blocked` via `_block_task` (`:812`);
`ApprovedTaskRunResult(ok=False, status="blocked", phase=PHASE_CODEX_ADVISORY_EVIDENCE)`
— the existing constant (`:50`), so no new phase string enters the vocabulary
consumed by `runtime_handoff_execution_from_handoff` or the recovery CLI. Attempt
lifecycle is the ordinary blocked path (`validating -> blocked`, reason
`runtime_governance_blocked`). Error string:

```text
Codex advisory evidence auto-generation failed: <category>: <message>
```

A new `ApprovedTaskRunResult.codex_advisory_generation: dict[str, Any]` field
(beside `codex_advisory_evidence`, `:175`) carries
`{enabled, attempted, skipped_reason, invoked, review_status, risk_level,
tool_error, artifact_paths, duration_seconds}` on every terminal path; add the
kwarg to `_blocked_failure` (`:930-993`) as `codex_advisory_evidence` already is.

**Idempotence.** Before invoking, the helper runs the gate helper against the
artifact dir. If it already reports `satisfied=True`, generation is skipped with
`skipped_reason="advisory_evidence_already_present"` and Codex is never spawned.
This is what keeps the smokes and any operator-pregenerated evidence untouched.

**Never silently pass**: a `tool_error` from auto-generation is a blocking
outcome, not advisory noise, and the artifact recording *why* it failed is on
disk before the block. See OD-1 for the trade-off this accepts.

## 4. Relationship to the #184 recovery path

**Nothing in `retry_advisory_evidence_transition` changes.** Its four
preconditions (`agent_taskflow/advisory_evidence_retry.py:83-86`) stay exactly
as strict, and `_check_advisory_evidence` (`:586-618`) keeps delegating verbatim
to `check_required_codex_advisory_evidence`. Its role changes, not its contract:

| before | after |
| --- | --- |
| routine second step of every first run | exception path for an advisory-infrastructure outage |

The two exception flows it must still serve, both already supported with no
edit:

- **Codex was down, then came back.** Auto-generation wrote `tool_error`, the
  runner blocked. The operator re-runs
  `scripts/run_codex_advisory_review.py --confirm-run` against the *same*
  Attempt artifact dir, overwriting the artifact with a real review, then runs
  `retry_advisory_evidence_transition --confirm-transition`. The precondition
  passes because the artifact is now contract-valid.
- **Codex is down indefinitely and the operator accepts `tool_error`.** The
  operator runs the recovery command directly against the `tool_error` artifact.
  It passes, because v0.2.5 semantics — carried over verbatim by `:586` — accept
  a structurally valid `tool_error` as evidence. The difference from the runner
  blocking is precisely that a named human took responsibility, with
  `--confirm-transition`, an operator identity, and a `task_events` audit row.

That asymmetry is the design, not an inconsistency: the runner refuses to
self-certify an advisory review that never happened; a human may accept it on
the record.

## 5. Authority boundaries

Advisory stays **evidence, not approval**. `validation_authority` is always
`false` and `human_review_required` always `true`, enforced by
`validate_payload` (`codex_advisory_review.py:623`) before any write.
Deterministic validators remain pytest / compileall / policy / changed-files.
Reaching `waiting_approval` is not approval; human final review is unchanged.

The automation must **not**:

- weaken, bypass, reimplement, or relax the v0.2.5 gate or the v0.2.4 contract
  validator — `_check_codex_advisory_evidence` and
  `codex_advisory_evidence_gate.py` are edited **not at all**;
- put any subprocess or generation code in `codex_advisory_evidence_gate.py` —
  `tests/test_codex_advisory_evidence_gate.py:672-678` asserts that module's
  source contains no `import subprocess`, no `subprocess.`, no
  `generate_codex_advisory_review`, no `invoke_codex_cli`, no `codex_command`;
- ever auto-generate a **dry-run** artifact. A `not_run` artifact is
  contract-valid, so auto-generating one would make the gate vacuously
  self-satisfying. Auto-generation is confirm-run or nothing;
- start blocking or passing on advisory *judgment*: `looks_good`,
  `needs_attention`, and `high_risk` all continue to reach `waiting_approval`,
  and `review_status == looks_good` is never required;
- invoke Codex more than once per run, retry, or use `shell=True`;
- approve, merge, push, create PRs, clean up, delete branches or worktrees,
  mutate approval records, or change `reset_task_status` semantics;
- change scheduler execution authority, ExecutionEngine authority, Attempt
  binding rules, or `verify_canonical_attempt`;
- become default-on for smokes, tests, or dry-run.

## 6. Test plan

New tests in `tests/test_approved_task_runner.py` unless noted. Use a fake Codex
command (`[sys.executable, "-c", ...]` via `--codex-advisory-command`), never a
real `codex` binary.

1. **Happy path** — fake command prints a valid advisory JSON object (all eight
   `review_checklist` areas, non-empty `human_review_priorities`,
   `review_status=looks_good`). Assert: task reaches `waiting_approval`;
   the five artifacts exist in the artifact dir; the fake was invoked exactly
   once (sentinel file counter); `codex_advisory_generation["invoked"] is True`;
   `codex_advisory_evidence["satisfied"] is True`.
2. **Failure blocks** — one subtest per class: exit 1, command not found,
   timeout (`--codex-advisory-timeout-seconds 1` against a sleeping script),
   unparseable stdout, and stdout asserting `validation_authority: true`. Assert
   for each: `result.ok is False`, `result.phase == "codex_advisory_evidence"`,
   task status `blocked`, artifact `review_status == "tool_error"` with the
   expected `tool_error["category"]`, stdout/stderr artifacts present, and the
   fake invoked exactly once.
3. **Config off restores current behaviour** — with the flag unset, no process
   is spawned (sentinel absent) and the run blocks at the gate exactly as today.
   The strongest form of this assertion is that every existing test in
   `tests/test_approved_task_runner.py`, `tests/test_run_approved_task_script.py`,
   `tests/test_codex_advisory_evidence_gate.py`, and
   `tests/test_advisory_evidence_retry.py` passes **unmodified**.
4. **Smoke isolation preserved** —
   `tests/test_run_issue_to_prepared_workspace_smoke.py:254` stays green
   unmodified; add an assertion that the smoke's runner request has
   auto-generation disabled. The two gate source-invariant tests
   (`test_codex_advisory_evidence_gate.py:672,676`) stay green unmodified.
5. **Attempt-dir threading** — with the attempt-scoped runtime path installed,
   assert the generated artifacts land in the Attempt artifact dir the gate
   reads, not in `artifact_root/<TASK_KEY>`. Direct regression for §1.
6. **Gate not weakened** — a fake command that emits a contract-invalid artifact
   (e.g. empty `human_review_priorities`) still blocks, and blocks with the
   *contract* errors, not the generation error.
7. **Idempotence / no unbounded retry** — pre-write contract-valid evidence,
   run with auto-generation enabled, assert the fake was never invoked and
   `skipped_reason == "advisory_evidence_already_present"`.
8. **Dry-run and disabled-gate rejection** — constructing the request with
   `dry_run=True` or `require_codex_advisory_evidence=False` plus
   auto-generation raises `ValueError` in `__post_init__`.
9. **Tick threading** — in `tests/test_github_issue_one_task_scheduler_tick.py`
   and `tests/test_run_github_issue_one_task_scheduler_tick_script.py`: the
   three flags reach `ApprovedTaskRunRequest` through the engine adapter, appear
   in `_runner_config_payload`, and are rejected in dry-run mode.

Report exact `python -m pytest -q` counts against the branch-point baseline SHA,
and `python -m compileall -q agent_taskflow scripts tests` exit code, per the
#184 verification format. (Baseline must be measured in a separate pristine
worktree: the smoke tests load `scripts/` at runtime.)

## 7. Open decisions for the operator

**OD-1 — Does an auto-generated `tool_error` block the run?**
*Options:* (a) block at `codex_advisory_evidence` with the `tool_error` artifact
as evidence; (b) let it pass, since v0.2.5 already accepts a structurally valid
`tool_error`, and surface it in the waiting-approval summary.
*Recommendation:* **(a)**. Under (b) a routine Codex outage would silently
degrade every task's advisory evidence to "never reviewed" while tasks kept
flowing to human review; the human would have to notice. Under (a) the failure
is loud, the artifact explains it, and §4 gives the operator two audited ways to
proceed. Cost: an advisory outage stops the queue until a human intervenes.

**OD-2 — Default on or off?**
*Options:* (a) `False`, with the cron tick profile opting in explicitly; (b)
`True`, with smokes and tests opting out.
*Recommendation:* **(a)**. It matches every comparable switch in the repo
(`--confirm-run`, `--confirm-approved-task`, `claude_code_enable_invocation`),
and it is the reason §2's smoke-safety argument needs no smoke edits at all.
Cost: `docs/github-issue-one-task-real-cron-profile.md` and the deployed cron
command must add the flag for the milestone to change anything in production —
a deployment-config change, which is human-only under CLAUDE.md governance.

**OD-3 — Sandbox in the default Codex command.**
*Options:* (a) keep `DEFAULT_CODEX_COMMAND` (`codex exec`) and require a
per-host `--codex-advisory-command` override where bubblewrap user namespaces
are unavailable (`docs/codex-advisory-review.md` documents
`codex exec --sandbox danger-full-access` for exactly that case); (b) bake a
host-specific default into the runner.
*Recommendation:* **(a)**. A sandbox-weakening default is not something the
runner should choose on the operator's behalf, and (b) would put a host-specific
value in a repo-owned default. Cost: one more flag in the cron profile on hosts
that need it.
