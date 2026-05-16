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

Validators are proof-of-work checks. They are not optional commentary and they
are not replaced by AI review. The changed-files validator is a scope guard that
helps connect executor output back to allowed paths in the contract.

FastAPI and Mission Control expose state, artifacts, and review evidence.
Mission Control is not the core engine; it is the observability and review
surface.

## Non-Goals

The current architecture explicitly does not include:

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
