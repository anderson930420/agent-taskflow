# Local Workspace Cleanup Inventory (P2-a)

`scripts/summarize_local_workspace_inventory.py` is a **read-only** operator
command that inventories the local Git worktrees attached to an Agent Taskflow
repository. It is the first phase (**P2-a**) of the local workspace cleanup
effort.

**P2-a is inventory only.** It looks, classifies, and recommends. It does
**not** delete anything, **not** run `git worktree remove`, **not** run `git
worktree prune`, **not** run `git reset`, **not** run `git clean`, **not** run
`rm`, **not** write the database, **not** modify crontab, **not** call GitHub,
and **not** start an executor or validator. The explicit, human-confirmed
cleanup actions are a later phase (**P2-b**); see
[Next phase: P2-b](#next-phase-p2-b) below.

The command runs only two read-only Git commands — `git worktree list
--porcelain` (in the repo root) and `git status --short` (in each existing
worktree) — and otherwise only checks whether paths exist.

## Why `/home/ubuntu/agent-taskflow-cron` must be preserved

`/home/ubuntu/agent-taskflow-cron` is the clean runtime worktree that the
installed cron tick executes from (Level 10H). Removing, pruning, or resetting
it would break the live scheduled execution path. The inventory marks it
`keep_runtime` and never recommends touching it. It is passed as a
`--runtime-worktree` and is the default runtime path.

## Why `/home/ubuntu/agent-taskflow` should be manually reviewed before cleanup

`/home/ubuntu/agent-taskflow` is the known dirty/stale manual checkout. It is
**not** the cron runtime and should not be used as one, but it may contain
local-only work, artifacts, logs, or uncommitted changes that a human needs to
inspect before any cleanup. The inventory marks it
`manual_review_dirty_checkout` and surfaces its changed paths and local-only
markers so a human can review them. It is passed as a
`--manual-review-worktree` and is the default manual-review path.

## What the command shows

For each worktree:

- `path`, `exists`
- `branch`, `head`, `detached`, `bare`, `locked`
- `prunable`, `prunable_reason`, `missing_or_prunable`
- `inside_tmp`, `within_path_prefix`
- `is_runtime`, `is_manual_review`
- `has_local_changes` (from read-only `git status --short`),
  `changed_path_count`, `changed_paths` (capped by `--status-limit`),
  `changed_paths_truncated`, `status_error`
- `local_only_markers` and `present_local_only_markers` for `.claude/`,
  `artifacts/`, `logs/`, `scripts/local/`, `.agent-taskflow/`
- `recommendation` and `reasons`

Plus a `summary` block (total worktrees, existing count, missing/prunable count,
dirty count, runtime count, tmp count, and recommendation counts) and a `safety`
block proving the run was read-only.

## How to run JSON mode

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_local_workspace_inventory.py \
  --repo-root /home/ubuntu/agent-taskflow \
  --runtime-worktree /home/ubuntu/agent-taskflow-cron \
  --manual-review-worktree /home/ubuntu/agent-taskflow \
  --path-prefix /tmp \
  --path-prefix /home/ubuntu \
  --status-limit 20 \
  --json
```

The JSON output includes `ok`, `schema_version`, `source`, `repo_root`,
`runtime_worktrees`, `manual_review_worktrees`, `path_prefixes`, `worktrees`,
`summary`, `warnings`, and `safety`.

## How to run human-readable mode

Omit `--json` for the default human-readable output:

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_local_workspace_inventory.py \
  --repo-root /home/ubuntu/agent-taskflow
```

The human-readable output lists each worktree with its recommendation and
reasons, the summary counts, the per-recommendation counts, any warnings, and
the read-only safety statement.

## How to interpret each recommendation

- **`keep_runtime`** — the worktree matches a known cron runtime worktree
  (`/home/ubuntu/agent-taskflow-cron`). Preserve it. Do not clean, prune, reset,
  or remove it.
- **`manual_review_dirty_checkout`** — either the known dirty/manual checkout
  (`/home/ubuntu/agent-taskflow`) or any worktree that has local changes. A
  human must review the changed paths and local-only markers before any
  cleanup. Nothing is removed automatically.
- **`candidate_tmp_worktree_review`** — a clean worktree inside `/tmp`. It is a
  candidate for cleanup review in P2-b, but P2-a does not remove it. (A dirty
  tmp worktree is routed to `manual_review_dirty_checkout` instead, so its
  uncommitted work is not lost.)
- **`prunable_missing_worktree_record`** — Git reports the worktree record as
  prunable, or the worktree path no longer exists on disk. It is a candidate for
  `git worktree prune` in a later confirmed phase. P2-a does not prune.
- **`clean_non_runtime_review`** — a clean worktree that is not the runtime, not
  the manual checkout, and not inside `/tmp`. Review it before any cleanup.
- **`no_action`** — out of the configured inventory scope (path prefixes), or
  the local change state could not be determined. No recommendation is made.

## What not to do yet

During P2-a, do **not**:

- delete any worktree directory or file,
- run `git worktree remove`,
- run `git worktree prune`,
- run `git reset` or `git clean`,
- run `rm` against any worktree,
- modify crontab or the cron runtime,
- write the database or call GitHub,
- start an executor or validator.

Use the inventory to decide what should happen, then let a human confirm those
actions in P2-b.

## Next phase: P2-b

P2-b is where explicit, **confirmed** cleanup actions happen, **after** this
inventory has been reviewed by a human. P2-b would, only on explicit human
confirmation, prune missing worktree records, remove reviewed clean tmp
worktrees, and act on the manual checkout once its local-only work has been
salvaged. P2-a deliberately stops at the inventory so that the destructive
actions remain a separate, human-gated step.
