# Workflow Policy Read-Only API Exposure Design

## Purpose

This document defines how workflow policy proof-of-work artifacts may later be
exposed through read-only API responses. It is a design document for future
implementation phases.

**This phase does not implement any API code, endpoints, schema changes, frontend
UI, dispatcher integration, or runtime enforcement.** It describes the design
space and safety constraints for any future API exposure work.

---

## Current Foundation

The workflow policy proof-of-work artifact chain has a complete, tested foundation:

```
workflow-policy.example.json
  → agent_taskflow/workflow_schema.py (loader/model)
  → scripts/validate_workflow_policy.py
  → scripts/summarize_workflow_policy.py
  → scripts/write_workflow_policy_summary_artifact.py (build_artifact())
  → scripts/run_workflow_policy_artifact_smoke.py
  → scripts/run_workflow_policy_pow_package_smoke.py (build_artifact_index())
  → scripts/run_workflow_policy_review_evidence_smoke.py
  → scripts/report_workflow_policy_review_evidence.py (standalone report)
```

**Canonical constants** (from `agent_taskflow/workflow_policy_artifacts.py`):

| Element | Value |
|---------|-------|
| Summary filename | `workflow_policy_summary.json` |
| Index filename | `artifact_index.json` |
| Summary artifact type | `workflow_policy_summary` |
| Index artifact type | `artifact_index` |
| Review evidence kind | `workflow_policy` |
| Package type | `workflow_policy_proof_of_work` |
| Artifact index version | `0.1` |

**Existing tests locking contracts:**

- `tests.test_workflow_policy_artifact_constants_contract` — doc ↔ constants alignment
- `tests.test_review_evidence_workflow_policy_artifacts` — `_file_kind()` classification
- `tests.test_workflow_policy_artifact_package_contract` — package structure
- `tests.test_workflow_policy_read_only_exposure_contract` — read-only shape
- `tests.test_report_workflow_policy_review_evidence_script` — standalone report command

**Existing review evidence API** (`agent_taskflow/api/review.py`):
`GET /api/tasks/{task_key}/review-evidence` returns an `artifacts` array with
`name`, `kind`, `size_bytes`, `is_validator_log`, `is_executor_log`,
`is_mission_contract` per entry. Workflow policy artifacts appear with
`kind == "workflow_policy"`.

---

## API Design Principles

Any future API exposure of workflow policy artifacts must be:

1. **Read-only.** The API must not trigger state transitions, artifact generation,
   validation, approval, or any dispatcher/executor actions.
2. **Evidence-oriented.** Workflow policy artifacts are proof-of-work metadata.
   They record policy state at generation time.
3. **Non-authoritative for enforcement.** Displaying workflow policy data in an
   API response does not imply that the dispatcher enforces that policy. Enforcement
   is handled by validators and deterministic code.
4. **Non-mutating.** API responses must not cause any file writes, status changes,
   or side effects.
5. **Not a state transition mechanism.** The API is a reporting interface, not a
   control interface.
6. **Not coupled to dispatcher behavior.** Future dispatcher preflight must be
   designed separately and explicitly.
7. **Backward-compatible.** Existing API consumers must not break if workflow
   policy evidence is added or absent.

---

## Candidate API Integration Strategy

### Option A — Extend Existing Review Evidence Response (Recommended First)

Extend the existing `GET /api/tasks/{task_key}/review-evidence` response with an
**optional** top-level field `workflow_policy_evidence`.

- Workflow policy artifacts already appear in the `artifacts` array with
  `kind == "workflow_policy"`.
- Adding a top-level field lets consumers who need full policy metadata opt-in,
  without changing existing artifact list behavior.
- Missing workflow policy artifacts produce `available: false` or an absent field,
  preserving backward compatibility.
- Existing tests already verify that workflow policy artifacts have correct
  `kind`, `name`, `size_bytes`, `is_validator_log`, `is_executor_log`,
  `is_mission_contract` in the existing response shape.

**When to use:** When the existing review evidence response is the primary
integration point and backward compatibility is critical.

### Option B — Add a Dedicated Read-Only Endpoint Later

A dedicated endpoint such as `GET /api/tasks/{task_key}/workflow-policy-evidence`
could provide a self-contained workflow policy evidence response.

- More explicit separation of concerns.
- Requires a new route handler, response schema, and tests.
- Must remain read-only; must not trigger artifact generation or validation.

**When to use:** When the workflow policy evidence response grows large enough
that it meaningfully diverges from the standard review evidence shape, or when
a separate versioning story is needed.

### Recommendation

