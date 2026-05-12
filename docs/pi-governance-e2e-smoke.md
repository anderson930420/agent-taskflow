# Pi Governance End-to-End Smoke

**Phase 25 — Smoke / Audit / Stabilization**

This document describes how to run a complete, repeatable end-to-end smoke test that
verifies the agent-taskflow governance chain using the PiExecutor backend. It covers
the full artifact lifecycle, policy validation, and review evidence API — not just
the executor path.

Use this document to verify the system is working correctly after any change to
the dispatcher, PiExecutor, mission contract, Pi orchestrator, policy validator,
or review evidence API.

---

## What This Smoke Tests

The smoke run exercises this complete chain:

```
create_pi_smoke_task.py
  → TaskMirrorStore (TaskRecord + TaskWorktreeRecord in temp DB)
  → run_dispatcher.py --validators pytest,openspec,policy
       → Dispatcher._write_mission_contract()
            → artifact_dir/mission_contract.json
       → PiExecutor.run()
            → load_contract_for_pi() → reads mission_contract.json
            → build_pi_mission_plan() → PiMissionPlan
            → write_pi_mission_plan() → artifact_dir/pi_mission_plan.json
            → render_pi_mission_prompt() with mission_plan → artifact_dir/pi_mission_prompt.md
            → Pi command: pi -p <rendered_prompt>
            → artifact_dir/pi-executor.log
       → PolicyCheckValidator.run()
            → checks artifact_dir/mission_contract.json
            → scans executor logs for forbidden actions
            → scans executor logs for secret assignments
            → artifact_dir/policy-validate.log
       → Review evidence API
            → GET /api/tasks/<task_key>/review-evidence
            → GET /api/tasks/<task_key>/artifacts/<name>
```

**What is NOT tested by this smoke:**

- Pi is not a governance layer — it is an executor backend only.
- Deterministic validators (pytest, openspec, policy, typecheck, lint) are
  the actual governance gate, not Pi.
- Human approval is the final gate — Pi never approves.
- No push / merge / cleanup / delete_branch / delete_worktree actions are
  executed or allowed.

---

## Safety Rules

> **Do not skip these rules.**

- Run only from a clean `git status`.
- Never run the smoke task against the main repository checkout.
- Use a dedicated worktree under `.worktrees/<task-key>`.
- Use a temporary or backed-up state database (not the production DB).
- Do not put API keys in repo files.
- Load provider secrets only via `source ~/.config/pi-agent/env`.
- Do not approve, merge, push, or cleanup as part of this smoke.
- Stop stuck `pi` processes before continuing.
- Remove all smoke artifacts when done.

---

## Prerequisites

Verify the environment before starting:

```bash
cd /home/ubuntu/agent-taskflow
git status --short
source .venv/bin/activate
source ~/.config/pi-agent/env
pi --version
python -m compileall agent_taskflow scripts tests
```

Expected `git status --short` is empty before and after the smoke run.

---

## Smoke Environment Variables

Set these once for the entire smoke workflow. Replace `/tmp/...` paths as needed.

```bash
export SMOKE_TASK_KEY="AT-PI-GOV-SMOKE"
export REPO_ROOT="/home/ubuntu/agent-taskflow"
export SMOKE_DB="/tmp/agent-taskflow-pi-gov-smoke.db"
export SMOKE_WORKTREE="$REPO_ROOT/.worktrees/$SMOKE_TASK_KEY"
export SMOKE_ARTIFACT_DIR="/tmp/agent-taskflow-pi-gov-artifacts/$SMOKE_TASK_KEY"
```

**Do not reuse task keys** — each smoke run should use a fresh key.

---

## Step 1 — Create the Smoke Task

Clean up any previous smoke artifacts first:

```bash
rm -rf "$SMOKE_WORKTREE"
rm -rf "$SMOKE_ARTIFACT_DIR"
rm -f "$SMOKE_DB"
```

