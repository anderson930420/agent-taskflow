# Release Readiness: Phase 17–25 Governance Pipeline

**Phase 26 — Audit / Documentation / Branch Consolidation**

This document summarizes the governance pipeline delivered across Phase 17–25
and records the release readiness state of the `phase-17-linear-style-mission-control-ui`
branch.

---

## Phase 17–25 Commit Chain

| Phase | Commit | Subject | Key Files |
|-------|--------|---------|-----------|
| 17 | `a8527ac` | Finalize linear-style Mission Control UI | `mission-control/app/` UI components |
| 18 | `5953720` | Add mission contract artifact schema | `agent_taskflow/mission_contract.py` |
| 19 | `70bd986` | Add policy check validator | `agent_taskflow/validators/policy.py` |
| 20 | `183e839` | Wire mission contract into dispatch flow | `agent_taskflow/dispatcher.py` |
| 21 | `4758f80` | Add typecheck and lint validators | `agent_taskflow/validators/typecheck.py`, `lint.py` |
| 22 | `f7b9c4b` | Add Mission Control review evidence UI | `agent_taskflow/api/review.py`, `schemas.py`, `main.py`; `mission-control/components/ReviewEvidence*.tsx` |
| 23 | `ed01d89` | Add Pi mission protocol adapter | `agent_taskflow/executors/pi_protocol.py` |
| 24 | `82f73c8` | Add Pi mission orchestrator spike | `agent_taskflow/executors/pi_orchestrator.py` |
| 25 | `d978634` | Document Pi governance end-to-end smoke | `docs/pi-governance-e2e-smoke.md` |

**9 commits ahead of `main` (`946477d`).**  
**Diff**: 34 files changed, 9,710 insertions(+), 308 deletions(−).

---

## Architecture Summary

```
TaskRecord (TaskMirrorStore)
  │
  ▼
Dispatcher.dispatch_task()
  │
  ├─→ _write_mission_contract()
  │        └─→ artifact_dir/mission_contract.json
  │
  ├─→ ExecutorContext
  │        └─→ executor.run()
  │              ├─→ [PiExecutor] load_contract_for_pi()
  │              │        ├─→ build_pi_mission_plan() → PiMissionPlan
  │              │        ├─→ write_pi_mission_plan() → artifact_dir/pi_mission_plan.json
  │              │        ├─→ render_pi_mission_prompt() → artifact_dir/pi_mission_prompt.md
  │              │        └─→ pi -p <rendered_prompt> → artifact_dir/pi-executor.log
  │              ├─→ [OpenCodeExecutor] opencode -p <prompt>
  │              ├─→ [ShellExecutor] bash <script>
  │              └─→ [ManualExecutor] no-op
  │
  ├─→ Deterministic Validators (from --validators flag)
  │        ├─→ PytestValidator → artifact_dir/pytest.log
  │        ├─→ OpenspecValidator → artifact_dir/openspec-validate.log
  │        ├─→ PolicyCheckValidator → artifact_dir/policy-validate.log
  │        │        └─ scans mission_contract.json + executor logs for
  │        │           governance violations + secret assignments
  │        ├─→ TypecheckValidator → artifact_dir/typecheck.log
  │        └─→ LintValidator → artifact_dir/lint.log
  │
  └─→ Review Evidence API (read-only)
         ├─→ GET /api/tasks/<key>/review-evidence
         ├─→ GET /api/tasks/<key>/artifacts
         └─→ GET /api/tasks/<key>/artifacts/<name>
                (path traversal blocked, secrets redacted, binary skipped)

Human approval gate
  └─→ Task status: waiting_approval
         └─→ Human accepts → merge
         └─→ Human rejects → revise
```

### Artifacts produced per run

| Artifact | Written By | Required? |
|----------|-----------|----------|
| `mission_contract.json` | `Dispatcher._write_mission_contract()` | **Required** |
| `pi_mission_plan.json` | `PiExecutor.run()` (when contract found) | Conditional |
| `pi_mission_prompt.md` | `PiExecutor.run()` (when contract found) | Conditional |
| `pi-executor.log` | `PiExecutor.run()` | Conditional |
| `pytest.log` | `PytestValidator` | Optional |
| `openspec-validate.log` | `OpenspecValidator` | Optional |
| `policy-validate.log` | `PolicyCheckValidator` | Recommended |
| `typecheck.log` | `TypecheckValidator` | Optional |
| `lint.log` | `LintValidator` | Optional |

---

## Governance Guarantees

