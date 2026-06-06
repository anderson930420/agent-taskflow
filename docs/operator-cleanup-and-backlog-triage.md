# Operator Cleanup and Backlog Triage Runbook

This runbook is **documentation and safety guidance only**. It describes how a
human operator reviews the backlog, triages blocked and queued work, handles old
smoke GitHub issues, and decides whether local worktrees or a one-time dirty
backup can be removed.

It adds **no automation**. There is no cleanup script, no scheduler change, and
no crontab change in this runbook. Every destructive action below is a **manual,
human-judgement** step that an operator performs deliberately, after inspecting
evidence. Nothing here runs on a timer, from a hook, or from a background worker.

> Manage work, not agents. Cleanup is an operator decision backed by preserved
> proof-of-work, never an automatic or worker-driven action.

## Safety boundaries (read first)

This runbook intentionally does **not**:

- **do not add automation**, daemons, schedulers, or background cleanup of any
  kind;
- add cleanup scripts or auto-archive behaviour;
- **do not modify crontab**, systemd timers, nginx, or deployment configuration;
- delete files, branches, worktrees, or tasks on its own;
- **do not auto-close issues** on GitHub;
- modify `TaskMirrorStore` or any orchestrator state;
- approve, merge, push, or mark work finally complete.

Human review remains the final gate. Treat every command in this document as a
**manual example** that you run yourself, only after the listed safe conditions
are confirmed.

### Protected paths (never remove)

The following worktrees are **protected** and must never be removed or cleaned by
any cleanup step in this runbook:

- `/home/ubuntu/agent-taskflow` — the protected main checkout.
- `/home/ubuntu/agent-taskflow-cron` — the protected Level 10H cron runtime.

Do not run `rm -rf`, `git worktree remove`, or any other destructive command
against either protected path. The cron runtime in particular is actively used
by the real scheduled tick; removing it would break scheduled execution.

## 1. Waiting-approval review

`waiting_approval` means a task has produced proof-of-work and is **waiting for a
human reviewer**. It does **not** mean the change is good, and it does **not**
mean the change will be published automatically. The cron profile runs with
`publish_after_execution=false` (execution-only mode), so reaching
`waiting_approval` never triggers a branch push or draft PR by itself.

### 1.1 Start from the read-only backlog summary

Use the read-only observability command to see the current backlog and the most
recent scheduled ticks before touching anything:

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_real_scheduled_execution.py \
  --db-path /home/ubuntu/.agent-taskflow/state.db \
  --log-path /home/ubuntu/agent-taskflow-cron/logs/github-issue-one-task-real-opencode.jsonl \
  --recent-limit 20
```

This prints the last tick, recent tick counts, the backlog
(`waiting_approval`, `blocked`, `queued`), and the ingestion failure registry.
It is read-only: it never writes the database, calls GitHub, or runs anything.

### 1.2 Inspect a single task in the TaskMirrorStore

`TaskMirrorStore` is the local SQLite mirror of orchestrator state (default
`/home/ubuntu/.agent-taskflow/state.db`). Each task record carries its status,
issue/spec metadata, and an `artifact_dir` pointing at its proof-of-work. Inspect
one waiting-approval task with the read-only summary command:

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_waiting_approval.py \
  --task-key AT-GH-74 \
  --db-path /home/ubuntu/.agent-taskflow/state.db
```

Do not edit the database directly during triage. Reading is fine; mutating
`TaskMirrorStore` is out of scope for cleanup.

### 1.3 Inspect the task `artifact_dir`

Each task records an `artifact_dir` (shown in the waiting-approval summary). List
its contents to see the proof-of-work index for that run:

```bash
ls -lh <artifact_dir>
```

### 1.4 Inspect executor artifacts

Inside a real executor run's `artifact_dir`, the key proof-of-work files are:

- `implementation_prompt.md` — the exact prompt handed to the executor.
- `opencode-events.jsonl` — the OpenCode event stream for the run.
- `git-status-after-opencode.txt` — working-tree status after the run.
- `diff-after-opencode.patch` — tracked diff the executor produced.
- `untracked-files-after-opencode.txt` — untracked files the executor created.
- `policy-validate.log` — the deterministic policy validator output.

Read them, for example:

```bash
cat <artifact_dir>/implementation_prompt.md
cat <artifact_dir>/git-status-after-opencode.txt
cat <artifact_dir>/diff-after-opencode.patch
cat <artifact_dir>/untracked-files-after-opencode.txt
cat <artifact_dir>/policy-validate.log
tail -n 20 <artifact_dir>/opencode-events.jsonl
```

### 1.5 Decision categories

After reviewing the artifacts, classify the waiting-approval task into exactly
one category:

- **a. Publish-worthy change** — the diff is correct, in scope, and valuable.
  This is a candidate for the explicit, human-gated push / draft-PR path. That
  path is a separate operator decision; it is **not** part of cleanup, and
  reaching this category does **not** auto-publish anything.
- **b. Smoke-only evidence** — the run proves the pipeline works end to end, but
  the diff itself is throwaway. Keep the artifacts as evidence; do not publish.
- **c. Bad / irrelevant output** — the executor produced wrong, empty, or
  off-target work. Record the reason and leave it for human disposition.

Reaching `waiting_approval` is the start of human review, not the end. Nothing
here authorises auto-publish.

## 2. Blocked task triage

`blocked` tasks are tasks the orchestrator or policy validator stopped. Triage
means understanding *why* each was blocked before deciding anything.

### 2.1 Known pre-fix smoke failures

Some blocked tasks are historical: they were blocked by a limitation that a
**later** change already fixed. Do not treat them as live failures.

- **AT-GH-67** was blocked *before* `implementation_prompt.md` generation
  existed. The pipeline now generates that artifact, so the original blocker no
  longer applies; the blocked record is preserved as history.
- **AT-GH-69** was blocked *before* the `SMOKE_TASK_KEY` policy false-positive
  hardening. The policy validator was later hardened, so the original
  false-positive no longer applies; the blocked record is preserved as history.

### 2.2 Policy-blocked local dogfood examples

Some blocked tasks are local self-dogfood runs that the **policy validator
intentionally blocked** because they described a suspicious action. These are
working-as-intended safety stops, not bugs:

- **GH-9603** — policy blocked on a suspicious action (`approved task`).
- **GH-9601** — policy blocked on a suspicious action (`delete branch`).

Preserve these as evidence that the policy gate fired correctly.

### 2.3 Triage rules

- **Do not blindly retry an old blocked smoke** if a later smoke already passed
  the same path. Check the more recent runs first; a green later smoke
  supersedes an old red one.
- **Preserve blocked evidence** unless it is being intentionally archived by a
  human. Do not delete or rewrite blocked records during cleanup.

## 3. Queued task handling

`queued` tasks are **not** a cleanup backlog and must **not** be auto-run from
this process. Each queued task requires an **operator relevance review** to
decide whether it is still worth running, given everything that has shipped
since it was queued.

- **AT-GH-14** ("Dogfood: add first real executor checklist") is an old queued
  dogfood task. Before running it, confirm it is still relevant and not already
  satisfied by later work.
- Do not auto-run queued tasks from cleanup. Dispatching a queued task is a
  deliberate, separate operator action — never a side effect of triage.

## 4. Old smoke GitHub issue handling

Smoke issues are GitHub issues created to drive an end-to-end test. Once a smoke
path is proven, an old smoke issue may be **manually** closed by an operator —
but only when every condition below holds:

- a **later smoke passed the same path**, so the old issue is superseded;
- **artifacts are preserved** (the proof-of-work for the superseding run still
  exists and is reviewable);
- **no code change remains** to be merged from the old smoke;
- a **comment explains the supersession** before the issue is closed.

