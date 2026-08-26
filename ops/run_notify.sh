#!/bin/bash
# Operator wrapper for taskflow_dc_notify.py (agent-taskflow -> Discord).
#
# Secrets policy: DISCORD_WEBHOOK_URL is NEVER committed to this repository.
# It is read from the environment, or from an untracked ops/notify.env
# (see notify.env.example). ops/notify.env is listed in .gitignore.
#
# Usage:
#   ops/run_notify.sh
#   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... ops/run_notify.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Fall back to the untracked local env file only when the variable is unset.
if [[ -z "${DISCORD_WEBHOOK_URL:-}" && -f "$SCRIPT_DIR/notify.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$SCRIPT_DIR/notify.env"
  set +a
fi

if [[ -z "${DISCORD_WEBHOOK_URL:-}" ]]; then
  echo "run_notify.sh: DISCORD_WEBHOOK_URL is not set." >&2
  echo "  export it, or copy $SCRIPT_DIR/notify.env.example to $SCRIPT_DIR/notify.env" >&2
  exit 2
fi
export DISCORD_WEBHOOK_URL

export TASKFLOW_DB_PATH="${TASKFLOW_DB_PATH:-$HOME/.agent-taskflow/state/github_issue_scheduler.sqlite3}"
export TASKFLOW_ARTIFACT_ROOT="${TASKFLOW_ARTIFACT_ROOT:-$REPO_ROOT/artifacts/github-issue-scheduler}"
# Notifier cursor state is operator runtime state; keep it outside the repo.
export TASKFLOW_NOTIFY_STATE="${TASKFLOW_NOTIFY_STATE:-$HOME/agent-taskflow-ops/notify_state.json}"

exec python3 "$SCRIPT_DIR/taskflow_dc_notify.py"