> These guarantees are enforced by the system, not by convention.

| Rule | Enforced By | Notes |
|------|-------------|-------|
| Worker may not approve itself | `PolicyCheckValidator` scans executor logs | Any "approve task" evidence in logs → policy fails |
| Worker may not push | `PolicyCheckValidator` scans executor logs | `git push` evidence in logs → policy fails |
| Worker may not merge | `PolicyCheckValidator` scans executor logs | `git merge` / `gh pr merge` evidence → policy fails |
| Worker may not cleanup | `PolicyCheckValidator` scans executor logs | `rm -rf .worktrees` / `cleanup` evidence → policy fails |
| Worker may not delete worktree | `PolicyCheckValidator` scans executor logs | `delete worktree` evidence → policy fails |
| Worker may not delete branch | `PolicyCheckValidator` scans executor logs | `delete branch` evidence → policy fails |
| Deterministic validators cannot be replaced by AI review | `PiMissionPlan` renders validators into prompt; `PolicyCheckValidator` checks they were run | If `--validators` is empty, policy will flag missing validators |
| Human approval is the final gate | `Dispatcher` sets status to `waiting_approval` after validators pass | Only the designated human approver can move status to `approved` |
| Secrets are not exposed in review evidence | `build_artifact_preview()` + `build_review_evidence()` scan content with regex patterns before serving | High-confidence secret assignments are redacted |

### Governance rule enforcement in PolicyCheckValidator

The `PolicyCheckValidator` performs three kinds of checks:

1. **Contract validity** — `mission_contract.json` exists, has valid schema, required fields are non-empty, `forbidden_actions` contains mandatory prohibitions.
2. **Forbidden action evidence** — Executor logs are scanned for patterns like `git push`, `approve task`, `cleanup completed`, `delete worktree`, etc.
3. **Secret assignment detection** — Executor logs are scanned for patterns like `API_KEY=...`, `"api_key": "sk-..."`, `SECRET_TOKEN=...`, etc.

---

## Validator Status

### Default validators (always used by `run_dispatcher.py`)

```
DEFAULT_VALIDATORS = ("pytest", "openspec")
```

These are hard-coded in `dispatcher.py` and cannot be changed by a worker.
Workers cannot remove, reorder, or substitute these validators.

### Optional validators (must be requested via `--validators`)

| Validator | Registration | Blocking? | Requires |
|-----------|-------------|-----------|---------|
| `policy` | `agent_taskflow/validators/registry.py` | No | None (pure Python) |
| `typecheck` | `agent_taskflow/validators/registry.py` | **Yes** if `mypy` not installed | `pip install mypy` |
| `lint` | `agent_taskflow/validators/registry.py` | **Yes** if `ruff` not installed | `pip install ruff` |

To run all validators:
```bash
python scripts/run_dispatcher.py --task-key AT-0001 --validators pytest,openspec,policy,typecheck,lint
```

The policy validator is the primary governance check. It does not call any AI,
does not require network access, and does not depend on mypy or ruff.

---

## Mission Control Status

### Frontend

- **Read-only** task board (`TaskBoard.tsx`)
- **Review evidence section** (`ReviewEvidenceSection.tsx` + `ReviewEvidencePanel.tsx`)
- **No approval actions** in the frontend — human approver uses separate workflow
- **No dispatcher state machine changes** — status transitions are driven by `Dispatcher`
- **No DB schema changes** — `TaskMirrorStore` schema unchanged since Phase 16

### API Endpoints

| Endpoint | Method | Behavior |
|----------|--------|---------|
| `/api/tasks/<key>/review-evidence` | GET | Returns `MissionContractSummary`, list of `ArtifactFileSummary`, `ValidatorResultSummary[]` |
| `/api/tasks/<key>/artifacts` | GET | Lists all files in artifact directory |
| `/api/tasks/<key>/artifacts/<name>` | GET | Returns `ArtifactPreview` with content, kind, truncated flag |

All endpoints are **read-only**. They do not call the dispatcher, do not modify
task state, and do not approve/reject tasks.

### Safety guarantees

- **Path traversal blocked** — artifact names are validated with `Path(name).resolve()`
  and checked against `artifact_dir` via `relative_to()`
- **Binary files skipped** — files with known binary extensions return empty content
- **Secret redaction** — `_SECRET_PATTERNS` regexes scan preview content; if found,
  the content is not included in the response (logged as warning)
- **20 KB preview limit** — files larger than 20 KB are truncated at 20 KB

---

## Pi Status