Create the task and its directories:

```bash
python scripts/create_pi_smoke_task.py \
  --task-key "$SMOKE_TASK_KEY" \
  --db-path "$SMOKE_DB" \
  --repo-path "$REPO_ROOT" \
  --artifact-root "/tmp/agent-taskflow-pi-gov-artifacts"
```

Expected JSON output:
```json
{
  "task_key": "AT-PI-GOV-SMOKE",
  "worktree_path": ".../.worktrees/AT-PI-GOV-SMOKE",
  "artifact_dir": "/tmp/agent-taskflow-pi-gov-artifacts/AT-PI-GOV-SMOKE",
  "executor": "pi",
  "next_dispatch_command": "python scripts/run_dispatcher.py --task-key AT-PI-GOV-SMOKE --db-path /tmp/agent-taskflow-pi-gov-smoke.db"
}
```

This script:
- Creates `.worktrees/<task-key>` and artifact directory.
- Writes `implementation_prompt.md` with a minimal smoke task.
- Inserts `TaskRecord` and `TaskWorktreeRecord` into the mirror DB.
- Does **not** call Pi, MiniMax, or any LLM provider.

---

## Step 2 — Start the Review Evidence API Server

The review evidence API must be running to verify artifact contents and
secret redaction. The API server reads task state from a SQLite DB —
**it must use the same DB as the smoke task and dispatcher**, otherwise the
review evidence endpoint will return 404 because the task will not exist
in the API server's DB.

### DB Path Alignment

The smoke workflow uses three separate processes, each with its own DB context:

| Process | Default DB | Smoke Workflow DB |
|---------|-----------|-------------------|
| `create_pi_smoke_task.py` | — | `--db-path $SMOKE_DB` |
| `run_dispatcher.py` | — | `--db-path $SMOKE_DB` |
| API server | `~/.agent-taskflow/state.db` | **must also use `$SMOKE_DB`** |

If the API server starts with its default DB, it will not contain the smoke
task record, and every review evidence endpoint will return 404. This is
expected behavior — it indicates a DB path mismatch, not an API or artifact
failure. The smoke report must record `SMOKE_DB` so future verification
runs can align the API server to the same DB.

### Starting the API Server with the Smoke DB

The API app factory accepts a `db_path` argument. Use either the official runner
script or the inline Python approach.

#### Option 1: Using the official runner script (recommended)

```bash
SMOKE_DB="/tmp/agent-taskflow-pi-gov-smoke.db"

python scripts/run_api.py \
  --db-path "$SMOKE_DB" \
  --host 127.0.0.1 \
  --port 8100 \
  --log-level warning
```

The runner script accepts these arguments:
- `--db-path` (required): Absolute path to the SQLite state DB
- `--host` (default: 127.0.0.1): Host to bind the server to
- `--port` (default: 8100): Port to bind the server to
- `--log-level` (default: warning): Uvicorn log level

#### Option 2: Using inline Python

```bash
SMOKE_DB="/tmp/agent-taskflow-pi-gov-smoke.db"

python3 - <<'PY'
import uvicorn, sys
sys.path.insert(0, '/home/ubuntu/agent-taskflow')
from agent_taskflow.api.main import create_app

app = create_app(db_path="$SMOKE_DB")
uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
PY
```

Verify it is running:

```bash
curl -s http://127.0.0.1:8100/health | python3 -m json.tool
# Expected: {"status":"ok","service":"agent-taskflow-api"}
```

Keep the server running throughout the smoke run. Stop it with:

```bash
pkill -f "uvicorn.*8100" || true
```

---

## Step 3 — Run the Dispatcher

Run the dispatcher with the governance validator chain:

```bash
python scripts/run_dispatcher.py \
  --task-key "$SMOKE_TASK_KEY" \
  --db-path "$SMOKE_DB" \
  --validators pytest,openspec,policy
```

### Validator Notes

