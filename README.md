# Agent Taskflow

Reusable human-gated task workflow tooling for AI coding agents.

Agent Taskflow provides scripts, templates, and documentation for running repo-backed AI worker tasks with explicit human approval gates.

## Purpose

This project is designed for workflows where:

1. A human creates a task with a clear spec.
2. An AI worker implements the task in an isolated worktree.
3. The worker produces a completion report and PR.
4. The task waits for human approval.
5. The human accepts, rejects, or requests revision.
6. Accepted tasks are merged and cleaned up by the human-controlled workflow.

## Core principles

- AI workers must not merge into main.
- AI workers must not self-approve.
- Every repo task should use a verified worktree.
- Human approval is required before acceptance.
- Task artifacts should remain auditable.
- The workflow should be reusable across multiple repositories.

## Current status

This repository is being extracted from the original `bullet_journal_app` workflow experiment and will be generalized into a reusable agent task orchestration toolkit.
