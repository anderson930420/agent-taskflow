# V1-F7 — Practical V1 README reconciliation

## Scope

This documentation-only task adopts and corrects a stopped four-file draft:
`README.md`, `README.zh-TW.md`, `tests/test_readme_current_architecture.py`,
and this handoff. It changes no runtime code, migrations, workflow
configuration, production services, cron, or database state.

The F7 packet explicitly substitutes a claim-to-source factual audit for a new
real-GitHub end-to-end run. This task therefore records the audit and focused
documentation validation; it does not claim a new E2E execution.

## Authority and reconciliation

The governing read-only control records are
`~/agent-taskflow-ops/v1/SPEC.md`, `~/agent-taskflow-ops/v1/RULINGS.md`, and
`~/agent-taskflow-ops/v1/FOLLOWUPS.md`. The higher-level Master Spec and Level 2
Roadmap are design inputs only where consistent with those sources.

The README now reflects these merged facts:

* Validated implementation skips legacy `waiting_approval`, writes
  `ready_for_integration`, and enters the per-repository queue once (F4/F8).
* The integration controller can integrate, validate, push/update a branch, and
  create/update its PR after an explicit confirmed trigger.
* GitHub review and merge are human-only. Taskflow does not issue `gh pr merge`,
  self-approve, force-push a published PR branch, or enable auto-merge.
* F9 remains the missing queue-consumer tick, so there is no non-test caller
  draining `ready_for_integration`; the operator trigger is still required.
* F10 remains the missing invocation/deployment surface for the other
  integration consumers and the parallel execution tick. Ruling 65 records the
  missing-invoker history; FOLLOWUPS.md's execution-vehicle draft proposes the
  cron surface under SPEC §47, without evidence of an installed scheduler.
* F2 remains the deferred repository-wide status-vocabulary and explicit
  migration cleanup. Step 6, F6, and F5 stay after the first real Ticket and
  must be prioritized by observed friction.

The READMEs name the enforcement modules, protected-push and blocked/paused
guards, the V1 handoffs, the operator-recorded capacity of two active executor
leases, its commit-bound evidence gate, and the recorded explicit migrations.
They distinguish the Ticket execution failure vocabulary from integration and
legacy GitHub-issue behavior. They retain canonical ExecutionEngine authority
for confirmed Level 2 execution while keeping it distinct from F8's Ticket
success status and the human merge gate.

## Original stale claims reconciled

The baseline English and Traditional Chinese READMEs shared these stale claims:

| Baseline claim | Reconciliation source |
| --- | --- |
| Current work starts as a GitHub Issue or spec | `agent_taskflow/api/tickets.py` Ticket creation and `ticket_creation.py`; the issue route is legacy. |
| Mission Control is read-only | `agent_taskflow/api/main.py` and `agent_taskflow/api/tickets.py` create and control Tickets. |
| Successful work waits at `waiting_approval` before integration | `ticket_lifecycle.py`, `integration_handoff.py`, and ruling 53: Tickets advance to `ready_for_integration`; legacy issue work retains the old state. |
| Push, draft PR creation, and cleanup are deferred | `integration_controller.py` and `integration_cleanup.py` implement callable, confirmed components; F9/F10 are their missing non-test invocation surfaces. |
| Publication and cleanup are human merge gates | `integration_git.py`, `integration_cleanup.py`, and SPEC §§31–37: merge is human-only; publication/cleanup are Taskflow operations when their confirmed callers run. |
| The legacy one-task scheduler is the current V1 scheduler | `parallel_scheduler.py`, `run_parallel_scheduler_tick.py`, and FOLLOWUPS F10; no V1 scheduler is installed. |

The same source mapping applies to both languages. The full, line-level
claim-to-source audit is preserved in this attempt's builder evidence.

The README test asserts hazardous safety clauses and their table rows rather
than whole sentences. It deliberately does not attempt to infer every possible
natural-language paraphrase; its inversion controls cover the documented merge,
self-approval, auto-merge, force-push, protected-push, and fallback claims in
both languages.

## Builder round 2 repair

Independent review found that the first rewrite's topic-presence checks could
borrow a negation from an adjacent clause. The replacement guards bind each
hazardous merge, self-approval, auto-merge, force-push, protected-push, and
fallback assertion to its own clause or table row in both languages. In-memory
inversion controls demonstrate that each guard rejects its unsafe opposite. The
reviewer's Chinese `gh pr merge` mutation and all five supplied English
mutations now fail the relevant guard while the unmodified README passes.

The repair also corrects the cron citation: ruling 65 records the missing
invoker, while FOLLOWUPS.md's execution-vehicle draft proposes the cron
surface. No runtime behavior changed.

The first builder-r2 standalone mutation-probe log was overwritten when the
corrected `PYTHONPATH=.` run was captured. Its observed import-path failure is
preserved by the orchestrator as
`builder-r2/validation/recovered-initial-mutation-probe.log`; the corrected
probe and expectation records are the validation result.

## Validation

| Check | Command | Result |
| --- | --- | --- |
| README source test | `PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python -m unittest tests.test_readme_current_architecture -v` | PASS — 10 tests |
| compile | `PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python -m compileall -q agent_taskflow tests scripts` | PASS |
| contract validator | `PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python scripts/validate_workflow_contract.py` | PASS |
| policy validator | `PYTHONPATH=. /home/ubuntu/agent-taskflow/.venv/bin/python scripts/validate_workflow_policy.py` | PASS |
| builder round 2 README source test | Same command | PASS — 11 tests |
| builder round 2 reviewer mutation probe | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .../review-r1/mutation_probe.py` | PASS — baseline passes; six supplied unsafe mutations fail their guards |
| builder round 2 compile and workflow validators | Same commands | PASS |
| previous complementary unittest discovery | Prior stopped attempt serial log | PASS — 5390 tests in 719.637s; 8 skipped; recorded exit `0` |
| previous VPS full suite | Prior stopped attempt log | 5384 passed, 8 skipped, 1992 subtests in 306.85s |

The previous pytest log is retained in the preserved origin snapshot. Its
`pytest-full.exit` contains one newline only because the bash capture used
lowercase `pipestatus` instead of uppercase `PIPESTATUS`; the terminal log's
final PASS summary is the available evidence. The first serial unittest capture
was lost, but the second is retained as `validation/full-suite.log`: `Ran 5390
tests in 719.637s` and `OK (skipped=8)`, with `full-suite.exit` equal to `0`.
This builder does not rerun either full suite merely to repair historical
capture artifacts.

## Attempt evidence

The full reconciliation, source audit, validation records, and scope note are at:

```text
/home/ubuntu/agent-taskflow-dev/.agent-taskflow/evidence/V1-F7/vps-native-adoption-20260914-01/builder-r1/
```

Round 2 evidence is separately preserved at:

```text
/home/ubuntu/agent-taskflow-dev/.agent-taskflow/evidence/V1-F7/vps-native-adoption-20260914-01/builder-r2/
```

That evidence records the stopped draft's origin separately from this native
VPS builder run. This run used no SSH and no subagents. No production checkout,
production database, service, or cron entry was modified.

## Remaining gates

The task requires independent review and orchestrator-owned reconciliation and
publication of the reviewed tree. GitHub human review and merge remain separate
final gates; this task must not self-approve, merge, or enable auto-merge.