- **pytest** — Expects a full repository. May skip or fail in the minimal
  smoke worktree. This is acceptable for governance smoke — the purpose is
  to verify the dispatch chain, not full project validation.
- **openspec** — May skip when no `openspec/` directory exists. Acceptable.
- **policy** — Runs against the artifact directory. This is the key governance
  validator for this smoke. It checks:
  - `mission_contract.json` exists and is valid
  - `forbidden_actions` contains mandatory governance prohibitions
  - Executor logs do not contain evidence of forbidden actions
  - Executor logs do not contain high-confidence secret assignments
- **typecheck** — Blocked if `mypy` is not installed. Informational only.
- **lint** — Blocked if `ruff` is not installed. Informational only.

If you want to include typecheck/lint in the smoke run, install the tools first:

```bash
pip install mypy ruff
```

If the tools are not installed, they will report `blocked` status. The smoke
still passes governance verification as long as policy passes.

---

## Step 4 — Verify Artifact Chain

After dispatch completes, check the artifact directory:

```bash
find "$SMOKE_ARTIFACT_DIR" -maxdepth 2 -type f | sort
```

Expected files (required unless marked optional):

```
/tmp/agent-taskflow-pi-gov-artifacts/AT-PI-GOV-SMOKE/
├── implementation_prompt.md           (written by create_pi_smoke_task.py)
├── mission_contract.json              (written by Dispatcher._write_mission_contract)
├── pi_mission_plan.json               (written by PiExecutor.run when contract exists)
├── pi_mission_prompt.md               (written by PiExecutor.run when contract exists)
├── pi-executor.log                    (written by PiExecutor.run)
├── policy-validate.log                (written by PolicyCheckValidator)
├── pytest.log                         (optional — may be absent or failed)
└── openspec-validate.log              (optional — may be absent or skipped)
```

### mission_contract.json

Verify the contract is present and valid:

```bash
python3 -c "
import json, sys
d = json.load(open('$SMOKE_ARTIFACT_DIR/mission_contract.json'))
print('schema_version:', d.get('schema_version'))
print('task_key:', d.get('task_key'))
print('executor:', d.get('executor'))
print('required_validators:', d.get('required_validators'))
print('human_approval_required:', d.get('human_approval_required'))
forbidden = d.get('forbidden_actions', [])
required = {'approve','push','merge','cleanup','delete_worktree','delete_branch','self_approve','force_push'}
missing = required - set(forbidden)
print('missing_required_forbidden:', missing if missing else 'none')
"
```

Expected output: no `missing_required_forbidden`.

### pi_mission_plan.json

Verify the mission plan exists when the contract was found:

```bash
test -f "$SMOKE_ARTIFACT_DIR/pi_mission_plan.json" && echo "EXISTS" || echo "MISSING (legacy fallback used)"
```

If present, verify it:

```bash
python3 -c "
import json
d = json.load(open('$SMOKE_ARTIFACT_DIR/pi_mission_plan.json'))
print('schema_version:', d.get('schema_version'))
print('task_key:', d.get('task_key'))
print('step_count:', len(d.get('steps', [])))
print('step_ids:', [s['step_id'] for s in d.get('steps', [])])
"
```

Expected: 5 steps with IDs `scout`, `planner`, `implementer`, `reviewer`, `handoff`.

### pi_mission_prompt.md

Verify the prompt contains the mission plan section:

```bash
grep -c "Pi Mission Plan" "$SMOKE_ARTIFACT_DIR/pi_mission_prompt.md"
grep -c "scout\|planner\|implementer\|reviewer\|handoff" "$SMOKE_ARTIFACT_DIR/pi_mission_prompt.md"
```

Expected: both grep commands return non-zero counts.

### pi-executor.log

Check the executor log exists and is non-empty:

```bash
test -f "$SMOKE_ARTIFACT_DIR/pi-executor.log" && wc -l "$SMOKE_ARTIFACT_DIR/pi-executor.log" || echo "MISSING"
```

