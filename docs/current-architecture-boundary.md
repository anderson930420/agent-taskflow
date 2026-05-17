# Current Architecture Boundary

This document maps the existing agent-taskflow codebase to the repositioned
Python-native, GitHub-oriented, multi-executor orchestration architecture.

No new runtime behavior is introduced by this document. It defines the boundary
between the current bridge-hardened system and future architecture work.

## Component Map

| Current component | Architecture role |
| --- | --- |
| SQLite store | Orchestrator state store |
| Dispatcher | Task scheduler / run lifecycle manager |
| Executor abstraction | Replaceable coding-agent adapter layer |
| `PiExecutor` / `OpenCodeExecutor` | Executor plugins |
| Validator abstraction | Proof-of-work validation layer |
| Changed-files validator | Scope guard for executor outputs |
| Local validation runner | Phase validation entrypoint |
| FastAPI API | State and proof-of-work query API |
| Next.js Mission Control | Observability / review dashboard |
| Artifact metadata | Proof-of-work index |
| Approval metadata | Human review gate |
| Golden path smokes | End-to-end workflow acceptance tests |

## Boundary Statements

The SQLite store remains the state source for local orchestration evidence. It
is not yet a production GitHub sync database.

The dispatcher owns the run lifecycle in the current Python core. It writes the
mission contract, invokes exactly one executor for a run, records executor
evidence, runs validators, and updates task state.

Executor classes are adapter implementations. They translate a task contract
into a backend-specific execution call. They must not become the governance
layer, self-approval layer, merge layer, or project-management layer.

Executor adapters are deterministic wrappers, not the AI workers themselves.
For example, `PiExecutor` and `OpenCodeExecutor` are responsible for command
construction, workspace selection, environment setup, log capture, artifact
routing, and standardized run results. The external AI coding tool invoked by
the adapter is the bounded worker.

Validators are proof-of-work checks. They are not optional commentary and they
are not replaced by AI review. The changed-files validator is a scope guard that
helps connect executor output back to allowed paths in the contract.

FastAPI and Mission Control expose state, artifacts, and review evidence.
Mission Control is not the core engine; it is the observability and review
surface.

## AI Worker Boundary

The AI worker may implement changes inside the assigned workspace and within
the allowed task scope, but it must not control orchestration policy, select
tasks, mutate lifecycle state directly, approve its own work, bypass validators,
push, merge, or clean up workspaces.

`WORKFLOW.md` and other workflow contract material may be read by deterministic
code and may be included in AI prompts for context. AI is not trusted to enforce
`WORKFLOW.md`. Enforcement belongs to deterministic code, validators,
changed-files checks, workspace checks, git checks, and human review.

## Component Ownership

| Component | Owner role |
| --- | --- |
| Dispatcher / orchestrator | Deterministic scheduler and lifecycle manager |
| Workspace manager | Planned deterministic workspace preparation and cleanup policy executor |
| Executor adapter | Deterministic CLI wrapper and result normalizer |
| AI coding agent | Bounded implementation worker |
| Validator | Deterministic proof-of-work checker |
| Human reviewer | Final approval / reject / rerun / block decision maker |

## Non-Goals

The current architecture explicitly does not include:

- AI self-orchestration loops
- an agent that chooses its own tasks
- an agent that validates its own work
- an agent that approves or merges its own changes
- prompt-only governance
- automatic merge
- automatic push
- automatic cleanup or delete
- self-approval
- production GitHub issue sync
- GitHub Projects support
- automatic PR creation
- remote worker pools
- multi-host scheduling
- a new workspace manager
- a new workflow engine

## Current Acceptance Surface

The current acceptance surface is intentionally local and deterministic:

- Mission Control golden path smoke validates the API, dispatcher, store,
  artifact, validator, and review-evidence path.
- PiExecutor golden path smoke validates the Pi adapter through fake-Pi mode.
- Changed-files validation is opt-in and contract-driven.
- The local validation runner is the standard command for phase validation.
- Compileall and unittest discovery verify the Python codebase remains importable
  and behaviorally stable.
