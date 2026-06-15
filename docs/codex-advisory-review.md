# Codex Advisory Reviewer Contract (v0.2.1)

The Codex Advisory Reviewer Contract is a read-only, dry-run contract that
inspects an existing task artifact directory and generates review artifacts for a
future Codex CLI design/code review stage.

This milestone (`v0.2.1 — Codex Advisory Reviewer Dry-Run Contract`) is dry-run
only. It does not invoke the Codex CLI or any subprocess. It produces artifacts
that a future Codex CLI reviewer would consume, and it records the hard authority
boundaries that any Codex advisory output must respect.

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

## Scope of this milestone

This milestone does not add a Claude Code executor and does not implement P5-f.
It does not change scheduler execution authority, lifecycle transitions,
approval/blocking behavior, merge, branch push, PR creation, cleanup, branch
deletion, worktree deletion, or approval record mutation. There is no real
`--run-codex` implementation in this milestone; `--dry-run` is the default and
only supported mode.
