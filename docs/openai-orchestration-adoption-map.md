# OpenAI Orchestration Adoption Map

The external orchestration reference is a design reference, not an immediate
replacement for the current Python core.

agent-taskflow should adopt selected orchestration concepts where they improve
traceability, reviewability, and reproducibility. It should not copy runtime
assumptions that would bypass the current governance model or expand scope
before the bridge is stable.

## Adopt Conceptually

- **Issue/task tracker as control plane** - future production work should enter
  through a durable tracker such as GitHub issues, while preserving local task
  records as the current bridge.
- **Long-running orchestration mindset** - tasks should be modeled as durable
  workflows with state, evidence, retries, and review, not as one-off CLI calls.
- **Per-task isolated workspace** - each task should have an explicit workspace
  boundary. The current worktree path rules are a bridge toward this.
- **Repo-owned workflow policy** - workflow policy should eventually live with
  the repository, likely in a `WORKFLOW.md` or similar contract file.
- **Proof-of-work before review** - executor output should be validated and
  indexed before a human is asked to approve.
- **Replaceable executor adapters** - Pi, OpenCode, shell, manual, and future
  coding agents should remain adapter implementations behind a stable boundary.
- **Observability dashboard** - Mission Control should show task state,
  artifacts, validation, and review evidence.
- **Human review gate** - human approval remains the final gate before any
  external merge or deployment action.

## Do Not Directly Adopt Yet

- upstream repository structure
- Elixir/reference runtime assumptions, if present in the reference design
- Linear-only tracker assumptions
- Codex-only executor assumptions
- automatic PR creation
- automatic merge
- automatic push
- automatic cleanup/delete
- remote worker pool
- multi-host orchestration
- a new workflow engine

## Adoption Rule

Adopt concepts only when they strengthen the Python core's task contract,
workspace boundary, proof-of-work evidence, and review gate. Do not adopt any
concept that makes an executor the architecture owner or lets automation bypass
deterministic validation and human approval.

The near-term path is reconciliation first:

1. Document the target boundaries.
2. Keep the current local validation suite green.
3. Define entry criteria for the next stage.
4. Add integrations only after the boundary is explicit and stable.