### policy-validate.log

Check the policy validator ran:

```bash
test -f "$SMOKE_ARTIFACT_DIR/policy-validate.log" && cat "$SMOKE_ARTIFACT_DIR/policy-validate.log" || echo "MISSING"
```

Expected: status should be `passed`. Any warnings about missing forbidden
actions or secret detections should be documented and reviewed.

---

## Review Evidence API DB Alignment

The review evidence API (`GET /api/tasks/<task_key>/review-evidence` and
`GET /api/tasks/<task_key>/artifacts/<name>`) reads task state from the
configured SQLite DB. For the smoke verification to work, the API server
**must be started with the same DB** used by the smoke task and dispatcher.

### Why 404 Happens

If the API server uses its default DB (`~/.agent-taskflow/state.db`) while
the smoke task was created with `$SMOKE_DB`, the review evidence endpoint
will return 404 because the task record does not exist in the default DB.
This is the expected result of a DB path mismatch — not an artifact failure
or an API bug.

The smoke report must record these three values for any post-smoke verification:

```bash
echo "SMOKE_TASK_KEY=$SMOKE_TASK_KEY"
echo "SMOKE_ARTIFACT_DIR=$SMOKE_ARTIFACT_DIR"
echo "SMOKE_DB=$SMOKE_DB"
```

### Successful Response Indicators

When the API server is aligned to the correct DB, the review evidence endpoint
returns:

```json
{
  "task_key": "<SMOKE_TASK_KEY>",
  "mission_contract": {
    "exists": true,
    "status": "present",
    "human_approval_required": true,
    "forbidden_actions": ["approve", "push", "merge", "cleanup", ...]
  },
  "artifacts": [
    {"name": "mission_contract.json", "kind": "mission_contract"},
    {"name": "pi_mission_plan.json", "kind": "other"},
    {"name": "pi_mission_prompt.md", "kind": "other"},
    {"name": "pi-executor.log", "kind": "executor_log"},
    {"name": "policy-validate.log", "kind": "validator_log"}
  ],
  "validator_results": [{"validator": "policy", "status": "passed", ...}],
  "policy_status": "passed",
  "policy_warnings": []
}
```

Key indicators of a successful governance smoke:

- `mission_contract.status` = `present`
- `mission_contract.human_approval_required` = `true`
- `artifacts` includes `pi_mission_plan.json`, `pi_mission_prompt.md`,
  `pi-executor.log`, `policy-validate.log`
- `validator_results` includes policy `passed`
- `policy_status` = `passed`
- No secret-like values in the response body

### Curl Verification Commands

```bash
TASK_KEY="$SMOKE_TASK_KEY"

# Full review evidence
curl -s "http://127.0.0.1:8100/api/tasks/${TASK_KEY}/review-evidence" | python3 -m json.tool

# Artifact previews
curl -s "http://127.0.0.1:8100/api/tasks/${TASK_KEY}/artifacts/pi_mission_prompt.md" | python3 -m json.tool
curl -s "http://127.0.0.1:8100/api/tasks/${TASK_KEY}/artifacts/pi_mission_plan.json" | python3 -m json.tool
curl -s "http://127.0.0.1:8100/api/tasks/${TASK_KEY}/artifacts/policy-validate.log" | python3 -m json.tool
```

---

## Step 5 — Verify Review Evidence API

### 5a — Get Full Review Evidence

```bash
curl -s http://127.0.0.1:8100/api/tasks/$SMOKE_TASK_KEY/review-evidence \
  | python3 -m json.tool
```

Expected fields in the response:

```json
{
  "task_key": "AT-PI-GOV-SMOKE",
  "mission_contract": {
    "exists": true,
    "status": "present"
  },
  "artifact_files": [
    {"name": "mission_contract.json", "kind": "mission_contract"},
    {"name": "pi_mission_plan.json", "kind": "other"},
    {"name": "pi_mission_prompt.md", "kind": "other"},
    {"name": "pi-executor.log", "kind": "executor_log"},
    {"name": "policy-validate.log", "kind": "validator_log"}
  ],
  "validator_results": [...]
}
```

### 5b — Verify Secret Redaction in Evidence

The review evidence API must not expose secret-like values. Run this check:

```bash
curl -s http://127.0.0.1:8100/api/tasks/$SMOKE_TASK_KEY/review-evidence \
  | python3 -c "
import json, sys, re
data = json.load(sys.stdin)
text = json.dumps(data)
secrets_found = []
patterns = [
    r'[A-Z_][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*[:=]',
    r'(?:api_key|token|secret)\s*=\s*[\"\']?(?:sk-|ak-)[A-Za-z0-9_-]{10,}',
]
for pat in patterns:
    if re.search(pat, text, re.IGNORECASE):
        secrets_found.append(pat)
if secrets_found:
    print('SECRETS DETECTED IN API RESPONSE:', secrets_found)
    sys.exit(1)
else:
    print('No secrets exposed in review evidence API response')
"
```

Expected: "No secrets exposed in review evidence API response"

### 5c — Preview pi_mission_prompt.md via API

```bash
curl -s http://127.0.0.1:8100/api/tasks/$SMOKE_TASK_KEY/artifacts/pi_mission_prompt.md \
  | python3 -m json.tool
```

Expected: `truncated: false` (file is small), `content` is the rendered prompt.

### 5d — Preview policy-validate.log via API

```bash
curl -s http://127.0.0.1:8100/api/tasks/$SMOKE_TASK_KEY/artifacts/policy-validate.log \
  | python3 -m json.tool
```

Expected: `kind: "validator_log"`, `content` contains the policy validation result.

### 5e — Preview pi_mission_plan.json via API

```bash
curl -s http://127.0.0.1:8100/api/tasks/$SMOKE_TASK_KEY/artifacts/pi_mission_plan.json \
  | python3 -m json.tool
```

Expected: `kind: "other"`, `content` contains valid JSON with 5 steps.

### 5f — List All Artifacts via API

```bash
curl -s http://127.0.0.1:8100/api/tasks/$SMOKE_TASK_KEY/artifacts \
  | python3 -m json.tool
```

Expected: A list of all artifact files with their names and kinds.

---

## Step 6 — Verify Governance Constraints

### Pi Is Not a Governance Layer

Pi is an **executor backend** — it executes the task as described in the rendered
prompt. It does not:

- Approve or reject tasks
- Validate code or policies
- Push, merge, or cleanup
- Make governance decisions

The governance chain is:

1. **Deterministic validators** (pytest, openspec, policy, typecheck, lint) —
   these are the automated governance checks.
2. **PolicyCheckValidator** — checks the mission contract and executor artifacts
   for governance violations and secret leaks.
3. **Human approval** — only the designated human approver can approve.
   Pi never approves.

This is confirmed by the policy validator log: if the executor log contains
evidence of forbidden actions (approve, push, merge, cleanup, etc.), the policy
validator will fail.

### Deterministic Validators Remain Required

AI reviews and mission loops cannot replace deterministic validators. Even if
Pi produces "perfect" code, the deterministic validators must still pass before
human approval is requested.

### No Push / Merge / Cleanup

The smoke task is designed to be minimal. The prompt tells Pi to write a single
file. It does not push, merge, or clean up anything. After the smoke run:

```bash
git status --short
# Must show no changes to the main repository.
```

### Human Approval Is the Final Gate

The dispatcher sets the task to `waiting_approval` after successful executor
and validator runs. Human approval is required before any code is merged.
Pi never approves itself.

---

## Step 7 — Cleanup

Do not skip cleanup. The smoke DB and artifact directories contain task state
and logs that should not be left behind.

