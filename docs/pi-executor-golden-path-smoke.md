# Pi Executor Golden Path Smoke

`scripts/run_pi_executor_golden_path_smoke.py` validates that the existing
`PiExecutor` can be controlled through the Mission Control backend path.

The smoke exercises:

```text
Mission Control API create
-> Mission Control API start
-> Dispatcher
-> existing PiExecutor
-> artifact file
-> script-local deterministic validator
-> SQLite store events/status
-> Mission Control API readback endpoints
```

The default mode is fake Pi mode. It creates an isolated temporary SQLite DB,
fake repository directory, `.worktrees/<task>` worktree, artifact directory,
and fake `pi` executable. The fake executable receives the real PiExecutor
command shape and writes `pi_golden_path_result.txt` under the task artifact
directory with exactly `pi-golden-path-ok` and no trailing newline. This keeps
automated tests deterministic and avoids calling the real Pi agent.

Generated Pi protocol artifacts use these canonical snake_case schema keys:

- `mission_contract`
- `artifacts`
- `required_validators`
- `forbidden_actions`
- `human_approval_required`

`pi_mission_plan.json` names generated proof-of-work artifacts under
`artifacts`: `mission_contract`, `mission_plan`, `mission_prompt`, and
`executor_log`.

Run the safe fake-Pi smoke:

```bash
python3 scripts/run_pi_executor_golden_path_smoke.py --keep-workspace
```

Run with an explicit fake binary:

```bash
python3 scripts/run_pi_executor_golden_path_smoke.py \
  --workspace-root /tmp/agent-taskflow-pi-golden-path \
  --fake-pi-bin /tmp/agent-taskflow-pi-golden-path/bin/pi \
  --keep-workspace
```

Run the real Pi smoke only when intentionally validating a real local Pi CLI:

```bash
python3 scripts/run_pi_executor_golden_path_smoke.py \
  --real-pi \
  --confirm-real-pi \
  --keep-workspace
```

The real Pi mode is opt-in because it executes the `pi` command on `PATH`.

## Prompt Scope

The generated prompt tells Pi to:

- write exactly one expected artifact file under the task artifact directory
- use exact deterministic content
- avoid modifying files outside the artifact directory
- avoid modifying the repository source
- avoid push, merge, approval, cleanup, branch deletion, and worktree deletion

## What It Proves

- The API can create and start a task using an isolated DB.
- The dispatcher can select the existing `PiExecutor`.
- The existing `PiExecutor` can run under Mission Control control.
- PiExecutor writes its normal log/protocol artifacts.
- A deterministic validator can verify Pi-produced output.
- Store events and task status are readable through API endpoints.
- A passing run reaches `waiting_approval`.

## What It Does Not Prove

- It does not exercise the frontend.
- It does not add or test remote tracker integration.
- It does not add or test multi-agent orchestration.
- It does not add a product executor type or registry entry.
- Fake mode does not call the real Pi agent.
- Real mode still does not push, merge, approve, clean up, delete branches, or
  delete worktrees as part of the smoke.
