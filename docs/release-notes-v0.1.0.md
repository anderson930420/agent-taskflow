# v0.1.0 — Agent Taskflow Governance Pipeline

**Tag:** `v0.1.0` (pending)
**Date:** 2026-05-13
**Status:** Final Release — ready for tagging after this checklist is reviewed

---

## Summary

v0.1.0 establishes agent-taskflow as a governance/control plane for AI coding task execution. It introduces TaskRecord lifecycle management, mission contracts, deterministic validators, review evidence API and UI, Mission Control interactive frontend, browser-compatible CORS, and human-only approval enforcement. This is the first non-prerelease version.

---

## Highlights

### Governance Pipeline
- **TaskRecord lifecycle** — `pending → running → waiting_approval → accepted/rejected`
- **mission_contract.json** — written by Dispatcher before executor runs; carries `forbidden_actions`, `required_validators`, `human_approval_required`, `governance_rules`
- **Forbidden actions** — approve, push, merge, cleanup, delete_worktree, delete_branch, self_approve, force_push
- **Human approval final gate** — every task requires human approval; no worker self-approval
- **No worker self-approval** — `decided_by` must be `"human"`; runtime guard enforces this
- **No push/merge/cleanup automation** — UI has no delete/merge/push action; policy validator scans logs for forbidden action evidence

### Executors
- **Manual executor** — manual task entry
- **Shell executor** — shell command execution
- **OpenCode executor** — OpenCode IDE integration
- **Pi executor** — Pi Agent integration with mission protocol
- **Pi mission protocol adapter** — `pi_protocol.py`, renders `pi_mission_prompt.md` with mission plan
- **Pi mission plan** — `pi_orchestrator.py` with `PiMissionPlan`/`PiMissionStep` frozen dataclasses; 5 deterministic steps: scout → planner → implementer → reviewer → handoff
- **Pi orchestrator is protocol metadata spike only** — not autonomous multi-agent runtime; defines step structure as data and renders into prompt; does not spawn concurrent agents or manage multi-round goal loops

### Validators
- **DEFAULT_VALIDATORS = ("pytest", "openspec")** — always run by default
- **Policy validator** — scans executor logs for forbidden action evidence; skips system-generated governance artifacts (`policy-validate.log`, `pytest.log`, `openspec-validate.log`, `pi_mission_prompt.md`, `pi_mission_plan.json`, `pi-executor.log`)
- **Optional typecheck validator** — runs `python3 -m mypy .`
- **Optional lint validator** — runs `python3 -m ruff check .`; rejects `--fix`/`--write`/`--apply`
- **Command validators use `shell=False`** and command safety checks

### Mission Control UI (Phase 45–49)
- **Interactive state UI** — real-time task status display
- **Task board state grouping** — tasks grouped by status
- **Create task UI** — task creation form
- **Dispatch UI** — executor and validator selection
- **API health indicator** — backend connectivity status
- **Loading/error states** — robust error handling
- **Validator summary card** — validator results display
- **Executor log panel** — log output viewer
- **Artifact review panel** — review evidence section
- **Inline artifact preview** — in-line artifact display
- **Artifact modal** — full artifact viewer
- **MissionContractViewer** — mission contract JSON viewer
- **PiMissionPlanViewer** — Pi mission plan JSON viewer
- **PolicyLogViewer** — policy validator log viewer

### Browser Connectivity
- **Local CORS support** — 127.0.0.1:3001 → 127.0.0.1:8100
- **Allowed local origins only** — no wildcard
- **allow_credentials=False** — safe cross-origin requests

### Human Approval Enforcement (Phase 52–54)
- **ApprovalRequest.decided_by requires "human"** — database-level literal type
- **Runtime guard on approve endpoint** — server rejects non-human identities at runtime
- **Frontend ActionPanel hard-coded** — sends `decided_by: "human"` in approval request
- **Browser approval dogfood completed** — AT-PI-SMOKE-28-R2 transitioned `waiting_approval → accepted` via browser UI click
- **Approval decision recorded** — `decided_by="human"` in approval record

