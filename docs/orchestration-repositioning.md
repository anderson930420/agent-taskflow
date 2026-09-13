# Orchestration Repositioning

agent-taskflow is being repositioned as a Python-native, GitHub-oriented,
multi-executor agent orchestration system inspired by Symphony-style workflow
models.

This was an architecture direction, not a runtime expansion in that pre-V1
phase. The deferred items in this document retain their historical phase
meaning; they are not a statement that every V1 foundation is absent today.
For that phase, GitHub integration, OpenAI orchestration integration, external
repository integration, new UI work, automatic PR creation, automatic merge,
remote worker pools, and new workflow engines were deferred.

## Current V1 Pointer

Current V1 has implemented deterministic local workspace preparation in
[`agent_taskflow.workspace_manager`](../agent_taskflow/workspace_manager.py)
and the bounded integration mechanics described in the
[V1 Step 2 controller map](v1-step2-integration-controller.md): an
allowlisted normal task-branch push, draft-PR create/update, and cleanup only
after either a verified human GitHub merge or explicit confirmed cancelled
cleanup. [WORKFLOW.md](../WORKFLOW.md) remains the current repository
boundary. This is not a deployment or autonomy claim: the tracked
[V1 F8 handoff](v1/handoff-f8.md) records that no automated integration caller
or queue consumer exists, while the remaining F9/F10 production caller work
remains absent.

## New Positioning

agent-taskflow coordinates software tasks through a governed work lifecycle:

1. **Task / issue source** - the origin of work. Today this is represented by
   local task records and API-created tasks. A production GitHub issue source
   is planned but not implemented yet.
2. **Workflow contract** - the task-level contract that records goal,
   executor, validators, allowed scope, forbidden actions, expected artifacts,
   and review requirements.
3. **Orchestrator / dispatcher** - the component that owns run lifecycle,
   task state transitions, executor invocation, and validator sequencing.
4. **Isolated workspace model** - the per-task workspace boundary. V1's
   workspace manager prepares or reuses local task worktrees; this historical
   document does not authorize a broader deployment or cleanup policy.
5. **Executor adapters** - replaceable coding-agent backends such as Pi,
   OpenCode, shell, or manual executors. Executors execute within the contract;
   they do not own the architecture.
6. **Deterministic validators** - proof-of-work checks that run after executor
   output and before human review.
7. **Proof-of-work artifacts** - mission contracts, executor logs, generated
   protocol artifacts, validator logs, file-scope evidence, and review evidence.
8. **Human review gates** - explicit human approval remains the final gate.
9. **Mission Control observability** - API and frontend views for state,
   evidence, artifacts, and review.

## Core Principles

- Mission Control is not the core engine.
- Mission Control is observability and review.
- Executors are adapters, not architecture owners.
- Validators are part of proof-of-work, not optional afterthoughts.
- Human approval is the final gate.
- The operating principle is: manage work, not agents.

## Deterministic Orchestration Boundary

agent-taskflow is not an AI agent that manages other AI agents.

The dispatcher, orchestrator, future workspace manager, executor adapters,
validators, state transitions, proof-of-work collection, and review gates are
deterministic Python-controlled workflow components.

AI coding agents such as Pi, OpenCode, Codex, Claude Code, or future executors
are invoked only as bounded implementation workers through executor adapters.
They do not own scheduling, task selection, state transitions, validation
decisions, approval decisions, merge behavior, push behavior, or cleanup
behavior.

Workflow policy may be included in prompts for context, but enforcement must
come from deterministic code, validators, git/workspace checks, changed-files
checks, and human review gates. AI workers are not trusted to enforce
`WORKFLOW.md` or any workflow contract by prompt adherence alone.

## Component Ownership

- **Dispatcher / orchestrator** - deterministic scheduler and lifecycle
  manager.
- **Workspace manager** - deterministic local workspace preparation; cleanup
  remains human-controlled or separately policy-gated.
- **Executor adapter** - deterministic CLI wrapper and result normalizer.
- **AI coding agent** - bounded implementation worker.
- **Validator** - deterministic proof-of-work checker.
- **Human reviewer** - final approval, reject, rerun, or block decision maker.

## Anti-Goals

agent-taskflow should not become:

- an AI self-orchestration loop
- an agent that chooses its own tasks
- an agent that validates its own work
- an agent that approves or merges its own changes
- a prompt-only governance system

## What This Means

The system should be judged by whether a task can be traced from source to
contract, execution, validation, artifacts, and review. Individual agents or
models are replaceable details behind executor adapters.

The immediate bridge-hardening path is to make the current Python core more
explicit about those boundaries before adding external integrations. The next
architecture work should preserve this order:

1. Make current contracts and artifacts precise.
2. Make local validation reproducible.
3. Reconcile component boundaries with the target architecture.
4. Only then add tracker, workspace, and remote orchestration features.
