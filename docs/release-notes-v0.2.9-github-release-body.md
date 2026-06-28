## v0.2.9 — Claude Code Real Invocation Workflow Policy + Golden Path Smoke

This release builds on the v0.2.7 Claude Code bounded implementer executor and
the v0.2.8 Claude Code opt-in real invocation profile. It is a workflow-policy
alignment and test-hardening release: it aligns workflow-policy documentation
with the already-defined `claude-code` executor and adds golden-path smoke
coverage for the v0.2.8 real invocation path.

Core rule (unchanged):

> Claude Code writes code; it does not decide whether the task is done.

### Workflow-policy alignment

`claude-code` is a defined, explicitly selectable bounded implementer executor
adapter. The workflow-policy documentation now reflects this directly.

- `claude-code` is not the default executor.
- `claude-code` is not added to the canonical `allowed_executors` example.
- To permit `claude-code` under a workflow policy, the policy must explicitly
  list `"claude-code"` in `allowed_executors`.

This is a documentation/policy alignment only. The executor itself was already
defined in v0.2.7 and wired for opt-in real invocation in v0.2.8.

### Golden-path smoke coverage

This release adds two golden-path smoke tests that exercise the v0.2.8 real
invocation path end to end:

- Runner-level: `tests/test_approved_task_runner.py::ApprovedTaskRunnerTests::test_claude_code_real_invocation_golden_path_smoke`
- CLI-level: `tests/test_run_approved_task_script.py::RunApprovedTaskScriptTests::test_run_approved_task_claude_code_real_invocation_golden_path_smoke`

The smokes use fake argv commands only. No real Claude Code is invoked in tests.
The fake command writes one file into the prepared worktree, emits stdout and
stderr, and exits 0.

The smokes verify that:

- Real invocation remains explicit opt-in.
- Command execution is argv-based, not shell-string based.
- The `cwd` is the prepared worktree.
- stdout and stderr are captured.
- `claude-code-execution.json` is written.
- The execution artifact uses `schema_version = "claude_code_executor.v1"`.
- The execution artifact records `executor = "claude-code"`.
- The execution artifact records `invocation_enabled = true`.
- The command argv, `cwd`, exit code, timeout state, and changed files are
  recorded.
- The authority fields remain `none`: `validation_authority = "none"`,
  `approval_authority = "none"`, `merge_authority = "none"`, and
  `cleanup_authority = "none"`.
- The execution artifact records `human_review_required = true`.
- Deterministic validators still run after the executor.
- A successful invocation without Codex advisory evidence remains blocked.
- A successful invocation with valid Codex advisory evidence can reach
  `waiting_approval`.
- `waiting_approval` is not approval.

### What does not change

This release changes no implementation behavior:

- No executor behavior change.
- No runner behavior change.
- No scheduler default change.
- No cron/systemd live profile change.
- No daemon/webhook/loop behavior.

### Authority boundaries (unchanged)

Claude Code remains a bounded implementer with no authority beyond writing code:

- No validator authority.
- No approval authority.
- No merge authority.
- No cleanup authority.
- No branch push, PR creation, merge, cleanup, or deletion behavior.

The Codex advisory evidence gate remains authoritative before `waiting_approval`.
Human final review remains required.

### Validation

- `PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_v028_release_docs tests.test_v029_release_docs`
- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
