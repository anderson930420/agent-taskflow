# v0.1.0-rc1 — Governance Pipeline Release Candidate

**Tag:** `v0.1.0-rc1`
**Commit:** `2039aab954364154ae160c4fa4c5848344e4c619`
**Date:** 2026-05-13
**Status:** Release Candidate — not for production use without further validation

---

## Summary

This is the first governance pipeline release candidate for agent-taskflow.
It introduces mission contracts, policy validation, Pi mission protocol artifacts,
review evidence API and UI, and a verified real Pi governance smoke path.

agent-taskflow is the governance and control plane. Pi, OpenCode, Shell, and
Manual are executor backends only. Deterministic validators are the actual
governance gate. Human approval is the final gate.

---

## What's New (Phases 17–32)

### Mission Contract
- `mission_contract.json` artifact written by `Dispatcher._write_mission_contract()`
  before executor runs
- Schema with `forbidden_actions`, `required_validators`, `human_approval_required`,
  `governance_rules`
- 8 required forbidden actions: `approve`, `push`, `merge`, `cleanup`,
  `delete_worktree`, `delete_branch`, `self_approve`, `force_push`

### Policy Validator
- `PolicyCheckValidator` — scans executor logs for forbidden action evidence
- Scans for high-confidence secret assignments
- Skips `policy-validate.log`, `pytest.log`, `openspec-validate.log`,
  `pi_mission_prompt.md`, `pi_mission_plan.json`, `pi-executor.log`
  (system-generated governance content, not worker violations)

### Typecheck and Lint Validators
- `TypecheckValidator` — runs `python3 -m mypy .` by default
- `LintValidator` — runs `python3 -m ruff check .` by default, rejects
  `--fix`/`--write`/`--apply`
- Both are opt-in; `DEFAULT_VALIDATORS` remains `("pytest", "openspec")`

### Pi Mission Protocol
- `pi_protocol.py` — renders `pi_mission_prompt.md` with mission plan section
- `pi_orchestrator.py` — `PiMissionPlan` and `PiMissionStep` frozen dataclasses
- 5 deterministic steps: `scout` → `planner` → `implementer` → `reviewer` → `handoff`
- Every step carries required forbidden actions
- `PiExecutor.run()` builds mission plan → writes `pi_mission_plan.json` →
  renders prompt with `mission_plan=` parameter

### Review Evidence API
- `GET /api/tasks/<task_key>/review-evidence` — full evidence bundle
- `GET /api/tasks/<task_key>/artifacts/<name>` — individual artifact preview
- Secret detection and redaction, binary skip, 20KB preview limit
- Path traversal prevention
- `GET /api/tasks/<task_key>/review-evidence` and `GET /api/tasks/<task_key>/artifacts/<name>`

### Mission Control UI
- `ReviewEvidenceSection.tsx` — `"use client"` component for review evidence display
- `ReviewEvidencePanel.tsx` — server-compatible display component

### Smoke Documentation
- `docs/pi-governance-e2e-smoke.md` — full smoke workflow (20KB)
- `docs/release-readiness-phase-17-25.md` — architecture summary, commit chain table
- `docs/pi-executor-real-run-smoke.md` — executor-specific smoke doc
- `tests/test_smoke_governance.py` — 39 governance doc tests

---

## Architecture

```
TaskRecord (in mirror DB)
    ↓
mission_contract.json (written by Dispatcher)
    ↓
pi_mission_plan.json (written by PiExecutor)
    ↓
pi_mission_prompt.md (rendered by PiExecutor with mission_plan=)
    ↓
PiExecutor / executor backend (Pi, OpenCode, Shell, Manual)
    ↓
Worker artifacts and executor logs
    ↓
Deterministic validators (pytest, openspec, policy, typecheck, lint)
    ↓
Review Evidence API + Mission Control UI
    ↓
Human approval (final gate)
```

---

## Governance Guarantees

- agent-taskflow is the governance/control plane
- Pi, OpenCode, Shell, Manual are executor backends only
- Worker cannot approve tasks
- Worker cannot self-approve
- Worker cannot push to remote branches
- Worker cannot force-push
- Worker cannot merge pull requests
- Worker cannot cleanup worktrees
- Worker cannot delete worktrees or branches
- AI reviewer/auditor cannot replace deterministic validators
- Deterministic validators remain required regardless of executor output
- Human approval is the final gate

---

## Validation Status

| Check | Result |
|-------|--------|
| Python tests | 620 passed |
| compileall | clean |
| frontend build | clean |
| Real Pi governance smoke | passed (task `AT-PI-SMOKE-28-R2`, status `waiting_approval`) |
| Review evidence API against smoke DB | passed |
| Artifact preview endpoints | passed (pi_mission_prompt.md, pi_mission_plan.json, policy-validate.log) |
| Secret leak scan | passed (no secrets in API response) |
| Worker forbidden action scan | passed (no approve/push/merge/cleanup evidence in logs) |
| Policy validator false positive fix | passed |

---

## Default and Optional Validators

**Default validators** (always run unless explicitly overridden):
```
DEFAULT_VALIDATORS = ("pytest", "openspec")
```

**Optional validators** (must be explicitly requested):
- `policy` — governance check (primary for Pi smoke)
- `typecheck` — requires `mypy`
- `lint` — requires `ruff`

Example with optional validators:
```bash
python scripts/run_dispatcher.py --validators policy
python scripts/run_dispatcher.py --validators policy,typecheck,lint
```

---

## What Is Not Included

- No real multi-Pi integration
- No autonomous multi-agent execution
- No worker self-approval
- No automatic merge, push, or cleanup
- No GitHub Release automation
- No DB schema changes
- No approval semantic changes
- No changes to `DEFAULT_VALIDATORS`
- No changes to dispatcher state machine

---

## Known Limitations

1. **Pi mission orchestrator is a protocol metadata spike**, not a true
   multi-agent runtime. It defines step structure as data and renders it into
   the prompt; it does not spawn multiple concurrent agents or manage multi-round
   goal loops.

2. **Policy/typecheck/lint are opt-in**, not default. The default validator set
   is `("pytest", "openspec")`. Tasks that need governance validation must
   explicitly request the `policy` validator.

3. **Review evidence API DB alignment**: The API server reads task state from
   a SQLite DB. For smoke verification, the API server must be started with the
   same DB used by the smoke task. If it uses the default DB, endpoints return 404.
   This is expected DB mismatch behavior, not an API failure.

4. **Real-world dogfood tasks** should be run after this RC to validate the full
   governance pipeline on real tasks before production use.

---

## Suggested Next Steps

1. **Create GitHub Release** from this RC, attach these release notes.
2. **Tag staging smoke** — run `docs/pi-governance-e2e-smoke.md` from the `v0.1.0-rc1`
   tag to verify the tagged version is self-consistent.
3. **Dogfood a real task** through the full governance pipeline (create task →
   dispatcher → Pi executor → policy validator → review evidence → human approval).
4. **Add deployment/packaging docs** if this will be used on another machine.
5. **Consider `v0.1.0` release** after dogfood confirms the pipeline works on real tasks.
