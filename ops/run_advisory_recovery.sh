#!/bin/bash
# Operator recovery for a task blocked at the codex_advisory_evidence gate.
#
# Generates confirm-run advisory evidence with the real Codex CLI, then runs
# the audited retry transition that moves the task blocked -> waiting_approval.
# Both steps are explicit and audited; neither approves the task.
#
# Usage: ops/run_advisory_recovery.sh <TASK_KEY> <ATTEMPT_ID>
#   e.g. ops/run_advisory_recovery.sh AT-GH-181 attempt-7fb0efa08eac422a9f8ba079fe3e7262
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TASK="${1:-}"; ATTEMPT="${2:-}"
[[ -z "$TASK" || -z "$ATTEMPT" ]] && { echo "usage: $0 AT-GH-XXX attempt-XXXX" >&2; exit 2; }

LOWER=$(echo "$TASK" | tr '[:upper:]' '[:lower:]')
ARTDIR="$REPO_ROOT/artifacts/github-issue-scheduler/$TASK/$ATTEMPT"
WORKTREE="$REPO_ROOT/.worktrees/$LOWER/$ATTEMPT"

CODEX_BIN="${CODEX_BIN:-/home/ubuntu/tools/pi-agent/bin/codex}"
TASKFLOW_DB_PATH="${TASKFLOW_DB_PATH:-$HOME/.agent-taskflow/state/github_issue_scheduler.sqlite3}"
TASKFLOW_OPERATOR="${TASKFLOW_OPERATOR:-anderson}"

cd "$REPO_ROOT"
PYTHONPATH=. .venv/bin/python3 scripts/run_codex_advisory_review.py \
  --task-key "$TASK" \
  --repo-path "$REPO_ROOT" \
  --worktree-path "$WORKTREE" \
  --artifact-dir "$ARTDIR" \
  --confirm-run \
  --codex-command "$CODEX_BIN exec --sandbox danger-full-access" \
  --timeout-seconds 600

PYTHONPATH=. .venv/bin/python3 scripts/retry_advisory_evidence_transition.py \
  --task-key "$TASK" \
  --db-path "$TASKFLOW_DB_PATH" \
  --artifact-dir "$ARTDIR" \
  --operator "$TASKFLOW_OPERATOR" \
  --confirm-transition
