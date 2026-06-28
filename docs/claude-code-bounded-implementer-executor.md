# Claude Code Bounded Implementer Executor

Status: v0.2.9

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

## v0.2.8 — Opt-in Real Invocation Profile

v0.2.8 wires the executor's existing opt-in invocation capability into the
approved task runner CLI (`scripts/run_approved_task.py`) so an operator can run a
confirmed task with Claude Code as a real bounded implementer. Real invocation
remains strictly opt-in; nothing about the default changes.

### How to run prompt-only / dry-run (default)

Select the executor without the enable flag. The executor generates the
implementer prompt and the `dry_run` execution artifact and never spawns a
subprocess:

```bash
python3 scripts/run_approved_task.py \
  --task-key AT-GH-123 \
  --executor claude-code \
  --repo-path /abs/path/to/repo \
  --db-path /abs/path/to/state.db \
  --artifact-root /abs/path/to/artifacts \
  --worktree-root /abs/path/to/worktrees \
  --confirm-approved-task
```

### How to explicitly enable real invocation

Real invocation requires **all three** of:

1. `--executor claude-code`
2. `--claude-code-enable-invocation`
3. `--claude-code-command-json` with an explicit argv

```bash
python3 scripts/run_approved_task.py \
  --task-key AT-GH-123 \
  --executor claude-code \
  --repo-path /abs/path/to/repo \
  --db-path /abs/path/to/state.db \
  --artifact-root /abs/path/to/artifacts \
  --worktree-root /abs/path/to/worktrees \
  --confirm-approved-task \
  --claude-code-enable-invocation \
  --claude-code-command-json '["claude", "-p"]' \
  --claude-code-timeout-seconds 900
```

If `--claude-code-enable-invocation` is omitted, the run stays prompt-only /
dry-run regardless of any other Claude Code option. If it is provided without
`--claude-code-command-json`, the runner blocks deterministically in the
`selection` phase before any workspace is prepared or any subprocess runs. Claude
Code-specific options are rejected when another executor is selected.

### How to configure command argv

The command is supplied as a JSON array of strings and is always executed as
argv. There is no shell parsing and `shell=True` is never used. The generated
implementer prompt text is appended as the final argument before execution.

### How timeout works

`--claude-code-timeout-seconds` sets the per-invocation timeout passed to the
executor context. When the configured command exceeds it, the run is recorded as
`timed_out` and the task is blocked; it never reaches `waiting_approval`.

### Where artifacts are written

Real invocation preserves the v0.2.7 artifact behavior, written under the task
artifact directory:

```text
claude-code-implementer-prompt.md
claude-code-execution.json
claude-code-stdout.log
claude-code-stderr.log
```

### How to inspect `claude-code-execution.json`

`claude-code-execution.json` records `schema_version`, `executor`, `status`,
`started_at`/`finished_at`, `worktree_path`, `repo_root`, `cwd` (always the
prepared worktree), `command`, `invocation_enabled`, `prompt_path`,
`stdout_path`, `stderr_path`, `exit_code`, `timed_out`, `blocking_errors`,
`warnings`, `changed_files`, and the always-`none` authority invariants with
`human_review_required: true`. A successful real invocation records
`status: completed`, `invocation_enabled: true`, the exit code, the exact command
argv, and the changed files reported by `git status --porcelain` in the worktree.

### What authority remains denied

A real invocation does not change the executor's authority. It does not validate,
approve, merge, cleanup, push branches, open PRs, delete branches or worktrees,
or set any approval state. The artifact authority fields remain `"none"` and
`human_review_required` remains `true`.

### Why deterministic validators still run afterward

A successful Claude Code exit means only that the bounded implementer attempt
completed. The runner still runs the deterministic validators after the executor.
If any validator fails, the task is blocked and does not proceed.

### Why the Codex advisory evidence gate still controls `waiting_approval`

Even after validators pass, the task may only enter `waiting_approval` when valid
Codex advisory artifact contract evidence is present. A successful invocation
cannot bypass this gate or set `waiting_approval` directly.

