## v0.2.4 — Codex Advisory Artifact Contract Validator

This release adds a deterministic, file-read-only validator for Codex advisory review artifacts.

The validator checks whether a Codex advisory review artifact satisfies the expected artifact contract established by v0.2.1, v0.2.2, and v0.2.3.

It validates artifact contract shape only.

It does not judge Codex's advisory review content.

### Added

- New deterministic Codex advisory artifact contract validator
- Validator name:
  - `codex_advisory_artifact_contract`
- New validator module:
  - `agent_taskflow/codex_advisory_artifact_contract_validator.py`
- New focused validator tests:
  - `tests/test_codex_advisory_artifact_contract_validator.py`
- Documentation for the v0.2.4 contract validator in:
  - `docs/codex-advisory-review.md`

### Contract checks

The validator checks:

- `codex-advisory-review.json` exists
- JSON parses as an object
- `schema_version` matches the established Codex advisory review schema
- `reviewer` matches the established Codex reviewer identity
- `task_key` is present
- expected `task_key` matches artifact `task_key` when provided
- `review_status` is present and allowed
- `risk_level` is present and allowed
- `validation_authority` is present and false
- `human_review_required` is present and true
- `codex-advisory-review.md` companion artifact exists
- confirm-run stdout/stderr companion artifacts exist when required by metadata
- `tool_error` is structurally valid when present or required
- `generated_at` is valid when present

### Pass semantics

The validator passes only when the artifact exists and every required contract invariant holds.

The validator does not fail merely because Codex reports one of these advisory statuses:

- `looks_good`
- `needs_attention`
- `high_risk`
- `tool_error`

These are valid advisory statuses and remain human-review evidence.

`tool_error` passes when it is structurally valid.

### Fail semantics

The validator fails on:

- missing `codex-advisory-review.json`
- malformed JSON
- JSON that is not an object
- missing or invalid schema / identity fields
- missing or mismatched `task_key`
- missing or invalid `review_status`
- missing or invalid `risk_level`
- missing or non-false `validation_authority`
- missing or non-true `human_review_required`
- missing markdown companion artifact
- missing required confirm-run stdout/stderr companions
- structurally invalid `tool_error`

### Safety boundary

This release adds validator capability only.

It does not:

- invoke Codex CLI
- import or call subprocess
- wire the validator into scheduler / runner required evidence flow
- require Codex artifacts before `waiting_approval`
- change `waiting_approval` transition behavior
- change v0.2.3 waiting approval summary behavior
- change `ready_for_human_review`
- change approval authority
- change validator authority
- change ExecutionEngine authority
- change runtime preflight behavior
- push branches
- create PRs
- merge
- cleanup
- delete branches
- delete worktrees
- add Claude Code executor
- implement P5-f

### Validation

- `python3 -m unittest tests.test_codex_advisory_artifact_contract_validator`
- `python3 -m compileall agent_taskflow scripts tests`
- `python3 -m unittest discover -s tests`
- Docs/related tests:
  - `tests.test_v021_release_docs`
  - `tests.test_v022_release_docs`
  - `tests.test_v023_release_docs`
  - `tests.test_codex_advisory_review`
  - `tests.test_run_codex_advisory_review_script`