**Proceed with Option A first** if the existing review evidence response can
safely carry the additional field without violating backward compatibility.
Option A is lower risk because it reuses existing endpoint machinery and tests.

Option B should be reserved for when a clear separation of concerns justifies
the additional endpoint surface.

---

## Candidate API Shape

The following shape is a **proposal; not implemented API behavior.** It describes
how the optional `workflow_policy_evidence` field could be added to the existing
review evidence response.

```
GET /api/tasks/{task_key}/review-evidence

Response:
{
  "task_key": "AT-0100",
  "mission_contract": { ... },
  "artifacts": [
    {
      "name": "workflow_policy_summary.json",
      "kind": "workflow_policy",
      "size_bytes": 1862,
      "is_validator_log": false,
      "is_executor_log": false,
      "is_mission_contract": false
    },
    {
      "name": "artifact_index.json",
      "kind": "workflow_policy",
      "size_bytes": 412,
      "is_validator_log": false,
      "is_executor_log": false,
      "is_mission_contract": false
    }
  ],
  "validator_results": [...],
  "policy_status": "passed",
  "policy_warnings": [...],

  // NEW: optional top-level field
  "workflow_policy_evidence": {
    "available": true,
    "artifact_index": {
      "name": "artifact_index.json",
      "artifact_type": "artifact_index",
      "path": "artifact_index.json",
      "package_type": "workflow_policy_proof_of_work",
      "artifact_index_version": "0.1",
      "generated_at": "2025-01-01T00:00:00Z",
      "artifacts": [
        {
          "name": "workflow_policy_summary",
          "artifact_type": "workflow_policy_summary",
          "path": "workflow_policy_summary.json",
          "required": true,
          "description": "Machine-readable workflow policy summary artifact."
        }
      ]
    },
    "summary": {
      "name": "workflow_policy_summary.json",
      "artifact_type": "workflow_policy_summary",
      "path": "workflow_policy_summary.json",
      "schema_version": "0.1",
      "validation_status": "passed",
      "validation_errors": [],
      "validation_warnings": [],
      "source_path": "/absolute/path/to/workflow-policy.example.json",
      "generated_at": "2025-01-01T00:00:00Z",
      "allowed_executors": ["manual", "shell", "opencode", "pi"],
      "required_validators": ["policy", "changed-files", "pytest", "typecheck", "lint"],
      "optional_validators": ["openspec"],
      "path_policy": {
        "allowed_paths": [],
        "forbidden_paths": []
      },
      "workspace_policy": {
        "isolation_required": true,
        "preferred_strategy": "per_task_worktree",
        "preserve_on_failure": true,
        "cleanup_control": "human_or_deterministic_policy"
      },
      "proof_of_work": {
        "required_artifacts": ["run_summary", "mission_contract"],
        "optional_artifacts": ["artifact_index", "handoff_decision"]
      },
      "human_review": {
        "required": true,
        "allowed_decisions": ["approve", "reject", "rerun", "block"]
      },
      "forbidden_actions": ["self_approve", "approve_without_human", "push"],
      "deferred_integrations": ["github_issues_sync", "automatic_pr_creation"],
      "governance_invariants": [
        {
          "invariant": "ai_workers_may_approve",
          "value": false,
          "description": "AI workers may not approve their own tasks."
        }
      ]
    },
    "review_artifacts": [
      {
        "name": "artifact_index.json",
        "kind": "workflow_policy",
        "size_bytes": 412,
        "is_validator_log": false,
        "is_executor_log": false,
        "is_mission_contract": false
      },
      {
        "name": "workflow_policy_summary.json",
        "kind": "workflow_policy",
        "size_bytes": 1862,
        "is_validator_log": false,
        "is_executor_log": false,
        "is_mission_contract": false
      }
    ]
  }
}
```

When workflow policy artifacts are **not available:**

```json
{
  "workflow_policy_evidence": {
    "available": false
  }
}
```

---

## Backward Compatibility Requirements

Any future API implementation must maintain backward compatibility:

1. **Existing review evidence response** must remain compatible. The existing
   `artifacts` array, `mission_contract`, `validator_results`, `policy_status`,
   and `policy_warnings` fields must continue to work as documented.
2. **Missing workflow policy artifacts** should produce `available: false` or
   an absent `workflow_policy_evidence` field. Existing tasks without workflow
   policy artifacts must not cause API errors.
3. **Existing artifact summaries** must continue returning the existing shape
   (`name`, `kind`, `size_bytes`, `is_validator_log`, `is_executor_log`,
   `is_mission_contract`).
