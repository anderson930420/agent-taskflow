# Next Stage Entry Criteria

This is a historical bridge-hardening Stage A entry record. Its requirements
and exclusions describe that phase's authorization boundary; they do not
prohibit current V1 foundations or assert that this repository is deployed.

Current V1 sources document deterministic local worktree preparation
([`agent_taskflow.workspace_manager`](../agent_taskflow/workspace_manager.py))
and the confirmed integration push, draft-PR, and cleanup mechanics in
[V1 Step 2](v1-step2-integration-controller.md). Cleanup requires either a
verified human GitHub merge or explicit confirmed cancelled cleanup; human
merge remains the normal review gate in [WORKFLOW.md](../WORKFLOW.md). The
tracked [V1 F8 handoff](v1/handoff-f8.md) records no automated integration
caller or queue consumer, so the remaining F9/F10 production caller work
means this makes no deployment or eligibility assertion.

At the time of this phase, Stage A could not begin until the bridge-hardening
baseline was complete and reproducible.

Stage A is the first stage where the project may begin preparing external
tracker, workspace, and orchestration architecture work. It is not authorized
by this document alone; the entry criteria below must be satisfied first.

## Historical Requirements Before Stage A

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

## Historical Required Evidence

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

## Historical Stage A May Include Later

Once entry criteria are satisfied, Stage A may define plans for:

- production GitHub issue/task source
- repo-owned workflow policy
- per-task workspace manager design
- adapter-neutral executor contracts
- proof-of-work artifact indexing
- review evidence requirements

Those are planning candidates, not implementation approval in this phase.

## Historical Stage A Must Still Exclude

Until separately approved, Stage A must still exclude:

- automatic PR creation
- automatic merge
- automatic push
- automatic cleanup/delete
- self-approval
- remote worker pool
- multi-host scheduling
- replacing the Python core with an external runtime
