# ops/ — operator scripts

Versioned copies of the operator scripts that drive the agent-taskflow loop on
the VPS. These are **operator tooling**, not part of the orchestrator: they wrap
`scripts/` entrypoints, the Codex CLI, and `gh`. They never approve tasks and
never merge — human review remains the final gate.

The live working copies run from `~/agent-taskflow-ops/`. This directory is the
version-controlled source of truth for them; changes should land here first.

## Scripts

| Script | Purpose |
| --- | --- |
| `run_tick_manual.sh` | Runs one github-issue scheduler tick by hand — the same tick cron drives. Picks up one `agent-taskflow-ready` issue, executes it through the `pi` slot (backed by `codex-as-pi.sh`), validates with pytest. |
| `codex-as-pi.sh` | Shim that lets the `pi` executor slot be driven by `codex exec`. Taskflow calls it as `--pi-bin` with `--no-session -p <prompt>`; the shim strips those flags and pipes the prompt to Codex. Override the binary with `CODEX_BIN`. |
| `run_advisory_recovery.sh` | Recovery for a task blocked at the `codex_advisory_evidence` gate. Generates confirm-run advisory evidence with the real Codex CLI, then runs the audited `blocked -> waiting_approval` retry transition. Takes `<TASK_KEY> <ATTEMPT_ID>`. |
| `publish_fallback.sh` | Human-fallback publication when the internal publication path is blocked (finding #8): commits the attempt worktree, pushes its branch, opens a **draft** PR. Takes `<TASK_KEY> <ISSUE_NUMBER>`. Never merges. |
| `run_notify.sh` | Wrapper that loads config/secrets and runs the notifier. |
| `taskflow_dc_notify.py` | Stdlib-only, read-only notifier: reads the taskflow SQLite mirror, detects tasks that entered a notify-worthy status since the last run, and posts one Discord message per transition with the draft PR link when available. Its cursor lives in `TASKFLOW_NOTIFY_STATE`, never in the taskflow DB. |

## Secrets

The Discord webhook URL must never enter this repository.

`run_notify.sh` reads `DISCORD_WEBHOOK_URL` from the environment, and falls back
to an untracked `ops/notify.env` only when the variable is unset:

```bash
cp ops/notify.env.example ops/notify.env
chmod 600 ops/notify.env
# edit ops/notify.env, then:
ops/run_notify.sh
```

`ops/notify.env` is listed in `.gitignore`. Only `ops/notify.env.example`, which
carries placeholders, is committed.

## Paths

Each script derives the repo root from its own location (`ops/..`), so a fresh
checkout works without editing. Machine-level paths that are genuinely outside
the repo keep VPS-layout defaults and are overridable by environment variable:

- `CODEX_BIN` — default `/home/ubuntu/tools/pi-agent/bin/codex`
- `TASKFLOW_DB_PATH` — default `$HOME/.agent-taskflow/state/github_issue_scheduler.sqlite3`
- `TASKFLOW_NOTIFY_STATE` — default `$HOME/agent-taskflow-ops/notify_state.json`
- `TASKFLOW_REPO` — default `anderson930420/agent-taskflow`
- `TASKFLOW_OPERATOR` — default `anderson`

## The operator loop

```text
label -> tick -> advisory recovery -> publish -> merge -> archive
```

1. **label** — add `agent-taskflow-ready` to exactly one GitHub issue. The tick
   is one-task-at-a-time; labelling several does not run several.
2. **tick** — `ops/run_tick_manual.sh`. Ingests the issue, prepares an attempt
   worktree, runs the executor, runs the pytest validator, writes evidence under
   `artifacts/github-issue-scheduler/<TASK_KEY>/<ATTEMPT_ID>/`.
3. **advisory recovery** — a first attempt commonly stops at the required
   `codex_advisory_evidence` gate with the task `blocked`. Run
   `ops/run_advisory_recovery.sh <TASK_KEY> <ATTEMPT_ID>` to produce confirm-run
   advisory evidence and take the audited transition to `waiting_approval`.
4. **publish** — when internal publication is blocked, run
   `ops/publish_fallback.sh <TASK_KEY> <ISSUE_NUMBER>` to push the branch and
   open a draft PR.
5. **merge** — human review. Read the diff and the evidence, then merge the PR
   yourself. No script merges.
6. **archive** — after the merge lands, kill any surviving executor processes
   (see rule 2 below), then archive the task and its worktree.

## Hard-won operational rules

### 1. NEVER start a tick while a PR merge is pending

A tick started before the previous PR is merged branches its attempt from a
stale `main`, and the resulting work conflicts or silently drops the pending
change. Before labelling the next issue, verify both:

```bash
gh pr view <n> --json state -q .state   # must print MERGED
git pull                                # must fast-forward, no merge commit
```

Only once the PR reads `MERGED` and `git pull` fast-forwards is it safe to label
the next issue and tick.

### 2. Killing the tick tmux session does NOT kill the executor

The Codex process tree is not a child of the tmux session in any way that
`tmux kill-session` reaches. Killing the session leaves the executor running: it
keeps writing into the attempt worktree and artifact directory, which corrupts
evidence and can resurrect files after you archive them.

Kill the executor explicitly and confirm it is gone before archiving:

```bash
ps aux | grep codex        # identify the surviving process tree
kill <pid>                 # escalate to kill -9 only if it does not exit
ps aux | grep codex        # confirm nothing remains
```

Only then archive the task and remove its worktree.
