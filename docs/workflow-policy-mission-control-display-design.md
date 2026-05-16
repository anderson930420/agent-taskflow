# Workflow Policy Mission Control Display Design

## Purpose

This document defines how Mission Control may later display `workflow_policy_evidence`
as read-only evidence in the task review/evidence panel. It is a display design
document for future implementation phases.

**This phase does not implement:**
- Mission Control UI or frontend code
- API changes or new endpoints
- Dispatcher integration or runtime enforcement
- Workflow policy control actions
- State transition mechanisms

The document describes a design space and safety constraints for any future
display implementation. Display is optional and non-authoritative.

---

## Current Backend Foundation

The `workflow_policy_evidence` field is already exposed read-only through the
existing review evidence API endpoint:

```
GET /api/tasks/{task_key}/review-evidence
```

The response includes:

```json
{
  "task_key": "AT-0100",
  "mission_contract": { ... },
  "artifacts": [ ... ],
  "validator_results": [ ... ],
  "policy_status": "passed",
  "policy_warnings": [ ... ],
  "workflow_policy_evidence": {
    "available": true | false,
    "artifact_index": { ... } | null,
    "summary": { ... } | null,
    "review_artifacts": [ ... ]
  }
}
```

**Key properties of the current backend:**

- `workflow_policy_evidence` is exposed read-only. The API never generates,
  validates, or mutates workflow policy artifacts.
- `workflow_policy_evidence.available` is `true` only when both canonical files
  exist, parse as valid JSON, and meet canonical contract requirements (correct
  `package_type`, `artifact_index_version`, `artifact_type`, `validation_status`,
  and all required fields present).
- Missing or corrupt artifacts produce `available: false`. This does not mean
  workflow policy failed — it only means artifacts are absent from review evidence.
- The API does not call the dispatcher, executor, validator registry, GitHub,
  or any frontend code. It is a pure read path.
- `available: false` does not trigger approval, blocking, or enforcement. It
  is an informational state.

**Reference implementation:** `agent_taskflow/api/review.py` —
`build_workflow_policy_evidence()`.

---

## UI Design Principles

Mission Control display of `workflow_policy_evidence` must be:

1. **Read-only.** No buttons, controls, or action triggers inside the panel.
2. **Evidence-oriented.** Displays policy metadata as proof-of-work context.
3. **Non-authoritative.** Display does not imply dispatcher enforcement.
4. **Non-mutating.** The panel must not cause any state transitions.
5. **Non-approval-surface.** The panel must not suggest approval readiness.
6. **Non-state-transition-surface.** The panel must not trigger or reflect task
   lifecycle state changes.
7. **Clearly labeled.** The panel identifies itself as "workflow policy evidence",
   not "workflow policy enforcement" or "policy status gate".
8. **Safe when unavailable.** When `available: false`, the panel explains that
   artifacts are absent without implying failure or blocking.

---

## Proposed UI Placement

### Option A — Task Detail Review Evidence Section (Recommended First)

Add a **"Workflow Policy Evidence"** collapsible panel below the existing
review/evidence area on the task detail page.

```
Task Detail
  ├── Summary
  ├── Mission Contract
  ├── Validation Results
  ├── Artifacts
  └── Review Evidence
        ├── Mission Contract Summary
        ├── Artifact Files
        ├── Validator Results
        ├── Policy Status
        └── [NEW] Workflow Policy Evidence  ← recommended
```

**Rationale:** Workflow policy evidence is part of the review/evidence bundle.
Placing it in this section keeps it evidence-oriented rather than control-oriented.
It is adjacent to related proof-of-work metadata (artifacts, validation results).

### Option B — Dedicated Collapsible Panel Near Artifacts

Add a standalone **"Workflow Policy Evidence"** panel in the artifacts area,
separately from the general artifact list but visible on the task detail view.

**Rationale:** Useful if the workflow policy evidence panel grows to include
more detailed metadata over time, or if a clear visual separation from general
artifacts is preferred.

### Recommendation