### Smoke / Dogfood Verification
- **Real Pi governance smoke** — AT-PI-SMOKE-28-R2 (Pi executor, policy validator)
- **Review evidence API smoke** — DB verification, artifact preview, secret redaction
- **Isolated v0.1.0-rc1 staging smoke** — tag-verified workflow
- **Dogfood API runner task** — full governance flow verification
- **Browser approval dogfood** — human clicked Approve on AT-PI-SMOKE-28-R2 via Mission Control UI

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
Deterministic validators (pytest, openspec, [policy], [typecheck], [lint])
    ↓
Review Evidence API + Mission Control UI
    ↓
Human approval (final gate)
```

---

## Validation Status

| Check | Result |
|-------|--------|
| Python tests | 815 passed |
| compileall | clean |
| frontend build | clean |
| CORS checks | passed |
| browser UI approval dogfood | passed |
| Real Pi governance smoke | passed (task AT-PI-SMOKE-28-R2, status `accepted`) |
| No direct Pi/OpenCode/Shell execution from UI | enforced |
| No push/merge/cleanup/delete action from UI | enforced |

---

## Default and Optional Validators

**Default validators** (always run unless explicitly overridden):
```
DEFAULT_VALIDATORS = ("pytest", "openspec")
```

**Optional validators** (must be explicitly requested via `--validators`):
- `policy` — governance check (primary for Pi smoke)
- `typecheck` — requires `mypy`
- `lint` — requires `ruff`

Example with optional validators:
```bash
python scripts/run_dispatcher.py --validators policy
python scripts/run_dispatcher.py --validators policy,typecheck,lint
```

---

## What Is Not Included in v0.1.0

- No real multi-Pi runtime
- No autonomous multi-agent execution
- No automatic merge/push/cleanup
- No auth / multi-user permissions
- No deployment packaging
- No formal DB migrations
- No production monitoring
- No responsive/mobile polish guarantee
- No OpenCode/Shell end-to-end production smoke
- No typecheck/lint real task run guarantee

---

## Known Limitations

1. **Pi orchestrator is protocol metadata spike only** — defines step structure as data and renders into the prompt; does not spawn multiple concurrent agents or manage multi-round goal loops.

2. **Policy/typecheck/lint are optional, not default** — `DEFAULT_VALIDATORS` is `("pytest", "openspec")`. Tasks that need governance validation must explicitly request the `policy` validator.

3. **Default active DB may contain selected smoke/demo tasks** depending on runtime setup. Use a dedicated smoke DB for isolated verification.

4. **UI create/dispatch full browser dogfood is not yet as deeply verified** as approval flow. The create/dispatch UI has been smoke-tested but the full end-to-end flow has not been verified with a real browser click.

5. **Auth is absent** — use local-only binding (127.0.0.1) for now. Do not expose to untrusted networks.

6. **Deployment requires manual process management** — no systemd/init scripts, no container orchestration in this release.

7. **Review evidence API DB alignment** — API server reads task state from a SQLite DB. For smoke verification, API server must be started with the same DB used by the task. DB mismatch returns 404 (expected behavior).

---

## Upgrade / Runtime Notes

- Run API: `python scripts/run_api.py --db-path <path>`
- Run Mission Control frontend on `127.0.0.1:3001`
- Ensure API CORS allowed origin matches frontend origin
- For review evidence API, API server must use the same DB as the task
- Frontend connects to `http://127.0.0.1:8100` (API server)

---

## Recommended Next Steps

1. **Create v0.1.0 final tag** after this checklist is reviewed and approved.
2. **Create non-prerelease GitHub Release** from the v0.1.0 tag.
3. **Preserve evidence** (staging clone, R2 evidence, dogfood evidence) until final release is accepted.
4. **After final release**, decide whether to delete staging clone and preserved evidence.
5. **Phase 55+ polish items**:
   - Default executor `pi` in CreateTaskForm
   - UI create/dispatch full browser dogfood verification
   - Responsive/mobile design
   - Accessibility improvements
   - Auth/multi-user design
   - Deployment docs
   - OpenCode/Shell smoke
   - typecheck/lint real run verification