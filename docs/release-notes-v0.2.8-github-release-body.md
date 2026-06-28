## v0.2.8 — Claude Code Opt-in Real Invocation Profile

This release builds on the v0.2.7 Claude Code bounded implementer executor. It
adds operator-facing CLI and config support for explicit, opt-in real
invocation of the `claude-code` executor.

Core rule (unchanged):

> Claude Code writes code; it does not decide whether the task is done.

### Operator-facing CLI flags

This release adds explicit operator controls for real invocation:

- `--claude-code-enable-invocation`
- `--claude-code-command-json`
- `--claude-code-timeout-seconds`

Real invocation requires all of the following:

- `--executor claude-code`
- the explicit enable flag (`--claude-code-enable-invocation`)
- an explicit command argv JSON (`--claude-code-command-json`)

Real invocation requires explicit enable flag. Real invocation requires
explicit command argv JSON.

### Default behavior

Dry-run / prompt-only remains the default. When real invocation is not
explicitly enabled, the executor generates the Claude Code implementer prompt
and a deterministic execution artifact, then stops without invoking any
subprocess.

### Command parsing and execution

The configured command is parsed as a JSON array of strings. The command is
passed as argv. There is no shell parsing and no `shell=True`.

When real invocation is enabled, the configured command runs with `cwd` set to
the prepared worktree. The timeout flows into `ExecutorContext.timeout_seconds`
and the timeout is enforced by the subprocess timeout.

The executor records stdout, stderr, exit code, and timeout state in artifacts.

### Artifacts

Artifact behavior is unchanged:

- `claude-code-implementer-prompt.md`
- `claude-code-execution.json`
- `claude-code-stdout.log`
- `claude-code-stderr.log`

The execution artifact `claude-code-execution.json` still uses schema version
`claude_code_executor.v1`.

For real invocation, the execution artifact records:

- `invocation_enabled = true`
- the command argv
- the `cwd`
- the exit code
- the timeout state
- the changed files
- `validation_authority = "none"`
- `approval_authority = "none"`
- `merge_authority = "none"`
- `cleanup_authority = "none"`
- `human_review_required = true`

### Deterministic safety gates

A missing command with invocation enabled is blocked deterministically before
any subprocess execution. Claude Code options on non-`claude-code` executors are
rejected.

A successful Claude Code invocation does not mean validators passed. The runner
still executes deterministic validators after the executor. The Codex advisory
evidence gate remains authoritative before `waiting_approval`. Human final
review remains required.

### Authority boundaries

Claude Code has no validation authority.

Claude Code has no approval authority.

Claude Code has no merge authority.

Claude Code has no cleanup authority.

Claude Code does not:

- validate
- approve
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

- `PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_v027_release_docs tests.test_v028_release_docs`
- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
