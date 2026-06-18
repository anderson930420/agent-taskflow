## v0.2.3 — Waiting Approval Summary Includes Codex Advisory Review Artifact

This release surfaces Codex advisory review artifacts inside the waiting approval summary as read-only human-review evidence.

Codex advisory review remains advisory-only and non-authoritative.

### Added

- New read-only Codex advisory review artifact summary helper
- Detection for `codex-advisory-review.json`
- Optional companion artifact path display for:
  - `codex-advisory-review.md`
  - `codex-advisory-review-stdout.txt`
  - `codex-advisory-review-stderr.txt`
- New `codex_advisory_review` key in waiting approval JSON summary
- New `## Codex Advisory Review` section in waiting approval markdown summary
- Markdown display for Codex advisory artifact paths and Codex advisory warnings
- Malformed JSON handling
- Non-object JSON handling
- Invalid `review_status` fallback to `unknown`
- Invalid `risk_level` fallback to `unknown`
- Invariant hardening for:
  - `validation_authority = false`
  - `human_review_required = true`
- Focused tests for Codex advisory review summary behavior
- Integration tests for waiting approval summary behavior

### Behavior

If Codex advisory artifacts are absent, the waiting approval summary reports:

- `present = false`
- `review_status = missing`
- `risk_level = unknown`
- `validation_authority = false`
- `human_review_required = true`

If Codex advisory artifacts are present, the waiting approval summary surfaces:

- `review_status`
- `risk_level`
- `summary`
- `tool_error`
- JSON artifact path
- markdown artifact path
- stdout/stderr artifact paths when present
- warnings for malformed or inconsistent artifacts

Malformed, invalid, missing-companion, or invariant-violating artifacts are warning-only. They do not fail waiting approval summary generation.

### Safety boundary

This release is evidence-display only.

It does not:

- invoke Codex CLI
- import or call subprocess
- change scheduler behavior
- change lifecycle transitions
- change approval authority
- change validator authority
- change ExecutionEngine authority
- change waiting_approval transition behavior
- change runtime preflight behavior
- push branches
- create PRs
- merge
- cleanup
- delete branches
- delete worktrees
- add Claude Code executor
- implement P5-f

Codex review statuses such as `looks_good`, `needs_attention`, `high_risk`, and `tool_error` are surfaced only as human-review evidence.

They do not affect:

- `ready_for_human_review`
- validator results
- lifecycle status
- approval authority
- execution authority

Hard invariants remain enforced:

- `validation_authority = false`
- `human_review_required = true`

### Validation

- `PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_codex_advisory_review_summary tests.test_waiting_approval_summary`
- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
