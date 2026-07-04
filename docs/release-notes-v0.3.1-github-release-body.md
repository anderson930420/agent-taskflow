# Agent TaskFlow v0.3.1

v0.3.1 is a narrow post-v0.3.0 safety hotfix for the Claude Code bounded implementer executor.

## Fixed

- Deliver Claude Code implementer prompts through stdin instead of subprocess argv.
- Normalize Claude Code startup `OSError` failures into blocked executor results and `tool_error` artifacts.
- Preserve bounded-implementer authority invariants: Claude Code output remains implementation evidence only, with no validation, approval, merge, or cleanup authority.

## Covered startup failures

This hotfix covers startup-time failures such as:

- non-executable configured command
- `PermissionError`
- `NotADirectoryError`
- `E2BIG`
- other startup-time `OSError` subclasses

## Validation

```bash
.venv/bin/python -m unittest tests.test_claude_code_executor tests.test_claude_code_operator_invocation_runbook -v
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall agent_taskflow scripts tests
```

Results:

- targeted Claude Code executor and runbook tests: 59 tests, OK
- full suite: 4065 tests, OK, skipped=8
- compileall: OK
