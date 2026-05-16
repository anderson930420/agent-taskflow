# Next Stage Entry Criteria

Stage A must not begin until the bridge-hardening baseline is complete and
reproducible.

Stage A is the first stage where the project may begin preparing external
tracker, workspace, and orchestration architecture work. It is not authorized
by this document alone; the entry criteria below must be satisfied first.

## Required Before Stage A

- `git status` is clean.
- Phase 77 deterministic Mission Control golden path smoke passes.
- Phase 78 fake-Pi smoke passes.
- Real-Pi smoke remains manual opt-in and has a recent recorded success.
- Phase 79 changed-files validator is available.
- Phase 80 local validation runner passes.
- Phase 81 Pi artifact schema cleanup is complete.
- Documentation clearly states Mission Control is observability/review only.
- Documentation clearly states executors are adapters only.
- Documentation clearly states GitHub integration is deferred.
- No UI expansion is present.
- No GitHub integration is present.
- No PR creation behavior is present.
- No merge behavior is present.
- No push behavior is present.
- No cleanup/delete expansion is present.

## Required Evidence

The expected local validation evidence is:

```bash
source .venv/bin/activate
python scripts/run_local_validation.py
python -m compileall agent_taskflow scripts tests
```

The fake-Pi smoke remains part of the local runner. The real-Pi smoke remains
manual opt-in only:

```bash
python scripts/run_pi_executor_golden_path_smoke.py \
  --real-pi \
  --confirm-real-pi \
  --keep-workspace
```

## Stage A May Include Later

Once entry criteria are satisfied, Stage A may define plans for:

- production GitHub issue/task source
- repo-owned workflow policy
- per-task workspace manager design
- adapter-neutral executor contracts
- proof-of-work artifact indexing
- review evidence requirements

Those are planning candidates, not implementation approval in this phase.

## Stage A Must Still Exclude

Until separately approved, Stage A must still exclude:

- automatic PR creation
- automatic merge
- automatic push
- automatic cleanup/delete
- self-approval
- remote worker pool
- multi-host scheduling
- replacing the Python core with an external runtime
