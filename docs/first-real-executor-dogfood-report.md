# First Real Executor Dogfood Report

## Executive summary

This report records the first successful real executor dogfood run using
agent-taskflow itself. The run proved that a bounded implementation worker can
move a small documentation/test task through the current semi-automatic control
plane without taking over orchestration, validation, approval, push, PR, merge,
or cleanup authority.

- Task key: `AT-DOGFOOD-REAL-001`
- Issue: Issue #14
- Draft PR: PR #15
- Executor: Pi executor
- Final outcome: a draft PR was created and left for human review
- Changed files:
  - `docs/operator-issue-to-draft-pr-dogfood.md`
  - `tests/test_operator_issue_to_draft_pr_runbook.py`

## Workflow completed

The completed chain was:

```text
GitHub Issue #14
-> ingest into local task mirror
-> issue_spec artifact
-> explicit workspace preparation
-> TaskWorktreeRecord with base_sha
-> Pi executor run
-> deterministic validation
-> waiting_approval
-> review evidence readback
-> PR handoff package
-> branch push dry-run
-> confirmed branch push
-> draft PR dry-run
-> confirmed draft PR creation
-> PR #15 opened as draft
-> human review remains final gate
```

This was a human/operator-driven dogfood run. The executor implemented the
bounded task only; agent-taskflow and the operator retained control of
workflow state, validation, evidence, branch publication, draft PR creation,
and review handoff.

## Evidence summary

The dogfood run produced and preserved these evidence locations:

- DB path:
  `/tmp/agent-taskflow-first-real-dogfood/state/state.db`
- Artifact root:
  `/tmp/agent-taskflow-first-real-dogfood/artifacts`
- Issue spec:
  `/tmp/agent-taskflow-first-real-dogfood/artifacts/AT-DOGFOOD-REAL-001/issue_spec.md`
- Worktree:
  `/home/ubuntu/agent-taskflow/.worktrees/AT-DOGFOOD-REAL-001`
- Task branch:
  `task/AT-DOGFOOD-REAL-001`
- Worktree commit:
  `8b6a07a Add first real executor dogfood checklist`
- PR handoff:
  `/tmp/agent-taskflow-first-real-dogfood/handoff/AT-DOGFOOD-REAL-001/pr_handoff.json`
- PR handoff markdown:
  `/tmp/agent-taskflow-first-real-dogfood/handoff/AT-DOGFOOD-REAL-001/pr_handoff.md`
- Branch push evidence:
  `/tmp/agent-taskflow-first-real-dogfood/artifacts/AT-DOGFOOD-REAL-001/branch_push.json`
- Draft PR evidence:
  `/tmp/agent-taskflow-first-real-dogfood/handoff/AT-DOGFOOD-REAL-001/draft_pr.json`

## Validation summary

Baseline before dogfood:

- `python -m unittest tests.test_branch_push tests.test_push_task_branch_script -v`
  passed.
- `python3 scripts/run_local_validation.py` passed.

Executor path:

- The first Pi run blocked because pytest was missing in the active `.venv`.
- pytest was installed into `.venv`.
- The same dispatcher path was rerun with Pi.
- The final task reached `waiting_approval`.
- Validator statuses:
  - pytest: passed
  - openspec: skipped

Post-change validation:

- `python -m unittest tests.test_operator_issue_to_draft_pr_runbook -v`
  passed, 22 tests.
- `python -m unittest discover -s tests -v` passed, 1706 tests.
- `python -m compileall agent_taskflow scripts tests` passed.
- `python3 scripts/run_local_validation.py` passed.
- openspec skipped because it was not available on PATH.

## Lessons learned

- The semi-automatic loop is now real, not only fake-gh/local smoke.
- Pi can function as a bounded implementation executor through the
  agent-taskflow flow.
- The environment must be validated before execution; missing pytest caused the
  first run to block.
- Validators should fail fast and preserve evidence rather than silently
  continuing.
- Documentation/test tasks are the right first dogfood target.
- The system correctly kept merge, approval, and cleanup under human control.
- Draft PR creation should remain draft-first and human-reviewed.
- The operator runbook should remain the primary control surface until Mission
  Control exposes the evidence clearly.

## Safety boundaries preserved

- no auto-merge
- no auto-approval
- no cleanup automation
- no force push
- no direct main edit
- no branch/worktree deletion
- executor did not push
- executor did not create PR
- executor did not self-approve
- human review remained final gate

## What worked

- Issue ingestion worked.
- Workspace preparation worked.
- The Pi executor path worked after the environment fix.
- Validators caught missing pytest.
- Review evidence was available.
- PR handoff was generated.
- The branch push foundation worked.
- The draft PR creation foundation worked.
- The final PR remained draft.

## What should improve next

- Add a preflight dependency check for pytest and key validator dependencies.
- Add Mission Control read-only exposure for branch_push, pr_handoff, and
  draft_pr evidence.
- Test OpenCode as the second real executor dogfood.
- Consider making the operator runbook command sequence easier to execute.
- Eventually add queue/polling only after repeated dogfood stability.

## Recommended next phase

Recommended next phase:

`Phase: Real Executor Preflight Dependency Check`

Alternative next phase:

`Phase: Mission Control Evidence Readback for Dogfood Artifacts`

Auto-merge and cleanup should remain deferred. Approval, merge, and workspace
cleanup must continue to require explicit human/operator control.
