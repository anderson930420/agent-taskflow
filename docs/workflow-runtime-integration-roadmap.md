# Workflow Runtime Integration Roadmap

This roadmap defines a safe path for future runtime integration of
`WORKFLOW.md` and the machine-readable workflow policy. It is planning
documentation only. It does not make the dispatcher, executors, validator
registry, API, Mission Control, or any runtime path read or enforce workflow
policy.

## Current Status

agent-taskflow currently has these workflow contract foundations:

- `WORKFLOW.md`: the human-readable repository workflow contract.
- `agent_taskflow/workflow_contract.py`: the isolated parser/model for
  `WORKFLOW.md`.
- `scripts/validate_workflow_contract.py`: the standalone validation command
  for the human-readable contract.
- `examples/workflow-policy.example.json`: the draft machine-readable workflow
  policy.
- `agent_taskflow/workflow_schema.py`: the isolated loader/model for the
  machine-readable policy.
- `scripts/validate_workflow_policy.py`: the standalone validation command for
  the machine-readable policy.
- `scripts/run_local_validation.py`: the standard local validation runner,
  which requires both workflow checks before the longer smoke and test checks.

These are validation-time checks. They are not dispatcher/runtime enforcement.
The dispatcher does not yet read workflow policy, executor adapters do not
enforce policy, and the validator registry is unchanged.

## Runtime Integration Principles

Runtime integration must be gradual, deterministic, and reversible.

- Policy is read by code, not interpreted or enforced by AI.
- AI coding workers may receive policy context in prompts, but they cannot
  enforce policy.
- Dispatcher state transitions remain deterministic Python-controlled workflow
  transitions.
- Executor adapters remain deterministic wrappers around bounded implementation
  workers.
- Validators enforce proof-of-work; AI worker claims are not sufficient
  evidence.
- Human review remains the final approval, reject, rerun, or block gate.
- Runtime integration must be opt-in or staged so behavior changes are
  reviewable.
- Each integration step must include focused tests and a documented rollback
  path.

## Safe Integration Stages

### Stage 1: Read-Only Exposure

Load the workflow policy in a read-only script or API endpoint and expose parsed
policy metadata for inspection. This stage must not change dispatch behavior,
task selection, executor invocation, validation decisions, or review decisions.

### Stage 2: Validation-Only Enforcement

Require valid `WORKFLOW.md` and machine-readable policy in local validation and
CI-like checks. Runtime task execution remains unchanged. This is the current
direction of the local validation runner and should remain separate from
dispatcher enforcement.

### Stage 3: Mission Contract Generation Alignment

Use workflow policy defaults when generating mission contracts, while still
allowing explicit task metadata to override defaults where the contract permits.
Tests must prove generated contracts include expected policy fields such as
`allowed_executors`, `required_validators`, `path_policy`, and
`forbidden_actions`.

### Stage 4: Dispatcher Preflight Checks

Add deterministic dispatcher preflight checks before an executor starts. The
dispatcher should verify that the requested executor is allowed, required
validators are declared, and path policy is present. Failures should move the
run to a blocked state before worker invocation.

### Stage 5: Executor Prompt Context Alignment

Include workflow policy context in executor prompts so bounded AI workers can
see the operating constraints. This is context only: validators, git/workspace
checks, dispatcher preflights, and human review still enforce policy.

### Stage 6: Workspace Policy Enforcement

When a workspace manager is introduced, use workflow policy to drive isolation,
preservation, and cleanup rules. Cleanup remains human-controlled or
deterministic policy-controlled. Workers cannot clean up their own workspaces.

### Stage 7: Review Evidence Integration

Record which workflow policy version governed a run and include it in review
evidence. Proof-of-work should link the mission contract, policy, validation
report, changed-files audit, artifact index, and human review decision.

## Explicit Non-Goals

This roadmap does not authorize:

- automatic GitHub issue sync
- GitHub Projects integration
- automatic PR creation
- automatic merge
- automatic push
- automatic cleanup or delete behavior
- AI-controlled scheduling
- AI self-approval
- prompt-only governance
- replacing the current Python core with an upstream repo

## First Safe Runtime-Adjacent Step

Phase 93 should add a read-only workflow policy summary command or API-free
report first.

Other possible next steps are:

- Add policy metadata to review evidence in a read-only way.
- Add mission contract generation tests that compare current contract fields
  with workflow policy defaults, without changing dispatcher behavior.

The preferred option is the read-only summary command because it exercises
policy loading, validation result presentation, and operator-facing output
without affecting task execution, dispatcher state transitions, executor
behavior, or review decisions.

## Acceptance Criteria Before Dispatcher Enforcement

Dispatcher preflight enforcement should not begin until:

- The local validation runner passes.
- Workflow policy schema validation passes.
- Mission contract generation alignment tests pass.
- The changed-files validator remains opt-in and tested.
- The fake-Pi golden path smoke still passes.
- One manual real-Pi smoke has a recent recorded success.
- A rollback path is documented for dispatcher preflight enforcement.
- No frontend expansion is present.
- No GitHub integration is present.
- No PR creation behavior is present.
- No merge behavior is present.
- No push behavior is present.
- No cleanup/delete expansion is present.