**Proceed with Option A first.** It is the lowest-friction placement that
keeps workflow policy evidence alongside other review/evidence content. Option B
can be considered if Option A proves insufficient as the panel matures.

---

## Proposed Panel States

### State A — `available: true`

The workflow policy proof-of-work package is attached. Display the following:

**Metadata display:**

| Field | Display label | Notes |
|-------|---------------|-------|
| `validation_status` | Policy validation | passed / failed |
| `schema_version` | Schema version | e.g., "0.1" |
| `source_path` | Source policy | file path or "not provided" |
| `generated_at` | Generated at | ISO-8601 timestamp |
| `allowed_executors` | Allowed executors | list, collapsed by default |
| `required_validators` | Required validators | list, collapsed by default |
| `optional_validators` | Optional validators | list, collapsed by default |
| `path_policy` | Path policy | expandable summary |
| `workspace_policy` | Workspace policy | expandable summary |
| `proof_of_work.required_artifacts` | Required artifacts | list, collapsed by default |
| `human_review.required` | Human review required | yes / no |
| `human_review.allowed_decisions` | Allowed decisions | list |
| `forbidden_actions` | Forbidden actions | list, collapsed by default |
| `deferred_integrations` | Deferred integrations | list, collapsed by default |
| `governance_invariants` | Governance invariants | expandable |

**Linked review artifacts:**

- `workflow_policy_summary.json` (kind: `workflow_policy`)
- `artifact_index.json` (kind: `workflow_policy`)

Display these as artifact links with kind badge, not as action buttons.

**Read-only notice:**
> "This evidence is read-only. Display does not imply dispatcher enforcement.
> Human review remains the final gate."

### State B — `available: false`

No workflow policy evidence artifacts are attached to this task or run.

**Display:**
> "No workflow policy evidence artifacts are attached to this task. This
> does not mean workflow policy failed — it only means the proof-of-work
> artifacts are absent from review evidence."

**Do not:**
- Show a red "failed" indicator
- Show approve/merge/push/cleanup controls
- Imply that the task is blocked or rejected

### State C — Partial or Corrupt Artifacts

If the backend exposes `available: false` due to corrupt or partial artifacts:

- Show the unavailable state (same as State B)
- Do not infer enforcement failure from absent artifacts
- Do not show approve/merge/push/cleanup actions

The key principle: `available: false` is an informational state, not a
failure or blocking state.

---

## Proposed Read-Only UI Copy

The following wording should appear in the panel:

**Panel header:**
> Workflow Policy Evidence
> (read-only)

**When available:**
> "Workflow policy evidence is read-only. Display does not imply dispatcher
> enforcement. Policy metadata helps reviewers understand the intended execution
> contract. Human review remains the final gate. Approval does not imply merge,
> push, or cleanup."

**When unavailable:**
> "No workflow policy evidence artifacts are attached to this task or run.
> This does not mean workflow policy failed — it only means artifacts are
> absent from review evidence."

**Artifact links (when available):**
> `workflow_policy_summary.json` · `artifact_index.json`

Do not add action verbs (approve, merge, push, cleanup) anywhere in the panel.

---

## Explicit Forbidden UI Behavior

Mission Control must **not** add the following inside or alongside the
workflow policy evidence panel:

- **approve** / **reject** / **rerun** / **block** controls
- **merge** button
- **push** button
- **cleanup** / **delete** button
- **regenerate policy artifact** button
- **validate policy** button that mutates state
- **dispatcher preflight** trigger
- **executor rerun** trigger
- **GitHub PR creation** button or link
- **GitHub issue sync** trigger
- **auto-approve** trigger
- **auto-merge** trigger
- Any action that changes task state

These controls belong in the appropriate task action area. They must not
appear inside the workflow policy evidence panel or be triggered by its presence.

---

## Data Dependency

Future Mission Control UI must consume `workflow_policy_evidence` only from
the existing read-only API response:

```
GET /api/tasks/{task_key}/review-evidence
  → response.workflow_policy_evidence
```