### 7a — Stop the API Server

```bash
pkill -f "uvicorn agent_taskflow.api.main:app" || true
```

### 7b — Stop Any Stuck Pi Processes

```bash
ps -f -u "$USER" | grep -E '[p]i|[n]ode|[p]ython.*uvicorn'
# Identify any stuck processes from the smoke run.
# Kill only smoke-related processes:
pkill -f "MiniMax-M2.7" || true
pkill -f "AT-PI-GOV-SMOKE" || true
sleep 2
pkill -9 -f "MiniMax-M2.7" || true
```

### 7c — Remove Smoke Artifacts

```bash
rm -rf "$SMOKE_WORKTREE"
rm -rf "$SMOKE_ARTIFACT_DIR"
rm -f "$SMOKE_DB"
```

### 7d — Verify Clean Repo State

```bash
cd /home/ubuntu/agent-taskflow
git status --short
```

Expected: empty output. All smoke artifacts must be outside the repo or in
temporary paths that are now deleted.

---

## Troubleshooting

### No API key found

```bash
source ~/.config/pi-agent/env
test -n "$MINIMAX_API_KEY" && echo "MINIMAX_API_KEY is set"
```

Do not write the key into repo files.

### Pi hangs

Use another SSH session:

```bash
ps -f -u "$USER" | grep -E '[p]i|[n]ode|[p]ython'
```

Terminate the smoke Pi process only:

```bash
kill <PID>
sleep 2
kill -9 <PID>
```

### mission_contract.json not written

The contract is written by `Dispatcher._write_mission_contract()` in the
`preparing` phase before the executor runs. If the contract is missing, the
dispatcher may have failed before the `preparing` phase (e.g., task status
not `queued`, governance validation failed, etc.).

Check the task status:

```bash
python3 -c "
from agent_taskflow.store import TaskMirrorStore
from pathlib import Path
store = TaskMirrorStore(Path('$SMOKE_DB'))
task = store.get_task('$SMOKE_TASK_KEY')
print('status:', task.status if task else 'NOT FOUND')
"
```

### pi_mission_plan.json not generated

The mission plan is generated by `PiExecutor.run()` when `mission_contract.json`
is found. If `pi_mission_plan.json` is missing but `mission_contract.json` is
present, check:

1. The executor log for errors
2. Whether `load_contract_for_pi()` found the contract
3. Whether `build_pi_mission_plan()` raised an exception

### Policy validator fails

Policy validator checks for:
- Missing required forbidden actions in the contract
- Evidence of forbidden actions in executor logs
- High-confidence secret assignments in executor logs

Review the policy validator log to identify the failure reason:

```bash
cat "$SMOKE_ARTIFACT_DIR/policy-validate.log"
```

### Review evidence API returns 404

The API server must be running at `http://127.0.0.1:8100`. Start it:

```bash
python scripts/run_api.py --db-path "$SMOKE_DB" --host 127.0.0.1 --port 8100 --log-level warning &
sleep 3
```

Or using the inline approach:

```bash
uvicorn agent_taskflow.api.main:app --host 127.0.0.1 --port 8100 &
sleep 3
```

Also verify the task exists and has an `artifact_dir`:

```bash
python3 -c "
from agent_taskflow.store import TaskMirrorStore
from pathlib import Path
store = TaskMirrorStore(Path('$SMOKE_DB'))
task = store.get_task('$SMOKE_TASK_KEY')
print('task_key:', task.task_key if task else 'NOT FOUND')
print('artifact_dir:', task.artifact_dir if task else 'N/A')
print('status:', task.status if task else 'N/A')
"
```

### Artifact preview returns 404 or 500

Check that the artifact file exists:

```bash
ls -la "$SMOKE_ARTIFACT_DIR/"
```

If a file exists but the API returns 404, the file name may contain path
traversal characters or the API server may have an issue. Path traversal
is blocked by the API (using `relative_to()` checks).

