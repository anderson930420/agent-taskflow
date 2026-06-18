## v0.2.6 — Codex Advisory Review Checklist Hardening

This release hardens the Codex advisory artifact contract so that every advisory
artifact must carry a structured `review_checklist` and non-empty
`human_review_priorities` guidance.

This is checklist coverage hardening, not Codex approval. A valid checklist does
not mean Codex approved the work; it means the advisory artifact is structurally
complete enough to be useful evidence for the human reviewer.

### Added requirements

Codex advisory artifacts now require:

- a structured `review_checklist`
- a non-empty `human_review_priorities` list

### Required checklist areas

The `review_checklist` must cover every required review area:

- `architecture_boundary`
- `design_risk`
- `test_quality`
- `silent_failure`
- `fallback_correctness`
- `race_concurrency`
- `path_cwd_repo_root`
- `human_review_priority`

### Valid checklist statuses

Each checklist area must report one of the following statuses:

- `pass`
- `concern`
- `not_applicable`
- `unknown`

### Advisory, not automatic blockers

Checklist statuses are advisory evidence and do not automatically block.

`concern`, `unknown`, and `not_applicable` are advisory evidence and do not block
by themselves. They never make the artifact contract-invalid on their own.

A valid advisory artifact whose `review_status` is `needs_attention`,
`high_risk`, or a structurally valid `tool_error` remains valid advisory evidence
when its checklist and `human_review_priorities` guidance are structurally valid.

`human_review_priorities` must be non-empty.

Dry-run, default, and `tool_error` fallback artifacts include deterministic
fallback priority guidance so they still satisfy the non-empty
`human_review_priorities` requirement.

### Contract-invalid cases

A Codex advisory artifact is contract-invalid when its checklist or priorities
are not structurally complete, specifically when the `review_checklist` is:

- missing
- malformed
- incomplete (missing a required checklist area)

or when `human_review_priorities` is:

- missing
- empty
- malformed

### Evidence gate inheritance

The v0.2.5 Codex advisory evidence gate inherits this hardening automatically
because it delegates to the deterministic Codex advisory artifact contract
validator. No separate evidence-gate change is required for the new checklist and
priority requirements to take effect before `waiting_approval`.

### Safety boundary

This release does not change any authority boundaries:

- Codex still has no approval authority.
- Codex still has no validator authority.
- Human final review is still required.

### Validation

- `PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_v025_release_docs tests.test_v026_release_docs`
- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
