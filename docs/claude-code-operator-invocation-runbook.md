# Claude Code Operator Invocation Runbook

Status: v0.3.0

> Scope: v0.3.0 adds operator-facing manual invocation guidance only. It does
> **not** change executor behavior, runner behavior, validator behavior, Codex
> advisory behavior, approval behavior, scheduler defaults, or cron/systemd
> profiles. It turns the v0.2.8/v0.2.9 technical capability into a safe,
> copy-pasteable manual procedure.

## 1. Purpose

This runbook explains exactly how a human operator can safely run a single,
already-confirmed approved task with Claude Code **real invocation** as a
bounded implementer.

Core semantic:

> Claude Code may be used as a bounded implementer only when the operator
> explicitly opts in. Claude Code writes code; it does not decide whether the
> task is done.

## 2. Preconditions

Before using this runbook, all of the following must already be true:

- The task exists and is explicitly confirmed for execution.
- The task workspace / worktree has been prepared by the normal pipeline.
- You have the absolute paths for the repo, state DB, artifact root, and
  worktree root.
- Claude Code is installed and runnable as an argv command (for example
  `claude`).
- You are running this manually as an operator — **not** from cron, systemd, a
  daemon, a webhook, or any loop.

## 3. Safety boundary

Claude Code real invocation does **not** grant any new authority. Even with real
invocation enabled, Claude Code still does **not**:

- validate
- approve
- set `waiting_approval`
- bypass deterministic validators
- bypass the Codex advisory artifact contract validator
- bypass the Codex advisory evidence gate
- bypass human final review
- push branches
- open or modify pull requests
- merge
- run cleanup
- delete branches
- delete worktrees
- change scheduler defaults
- change cron/systemd live profiles

The authoritative pipeline is unchanged:

```text
confirmed task / prepared worktree
↓
bounded implementer executor (Claude Code, opt-in real invocation)
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

The execution artifact always records these authority invariants, regardless of
whether invocation happened:

```text
validation_authority = "none"
approval_authority = "none"
merge_authority = "none"
cleanup_authority = "none"
human_review_required = true
```

These deterministic validators still run after the executor and own pass/fail.
The Codex advisory evidence gate remains authoritative for the transition into
`waiting_approval`. Human final review remains required. `waiting_approval` is a
handoff to a human reviewer — `waiting_approval` is not approval.

## 4. Required operator inputs

Use these placeholders consistently when adapting the commands below:

- `<TASK_KEY>` — the confirmed task key, e.g. `AT-GH-123`
- `<REPO_PATH>` — absolute path to the repository
- `<DB_PATH>` — absolute path to the orchestrator state SQLite DB
- `<ARTIFACT_ROOT>` — absolute path to the artifact root
- `<WORKTREE_ROOT>` — absolute path to the controlled worktree root

## 5. Dry-run / prompt-only command

The default behavior is safe and prompt-only. Selecting the executor **without**
the enable flag generates the implementer prompt and a `dry_run` execution
artifact and never spawns a subprocess.

The dry-run / prompt-only command selects `--executor claude-code` but
intentionally does not include `--claude-code-enable-invocation`:

```bash
python3 scripts/run_approved_task.py \
  --task-key <TASK_KEY> \
  --executor claude-code \
  --repo-path <REPO_PATH> \
  --db-path <DB_PATH> \
  --artifact-root <ARTIFACT_ROOT> \
  --worktree-root <WORKTREE_ROOT> \
  --confirm-approved-task
```

Because `--claude-code-enable-invocation` is omitted, the run stays prompt-only /
dry-run regardless of any other Claude Code option. Run the dry-run first and
inspect the generated prompt before considering real invocation.

## 6. Real invocation command

Real invocation requires **explicit opt-in** and **all four** of:

- `--executor claude-code`
- `--claude-code-enable-invocation`
- `--claude-code-command-json`
- `--claude-code-timeout-seconds`

```bash
python3 scripts/run_approved_task.py \
  --task-key <TASK_KEY> \
  --executor claude-code \
  --repo-path <REPO_PATH> \
  --db-path <DB_PATH> \
  --artifact-root <ARTIFACT_ROOT> \
  --worktree-root <WORKTREE_ROOT> \
  --confirm-approved-task \
  --claude-code-enable-invocation \
  --claude-code-command-json '["claude", "-p"]' \
  --claude-code-timeout-seconds 900
