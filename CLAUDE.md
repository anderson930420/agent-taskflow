# Agent Taskflow / Mission Control Instructions

You are working in the `agent-taskflow` repository.

This repository builds a Python-native, GitHub-oriented, Symphony-style agent orchestration system.

The core principle is:

> Manage work, not agents.

AI coding tools such as Pi, OpenCode, Codex, Claude Code, or future tools are bounded implementation workers. They are not the orchestrator, reviewer, validator, merger, or cleanup authority.

## Required Context

Read `WORKFLOW.md` when the task involves any of the following:

- task execution workflow
- executor behavior
- validator behavior
- proof-of-work artifacts
- workspace policy
- changed-files or path policy
- approval, rejection, rerun, or blocking behavior
- Mission Control review semantics
- governance rules

`WORKFLOW.md` is the repo-owned workflow contract. This file is the short project instruction entrypoint for coding agents.

If `WORKFLOW.md` is relevant, read it before editing, not after implementation.

## Operating Rules

- Prefer small, reviewable changes.
- Before editing, inspect relevant files and explain intended changes.
- Reuse existing project patterns before introducing new abstractions.
- Keep executor, validator, store, API, and frontend boundaries clean.
- Do not edit unrelated files.
- Do not perform cosmetic rewrites unrelated to the task.
- Do not introduce new dependencies unless explicitly required.
- Do not touch secrets, `.env` files, SSH keys, API keys, tokens, or system credentials.
- Do not weaken tests, validators, governance checks, or safety policies.
- Do not fake success, fake validation, or fabricate artifacts.

## Governance Rules

Do not do any of the following unless the human explicitly asks:

- create commits
- push
- merge
- rebase shared branches
- delete branches
- delete worktrees
- run destructive cleanup
- close issues
- approve tasks
- mark work as finally complete
- bypass validators
- change approval records to imply human approval
- change deployment, systemd, nginx, or cron configuration

Human review remains the final gate.

## Architecture Boundaries

Use these interpretations consistently:

- SQLite store is orchestrator state storage.
- Dispatcher owns scheduler and run lifecycle behavior.
- Executor adapters are deterministic CLI wrappers and result normalizers.
- Pi and OpenCode are executor backends only.
- Validators are deterministic proof-of-work gates.
- FastAPI exposes state and proof-of-work for review.
- Mission Control is observability and review, not the execution core.
- Artifact metadata is a proof-of-work index.
- Approval metadata is a human review gate.
- Golden path smoke tests are end-to-end workflow acceptance tests.

## Validation Expectations

After code changes, run the most relevant validation.

For Python changes, prefer:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall agent_taskflow scripts tests
```

For focused Python changes, run the smallest relevant test first, then broader validation when appropriate.

For Mission Control frontend changes, prefer:

```bash
cd mission-control && npm run build
```

For smoke-script changes, run the corresponding smoke test and its unit test when available.

Never claim a command passed unless it was actually run and observed to pass.

If a command was not run, say it was not run and explain why.

If a command failed, report the failure clearly.

## Final Report Format

End each implementation task with:

```text
Final Report

1. Starting state
- Branch:
- Git status:

2. Implementation summary
- ...

3. Files changed
- ...

4. Validation
- Command:
  Result:

5. Artifacts
- ...

6. Final state
- Git status:
- Commit created: yes/no

7. Blockers / follow-ups
- ...
```

## Completion Standard

A task is implementation-complete only when:

- the requested change is implemented
- relevant tests pass or failures are clearly reported
- proof-of-work is available
- the final report is accurate
- the work is ready for human review

The task is not finally approved until a human reviewer approves it.