### pytest / openspec validator fails or skips

This is acceptable for governance smoke. The purpose of this smoke is to verify
the governance chain (contract, plan, prompt, policy validation, review evidence),
not full project validation. If full project validation is needed, run the smoke
in a full repository checkout with proper test infrastructure.

---

## Expected Smoke Summary

After a successful smoke run, you should have verified:

| Check | Expected Result |
|-------|-----------------|
| `mission_contract.json` exists | Yes — written by dispatcher |
| `mission_contract.json` valid JSON | Yes |
| Contract has required forbidden actions | Yes — approve, push, merge, etc. |
| Contract has `human_approval_required: true` | Yes |
| `pi_mission_plan.json` exists | Yes — when contract found |
| `pi_mission_plan.json` has 5 steps | scout, planner, implementer, reviewer, handoff |
| `pi_mission_prompt.md` contains plan section | Yes — rendered with mission plan |
| `pi-executor.log` exists | Yes — from PiExecutor.run() |
| `policy-validate.log` exists | Yes — from PolicyCheckValidator |
| Policy validation status | `passed` |
| No forbidden action evidence in logs | Confirmed |
| No secrets exposed in review evidence API | Confirmed |
| API lists all expected artifacts | Yes |
| Artifact preview API works for all key files | Yes |
| `git status` is clean after cleanup | Yes — no repo changes |
| Smoke artifacts removed | Yes — DB and artifact dirs deleted |

---

## Running the Smoke Without Full Validator Set

If you only want to verify the Pi executor path and policy validation (without
running pytest/openspec which require a full repo checkout):

```bash
python scripts/run_dispatcher.py \
  --task-key "$SMOKE_TASK_KEY" \
  --db-path "$SMOKE_DB" \
  --validators policy
```

This runs only the policy validator. The smoke is still valid for verifying
the governance chain — policy is the primary governance validator.

If you also want typecheck/lint (requires `mypy` and `ruff` installed):

```bash
pip install mypy ruff
python scripts/run_dispatcher.py \
  --task-key "$SMOKE_TASK_KEY" \
  --db-path "$SMOKE_DB" \
  --validators policy,typecheck,lint
```

---

## Quick Reference Commands

```bash
# Full smoke run (one-liner)
SMOKE_TASK_KEY="AT-PI-GOV-SMOKE" \
REPO_ROOT="/home/ubuntu/agent-taskflow" \
SMOKE_DB="/tmp/agent-taskflow-pi-gov-smoke.db" \
SMOKE_ARTIFACT_DIR="/tmp/agent-taskflow-pi-gov-artifacts/$SMOKE_TASK_KEY" \
SMOKE_WORKTREE="$REPO_ROOT/.worktrees/$SMOKE_TASK_KEY" \
; \
rm -rf "$SMOKE_WORKTREE" "$SMOKE_ARTIFACT_DIR" "$SMOKE_DB" \
; \
python scripts/create_pi_smoke_task.py --task-key "$SMOKE_TASK_KEY" --db-path "$SMOKE_DB" --repo-path "$REPO_ROOT" --artifact-root "/tmp/agent-taskflow-pi-gov-artifacts" \
; \
python scripts/run_api.py --db-path "$SMOKE_DB" --host 127.0.0.1 --port 8100 --log-level warning & \
sleep 3 \
; \
python scripts/run_dispatcher.py --task-key "$SMOKE_TASK_KEY" --db-path "$SMOKE_DB" --validators policy \
; \
curl -s http://127.0.0.1:8100/api/tasks/$SMOKE_TASK_KEY/review-evidence | python3 -m json.tool \
; \
pkill -f "uvicorn.*8100" || true \
; \
rm -rf "$SMOKE_WORKTREE" "$SMOKE_ARTIFACT_DIR" "$SMOKE_DB" \
; \
cd /home/ubuntu/agent-taskflow && git status --short
```