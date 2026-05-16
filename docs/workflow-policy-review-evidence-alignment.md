# Workflow Policy Review Evidence Alignment

This document describes how workflow contract and workflow policy metadata
should eventually align with review evidence and proof-of-work artifacts. It is
planning documentation only. It does not add API endpoints, frontend behavior,
dispatcher enforcement, executor behavior, validator registry changes, GitHub
integration, workspace management, or a new workflow engine.

## Current State

agent-taskflow currently has these workflow policy foundations:

- `WORKFLOW.md`: the human-readable repository workflow contract.
- `scripts/validate_workflow_contract.py`: the standalone command that validates
  the human-readable contract.
- `examples/workflow-policy.example.json`: the draft machine-readable workflow
  policy.
- `scripts/validate_workflow_policy.py`: the standalone command that validates
  the machine-readable policy.
- `scripts/summarize_workflow_policy.py`: the read-only, API-free command that
  summarizes the machine-readable policy.
- `scripts/run_local_validation.py`: the standard local validation runner,
  which requires both workflow contract and workflow policy validation.

The review evidence API already exists for task, run, artifact, and validator
evidence. Workflow policy metadata is not yet embedded into runtime review
evidence.

## Target Review Evidence Alignment

Future review evidence for each task/run should show:

- workflow contract source path
- workflow contract validation status
- workflow policy source path
- workflow policy `schema_version`
- workflow policy validation status
- allowed executors
- required validators
- optional validators
- path policy summary
- workspace policy summary
- proof-of-work required artifacts
- human review policy
- forbidden actions
- deferred integrations
- policy hash or content digest, future
- policy snapshot or artifact reference, future

This metadata should be presented as evidence context. It must not imply runtime
enforcement until deterministic enforcement has actually been implemented.

## Proof-of-Work Artifact Alignment

Future phases may generate or link workflow policy artifacts such as:

- `workflow_contract_summary.json`
- `workflow_policy_summary.json`
- `workflow_policy_validation.json`
- `workflow_policy_snapshot.json`
- `workflow_policy_digest.txt`

These artifacts are target alignment points, not current deliverables. This
phase does not add artifact writers or attach workflow policy metadata to
runtime evidence.

## Safety Principles

- Review evidence exposure must be read-only.
- Policy metadata display must not claim or imply runtime enforcement unless
  enforcement exists.
- AI workers may receive policy context, but they do not enforce policy.
- Validators and deterministic code enforce policy.
- Human review remains the final approval, reject, rerun, or block gate.
- Approval does not imply merge, push, cleanup, or delete behavior.

## Suggested Staged Implementation

### Stage 1: API-Free Report

The read-only report already exists as `scripts/summarize_workflow_policy.py`.
It loads, validates, and summarizes the policy without touching dispatcher,
runtime, API, or frontend paths.

### Stage 2: Manual Summary Artifact

Add a manual/report command that writes a workflow policy summary artifact. The
command should be explicit, deterministic, and outside dispatcher runtime.

### Stage 3: Controlled Proof-of-Work Attachment

Attach a policy summary artifact to a proof-of-work package in a smoke or other
controlled run. This should prove artifact shape and evidence linking without
changing runtime enforcement.

### Stage 4: Read-Only Review Evidence API Exposure

Expose policy summary metadata through the review evidence API as read-only
evidence. This stage must not change dispatcher decisions, executor behavior, or
validator registry semantics.

### Stage 5: Read-Only Mission Control Display

Display workflow policy metadata in Mission Control as review context only. The
UI must make clear whether policy is validation-only or runtime-enforced.

### Stage 6: Later Dispatcher Preflight Consideration

Only after the evidence path is stable should the project consider dispatcher
preflight enforcement for allowed executors, required validators, and path
policy presence.

## Explicit Non-Goals

This alignment does not add:

- runtime policy enforcement
- dispatcher preflight checks
- API endpoints
- Mission Control UI changes
- GitHub sync
- automatic PR creation
- automatic merge, push, cleanup, or delete behavior
- AI self-governance

## Recommended Next Phase

Phase 95 should add a workflow policy summary artifact generator command. The
command should create a JSON artifact from the policy summary without touching
dispatcher runtime, API code, frontend code, executor behavior, or validator
registry semantics.
