## v0.2.1 — Codex Advisory Reviewer Dry-Run Contract

This release adds the Codex Advisory Reviewer dry-run contract.

### Added

- Read-only Codex advisory review artifact generation
- `codex-advisory-review-prompt.md`
- `codex-advisory-review.json`
- `codex-advisory-review.md`
- Packaged CLI entry point:
  - `agent-taskflow-codex-advisory-review`
- Script shim:
  - `scripts/run_codex_advisory_review.py`
- Contract documentation:
  - `docs/codex-advisory-review.md`

### Safety boundary

Codex advisory review is advisory-only and non-authoritative.

It does not:

- invoke Codex CLI
- invoke subprocesses
- change scheduler behavior
- change lifecycle transitions
- change approval authority
- change ExecutionEngine authority
- approve, block, merge, push, or cleanup
- delete branches or worktrees
- add Claude Code executor
- implement P5-f

The generated review payload preserves:

- `validation_authority = false`
- `human_review_required = true`

Deterministic validators remain pytest / compileall / policy / changed-files. Human approval remains the final gate.

### Validation

- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
