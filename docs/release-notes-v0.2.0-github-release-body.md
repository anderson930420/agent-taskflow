# Agent Taskflow v0.2.0

v0.2.0 advances agent-taskflow from the initial governance pipeline into scheduled one-task automation, observability, ExecutionEngine migration scaffolding, and packaged CLI stabilization.

## Highlights

- GitHub issue one-task scheduler tick
- Shared lock behavior for manual and scheduled runs
- Execution/publication separation for scheduled execution
- Ingestion failure registry and duplicate-trigger suppression
- Blocked backlog visibility
- Structured scheduler tick observability summaries
- ExecutionEngine request builder, shadow compare, opt-in path, and fallback assessment
- Minimal Python packaging metadata
- Packaged console entry points under `agent_taskflow.cli`
- Compatibility shims for existing `scripts/run_*.py` paths
- Local validation guard for non-repo execution

## Safety

The release preserves the core project boundary: agent-taskflow manages work, not agents. Human review remains the final authority, scheduled execution remains bounded to one task per confirmed tick, and ExecutionEngine work remains opt-in migration scaffolding rather than default authority.

## Validation

- #98 CI: success
- #99 CI: success
- #100 CI: success
- #101 CI: success
- Local full suite: 3723 tests passed
- compileall: clean
