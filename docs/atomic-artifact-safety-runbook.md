# Atomic Artifact Safety Runbook

## 1. Purpose

This operator-facing, safety-oriented runbook covers artifact and evidence
atomic writes, read-only orphan atomic temporary-file audit, and manual
`blocked -> queued` recovery. It explains how to inspect evidence and use the
existing recovery gate without granting approval, validation, merge, or
cleanup authority.

## 2. Status reconciliation

| Capability | Status |
| --- | --- |
| P6-A atomic write permissions, durability, and symlink behavior | Done. |
| P6-B audited `blocked -> queued` reset CLI | Done. |
| P6-C read-only atomic temp orphan audit | Done. |
| Automatic cleanup | Not done and not part of this workflow. |
| changed-files exclusion | Not done and intentionally avoided. |

## 3. Atomic write facts

P6-A applies to artifact, evidence, and report files written through the
atomic-write helpers. The helpers use these semantics:

- Temporary files are created in the same directory as the target, so the
  final replace is a same-filesystem operation.
- The temporary-file pattern is `.{target.name}.{16 lowercase hex}.tmp`.
- Data is flushed and file-fsynced before `os.replace`; directory fsync is
  best-effort after replacement to strengthen crash durability.
- Existing regular-file permission bits are preserved. New files use `0o666`
  subject to the process umask.
- Existing symlinks are not followed. Replacement replaces the symlink path
  itself and leaves the former symlink target unchanged.
- Before the replace, an existing target should remain intact. Readers should
  see either its previous complete content or the new complete content, not a
  partially written target.
- Normal exception cleanup is best-effort. A process crash or `SIGKILL` can
  leave orphan temp files.

These semantics do not apply to JSONL append logs, SQLite writes, or streamed
subprocess logs.

## 4. Orphan temp audit command

Use JSON output for machine-readable inspection:

```bash
python3 scripts/summarize_atomic_temp_orphans.py --root . --json
```

Use the human-readable report with an explicit entry limit:

```bash
python3 scripts/summarize_atomic_temp_orphans.py --root . --max-entries 100
```

The command is read-only. For each reported match it includes the candidate
target path (`candidate_target_path`), 16-character random segment, size,
modification time, and file type/regular-file indicator.

The audit command:

- does not delete files;
- does not modify files;
- does not write DB records;
- does not modify `.gitignore`;
- does not modify changed-files validators;
- does not add changed-files exclusions; and
- does not run cleanup, executors, validators, approval, or merge.

## 5. What to do when orphan temp files are found

Follow this decision flow:

1. Inspect the report, including warnings and any truncation indicator.
2. Correlate each `candidate_target_path` with the relevant task and artifact
   context.
3. Treat orphan temp files as audit evidence, not as automatically ignorable
   noise.
4. Do not hide them from changed-files evidence.
5. Do not add global exclude rules.
6. Do not delete them as part of this runbook.
7. Decide separately whether the task remains blocked or is eligible for the
   explicitly confirmed recovery below.

If cleanup is ever needed, it must be a separate, explicit, human-confirmed
cleanup PR or workflow. Finding an orphan does not itself authorize cleanup or
a task status change.

## 6. Blocked -> queued recovery command

First preview the single supported transition:

```bash
python3 scripts/reset_task_status.py \
  --db-path /path/to/state.db \
  --task-key AT-EXAMPLE \
  --from-status blocked \
  --to-status queued \
  --reason "operator inspected stale orphan temp evidence" \
  --dry-run
```

After inspection, perform the reset only with explicit operator confirmation:

```bash
python3 scripts/reset_task_status.py \
  --db-path /path/to/state.db \
  --task-key AT-EXAMPLE \
  --from-status blocked \
  --to-status queued \
  --reason "operator inspected stale orphan temp evidence" \
  --confirm-reset
```

The command has a deliberately narrow boundary:

- Only `blocked -> queued` is supported.
- `--from-status blocked` is required.
- `--to-status` defaults to `queued` and must be `queued` if provided.
- `--reason` is required and must be non-empty.
- Mutation requires `--confirm-reset`.
- `--dry-run` must not mutate the task or write reset audit records.
- This is not approval.
- This is not merge.
- This is not cleanup.
- This is not validation authority.
- This does not call `approved_task_runner`.

The reset only returns an inspected, locally mirrored blocked task to the
queue. Any later execution and validation remain subject to their existing
deterministic gates and human review boundaries.

## 7. Explicit forbidden actions

- Do not add atomic temp files to `.gitignore`.
- Do not add changed-files validator exclusions for atomic temp files.
- Do not automatically delete orphan temp files.
- Do not use `reset_task_status.py` as approval.
- Do not use `reset_task_status.py` as validation authority.
- Do not run cleanup from the audit command.
- Do not hide orphan temp files from evidence.

## 8. Recommended validation checklist

Run these checks from the repository root:

```bash
python3 scripts/summarize_atomic_temp_orphans.py --help
python3 scripts/reset_task_status.py --help
PYTHONPATH=. .venv/bin/python3 -m unittest tests.test_p6_atomic_artifact_runbook_docs -v
PYTHONPATH=. .venv/bin/python3 -m unittest discover -s tests
PYTHONPATH=. .venv/bin/python3 -m compileall agent_taskflow scripts tests
git diff --check
```
