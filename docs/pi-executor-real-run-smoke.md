# Pi Executor Real-Run Smoke Test

**Phase 14 — Manual-only smoke documentation**

This document describes a manual smoke test for the real Pi executor path. Unit tests must continue using a fake `pi` binary or mocked subprocess calls. Real Pi/MiniMax execution must not be added to CI.

## Purpose

This smoke test verifies the real path:

```text
TaskMirrorStore task
→ dispatcher
→ executor="pi"
→ PiExecutor
→ real pi CLI
→ MiniMax provider
→ executor log / task events
```

This procedure is manual because it uses a real provider key and can spend API credits.

## Safety Rules

- Run only from a clean branch.
- Never run the smoke task directly against the main checkout.
- Use a dedicated worktree under `.worktrees/<task-key>`.
- Use a temporary or backed-up state database.
- Do not put API keys in repo files.
- Load provider secrets only with `source ~/.config/pi-agent/env`.
- Do not commit generated smoke artifacts, logs, worktrees, or temporary DB files.
- Stop stuck `pi` processes before continuing.
- Do not merge, push, approve, reject, or cleanup automatically as part of this smoke test.

## Prerequisites

```bash
cd /home/ubuntu/agent-taskflow
source .venv/bin/activate
source ~/.config/pi-agent/env

pi --version
python -m compileall agent_taskflow scripts tests
git status --short
```

Expected `git status --short` output is empty.

Verify the provider without touching the repo:

```bash
pi \
  --provider minimax \
  --model MiniMax-M2.7 \
  --no-session \
  -p "Reply exactly: pi-real-smoke-ready"
```

Expected response:

```text
pi-real-smoke-ready
```

## Confirm Dispatcher CLI

Do not assume dispatcher flags. Inspect the actual script first:

```bash
python scripts/run_dispatcher.py --help
```

Use only flags that are shown by the help output. The existing tests expect `--task-key` to be supported.

## Controlled Real-Run Workflow

Set smoke-only paths:

```bash
export SMOKE_TASK_KEY="AT-PI-SMOKE"
export REPO_ROOT="/home/ubuntu/agent-taskflow"
export SMOKE_DB="/tmp/agent-taskflow-pi-smoke.db"
export SMOKE_WORKTREE="$REPO_ROOT/.worktrees/$SMOKE_TASK_KEY"
export SMOKE_ARTIFACT_DIR="/tmp/agent-taskflow-pi-smoke-artifacts/$SMOKE_TASK_KEY"
```

Create isolated directories and seed the task mirror using the helper:

```bash
python scripts/create_pi_smoke_task.py \
  --task-key "$SMOKE_TASK_KEY" \
  --db-path "$SMOKE_DB" \
  --repo-path "$REPO_ROOT" \
  --artifact-root "/tmp/agent-taskflow-pi-smoke-artifacts"
```

The helper creates `.worktrees/<task-key>` and the artifact directory, writes
`implementation_prompt.md`, and inserts `TaskRecord` + `TaskWorktreeRecord`
into the mirror DB. It prints a JSON summary including the next dispatch
command.

Run the dispatcher only after confirming its supported flags:

```bash
python scripts/run_dispatcher.py --help
```

Use the repository-supported `--db-path` option:

```bash
python scripts/run_dispatcher.py \
  --task-key "$SMOKE_TASK_KEY" \
  --db-path "$SMOKE_DB" \
  --validators openspec
```

If `--db-path` or an equivalent test-only DB option is not supported, do not run against the production/default state DB. Add a safe test-only entry point before performing this real-run smoke test.

For helper-created minimal smoke worktrees, use `--validators openspec`. The default validator set includes `pytest`, which expects a full repository checkout and may fail in the minimal smoke worktree. In this smoke workflow, `openspec` may skip when no `openspec/` directory exists; that is acceptable because the purpose is to verify the real Pi executor path, not full project validation.

## Verification

Check worktree output:

```bash
find "$SMOKE_WORKTREE" -maxdepth 2 -type f -print
cat "$SMOKE_WORKTREE/pi_smoke_result.txt" 2>/dev/null || true
```

Expected content:

```text
pi-real-run-smoke-ok
```

Check executor log:

```bash
find "$SMOKE_ARTIFACT_DIR" -maxdepth 2 -type f -print
sed -n '1,220p' "$SMOKE_ARTIFACT_DIR/pi-executor.log"
```

Check task state and events:

```bash
python - <<'PY'
import os
from pathlib import Path

from agent_taskflow.store import TaskMirrorStore

task_key = os.environ["SMOKE_TASK_KEY"]
db_path = Path(os.environ["SMOKE_DB"]).resolve()

store = TaskMirrorStore(db_path)
task = store.get_task(task_key)
print(task)

for event in store.list_task_events(task_key):
    print(event.event_type, event.payload_json)
PY
```

## Cleanup

Stop stuck Pi processes:

```bash
ps -f -u "$USER" | grep -E '[p]i|[n]ode'
pkill -f "^pi" || true
pkill -f "MiniMax-M2.7" || true
```

Remove smoke-only files:

```bash
rm -rf "$SMOKE_WORKTREE"
rm -rf "/tmp/agent-taskflow-pi-smoke-artifacts"
rm -f "$SMOKE_DB"
```

Return to a clean repo:

```bash
git status --short
```

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

Terminate only the smoke Pi process:

```bash
kill <PID>
sleep 2
kill -9 <PID>
```

### Unit tests launch real Pi

That is a test bug. Unit and integration tests must use fake Pi binaries or mocked subprocess calls. Real Pi/MiniMax execution belongs only in this manual smoke workflow.

### Prompt missing

The Pi executor requires:

```text
<artifact_dir>/implementation_prompt.md
```

### Worktree rejected

The worktree must be under:

```text
<repo>/.worktrees/<task-key>
```

Do not point the task worktree at the main checkout.

### Dispatcher blocks because status is wrong

The task must be in a dispatchable state such as:

```text
queued
```
