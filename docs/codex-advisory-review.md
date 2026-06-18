# Codex Advisory Reviewer Contract (v0.2.2)

The Codex Advisory Reviewer Contract inspects an existing task artifact directory
and generates review artifacts for the Codex CLI design/code review stage.

The default mode (since `v0.2.1 — Codex Advisory Reviewer Dry-Run Contract`) is
dry-run: it does not invoke the Codex CLI or any subprocess. It produces
artifacts that a Codex CLI reviewer would consume, and it records the hard
authority boundaries that any Codex advisory output must respect.

`v0.2.2 — Codex Advisory Reviewer Confirm-Run Support` adds an explicit opt-in
confirm-run mode (`--confirm-run`) that invokes the Codex CLI exactly once,
captures its output, and parses it into advisory findings only. Dry-run remains
the default. See [Confirm-run support (v0.2.2)](#confirm-run-support-v022) below.

## What it is

A pure module (`agent_taskflow/codex_advisory_review.py`) plus a CLI
(`agent_taskflow/cli/run_codex_advisory_review.py`, with the
`scripts/run_codex_advisory_review.py` shim and the
`agent-taskflow-codex-advisory-review` console entry point) that:

- normalizes the task key and paths
- inspects evidence file presence in the artifact directory
- builds an evidence manifest
- renders a Codex CLI review prompt
- builds a dry-run JSON payload
- builds a dry-run markdown summary
- validates payload invariants before writing
- writes the artifacts into the artifact directory

## Artifacts it generates

Inside the given artifact directory:

- `codex-advisory-review-prompt.md` — the review prompt for a future Codex CLI
  reviewer.
- `codex-advisory-review.json` — the structured dry-run review payload
  (`schema_version = codex_advisory_review.v1`).
- `codex-advisory-review.md` — a human-readable dry-run summary.

For this dry-run milestone, generated reviews always use:

- `review_status = not_run`
- `risk_level = unknown`

Allowed `review_status` values: `not_run`, `looks_good`, `needs_attention`,
`high_risk`, `tool_error`.

Allowed `risk_level` values: `unknown`, `low`, `medium`, `high`.

## Evidence it reads

Evidence detection is generic and executor-neutral. It does not hard-code
`opencode`, `pi`, or `shell`, and it is designed to work naturally for a future
Claude Code executor's artifacts as well. It reads the presence of common
evidence files if present:

- `task_execution_package.json`
- `implementation_prompt.md`
- `mission_contract.json`
- executor logs (generic `*.log` discovery)
- `pytest.log`
- `compileall.log`
- `policy-validate.log`
- `changed-files-audit.json`

Detection is file-presence inspection only. It does not read file contents, run
subprocesses, or invoke any executor or reviewer.

## Why it is advisory-only

Codex advisory review is advisory only. It is never deterministic validation
authority. Its output is guidance for a human reviewer, never a gate decision.
The generated payload enforces this with two hard invariants:

- `validation_authority` must always be `false`.
- `human_review_required` must always be `true`.

The payload writer validates these invariants before writing and refuses to
write a payload that violates them.

## Why it is not a deterministic validator

Deterministic validators remain `pytest`, `compileall`, `policy`, and
`changed-files`. These are the proof-of-work gates that decide whether evidence
passes. A Codex advisory review must never be treated as deterministic
validation and must never substitute for these validators.

## What it is allowed to do

- inspect file presence in the artifact directory
- build an evidence manifest
- render the review prompt
- build the dry-run JSON payload and markdown summary
- validate payload invariants
- write the three review artifacts into the artifact directory

## What it must never do

- invoke a subprocess or the Codex CLI
- approve, block, merge, push, or clean up
- delete branches or worktrees
- change task lifecycle state
- mutate approval records
- create commits, push branches, or create PRs
- delete files outside the generated review artifacts

## How it fits the later flow

```text
Implementer executor
→ deterministic validators
→ Codex advisory reviewer
→ human final approval
```

The Codex advisory reviewer sits after deterministic validators and before human
final approval. It adds advisory signal; it never adds authority.

## Confirm-run support (v0.2.2)

Dry-run remains the default. Confirm-run is an explicit opt-in that invokes the
Codex CLI exactly once:

```bash
agent-taskflow-codex-advisory-review \
  --task-key GH-1234 \
  --repo-path /path/to/repo \
  --worktree-path /path/to/worktree \
  --artifact-dir /path/to/artifacts/GH-1234 \
  --confirm-run \
  --codex-command codex \
  --timeout-seconds 300
```

- Dry-run remains the default. The Codex CLI is **never** invoked unless
  `--confirm-run` is explicitly supplied.
- `--codex-command` (default `codex`) may only be used together with
  `--confirm-run`. The command is split with `shlex.split` and always run with
  `shell=False`.
- `--timeout-seconds` (default `300`) must be a positive integer.
- The advisory prompt is sent to Codex on stdin. The reviewer captures stdout,
  stderr, exit code, timeout status, and duration.
- Codex stdout/stderr are written as artifacts:
  - `codex-advisory-review-stdout.txt`
  - `codex-advisory-review-stderr.txt`
  - (these are not written in dry-run mode.)
- Codex output is parsed into advisory findings only. A raw JSON object or a
  JSON object inside a fenced ```json block is accepted. Only the advisory
  fields (`review_status`, `summary`, `*_findings`, `risk_level`,
  `recommended_human_focus`, `suggested_followups`, `missing_evidence`) are
  merged. Canonical fields (`schema_version`, `reviewer`, `task_key`,
  `validation_authority`, `human_review_required`, `artifacts`, `generated_at`,
  `repo_path`, `worktree_path`, `artifact_dir`, `governance`) always win.
- The two hard invariants are always enforced by agent-taskflow:
  `validation_authority` is always `false` and `human_review_required` is always
  `true`, even if Codex output tries to set them otherwise.

### Tool errors become advisory artifacts

Confirm-run never crashes the workflow on a Codex problem. Each of the following
is downgraded to a valid advisory artifact with `review_status = tool_error` and
`risk_level = unknown` (invariants still enforced):

- command not found (`FileNotFoundError`)
- timeout
- non-zero exit code
- stdout that cannot be parsed into a JSON object
- output that violates an invariant (e.g. `validation_authority = true`,
  `human_review_required = false`, an invalid `review_status` such as
  `approved` / `passed` / `failed` / `blocked` / `merge_ready`, or an invalid
  `risk_level`)

A `tool_error`, `needs_attention`, or `high_risk` result is advisory signal
only. It does not exit non-zero and it does not block or approve the task. The
CLI exits `0` whenever a valid advisory artifact was written, and exits `1` only
on invalid input or artifact write failure.

### Confirm-run remains advisory-only and non-authoritative

Confirm-run does not change any of the boundaries above. Codex cannot approve,
block, validate, merge, push, cleanup, delete branches, delete worktrees, or
change lifecycle. It does not create commits, push branches, or create PRs.
Human final approval remains required and deterministic validators remain
pytest / compileall / policy / changed-files.

## Scope of this milestone

`v0.2.2` adds confirm-run invocation only. It intentionally does **not** include
the following, which remain out of scope:

- the `waiting_approval` summary integration is intentionally not included in
  `v0.2.2`
- the Claude Code executor is intentionally not included in `v0.2.2`
- P5-f is intentionally not included in `v0.2.2`

It does not add a Claude Code executor and does not implement P5-f. It does not
change scheduler execution authority, `ExecutionEngine` authority, the
`approved_task_runner`, the confirmation verifier authority, the
`waiting_approval` transition or its summary integration, lifecycle transitions,
approval/blocking behavior, merge, branch push, PR creation, cleanup, branch
deletion, worktree deletion, or approval record mutation. The reviewer never
uses `shell=True` and never adds ambiguous flags such as `--approve`,
`--validate`, `--merge`, `--execute-approval`, or `--run-validator`.

## Waiting approval summary integration (v0.2.3)

`v0.2.3 — Waiting Approval Summary Includes Codex Advisory Review Artifact` adds
a read-only summary layer that surfaces any Codex advisory review artifacts as
human-review evidence inside the waiting-approval review summary.

A pure helper module
(`agent_taskflow/codex_advisory_review_summary.py`) exposes
`summarize_codex_advisory_review_artifacts(artifact_dir)` returning a
`CodexAdvisoryReviewSummary`. The waiting-approval summary
(`agent_taskflow/waiting_approval_summary.py`) wires this into its JSON output
under the `codex_advisory_review` key and into its markdown output under a
`## Codex Advisory Review` section.

### What it detects

Only the Codex advisory review artifacts produced by `v0.2.1` / `v0.2.2` are
detected, by file presence in the task artifact directory:

- `codex-advisory-review.json`
- `codex-advisory-review.md`
- `codex-advisory-review-stdout.txt`
- `codex-advisory-review-stderr.txt`

### What it exposes

The `codex_advisory_review` section exposes `present`, `review_status`,
`risk_level`, `validation_authority` (always `false`), `human_review_required`
(always `true`), `json_path`, `markdown_path`, `stdout_path`, `stderr_path`,
`summary`, `tool_error`, and `warnings`.

### How it behaves

- **Artifact absent:** `present = false`, `review_status = "missing"`,
  `risk_level = "unknown"`, `validation_authority = false`,
  `human_review_required = true`, `warnings = []`. The summary does not fail.
- **JSON valid:** advisory fields (`review_status`, `risk_level`, `summary`,
  `tool_error`) are surfaced and artifact/companion paths are included.
- **JSON malformed:** `present = true`, `review_status = "malformed"`, a parse
  warning is added, and the whole summary still succeeds.
- **Invariant violation:** a JSON claiming `validation_authority = true` or
  `human_review_required = false` is not trusted; the summary forces
  `validation_authority = false` and `human_review_required = true` and adds a
  warning.
- **Invalid `review_status` / `risk_level`:** coerced to `unknown` with a
  warning.
- **Missing companion files:** referenced markdown/stdout/stderr files that are
  absent produce warnings but never fail the summary.

### What it does not do

This summary is evidence only. It reads files only. In `v0.2.3` it does not
invoke Codex, does not run a subprocess, does not validate, does not approve,
does not block, does not merge, does not push, does not cleanup, does not delete
branches or worktrees, does not change scheduler behavior, does not change
lifecycle transitions or the `waiting_approval` transition, and does not change
`ExecutionEngine` authority. Codex advisory status never affects
`execution_allowed`, the validator result, the approval decision, or
`ready_for_human_review`. It does not integrate a Claude Code executor and does
not implement P5-f. Human final approval remains required and deterministic
validators remain pytest / compileall / policy / changed-files.

## Codex Advisory Artifact Contract Validator (v0.2.4)

`v0.2.4 — Codex Advisory Artifact Contract Validator` adds a deterministic,
file-read-only validator that checks whether a Codex advisory review artifact
exists and satisfies the expected artifact contract established by `v0.2.1` /
`v0.2.2` / `v0.2.3`.

The validator name is:

```text
codex_advisory_artifact_contract
```

The validator lives in a pure helper module
(`agent_taskflow/codex_advisory_artifact_contract_validator.py`) and exposes
`validate_codex_advisory_artifact_contract(request)` returning a
`CodexAdvisoryArtifactContractValidationResult`. The request takes the task
artifact directory and an optional expected `task_key`.

The guiding principle is:

```text
Validate artifact contract, not advisory judgment.
```

### What it validates

The validator validates the artifact contract only:

- artifact presence (`codex-advisory-review.json` exists)
- the JSON parses and is a JSON object
- required schema/identity fields (`schema_version`, `reviewer`, `task_key`)
- task binding (the artifact `task_key` matches the expected `task_key` when one
  is provided)
- allowed advisory enums (`review_status`, `risk_level`)
- authority invariants (`validation_authority` is `false`,
  `human_review_required` is `true`)
- the companion `codex-advisory-review.md` artifact exists
- confirm-run output artifacts (`codex-advisory-review-stdout.txt` and
  `codex-advisory-review-stderr.txt`) exist when the artifact metadata indicates
  a confirm-run / `codex_cli_invoked` / `codex_outputs`
- `tool_error` is structurally valid when present
- `generated_at` is a valid timestamp string when present

It inspects only the established Codex advisory artifact files
(`codex-advisory-review.json`, `codex-advisory-review.md`,
`codex-advisory-review-stdout.txt`, `codex-advisory-review-stderr.txt`,
`codex-advisory-review-prompt.md`).

### PASS / FAIL semantics

The validator **passes** when the artifact exists and satisfies every contract
invariant above. It **fails** when the JSON is missing, malformed, or not an
object; when `task_key` mismatches; when `review_status`/`risk_level` are missing
or invalid; when `validation_authority` is missing or not `false`; when
`human_review_required` is missing or not `true`; when the companion markdown is
missing; when required confirm-run stdout/stderr companions are missing; when
`tool_error` is present but structurally invalid; or when required
schema/identity fields are missing or invalid.

Unlike the `v0.2.3` waiting-approval summary (which never fails and downgrades
problems to warnings), this validator is strict: a missing artifact, malformed
JSON, or any invariant violation is a validator failure.

### What it does not judge

The validator does **not** judge the quality, correctness, severity, or
usefulness of Codex's advisory review content. The advisory statuses
`looks_good`, `needs_attention`, `high_risk`, and `tool_error` are all valid
Codex advisory statuses and are human-review evidence, not deterministic
validator outcomes. In particular:

```text
review_status = "high_risk"       -> contract can pass
review_status = "needs_attention" -> contract can pass
review_status = "tool_error"      -> contract can pass (if structurally valid)
```

`tool_error` means the Codex invocation failed or timed out; if the artifact
records that failure in the expected contract shape, the contract validator
passes.

### What it does not do

In `v0.2.4` this validator reads files only. It does not judge Codex's review
content, does not fail on `high_risk` / `needs_attention` / `tool_error` by
itself, does not invoke Codex, does not import or call `subprocess`, does not
change the scheduler / runner required evidence flow, does not change the
`waiting_approval` transition, and does not change approval / validator /
`ExecutionEngine` authority. It does not change the `v0.2.3` waiting-approval
summary behavior.

`v0.2.4` adds the deterministic validator capability only. It intentionally does
**not** make the validator required before `waiting_approval`; that required
evidence gate is reserved for `v0.2.5`. Human final approval remains required and
deterministic validators remain pytest / compileall / policy / changed-files.

## Required Codex advisory evidence before waiting_approval (v0.2.5)

`v0.2.5` makes valid Codex advisory artifact contract evidence **required**
before a task may transition into `waiting_approval`.

The core semantic is:

```text
Require Codex advisory evidence, not Codex approval.
```

A task may enter `waiting_approval` only when:

```text
existing deterministic validators pass
AND
the v0.2.4 codex_advisory_artifact_contract validator passes
```

This is **required evidence, not required approval**. The deterministic validator
validates the artifact *contract*; the human reviewer evaluates the advisory
*content*. Codex is never validation, approval, or merge authority.

### Where it is enforced

The requirement is enforced at the approved-task transition boundary
(`approved_task_runner.run_approved_task`), after the existing deterministic
validators pass and before the task status is moved to `waiting_approval`. A
small explicit gate helper, `codex_advisory_evidence_gate`, wraps the v0.2.4
validator:

```text
check_required_codex_advisory_evidence(...)
  -> validate_codex_advisory_artifact_contract(...)
```

All flows that reach `waiting_approval` through `run_approved_task` (the
one-shot runner, the queued-task handoff, the scheduler one-task tick, the
one-shot pipeline, and the execution-engine adapter) inherit the gate.

### When the gate blocks waiting_approval

The gate withholds `waiting_approval` (the runner blocks at the
`codex_advisory_evidence` phase and surfaces the contract validator errors as
blocking evidence) when:

```text
codex-advisory-review.json is missing
the advisory artifact JSON is malformed
the advisory artifact JSON is not an object
task_key mismatches the expected task_key
review_status is missing or invalid
risk_level is missing or invalid
validation_authority is missing or not false
human_review_required is missing or not true
required companion artifacts are missing
confirm-run stdout/stderr companions are required but missing
tool_error is structurally invalid
schema / identity fields are missing or invalid
```

A missing or invalid artifact is surfaced as **required-evidence** blocking, not
as a Codex judgment failure.

### What does NOT block by itself

The gate must **not** fail merely because Codex reported a particular advisory
status. All of the following are valid evidence statuses and reach
`waiting_approval` when the contract is otherwise valid:

```text
review_status = "looks_good"      -> can enter waiting_approval
review_status = "needs_attention" -> can enter waiting_approval
review_status = "high_risk"       -> can enter waiting_approval
review_status = "tool_error"      -> can enter waiting_approval (if structurally valid)
```

`tool_error` means the Codex advisory attempt failed or timed out; a
structurally valid `tool_error` artifact is still valid required evidence.

### What it does not do

The `v0.2.5` gate:

- requires valid contract evidence, **not** Codex approval;
- does **not** require `review_status == looks_good`;
- does **not** block on `high_risk` / `needs_attention` / `tool_error` by itself;
- does **not** invoke the Codex CLI and adds **no** subprocess behavior;
- does **not** import or call `subprocess`;
- does **not** push branches, create PRs, merge, clean up, delete branches, or
  delete worktrees;
- does **not** change approval authority, validator authority, `ExecutionEngine`
  authority, or the human final review requirement;
- does **not** change the `v0.2.3` waiting-approval summary, which continues to
  display the Codex advisory artifact details once `waiting_approval` is reached.

Once a task reaches `waiting_approval`, human final approval remains required and
deterministic validators remain pytest / compileall / policy / changed-files.
