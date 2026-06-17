## v0.2.2 — Codex Advisory Reviewer Confirm-Run Support

This release adds explicit confirm-run support for the Codex Advisory Reviewer.

Dry-run remains the default and invokes no subprocess. Codex CLI is invoked only when the operator explicitly supplies `--confirm-run`.

### Added

- Explicit `--confirm-run` support
- `--codex-command`
- `--timeout-seconds`
- Codex CLI invocation with `shell=False`
- Advisory prompt passed through stdin
- stdout/stderr capture artifacts:
  - `codex-advisory-review-stdout.txt`
  - `codex-advisory-review-stderr.txt`
- Codex invocation metadata:
  - command
  - cwd
  - timeout seconds
  - duration seconds
  - timed-out state
  - exit code
  - stdout/stderr artifact paths
- Raw JSON and fenced JSON Codex output parsing
- `tool_error` advisory fallback for:
  - command not found
  - timeout
  - non-zero exit
  - unparseable stdout
  - invalid review status
  - invalid risk level
  - authority invariant violations

### Preserved behavior

Dry-run remains the default mode.

Dry-run writes:

- `codex-advisory-review-prompt.md`
- `codex-advisory-review.json`
- `codex-advisory-review.md`

Confirm-run additionally writes:

- `codex-advisory-review-stdout.txt`
- `codex-advisory-review-stderr.txt`

### Safety boundary

Codex advisory review remains advisory-only and non-authoritative.

Hard invariants remain enforced:

- `validation_authority = false`
- `human_review_required = true`

`needs_attention`, `high_risk`, and `tool_error` are advisory signals only. They do not cause validator-style failure.

This release does not change:

- scheduler behavior
- lifecycle transitions
- approval authority
- ExecutionEngine authority
- waiting_approval summary integration
- branch push
- PR creation
- merge behavior
- cleanup behavior
- branch deletion
- worktree deletion
- Claude Code executor integration
- P5-f

### Validation

- `PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_codex_advisory_review tests.test_run_codex_advisory_review_script`
- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
