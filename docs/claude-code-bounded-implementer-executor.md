# Claude Code Bounded Implementer Executor

Status: v0.2.7

## Purpose

The Claude Code Bounded Implementer Executor adds Claude Code as a bounded
implementation worker in Agent Taskflow. It may be used to implement code inside
an already prepared/confirmed worktree.

Core semantic:

> Claude Code writes code; it does not decide whether the task is done.

Claude Code is an executor backend only. Like Pi and OpenCode, it is a
deterministic CLI wrapper and result normalizer wrapped around a bounded AI
coding worker. It is not the orchestrator, validator, reviewer, merger, or
cleanup authority.

## Executor role

The executor:

- reads task/spec context (the existing implementation prompt, if present)
- reads the prepared worktree path
- generates a Claude Code implementer prompt
- optionally invokes a configured Claude Code command, only when explicitly enabled
- writes execution artifacts/logs
- returns an execution status

It must not:

- approve tasks
- set `waiting_approval` by itself
- mark validators as passed or bypass deterministic validators
- bypass the Codex advisory artifact contract validator or evidence gate
- open PRs, push branches, merge
- delete branches or worktrees, or run cleanup
- modify scheduler lifecycle authority, Mission Control authority, or approval semantics

## How it differs from a validator

A validator is a deterministic proof-of-work gate that decides pass/fail. The
Claude Code executor produces code changes and evidence; it never decides
whether evidence passes. Deterministic validators run *after* the executor and
own the pass/fail decision.

## How it differs from approval

Approval is the human review gate. The executor never approves, never sets
`waiting_approval`, and the execution artifact always records
`human_review_required = true`. Reaching `waiting_approval` still requires
deterministic validators to pass and the Codex advisory evidence gate to be
satisfied; final approval is always a human decision.

## Prompt artifact

The executor generates a deterministic implementer prompt and writes it to:

```text
claude-code-implementer-prompt.md
```

The prompt includes the task key, worktree path, repo root, task/spec summary
(when available), the allowed bounded-implementer role, the disallowed authority
(approval, validation, merge, cleanup, deletion, scheduler control), the hard
prohibitions (no push/PR/merge/delete/cleanup), the expected output (code changes
only inside the prepared worktree, plus a report of changed files and commands
run), and a statement that deterministic validators and human review decide
completion.

The policy validator treats this prompt as an instruction/spec artifact (it
documents prohibitions as governance text), so it is scanned for secret leakage
only, exactly like `implementation_prompt.md` and `pi_mission_prompt.md`.

## Execution artifact

Every executor attempt writes a deterministic JSON artifact:

```text
claude-code-execution.json
```

Schema (`schema_version: claude_code_executor.v1`):

```json
{
  "schema_version": "claude_code_executor.v1",
  "executor": "claude-code",
  "task_key": "AT-GH-123",
  "status": "dry_run|completed|failed|timed_out|blocked|tool_error",
  "started_at": "ISO-8601 or null",
  "finished_at": "ISO-8601 or null",
  "worktree_path": "...",
  "repo_root": "...",
  "cwd": "...",
  "command": ["..."],
  "invocation_enabled": false,
  "prompt_path": "...",
  "stdout_path": "... or null",
  "stderr_path": "... or null",
  "exit_code": 0,
  "timed_out": false,
  "blocking_errors": [],
  "warnings": [],
  "changed_files": [],
  "validation_authority": "none",
  "approval_authority": "none",
  "merge_authority": "none",
  "cleanup_authority": "none",
  "human_review_required": true
}
```

The authority invariants are always recorded as `"none"` and
`human_review_required` is always `true`. The `status` field is intentionally
richer than the orchestrator's executor-result status vocabulary so the artifact
faithfully records what the bounded attempt did:

- `dry_run` — prompt generated, Claude Code not invoked (default).
- `completed` — opt-in invocation exited 0. This means only that the bounded
  executor attempt completed. It does **not** mean validators passed, the task
  was approved, or the task reached `waiting_approval`.
- `failed` — opt-in invocation exited non-zero.
- `timed_out` — opt-in invocation exceeded the configured timeout.
- `blocked` — preflight failed; the executor did not run.
- `tool_error` — the configured Claude Code command could not start.

## Dry-run / prompt-only behavior

Default behavior is safe and prompt-only. The executor generates the implementer
prompt and the execution artifact, records `status: dry_run`, and never invokes
a subprocess. The orchestrator's executor-result status is `completed` (the
bounded attempt completed), after which deterministic validators run normally.

When resolved through the registry (`get_executor("claude-code")`) or the runner,
the executor is always in dry-run mode.

## Opt-in invocation behavior

Real invocation is opt-in and requires both:

- `enable_invocation=True`, and
- an explicitly configured `command` (e.g. `["claude", "-p"]`).

When enabled, the executor appends the generated prompt text as the final
argument, runs the command with `cwd` set to the prepared worktree, enforces the
context timeout, captures stdout and stderr to `claude-code-stdout.log` and
`claude-code-stderr.log`, and records the exit code in the execution artifact.
Constructing an invocation-enabled executor without a command raises
`ValueError`.

There is no always-on runner, daemon, webhook, or loop. Claude Code is not wired
into cron/systemd live profiles and is not the scheduler default executor.

## Preflight checks

Before a run proceeds, `check_claude_code_preflight` validates at minimum:

- `task_key` is non-empty
- repo root is provided, exists, and is a directory
- worktree path is provided, exists, and is a directory
- worktree path is inside the configured worktree root, when one is configured
- when real invocation is enabled, an explicit command is configured

The execution `cwd` is always the prepared worktree, never an arbitrary user
cwd. The artifact output directory is created safely under the task artifact
area. If a preflight check fails, the executor records a `blocked` execution
artifact and returns a blocked executor result; it does not crash.

## Safety boundaries

```text
Claude Code writes code only.
Claude Code does not approve.
Claude Code does not validate.
Claude Code does not merge.
Claude Code does not cleanup.
Claude Code does not set waiting_approval.
Human final review remains required.
```

## Validation flow after the executor

The pipeline is unchanged by this executor:

```text
confirmed task / prepared worktree
↓
Claude Code bounded implementer executor
↓
deterministic validators
↓
Codex advisory artifact contract validator
↓
Codex advisory evidence gate
↓
waiting_approval
↓
human final review
```

## Non-goals

- Not the default executor.
- Not wired into cron/systemd live profiles.
- No daemon/webhook/loop behavior.
- No validator, approval, merge, cleanup, push, PR, or deletion authority.
- Does not bypass deterministic validators, the Codex advisory evidence gate, or
  human final review.
- Does not implement an ExecutionEngine takeover.
