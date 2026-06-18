## v0.2.5 — Require Codex Advisory Artifact Evidence Before waiting_approval

This release requires valid Codex advisory artifact contract evidence before a task can transition into `waiting_approval`.

This is required evidence, not Codex approval.

A task may enter `waiting_approval` only when:

- existing deterministic validators pass
- the Codex advisory artifact contract validator passes

The required contract validator is:

- `codex_advisory_artifact_contract`

### Added

- New required Codex advisory evidence gate:
  - `agent_taskflow/codex_advisory_evidence_gate.py`
- New required evidence helper:
  - `check_required_codex_advisory_evidence`
- New result fields for approved task runner output:
  - `codex_advisory_evidence`
  - `codex_advisory_evidence_required`
  - `codex_advisory_evidence_satisfied`
- New pre-`waiting_approval` phase:
  - `codex_advisory_evidence`
- Tests for the evidence gate:
  - `tests/test_codex_advisory_evidence_gate.py`
- Additional approved task runner tests for the actual transition boundary
- Documentation for required Codex advisory evidence in:
  - `docs/codex-advisory-review.md`

### Behavior

The approved task runner now checks Codex advisory evidence after deterministic validators pass and before flipping task status to `waiting_approval`.

When the Codex advisory artifact contract is valid, the task may enter `waiting_approval`.

Valid advisory statuses include:

- `looks_good`
- `needs_attention`
- `high_risk`
- `tool_error`, when structurally valid

These advisory statuses do not block by themselves.

The gate blocks `waiting_approval` when Codex advisory evidence is:

- missing
- malformed
- not a JSON object
- task-key mismatched
- missing or invalid `review_status`
- missing or invalid `risk_level`
- missing or non-false `validation_authority`
- missing or non-true `human_review_required`
- missing required companion artifacts
- missing required confirm-run stdout/stderr companions
- structurally invalid as `tool_error`
- otherwise contract-invalid

### Required evidence, not approval

This release does not require Codex to report `looks_good`.

A valid `needs_attention` artifact is required evidence.

A valid `high_risk` artifact is required evidence.

A structurally valid `tool_error` artifact is required evidence.

The human reviewer evaluates the advisory content.

Codex advisory review is not approval authority.

### Default enforcement

`ApprovedTaskRunRequest.require_codex_advisory_evidence` defaults to `True`.

An explicit opt-out exists for compatibility and targeted tests, but the default approved task runner behavior requires valid Codex advisory evidence before `waiting_approval`.

### Policy validator note

Codex advisory artifacts are treated as instruction/advisory artifacts by the policy validator.

This avoids false positives from governance-prohibition text such as "no merge" or "delete worktree" inside advisory artifacts.

Secret scanning is still preserved.

### Safety boundary

This release does not:

- invoke Codex CLI
- add subprocess behavior
- make Codex judgment validator authority
- require `review_status == looks_good`
- block merely because `review_status == high_risk`
- block merely because `review_status == needs_attention`
- block merely because `review_status == tool_error`
- change approval authority
- change ExecutionEngine authority
- change human final approval requirement
- push branches
- create PRs
- merge
- cleanup
- delete branches
- delete worktrees
- add Claude Code executor
- implement P5-f

### Validation

- `PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_codex_advisory_artifact_contract_validator tests.test_codex_advisory_evidence_gate tests.test_approved_task_runner`
- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
