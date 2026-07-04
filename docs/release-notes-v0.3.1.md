# Agent TaskFlow v0.3.1 Release Notes

v0.3.1 is a narrow post-v0.3.0 safety hotfix release for the Claude Code bounded implementer executor.

## Summary

This release hardens Claude Code executor invocation behavior after v0.3.0.

The fix addresses two related issues:

- Claude Code implementer prompts are now delivered through stdin instead of being appended to subprocess argv.
- Claude Code startup `OSError` failures are now normalized into blocked executor results and `tool_error` execution artifacts instead of escaping and crashing the runner.

## Fixed

### Claude Code prompt transport

`ClaudeCodeExecutor` no longer appends the full implementer prompt to the subprocess command argv.

Instead, the configured command argv remains exactly the configured command, and the prompt is delivered through stdin.

This avoids:

- large-prompt argv failures such as `E2BIG`
- prompt exposure through process listings
- invocation inconsistency with the Codex advisory path

### Claude Code startup failure handling

Claude Code startup-time `OSError` subclasses are now handled as blocked executor outcomes.

This includes cases such as:

- command exists but is not executable
- `PermissionError`
- `NotADirectoryError`
- `E2BIG`
- other startup-time `OSError` subclasses

These failures now produce:

- a blocked executor result
- a `tool_error` execution artifact
- preserved human-review requirements

## Authority invariant

Claude Code remains a bounded implementer executor.

This release does not grant Claude Code validation, approval, merge, or cleanup authority. Claude Code output remains implementation evidence only, and human review remains required.

## Scope

Changed in this release:

- Claude Code executor invocation transport
- Claude Code startup failure recording
- matching regression coverage
- one stale runbook statement about argv prompt delivery

Intentionally not changed:

- Codex advisory contract semantics
- scheduler, runner, registry, cron, or systemd behavior
- changed-files parsing
- atomic artifact writing
- release pipeline behavior

## Validation

Validated before release metadata preparation:

```bash
.venv/bin/python -m unittest tests.test_claude_code_executor tests.test_claude_code_operator_invocation_runbook -v
```

Result:

```text
59 tests, OK
```

```bash
.venv/bin/python -m unittest discover -s tests
```

Result:

```text
Ran 4065 tests in 190.2s — OK (skipped=8)
```

```bash
.venv/bin/python -m compileall agent_taskflow scripts tests
```

Result:

```text
OK
```
