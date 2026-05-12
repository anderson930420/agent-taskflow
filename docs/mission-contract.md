# Mission Contract Artifact

## What Is a Mission Contract?

A Mission Contract is a JSON artifact produced at dispatch time, before the
executor runs. It captures:

- The task intent (goal, title, task key)
- The execution environment (repo, worktree, artifact directory)
- The executor configuration (executor name, model, provider)
- The required validation gates (pytest, openspec, etc.)
- The explicit governance rules that the executor and any sub-agents must respect

The contract is written to `<artifact_dir>/mission_contract.json` and is
human-readable, machine-parseable, and auditable.

## What It Is NOT

The Mission Contract is not:

- **A validator** — it does not block on its own. It is consumed after the
  executor runs but does not replace pytest, openspec, or any future validator.
- **An orchestrator** — it does not drive loops, sub-agent coordination, or
  multi-round iteration. That is outside the scope of this artifact.
- **A governance layer replacement** — agent-taskflow remains the control plane.
  The contract merely documents the governance rules; it does not enforce them.
- **A replacement for deterministic validators** — deterministic validators
  (pytest, openspec, typecheck, lint, policy checks) remain required regardless
  of what the executor produces. AI reviewer/auditor cannot replace them.
- **A multipi agent system** — research/planner/implementer/reviewer/scout agents
  are out of scope for this artifact and this phase.
- **A Pi Mission Orchestrator** — the contract is not the orchestrator.
  The orchestrator (if introduced in a future phase) would *consume* the contract,
  not replace it.

## Relationship to Other Components

```
TaskRecord (SQLite)
       │
       ▼  dispatch
MissionContract (JSON artifact)
       │
       ▼  consumed by
Executor (Pi / OpenCode / Shell)
       │
       ▼  produces artifacts
Validator (pytest / openspec / ...)
       │
       ▼  pass/fail
Human Approval (via API / UI)
```

The contract sits between `TaskRecord` and the `Executor`. It is produced once
per dispatch run and written to disk before the executor is invoked.

## Contract Schema

```json
{
  "schema_version": "1",
  "task_key": "AT-001",
  "title": "Implement feature X",
  "goal": "Implement feature X as described in openspec/.",
  "repo_path": "/home/ubuntu/agent-taskflow",
  "worktree_path": "/home/ubuntu/agent-taskflow/.worktrees/AT-001",
  "artifact_dir": "/tmp/agent-taskflow-artifacts/AT-001",
  "implementation_prompt_path": "/tmp/agent-taskflow-artifacts/AT-001/implementation_prompt.md",
  "executor": "pi",
  "model": "MiniMax-M2.7",
  "provider": "minimax",
  "required_validators": ["pytest", "openspec"],
  "forbidden_actions": [
    "approve",
    "push",
    "merge",
    "cleanup",
    "delete_worktree",
    "delete_branch",
    "self_approve",
    "force_push"
  ],
  "expected_artifacts": [
    "executor_log",
    "validator_logs",
    "git_status",
    "git_diff"
  ],
  "human_approval_required": true,
  "governance_rules": [
    "agent-taskflow is the governance/control plane.",
    "Pi, OpenCode, and Shell are executor backends only.",
    "Worker cannot approve tasks.",
    "Worker cannot push to remote branches.",
    "Worker cannot merge PRs.",
    "Worker cannot cleanup worktrees.",
    "Worker cannot delete branches.",
    "Worker cannot self-approve.",
    "Worker cannot force-push.",
    "AI reviewer/auditor cannot replace deterministic validators.",
    "Deterministic validators remain required regardless of executor output.",
    "Human approval is the final gate.",
    "Artifacts/logs/validation results must be traceable and rerunnable."
  ]
}
```

## Key Fields

### schema_version
Always `"1"` in this phase. Future phases may increment this when the schema
changes.

### goal
Free-text description of what the executor should do. Typically derived from
the task title or description. Must not be empty.

### required_validators
List of validator names that must pass. Default is `["pytest", "openspec"]`.
Additional validators (typecheck, lint, policy checks) will be added in later
phases.

### forbidden_actions
Hardcoded list of actions the executor and any sub-agent must never take.
This list is never user-configurable; it is part of the governance contract.

### governance_rules
Human-readable list of governance constraints. These are derived from the
`forbidden_actions` and additional governance principles. Reviewers can read
this list to understand what rules apply.

### human_approval_required
Always `true`. A human must approve before the task can leave `waiting_approval`.
Worker cannot bypass this gate.

## Secret Handling

The Mission Contract must never contain secret-like values. The following are
explicitly rejected at build time:

- Keys with names matching: `key`, `token`, `secret`, `password`, `credential`,
  `api_key`, `access_token`, `refresh_token`, `authorization`
- Values containing strings that look like API keys (e.g. `sk-`, `api_key=...`)

Secrets may be passed at runtime via the executor's `env` parameter, but that
bypasses the contract write path and is the caller's responsibility.

## Module Reference

```python
from agent_taskflow.mission_contract import (
    MissionContract,           # frozen dataclass
    build_mission_contract,    # build from raw fields
    build_from_task_fields,    # convenience wrapper (no extra)
    mission_contract_to_dict,  # serialize to JSON-safe dict
    write_mission_contract,   # write to <artifact_dir>/mission_contract.json
    read_mission_contract,    # read and validate from file
)
```

## Dispatcher Integration (Minimal)

The dispatcher calls `write_mission_contract` after constructing the
`ExecutorContext` but before invoking `executor.run()`. This ensures the
contract is on disk before the executor starts and can be audited if the run
fails.

The contract write is idempotent (a fresh contract is written each dispatch run).

## Future Phases

| Phase | Topic |
|-------|-------|
| Phase 18 (this) | Mission Contract artifact |
| Future | TypecheckValidator |
| Future | LintValidator |
| Future | PolicyCheckValidator |
| Future | Pi executor consumes mission_contract.json |
| Future | Pi Mission Orchestrator (out of scope for this phase) |
| Future | multipi-style agents (out of scope for this phase) |

The Mission Contract is designed to be extended without breaking existing
consumers. A future `schema_version: "2"` would add new fields while retaining
backward compatibility for readers.