## v0.2.7 — Claude Code Bounded Implementer Executor

This release adds Claude Code as an explicit bounded implementer executor option.

External executor kind:

- `claude-code`

Core rule:

> Claude Code writes code; it does not decide whether the task is done.

Claude Code is an implementer, not a validator. It may generate a bounded
implementer prompt artifact and may write a deterministic execution artifact,
but it never decides whether validation passed or whether work may proceed to
approval.

### Artifacts

Prompt artifact:

- `claude-code-implementer-prompt.md`

Execution artifact:

- `claude-code-execution.json`
- schema version `claude_code_executor.v1`

The execution artifact records the bounded executor attempt and always carries
these authority invariants:

- `validation_authority = "none"`
- `approval_authority = "none"`
- `merge_authority = "none"`
- `cleanup_authority = "none"`
- `human_review_required = true`

Claude Code has no validation authority.

Claude Code has no approval authority.

Claude Code has no merge authority.

Claude Code has no cleanup authority.

Human final review remains required.

### Default behavior

Default behavior is prompt-only / dry-run.

The default executor path generates the Claude Code implementer prompt and a
deterministic execution artifact, then stops. It does not invoke Claude Code as a
subprocess.

Real invocation is opt-in only.

Real invocation requires explicit command configuration. When real invocation is
enabled, the configured command runs with `cwd` set to the prepared worktree.
The executor records stdout, stderr, exit code, and timeout state in artifacts.

### Preflight checks

Preflight checks cover:

- non-empty task key
- repo root exists and is directory
- worktree path exists and is directory
- worktree-root containment when configured
- explicit command required when invocation is enabled

### Validation and review flow

The runner still executes deterministic validators after the executor.

The Codex advisory artifact contract validator still runs after deterministic validators.

The Codex advisory evidence gate remains authoritative before `waiting_approval`.

Human final review remains required.

### Safety boundary

Claude Code does not:

- approve
- validate
- mark validators as passed
- set `waiting_approval`
- bypass deterministic validators
- bypass Codex advisory evidence gate
- bypass human final review
- push branches
- open PRs
- merge
- cleanup
- delete branches
- delete worktrees
- change scheduler defaults
- change cron/systemd live profiles

### Validation

- `PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_v026_release_docs tests.test_v027_release_docs`
- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