### PiExecutor (`agent_taskflow/executors/pi.py`)

- Uses `pi -p <prompt_text>` via subprocess with `shell=False`
- Requires `implementation_prompt.md` or rendered `pi_mission_prompt.md`
- When `mission_contract.json` exists in artifact_dir:
  1. `load_contract_for_pi()` reads the contract
  2. `build_pi_mission_plan()` creates a `PiMissionPlan` with 5 steps
  3. `write_pi_mission_plan()` writes `pi_mission_plan.json`
  4. `render_pi_mission_prompt()` embeds the plan as a markdown section
  5. Pi receives the full rendered prompt via `-p`
- When `mission_contract.json` does not exist: falls back to legacy `implementation_prompt.md`

### Pi Mission Plan (`agent_taskflow/executors/pi_orchestrator.py`)

- **5 deterministic steps**: scout → planner → implementer → reviewer → handoff
- Each step has: `step_id`, `role`, `title`, `objective`, `allowed_actions`, `forbidden_actions`, `expected_outputs`
- Top-level plan keys include: `mission_contract`, `artifacts`,
  `required_validators`, `forbidden_actions`, and `human_approval_required`
- Every step includes mandatory forbidden actions: `approve`, `self_approve`, `push`, `force_push`, `merge`, `cleanup`, `delete_worktree`, `delete_branch`
- Plan is deterministic: same contract → same plan (no randomness in step ordering or content)
- This is a **protocol metadata spike**, not an autonomous multi-agent system

### Pi Mission Protocol (`agent_taskflow/executors/pi_protocol.py`)

- `render_pi_mission_prompt()` produces a structured markdown prompt
- Sections: Header, Mission Goal, Working Context, Required Deterministic Validators, Forbidden Actions, Expected Artifacts, Governance Rules, Execution Instructions, Pi Mission Plan (conditional), Original Task Prompt (conditional, secrets redacted)
- **Secret detection** applies only to `original_prompt` field — `goal` is trusted governance metadata and is not scanned

### Real-run status

- **Pi real-run smoke is manual and documented** in `docs/pi-executor-real-run-smoke.md`
- Phase 25 expanded the smoke documentation with `docs/pi-governance-e2e-smoke.md` covering the full governance chain
- No automated CI smoke of real Pi runs — this is intentional (API costs, environment dependency)
- Pi does NOT self-validate, does NOT self-approve, does NOT push/merge/cleanup

---

## Default Validators

```
DEFAULT_VALIDATORS = ("pytest", "openspec")
```

Location: `agent_taskflow/dispatcher.py`, constant `DEFAULT_VALIDATORS`.

- These are the validators used when `--validators` is not specified
- Workers cannot change these defaults
- `policy`, `typecheck`, `lint` are opt-in via `--validators`
- These defaults have not changed since Phase 16

---

## Release Readiness Checklist

| Check | Status | Notes |
|-------|--------|-------|
| Python tests pass | ✅ 564 tests | All phases |
| compileall clean | ✅ | `agent_taskflow`, `scripts`, `tests` |
| frontend build clean | ✅ | `npm run build` in `mission-control/` |
| git status clean | ✅ | No uncommitted changes |
| No DB schema change | ✅ | `TaskMirrorStore` schema unchanged |
| No approval semantic change | ✅ | Status progression unchanged |
| No destructive UI action | ✅ | Frontend is read-only |
| No default validator change | ✅ | `("pytest", "openspec")` unchanged |
| No new executor | ✅ | No new executor added since Phase 16 |
| No new validator in defaults | ✅ | policy/typecheck/lint are opt-in |
| Policy validator does not call AI | ✅ | Pure Python, no network |
| Mission Control API is read-only | ✅ | No dispatch/approve/reject endpoints |
| Pi never approves itself | ✅ | Forbidden in all step configs |
| Pi never pushes/merges/cleans up | ✅ | Forbidden in all step configs |
| Secrets not exposed in API | ✅ | Regex scanning before serving previews |

---

## Recommended Next Phases

### Option A: Manual Pi Governance Smoke Run

Execute the smoke documented in `docs/pi-governance-e2e-smoke.md`:

