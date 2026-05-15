# Mission Control Golden Path Smoke

`scripts/run_mission_control_smoke.py` is a deterministic backend smoke for
the Mission Control golden path.

It exercises this path:

```text
Mission Control API create
-> Mission Control API start
-> Dispatcher
-> script-local SmokeExecutor
-> real artifact file
-> script-local SmokeValidator
-> SQLite store events/status
-> Mission Control API readback endpoints
```

The smoke uses an isolated temporary SQLite database and workspace by default.
It creates a fake repository directory, a `.worktrees/<task>` directory, and a
task artifact directory under that temporary workspace. The executor writes
`mission_control_smoke_result.txt` with deterministic content. The validator
reads that file and verifies the exact content. A passing run ends with task
status `waiting_approval`.

Run it with:

```bash
python3 scripts/run_mission_control_smoke.py
```

To keep artifacts for inspection:

```bash
python3 scripts/run_mission_control_smoke.py --keep-workspace
```

To use a known isolated directory:

```bash
python3 scripts/run_mission_control_smoke.py \
  --workspace-root /tmp/agent-taskflow-mc-smoke \
  --task-key AT-MC-SMOKE
```

## What It Proves

- The API can create a queued task in an isolated store.
- The API start action invokes the dispatcher.
- The dispatcher uses executor and validator abstractions.
- The executor writes a real deterministic artifact file.
- The validator reads and verifies that artifact.
- Dispatcher results are recorded in the store.
- API readback endpoints expose task status, executor runs, validation results,
  artifacts, artifact preview, and review evidence.

## What It Does Not Prove

- It does not exercise the Mission Control frontend.
- It does not call GitHub or any remote service.
- It does not run Pi, OpenCode, Shell, MiniMax, or another external worker.
- It does not add or verify a product executor type or registry entry.
- It does not approve, merge, push, clean worktrees, or delete branches.
- It does not validate multi-agent orchestration.

The `SmokeExecutor` and `SmokeValidator` are script-local fixtures. They exist
only to make the golden path deterministic and cheap to run.