When those conditions hold, an operator may run the following **manual
examples** by hand. They are illustrative only — copy, adjust the issue number
and text, and run them yourself after confirming the conditions:

```bash
# MANUAL EXAMPLE — run by hand, only after the conditions above are confirmed.
gh issue comment <issue-number> --body \
  "Superseded by <later-smoke>. Same path passed; artifacts preserved; no code change remains."

# MANUAL EXAMPLE — run by hand, only after posting the supersession comment.
gh issue close <issue-number> --reason completed
```

**Do not auto-close issues.** There is no automation, hook, or scheduled job in
this runbook that closes GitHub issues. Closing an issue is always an explicit
human action.

## 5. Local worktree cleanup

Old task worktrees accumulate under paths such as `/tmp/agent-taskflow-level-*`
and `/tmp/agent-taskflow-main-after-*`. They can be removed by an operator once
they are provably finished — but only through git's own worktree commands, never
with a raw recursive delete.

### 5.1 List worktrees

Start by listing every registered worktree and its branch/commit:

```bash
git worktree list
```

### 5.2 Safe removal conditions

Only consider removing a worktree when **all** of the following are true:

- its **PR was merged** (or the work is otherwise provably finished);
- the **worktree is clean** (no uncommitted or untracked work you still need);
- it is **not used by cron** or any other live process;
- its **artifacts are preserved** elsewhere, or are genuinely not needed.

Confirm the worktree is clean before removing it:

```bash
git -C <path> status --short
```

An empty result means clean. If anything prints, stop and review it first.

### 5.3 Manual removal commands

When the safe conditions hold, remove the worktree with git's own commands so the
worktree registry stays consistent:

```bash
git worktree remove <path>
git worktree prune
```

`git worktree remove <path>` unregisters and deletes the worktree directory
safely; `git worktree prune` cleans up stale administrative entries for
worktrees whose directories are already gone.

> **Warning:** Never `rm -rf` a worktree directory before
> `git worktree remove <path>`. A raw recursive delete leaves the worktree
> registered, corrupts `git worktree list`, and can confuse later git
> operations. Always remove worktrees through git, not the filesystem.

### 5.4 Protected worktrees

Never remove the protected paths listed earlier:

- `/home/ubuntu/agent-taskflow`
- `/home/ubuntu/agent-taskflow-cron`

These must never be passed to `git worktree remove` or deleted by any means.

## 6. Dirty backup deletion policy

A one-time safety backup of pre-clean runtime state lives at:

```text
/home/ubuntu/agent-taskflow-dirty-backup
```

It holds the captured status, file list, tracked diff, and an archive of
untracked runtime files (`untracked-runtime-backup.tar.gz`) taken before a main
checkout was cleaned.

### 6.1 Inspect before deciding

Always inspect the backup before considering deletion:

```bash
ls -lh /home/ubuntu/agent-taskflow-dirty-backup
tar -tzf /home/ubuntu/agent-taskflow-dirty-backup/untracked-runtime-backup.tar.gz
```

The `tar -tzf` listing shows the archived files without extracting them, so you
can confirm nothing important would be lost.

### 6.2 Safe deletion conditions

The dirty backup may be deleted only when **all** of the following hold:

- the **main checkout is clean** (`git -C /home/ubuntu/agent-taskflow status
  --short` prints nothing);
- **Level 10I observability is verified** working from main;
- **cron has run safely for 24h+** with no failures, lock contention, or
  malformed lines;
- **no missing scripts or artifacts** were discovered that the backup would be
  needed to recover.

If any condition is in doubt, keep the backup.

### 6.3 Deletion command (last resort, manual)

Only when every condition in 6.2 is confirmed, an operator may delete the backup
directory by hand:

```bash
rm -rf /home/ubuntu/agent-taskflow-dirty-backup
```

This is the single deletion command in this runbook, placed last on purpose. It
targets the one-time backup directory only. It is a deliberate manual action,
never automated, and it must never be redirected at a protected path.
