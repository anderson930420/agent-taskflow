# Local Validation Runner

`scripts/run_local_validation.py` is the standard local validation command for
bridge hardening phases.

Activate the project virtual environment first:

```bash
source .venv/bin/activate
python scripts/run_local_validation.py
```

The runner prints `sys.executable`, `VIRTUAL_ENV`, and whether `VIRTUAL_ENV` is
set before running validation. It also verifies that `fastapi` and `uvicorn`
can be imported. If those dependencies are missing, it fails with a message
explaining that the project `.venv` should be activated.

## Checks

The runner executes these checks in order:

```bash
python scripts/validate_workflow_contract.py
python scripts/run_mission_control_smoke.py --keep-workspace
python scripts/run_pi_executor_golden_path_smoke.py --keep-workspace
python -m unittest discover -s tests -v
python -m compileall agent_taskflow scripts tests
openspec validate --all --no-interactive
```

Python commands are invoked with the current `sys.executable`, so an activated
`.venv` stays consistent across all required checks.

Workflow contract validation is required in the local runner only. It checks
the repo-level `WORKFLOW.md` contract skeleton before longer smoke tests run;
it does not make the dispatcher or runtime require `WORKFLOW.md`.

`openspec` is optional. If it is available on `PATH`, the runner executes it.
If it is unavailable, the runner marks the check as skipped and still exits
successfully when all required checks pass.

## Pi Scope

The PiExecutor smoke runs in fake-Pi mode by default. The local validation
runner does not require or invoke a real Pi agent.

Real-Pi smoke remains manual opt-in only:

```bash
python scripts/run_pi_executor_golden_path_smoke.py \
  --real-pi \
  --confirm-real-pi \
  --keep-workspace
```

## Summary

At the end of a run, the script prints a structured human-readable summary for
each check, including the check name, command, status, return code, duration
when available, and a short reason for failed or skipped checks.

The runner exits nonzero if any required check fails. A skipped unavailable
`openspec` command does not fail the runner.
