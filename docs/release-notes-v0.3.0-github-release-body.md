## v0.3.0 — Claude Code Operator Invocation Runbook

This release builds on the v0.2.7 Claude Code bounded implementer executor, the
v0.2.8 Claude Code opt-in real invocation profile, and the v0.2.9 workflow-policy
alignment and golden-path smoke coverage. It is a documentation-only release: it
turns the already-shipped Claude Code real invocation capability into a safe,
copy-pasteable manual procedure for human operators.

Core rule (unchanged):

> Claude Code writes code; it does not decide whether the task is done.

### Scope

v0.3.0 adds operator-facing manual invocation guidance only. It does not change
executor, runner, registry, scheduler, validator, Codex advisory, approval,
merge, or cleanup behavior.

- Adds `docs/claude-code-operator-invocation-runbook.md`.
- Links the runbook from `docs/claude-code-bounded-implementer-executor.md`.
- Adds `tests/test_claude_code_operator_invocation_runbook.py`.

### What the runbook covers

The runbook gives a human operator a bounded, copy-pasteable procedure for
running a single already-confirmed approved task with Claude Code as a bounded
implementer.

- A copy-pasteable dry-run / prompt-only command.
- A copy-pasteable real invocation command.
- A pre-run checklist and a post-run checklist.
- Documented expected artifacts and artifact inspection commands.
- A documented expected success path and expected blocked path.
- Documented timeout guidance.

#### Dry-run / prompt-only command

The dry-run / prompt-only command selects `--executor claude-code` but
intentionally omits `--claude-code-enable-invocation`. Because the
dry-run omits `--claude-code-enable-invocation`, the run stays prompt-only and
never spawns a subprocess. Run the dry-run first and inspect the generated
prompt before considering real invocation.

#### Real invocation command

Real invocation requires explicit opt-in and all four of:

- `--executor claude-code`
- `--claude-code-enable-invocation`
- `--claude-code-command-json`
- `--claude-code-timeout-seconds`

Execution semantics documented by the runbook:

- The command JSON is argv-based.
- There is no shell parsing.
- `shell=True` is not used.
- The generated implementer prompt is appended as the final argv argument.
- The `cwd` is the prepared worktree.

### Expected artifacts

A real invocation writes, under the task artifact directory:

```text
claude-code-implementer-prompt.md
claude-code-execution.json
claude-code-stdout.log
claude-code-stderr.log
```

The runbook documents how to inspect these artifacts and how to read the
`claude-code-execution.json` execution record.

### Authority invariants (unchanged)

Even with real invocation enabled, the execution artifact always records the
same authority invariants:

```text
validation_authority = "none"
approval_authority = "none"
merge_authority = "none"
cleanup_authority = "none"
human_review_required = true
```

Deterministic validators still run after the executor and own pass/fail. The
Codex advisory evidence gate remains authoritative for the transition into
`waiting_approval`. Human final review remains required. `waiting_approval` is a
handoff to a human reviewer — `waiting_approval` is not approval.

### What does not change

This release changes no implementation behavior:

- No executor behavior change.
- No runner behavior change.
- No registry behavior change.
- No scheduler behavior change (scheduler defaults unchanged).
- No cron/systemd live profile change.
- No validator behavior change.
- No Codex advisory behavior change.
- No approval behavior change.
- No merge behavior change.
- No cleanup behavior change.
- No daemon/webhook/loop behavior.

`claude-code` is not made the default executor and is not wired into any live
cron/systemd profile. There is no
branch push / PR creation / merge / cleanup / deletion behavior introduced by
this release or by the documented procedure.

### Related docs

- `docs/claude-code-operator-invocation-runbook.md`
- `docs/claude-code-bounded-implementer-executor.md`

### Validation

- `PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_v029_release_docs tests.test_v030_release_docs`
- `PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests`
- `PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests`
