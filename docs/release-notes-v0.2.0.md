# v0.2.0 — Scheduled One-Task Automation and Packaged CLI Stabilization

**Tag:** `v0.2.0` (pending)
**Date:** 2026-06-11
**Status:** Final release candidate — ready for tagging after this PR is merged and validation is reviewed

---

## Summary

v0.2.0 advances agent-taskflow beyond the initial governance pipeline release into a scheduled, one-task-at-a-time automation control plane. It adds GitHub issue scheduler ticks, live-operation deployment examples, observability summaries, ExecutionEngine migration scaffolding, ingestion failure hardening, Python packaging metadata, packaged console entry points, and explicit local-validation guardrails.

The core invariant remains unchanged: agent-taskflow manages work, not agents. Human approval remains the final authority. The system still does not auto-merge, self-approve, destructively clean up branches/worktrees, run an unbounded daemon, or process multiple tasks concurrently.

---

## Highlights

### Scheduled One-Task Automation

- **GitHub issue one-task scheduler tick** — confirmed scheduler path processes at most one eligible task per tick.
- **Manual and scheduled entry points share locking** — shared flock behavior prevents manual automation and scheduled ticks from racing each other.
- **Confirmed preset remains explicit** — destructive or publication behavior is not implied by scheduling.
- **Execution and publication are separated** — `publish_after_execution=False` allows confirmed scheduled execution to stop at `waiting_approval` without branch push or draft PR creation.
- **Runner configuration wiring** — executor, validator, worktree root, command, model/provider/tools, Pi binary, and approved-task preflight settings can be passed through the scheduler tick CLI.
- **Deployment examples** — systemd timer/service and cron examples document how to run one-task scheduled ticks without introducing a daemon or webhook runtime.

### GitHub Issue Intake and Backlog Hardening

- **Ingestion failure registry** — failed issue ingestion is recorded instead of silently retried without context.
- **Quarantine semantics** — repeatedly failing ingestion candidates can be suppressed from previews until addressed.
- **Duplicate-trigger suppression** — a second confirmed select-first pass does not re-run an already consumed candidate.
- **Blocked backlog visibility** — blocked candidates become visible as backlog evidence rather than disappearing from operator view.
- **Mission Control remains read-only** — UI additions expose state and summaries without adding approval, merge, cleanup, or runtime mutation authority.

### Observability and Operator Evidence

- **Scheduler tick summaries** — scheduler output can include structured summaries suitable for logs and operator review.
- **JSON-friendly output contracts** — CLI outputs preserve machine-readable payloads for automation logs.
- **Observability-only flags** — summary flags expose status without introducing destructive command names or hidden runtime authority.
- **Runtime audit remains evidence, not approval authority** — validation and human review gates remain separate from runtime observability.

### ExecutionEngine Migration Scaffolding

- **ExecutionEngine request builder** — introduces structured request preparation for the future execution path.
- **Shadow compare** — allows legacy and ExecutionEngine paths to be compared without transferring authority.
- **Opt-in ExecutionEngine path** — ExecutionEngine behavior is explicitly opt-in, not the default scheduler authority.
- **Fallback assessment hardening** — fallback status is made machine-readable and cannot be confused with approval or execution permission.
- **Legacy path remains authority** — P5 work is migration scaffolding and evidence capture, not a runtime authority switch.

### Python Packaging and CLI Namespace Stabilization

- **Minimal packaging metadata** — `pyproject.toml` now declares project metadata, dependencies, and console scripts.
- **Packaged console commands** — core commands are exposed as:
  - `agent-taskflow-local-validation`
  - `agent-taskflow-github-issue-one-task-automation`
  - `agent-taskflow-github-issue-one-task-scheduler-tick`
- **CLI namespace cleanup** — console entry points target `agent_taskflow.cli.*` instead of a top-level `scripts` package.
- **Top-level package collision avoided** — package discovery is limited to `agent_taskflow*`, and `scripts/__init__.py` is removed.
- **Compatibility shims preserved** — existing `scripts/run_*.py` paths continue to delegate to packaged CLI modules, preserving live cron compatibility.
- **Local validation guard** — `agent-taskflow-local-validation` now fails clearly outside a repository checkout instead of running repo-only subprocess commands from `site-packages`.

---

## Architecture Scope

v0.2.0 keeps the same safety shape as v0.1.0 while adding scheduled operation around it:

```text
GitHub Issue / operator-authored work item
    ↓
Deterministic discovery and ingestion
    ↓
One-task scheduler tick with shared lock
    ↓
Task execution package / handoff
    ↓
Executor adapter and deterministic validators
    ↓
waiting_approval
    ↓
Human review and explicit approval decision
```

The scheduler may prepare and execute one confirmed task at a time, but it does not turn the system into an autonomous multi-task agent loop. Publication, merge, cleanup, and final approval remain outside automatic authority.

---

## Validation Status

| Check | Result |
|-------|--------|
| #98 CI | success |
| #99 CI | success |
| #100 CI | success |
| #101 CI | success |
| Local full test suite | 3723 tests passed |
| compileall | clean |
| Packaging entry point source checks | passed |
| Local validation non-repo guard tests | passed |

---

## Safety and Governance Guarantees

- **No self-approval** — workers still cannot approve their own output.
- **No automatic merge** — scheduler and CLI additions do not merge PRs.
- **No automatic branch push in execution-only mode** — scheduled execution can stop at `waiting_approval`.
- **No destructive cleanup automation** — no automatic branch/worktree deletion is introduced.
- **No daemon or webhook runtime** — deployment examples use bounded one-task ticks.
- **No multi-task scheduler loop** — the scheduler path remains one task per confirmed tick.
- **No hidden ExecutionEngine authority transfer** — ExecutionEngine work is opt-in evidence and migration scaffolding.
- **Mission Control remains read-only for scheduler state** — observability does not imply mutation authority.

---

## Known Limitations

1. **Local validation is source-checkout oriented** — `agent-taskflow-local-validation` is intentionally guarded to run from an agent-taskflow repository checkout or editable checkout, not from arbitrary `site-packages` locations.

2. **ExecutionEngine is not the default authority** — P5 work prepares shadow comparison and fallback assessment, but the legacy scheduler path remains authoritative.

3. **Scheduled operation is bounded** — v0.2.0 adds one-task scheduled ticks, not an always-on multi-task daemon.

4. **Human review is still required** — reaching `waiting_approval` is not equivalent to acceptance, merge, or publication.

5. **Deployment examples are examples** — systemd and cron files document operational patterns but do not create a managed hosted service.

---

## Upgrade / Runtime Notes

- Install editable for local development:

```bash
python3 -m pip install -e .
```

- Run local validation from a repository checkout:

```bash
agent-taskflow-local-validation
```

- Review scheduler tick help:

```bash
agent-taskflow-github-issue-one-task-scheduler-tick --help
```

- Existing script paths remain available for compatibility:

```bash
scripts/run_local_validation.py
scripts/run_github_issue_one_task_scheduler_tick.py --help
scripts/run_github_issue_one_task_automation.py --help
```

---

## Recommended Release Actions

1. Merge the v0.2.0 release metadata PR.
2. Confirm CI is green on the release metadata commit.
3. Delete the incorrect `v0.1.1` tag if it was only a transient local stabilization tag.
4. Create annotated tag `v0.2.0` from the merged `main` commit.
5. Create a GitHub Release from `v0.2.0` and use this document as the release body.
