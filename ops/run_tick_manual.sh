#!/bin/bash
# Run one github-issue scheduler tick by hand (the same tick cron drives).
#
# HARD RULE: never start a tick while a PR merge is pending. Confirm the open
# PR is MERGED and that `git pull` fast-forwards first -- see ops/README.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TASKFLOW_DB_PATH="${TASKFLOW_DB_PATH:-$HOME/.agent-taskflow/state/github_issue_scheduler.sqlite3}"
TASKFLOW_REPO="${TASKFLOW_REPO:-anderson930420/agent-taskflow}"

cd "$REPO_ROOT"
PYTHONPATH=. .venv/bin/python3 scripts/run_github_issue_one_task_scheduler_tick.py \
  --repo "$TASKFLOW_REPO" \
  --db-path "$TASKFLOW_DB_PATH" \
  --local-repo-path "$REPO_ROOT" \
  --artifact-root "$REPO_ROOT/artifacts/github-issue-scheduler" \
  --include-label agent-taskflow-ready \
  --confirmed \
  --executor pi \
  --pi-bin "$SCRIPT_DIR/codex-as-pi.sh" \
  --validator pytest \
  --operator anderson \
  --json