### Why human final review remains required

`waiting_approval` is a handoff to a human reviewer, not approval. Final approval
is always a human decision; the executor never approves.

## v0.2.9 — Real Invocation Workflow Policy + Golden Path Smoke

v0.2.9 adds no new executor behavior. It aligns workflow policy and
documentation with the v0.2.8 opt-in real invocation profile and adds focused
golden-path smoke coverage. The core semantic is unchanged:

```text
Claude Code may be selected as a bounded implementer executor.
Real invocation remains explicit opt-in.
Claude Code still does not decide whether the task is done.
```

### Explicitly selectable bounded implementer executor

`claude-code` is an explicitly selectable bounded implementer executor. It is a
registered executor name (`list_executor_names()` / `SUPPORTED_EXECUTORS`),
selected with `--executor claude-code`. Selecting it never grants it validator,
approval, merge, cleanup, scheduler, or lifecycle authority.

### Real invocation remains opt-in; dry-run remains default

Selecting the executor does not invoke Claude Code. The default remains
prompt-only / dry-run: the executor generates the implementer prompt and a
`dry_run` execution artifact and never spawns a subprocess. Real invocation
requires **all three** of `--executor claude-code`,
`--claude-code-enable-invocation`, and `--claude-code-command-json` with an
explicit argv. Omitting the enable flag keeps the run dry-run regardless of any
other Claude Code option.

### Workflow-policy / allowed-executor alignment

The workflow policy schema documents `claude-code` as a defined, explicitly
selectable bounded implementer executor adapter. It is not added to the canonical
`allowed_executors` example (`["manual", "shell", "opencode", "pi"]`) and is not a
default: to permit it under a given policy, that policy's `allowed_executors`
must list `"claude-code"`. See
[workflow-schema.md](workflow-schema.md#allowed_executors).

### Golden-path smoke coverage

Golden-path smokes exercise the v0.2.8 real invocation path end-to-end in a safe
fake-command environment (no real Claude Code is invoked):

- `tests/test_approved_task_runner.py::ApprovedTaskRunnerTests::test_claude_code_real_invocation_golden_path_smoke`
- `tests/test_run_approved_task_script.py::RunApprovedTaskScriptTests::test_run_approved_task_claude_code_real_invocation_golden_path_smoke`

They use a fake argv command (never a shell string) that runs with `cwd` set to
the prepared worktree, writes one file into the worktree, emits stdout and
stderr, and exits 0. They assert the command is passed as argv, stdout/stderr are
captured to `claude-code-stdout.log` / `claude-code-stderr.log`, and
`claude-code-execution.json` records `schema_version = "claude_code_executor.v1"`,
`executor = "claude-code"`, `invocation_enabled = true`, the command argv, `cwd`,
exit code, timeout state, changed files, and the always-`none` authority
invariants with `human_review_required = true`. They also assert that
deterministic validators still run after the executor, that without Codex
advisory evidence a successful invocation stays blocked, that with valid Codex
advisory evidence it can reach `waiting_approval`, and that `waiting_approval` is
a handoff to a human reviewer — not approval.

### What does not change in v0.2.9

- No scheduler default change. `claude-code` is not the scheduler default
  executor.
- No cron/systemd live profile change. Claude Code is not wired into any live
  profile, daemon, webhook, or loop.
- No validator authority. Deterministic validators still run after the executor
  and own pass/fail.
- No approval authority. The executor never approves and never sets
  `waiting_approval`.
- No merge authority and no cleanup authority. No branch push, PR creation,
  merge, branch deletion, worktree deletion, or cleanup.
- The Codex advisory evidence gate remains authoritative for the transition into
  `waiting_approval`.
- Human final review remains required.

## Non-goals

- Not the default executor.
- Not wired into cron/systemd live profiles.
- No daemon/webhook/loop behavior.
- No validator, approval, merge, cleanup, push, PR, or deletion authority.
- Does not bypass deterministic validators, the Codex advisory evidence gate, or
  human final review.
- Does not implement an ExecutionEngine takeover.