```bash
# Full smoke run with policy validator only
SMOKE_TASK_KEY="AT-PI-GOV-SMOKE-26"
REPO_ROOT="/home/ubuntu/agent-taskflow"
SMOKE_DB="/tmp/agent-taskflow-pi-gov-smoke-26.db"
SMOKE_ARTIFACT_DIR="/tmp/agent-taskflow-pi-gov-artifacts/$SMOKE_TASK_KEY"
SMOKE_WORKTREE="$REPO_ROOT/.worktrees/$SMOKE_TASK_KEY"

rm -rf "$SMOKE_WORKTREE" "$SMOKE_ARTIFACT_DIR" "$SMOKE_DB"

python scripts/create_pi_smoke_task.py \
  --task-key "$SMOKE_TASK_KEY" --db-path "$SMOKE_DB" \
  --repo-path "$REPO_ROOT" --artifact-root "/tmp/agent-taskflow-pi-gov-artifacts"

uvicorn agent_taskflow.api.main:app --host 127.0.0.1 --port 8100 &
sleep 3

python scripts/run_dispatcher.py \
  --task-key "$SMOKE_TASK_KEY" --db-path "$SMOKE_DB" \
  --validators policy

# Verify artifacts and API response
curl -s http://127.0.0.1:8100/api/tasks/$SMOKE_TASK_KEY/review-evidence | python3 -m json.tool

pkill -f "uvicorn agent_taskflow.api.main:app" || true
rm -rf "$SMOKE_WORKTREE" "$SMOKE_ARTIFACT_DIR" "$SMOKE_DB"
git status --short
```

### Option B: Merge Branch to Main

The branch is ready for merge. To consolidate:

```bash
git checkout main
git pull origin main
git merge phase-17-linear-style-mission-control-ui
# Resolve conflicts if any (expected: minimal)
git push origin main
```

### Option C: Operational Packaging / Deployment Docs

Add deployment documentation for:
- Installing agent-taskflow in a new environment
- Configuring `~/.config/pi-agent/env` for Pi provider
- Running the Mission Control API in production
- Setting up the frontend with `npm run build`

### Option D: Real-World Dogfood Task

Run a real task through the system using Pi executor:
1. Create a task for an actual codebase improvement
2. Dispatch with `python scripts/run_dispatcher.py --validators pytest,openspec,policy`
3. Observe the full artifact chain
4. Use the review evidence API to verify artifact contents
5. Perform human approval manually

---

## Phase 17–25 Key Files by Category

### Core governance
- `agent_taskflow/dispatcher.py` — task dispatch, mission contract write, validator orchestration
- `agent_taskflow/mission_contract.py` — contract schema, read/write/validate functions
- `agent_taskflow/validators/policy.py` — governance enforcement validator

### Pi executor chain
- `agent_taskflow/executors/pi.py` — PiExecutor backend
- `agent_taskflow/executors/pi_protocol.py` — prompt rendering with plan embedding
- `agent_taskflow/executors/pi_orchestrator.py` — deterministic mission plan builder

### Deterministic validators
- `agent_taskflow/validators/pytest.py`
- `agent_taskflow/validators/openspec.py`
- `agent_taskflow/validators/typecheck.py` (opt-in)
- `agent_taskflow/validators/lint.py` (opt-in)
- `agent_taskflow/validators/command.py` — shared subprocess helper

### Mission Control (read-only API + UI)
- `agent_taskflow/api/main.py` — FastAPI app, endpoints
- `agent_taskflow/api/review.py` — read-only review evidence helpers
- `agent_taskflow/api/schemas.py` — Pydantic models for API responses
- `mission-control/app/tasks/[taskKey]/page.tsx` — task detail page
- `mission-control/components/ReviewEvidenceSection.tsx` — interactive evidence panel
- `mission-control/components/ReviewEvidencePanel.tsx` — server-compatible display component

### Documentation
- `docs/mission-contract.md` — contract schema documentation
- `docs/pi-executor-real-run-smoke.md` — manual Pi real-run smoke
- `docs/pi-governance-e2e-smoke.md` — full governance chain smoke procedure

### Scripts
- `scripts/create_pi_smoke_task.py` — smoke task creation helper
- `scripts/run_dispatcher.py` — CLI dispatch entry point

---

## What Has NOT Been Changed

The following have remained **unchanged since Phase 16**:

- `DEFAULT_VALIDATORS` = `("pytest", "openspec")`
- `Dispatcher` state machine — status progression: `queued` → `preparing` → `implementing` → `validating` → `waiting_approval`
- `TaskMirrorStore` DB schema
- Approval semantics — human approval is the final gate; no automated approval
- Mission Control frontend — no approval UI, no destructive actions
- `Executor` base class interface
- `Validator` base class interface

---

*Generated for Phase 26 release readiness audit. Branch: `phase-17-linear-style-mission-control-ui`.*