```

If `--claude-code-enable-invocation` is omitted, the run stays dry-run no matter
what else is set. If the enable flag is provided without
`--claude-code-command-json`, the runner blocks deterministically in the
`selection` phase before any workspace is prepared or any subprocess runs.

## 7. How to choose command argv

The command is supplied as a JSON array of strings via
`--claude-code-command-json`, for example:

```text
'["claude", "-p"]'
```

Important properties:

- The command is executed as argv.
- The command is argv-based; there is no shell parsing.
- `shell=True` is not used.
- The generated implementer prompt text is delivered to the command over stdin
  by the executor; it is never appended as an argv argument.
- The cwd is the prepared worktree; the cwd is always the prepared worktree,
  never an arbitrary operator cwd.

Choose the smallest argv that invokes Claude Code in a single-shot,
non-interactive mode appropriate to your installed CLI. Do not embed shell
operators (pipes, `&&`, redirects); they will not be interpreted because there
is no shell parsing.

## 8. Timeout guidance

`--claude-code-timeout-seconds` sets the per-invocation timeout passed to the
executor context. It must be finite and positive. If the configured command
exceeds the timeout, the run is recorded as `timed_out` and the task is blocked;
it never reaches `waiting_approval`. Choose a bound that fits the task size; a
few minutes for small doc/test tasks, longer for larger implementation tasks.

## 9. Expected artifacts

A real invocation writes the following under the task artifact directory:

```text
claude-code-implementer-prompt.md
claude-code-execution.json
claude-code-stdout.log
claude-code-stderr.log
```

A dry-run writes `claude-code-implementer-prompt.md` and
`claude-code-execution.json` only (no stdout/stderr logs, since nothing is
invoked).

## 10. How to inspect artifacts

Inspect the execution record and logs in the task artifact directory:

```bash
cat <ARTIFACT_ROOT>/<TASK_KEY>/claude-code-execution.json
cat <ARTIFACT_ROOT>/<TASK_KEY>/claude-code-stdout.log
cat <ARTIFACT_ROOT>/<TASK_KEY>/claude-code-stderr.log
```

`claude-code-execution.json` (schema `claude_code_executor.v1`) records
`status`, `started_at`/`finished_at`, `worktree_path`, `repo_root`, `cwd`
(always the prepared worktree), `command` (the exact argv), `invocation_enabled`,
`prompt_path`, `stdout_path`, `stderr_path`, `exit_code`, `timed_out`,
`blocking_errors`, `warnings`, `changed_files`, and the always-`none` authority
invariants with `human_review_required` set to `true`.

## 11. Expected success path

A successful real invocation records, in `claude-code-execution.json`:

- `status` is `completed`
- `invocation_enabled` is `true`
- `exit_code` is `0`
- `command` is the exact argv you supplied
- `changed_files` lists the files reported by `git status --porcelain` in the
  worktree

`status: completed` means only that the bounded implementer attempt completed. It
does **not** mean validators passed, the task was approved, or the task reached
`waiting_approval`. The runner then runs deterministic validators; only after
validators pass and the Codex advisory evidence gate is satisfied may the task
enter `waiting_approval` for human review.

## 12. Expected blocked path

The task is blocked (and never reaches `waiting_approval`) when, for example:

- Preflight fails — `status: blocked` (missing/invalid worktree, repo root, or
  task key, or enable-invocation without a configured command).
- The configured command cannot start — `status: tool_error`.
- The command exceeds the timeout — `status: timed_out`.
- The command exits non-zero — `status: failed`.
- A deterministic validator fails after the executor.
- The Codex advisory evidence gate is not satisfied.

In every blocked case the authority invariants remain `none` and
`human_review_required` remains `true`.

## 13. How to verify validators ran

A successful Claude Code exit does not skip validation. After the executor, the
runner runs the deterministic validators; deterministic validators still run and
own pass/fail. Verify their result artifacts/logs are present for the task and
that they recorded a decision. If any validator fails, the task is blocked and
does not proceed to `waiting_approval`.

## 14. How to verify Codex advisory evidence gate passed

Even after deterministic validators pass, the task may only enter
`waiting_approval` when valid Codex advisory artifact contract evidence is
present. The Codex advisory evidence gate remains authoritative for the
transition into `waiting_approval`; a successful invocation cannot bypass this
gate. Verify the Codex advisory artifact contract evidence exists for the task
before expecting a `waiting_approval` transition.

## 15. How to confirm `waiting_approval` is not approval

`waiting_approval` is a handoff to a human reviewer, not approval.
`waiting_approval` is not approval. The executor never approves, never sets
`waiting_approval` itself, and `human_review_required` remains `true`. Human
final review remains required and is always a human decision. Confirm the final
state is `waiting_approval` (a review handoff), not an approved state.

## 16. What not to do

Do not, as part of this procedure:

- Run real invocation from cron, systemd, a daemon, a webhook, or any loop.
- Make `claude-code` the default executor.
- Change scheduler defaults. There is no scheduler default change here.
- Change cron/systemd live profiles. There is no cron/systemd live profile change here.
- Enable or perform any branch push, PR creation, merge, cleanup, branch
  deletion, or worktree deletion behavior. There is no branch push / PR creation / merge / cleanup / deletion behavior in this procedure.
- Treat `status: completed` or `waiting_approval` as approval.
- Pass the command as a shell string or embed shell operators (there is no shell
  parsing and `shell=True` is not used).

## 17. Troubleshooting

- Run stays dry-run unexpectedly: confirm `--claude-code-enable-invocation` is
  present. Without it the run is always prompt-only / dry-run.
- Runner blocks in the `selection` phase: you enabled invocation without
  `--claude-code-command-json`. Supply a JSON array of strings.
- `status: tool_error`: the argv could not start. Confirm the Claude Code CLI is
  installed and the first argv element is on `PATH`.
- `status: timed_out`: increase `--claude-code-timeout-seconds` if appropriate,
  or reduce task scope. The value must be finite and positive.
- Shell operators in the command appear ignored: this is expected. The command
  is argv-based; there is no shell parsing.
- Task does not reach `waiting_approval` after a successful exit: check that
  deterministic validators passed and that the Codex advisory evidence gate is
  satisfied.

## Pre-run checklist

Before running real invocation, confirm every item:

- [ ] Am I on the correct repo?
- [ ] Is the task explicitly confirmed?
- [ ] Is this a single task?
- [ ] Is `--executor claude-code` intentional?
- [ ] Am I intentionally enabling real invocation?
- [ ] Is the command JSON a JSON array of strings?
- [ ] Is timeout finite and positive?
- [ ] Is worktree root controlled?
- [ ] Am I not running this from cron/systemd?
- [ ] Am I not enabling merge/cleanup/delete behavior?
- [ ] Do I understand that `waiting_approval` is not approval?

## Post-run checklist

After running, verify every item:

- [ ] Did the executor artifact exist?
- [ ] Did stdout/stderr logs exist?
- [ ] Did `claude-code-execution.json` record `invocation_enabled` correctly?
- [ ] Did authority fields remain `none`?
- [ ] Did deterministic validators run?
- [ ] Did the Codex advisory evidence gate pass?
- [ ] Is the final state `waiting_approval` rather than approval?
- [ ] Did no push/PR/merge/cleanup/delete occur?

## Related docs

- [Claude Code Bounded Implementer Executor](claude-code-bounded-implementer-executor.md)
