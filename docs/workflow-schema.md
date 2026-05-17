# Workflow Policy Schema Draft

This document defines a draft machine-readable workflow policy for
agent-taskflow. It is not enforced by dispatcher, executor, validator registry,
API, Mission Control, or any runtime path yet.

`WORKFLOW.md` remains the human-readable repository workflow contract. A
machine-readable policy may later be paired with `WORKFLOW.md`, embedded into
it, or referenced from it. The purpose of this draft is to align future parser,
validator, dispatcher, executor prompt, and review evidence work before any
runtime enforcement is added.

The current example policy lives at:

```text
examples/workflow-policy.example.json
```

## Top-Level Fields

### schema_version

Identifies the draft schema version. The current draft value is:

```json
"0.1"
```

### orchestration_boundary

Declares the deterministic orchestration boundary:

```json
{
  "deterministic_orchestration": true,
  "ai_workers_bounded": true,
  "ai_workers_may_schedule_tasks": false,
  "ai_workers_may_approve": false,
  "ai_workers_may_merge": false,
  "ai_workers_may_push": false,
  "ai_workers_may_cleanup": false
}
```

AI coding tools are bounded implementation workers invoked through executor
adapters. They do not own scheduling, approval, merge, push, cleanup, or
workflow policy enforcement.

### allowed_executors

Lists executor adapters allowed by policy. The current example includes:

```json
["manual", "shell", "opencode", "pi"]
```

Future adapters such as Codex or Claude Code can be added after their adapter
contracts are defined.

### required_validators

Lists validators expected as proof-of-work gates. The draft example includes:

```json
["policy", "changed-files", "pytest", "typecheck", "lint"]
```

`openspec` may be treated as optional when it is not available on `PATH`,
matching the local validation runner behavior.

### path_policy

Defines changed-files policy inputs:

```json
{
  "allowed_paths": [],
  "forbidden_paths": []
}
```

Semantics:

- `forbidden_paths` wins.
- `allowed_paths` constrains changed files when non-empty.
- Untracked, modified, deleted, renamed, and copied files must be auditable.
- Artifact directories outside the repo are separate from repo diff audit.

### workspace_policy

Defines the intended workspace policy:

```json
{
  "isolation_required": true,
  "preferred_strategy": "per_task_worktree",
  "preserve_on_failure": true,
  "cleanup_control": "human_or_deterministic_policy"
}
```

The workspace manager is planned but not implemented yet. This schema draft
does not add workspace manager behavior.

### proof_of_work

Defines expected proof-of-work artifacts:

```json
{
  "required_artifacts": [
    "run_summary",
    "mission_contract",
    "executor_log",
    "validation_report",
    "changed_files_audit"
  ],
  "optional_artifacts": [
    "implementation_prompt",
    "artifact_index",
    "handoff_decision"
  ]
}
```

These names are policy concepts, not a new runtime artifact writer.

### human_review

Defines the human review gate:

```json
{
  "required": true,
  "allowed_decisions": ["approve", "reject", "rerun", "block"]
}
```

Approval does not imply automatic merge, automatic push, or automatic cleanup.
Those actions remain outside worker authority and require separate
deterministic or human-controlled policy.

### forbidden_actions

Lists actions that workers and prompt-only governance cannot perform:

```json
[
  "self_approve",
  "approve_without_human",
  "push",
  "force_push",
  "merge",
  "auto_merge",
  "cleanup",
  "delete_worktree",
  "delete_branch"
]
```

### deferred_integrations

Lists known integrations and runtime behaviors deferred from this draft:

```json
[
  "github_issues_sync",
  "github_projects",
  "automatic_pr_creation",
  "automatic_merge",
  "remote_worker_pool",
  "multi_host_scheduling"
]
```

## Enforcement Status

Runtime enforcement is deferred. This draft does not make the dispatcher read
the schema, does not change executor behavior, does not change validator
registry semantics, and does not introduce a workflow engine.