It must **not**:
- Read workflow policy artifacts directly from the filesystem
- Call dispatcher methods to fetch policy state
- Call executor methods to check policy enforcement
- Call validator methods to validate policy at display time
- Make GitHub API calls to fetch policy metadata

The API is the single source of truth for displayed `workflow_policy_evidence`.

---

## Accessibility / UX Notes

- Use clear, non-technical labels where possible (e.g., "Policy validation"
  instead of exposing internal field names).
- Use collapsed/expandable sections for long arrays (executors, validators,
  forbidden actions, deferred integrations).
- Show empty lists as "None declared" or "Not specified" rather than blank.
- Avoid green "passed" styling that could be mistaken for task approval
  status. Prefer neutral styling: "Policy validation: passed" without the
  visual weight of a success state.
- Separate workflow policy evidence status from task approval status visually.
  The two must never be conflated.
- Do not use the word "enforcement" in the panel label or primary description.
- If a link to `workflow_policy_summary.json` is shown, display it as a read-only
  reference, not a download-or-action button.

---

## Non-Goals

This design does not add:

- **Frontend implementation:** No Mission Control UI or component code.
- **API changes:** No new endpoints or modified responses.
- **Dispatcher enforcement:** No runtime policy checks or preflight enforcement.
- **Executor behavior changes:** Executors are unaffected.
- **Validator registry changes:** Validator selection remains unchanged.
- **GitHub sync:** No GitHub API calls or repository state changes.
- **PR creation:** No pull request automation.
- **Automatic merge / push / cleanup:** No automation of these actions.
- **AI self-governance:** AI workers do not enforce or approve policy via UI.
- **Prompt-only governance:** Policy context delivered via prompts is out of scope.
- **State transition surface:** The panel does not trigger or reflect task state changes.

---

## Preconditions Before Frontend Implementation

Before implementing Mission Control display of `workflow_policy_evidence`,
the following preconditions must be met:

1. `scripts/run_local_validation.py` passes all checks.
2. `tests.test_workflow_policy_read_only_api_contract` passes — Phase 110 API
   exposure tests confirm the response shape is stable.
3. `workflow_policy_evidence` response is stable — the `available`, `artifact_index`,
   `summary`, and `review_artifacts` fields behave as documented.
4. Read-only exposure contract tests pass with no write behavior in the same phase.
5. Source-level tests verify the design document mentions no forbidden UI behaviors.
6. No new action buttons (approve/merge/push/cleanup) are introduced in the same
   phase as display.
7. UI source-level tests verify no forbidden actions are mentioned as part of the
   workflow policy evidence display.

---

## Recommended Phase 112

**Phase 112: Add Mission Control Display Contract/Source Tests for the Planned
Read-Only Workflow Policy Evidence Panel.**

Before implementing the panel, add source-level tests that verify:
- Mission Control display design doc exists and is reviewed
- The doc specifies read-only display only
- No forbidden UI behaviors (approve/merge/push/cleanup) are mentioned
- No frontend implementation is described as part of this phase
- Display does not imply enforcement
- Human review remains the final gate
- `available: true` and `available: false` states are documented
- Phase 112 itself remains docs-only or docs/tests-only

This follows the established pattern: lock the design contract before
implementing the behavior.

**Alternative:** Implement a minimal read-only panel only after the
Phase 112 source-level design contract tests are defined and passing.

**The preferred path is design-contract tests first** to ensure the
display implementation has a stable, reviewed target to implement against.

---

## Reference

- Backend implementation: `agent_taskflow/api/review.py` — `build_workflow_policy_evidence()`
- API endpoint: `GET /api/tasks/{task_key}/review-evidence`
- API contract tests: `tests.test_workflow_policy_read_only_api_contract.py`
- Related docs:
  - `docs/workflow-policy-read-only-exposure-plan.md` — staged exposure roadmap
  - `docs/workflow-policy-read-only-api-exposure-design.md` — API design
  - `docs/workflow-policy-artifact-metadata-contract.md` — canonical constants