4. **Existing "other" artifacts** remain valid. The API must not reject or
   alter non-workflow-policy artifacts.
5. **API clients must not assume runtime enforcement** from display. The API
   is a reporting interface only.

---

## Safety Wording

The following principles are non-negotiable for any future API exposure:

1. **Display is not enforcement.** Showing workflow policy metadata in an API
   response does not imply that the dispatcher enforces that policy.
2. **API response is not approval.** A `workflow_policy_evidence` field in the
   response does not indicate approval readiness.
3. **API response is not merge readiness.** The presence of workflow policy
   artifacts does not imply that a task is ready to merge.
4. **API response is not push or cleanup permission.** Policy metadata in an API
   response does not authorize push, merge, or cleanup actions.
5. **AI workers receive context, not authority.** AI workers may receive policy
   context via API responses to inform their work. They do not enforce policy
   or make governance decisions.
6. **Deterministic code, validators, and human review remain the enforcement
   path.** Validators verify policy compliance. Human review is the final gate.

---

## Non-Goals

This design does not add:

- **Write APIs:** No endpoints that modify task state, artifacts, or configuration.
- **Approval APIs:** No approval, rejection, or rerun endpoints beyond existing ones.
- **Merge APIs:** No merge, rebase, or branch management endpoints.
- **Push APIs:** No push, force-push, or remote synchronization endpoints.
- **Cleanup/delete APIs:** No worktree cleanup, artifact deletion, or branch deletion.
- **GitHub sync:** No GitHub API calls, repository state polling, or sync logic.
- **PR creation:** No pull request automation or creation endpoints.
- **Dispatcher preflight checks:** Any future dispatcher preflight requires a
  separate design document and tests.
- **Executor behavior changes:** Executors are unaffected by workflow policy API exposure.
- **Validator registry changes:** Validator selection and execution remain unchanged.
- **Mission Control UI changes:** No frontend additions or modifications.
- **AI self-governance:** AI workers do not approve, block, or enforce policy via API.

---

## Preconditions Before Implementation

Before any API implementation phase, the following preconditions must be met:

1. `scripts/run_local_validation.py` passes all checks.
2. `scripts/report_workflow_policy_review_evidence.py` works correctly in both
   generate and read modes.
3. `scripts/run_workflow_policy_review_evidence_smoke.py` passes.
4. `tests.test_workflow_policy_read_only_exposure_contract` passes (read-only shape).
5. `tests.test_workflow_policy_artifact_package_contract` passes (package structure).
6. `tests.test_workflow_policy_artifact_constants_contract` passes (doc ↔ constants).
7. No runtime behavior changes are made in the same phase as API implementation.
8. A rollback path is documented before any API endpoint is deployed.
9. API response backward compatibility is tested before shipping.

---

## Recommended Implementation Sequence

### Phase 109 — Add Read-Only API Contract Tests

Add tests that verify the expected API response shape (the `workflow_policy_evidence`
field) against the existing review evidence response, without implementing any
API endpoint. This locks the expected response shape before any implementation.

This follows the established pattern from Phases 102–106: **lock the contract before
implementing the behavior.**

**Acceptance criteria:**
- Tests verify `workflow_policy_evidence.available` when artifacts exist and when
  they are absent.
- Tests verify all proposed nested fields are present in the expected shape.
- Tests verify backward compatibility with existing response fields.
- No API endpoint implementation.

### Phase 110 — Implement Minimal Read-Only API Exposure

Implement the `workflow_policy_evidence` field in the existing review evidence
response (Option A) only if Phase 109 tests clarify the required shape and confirm
backward compatibility is achievable without refactoring.

**Acceptance criteria:**
- Existing review evidence response passes all existing tests.
- New `workflow_policy_evidence` field is optional and backward-compatible.
- Missing artifacts produce `available: false`.
- No write behavior, no state transitions, no approval automation.

### Phase 111 — Mission Control Read-Only Display Design Docs

Add design documentation for how Mission Control may display workflow policy
metadata in review/evidence panels, with no UI implementation yet.

**Acceptance criteria:**
- Design docs specify read-only display only.
- No buttons, approve/merge/push/cleanup controls.
- UI reads from existing review evidence API.
- Covered by UI doc tests before shipping.

### Dispatcher Enforcement — Future Separate Phase

Dispatcher preflight checks based on workflow policy artifacts are a much later
phase and require:
- A separate preflight design document
- Explicit contract tests for preflight behavior
- Human review as the final gate
- No AI self-governance or self-approval

This is out of scope for the current workflow policy artifact exposure series.