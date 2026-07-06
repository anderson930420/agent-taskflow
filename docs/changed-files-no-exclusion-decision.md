# Changed-files No-Exclusion Decision

## Purpose

This decision record closes the remaining #7/#8 status-reconciliation item.
Atomic temp files may appear after crashes or `SIGKILL`. They are evidence to
inspect, not noise to hide.

## Decision

- Do not add changed-files validator exclusions for atomic temp files.
- Do not add global ignore/exclude rules for
  `.{target.name}.{16 lowercase hex}.tmp`.
- Do not hide orphan atomic temp files from evidence.
- Do not modify `.gitignore` for atomic temp files.
- Do not teach validators to silently ignore atomic temp files.

## Rationale

Changed-files evidence is a safety boundary. A broad pattern-based exclusion
could hide files that should remain visible. Atomic temp candidates can include
artifact or evidence context that operators need to inspect, so hiding them
globally would make validator output less trustworthy. The safer resolution is
visibility plus operator review, not global exclusion.

## Approved alternative

Use the read-only orphan audit to surface atomic temp candidates. For
machine-readable output, run:

```bash
python3 scripts/summarize_atomic_temp_orphans.py --root . --json
```

For a human-readable report with an explicit limit, run:

```bash
python3 scripts/summarize_atomic_temp_orphans.py --root . --max-entries 100
```

Follow `docs/atomic-artifact-safety-runbook.md` for the operator procedure. If
cleanup is ever needed, it must be a
separate, explicit, human-confirmed cleanup workflow or PR.

The audit command is not cleanup. The audit command is not approval. The audit
command is not validation authority. The audit command
does not run executors or validators.

## Relationship to P6-A/B/C/D

| Work item | Status |
| --- | --- |
| P6-A atomic write safety | Completed. |
| P6-B `blocked -> queued` reset CLI | Completed. |
| P6-C read-only orphan audit | Completed. |
| P6-D operator runbook | Completed. |
| P6-E changed-files no-exclusion decision | This record. |

## Forbidden follow-up

- Do not add changed-files validator exclusions for atomic temp files.
- Do not add atomic temp files to `.gitignore`.
- Do not hide orphan atomic temp files from evidence.
- Do not automatically delete orphan atomic temp files.
- Do not treat atomic temp matches as validator-ignored noise.
- Do not implement broad pattern-based filtering for
  `.{target.name}.{16 lowercase hex}.tmp`.

## Safe future work

A future cleanup workflow, if needed, must be separate from changed-files
validation. It must be explicit, human-confirmed, auditable, and narrow. It
must not weaken changed-files evidence. It must not be implemented in this PR.
