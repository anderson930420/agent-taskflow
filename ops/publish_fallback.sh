#!/bin/bash
# Human-fallback publication for an attempt whose internal publication path is
# blocked (finding #8): commit the attempt worktree, push its branch, and open
# a DRAFT PR. Never merges -- human review remains the final gate.
#
# Usage: ops/publish_fallback.sh <TASK_KEY> <ISSUE_NUMBER>
#   e.g. ops/publish_fallback.sh AT-GH-181 181
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TASK="${1:-}"; ISSUE="${2:-}"
[[ -z "$TASK" || -z "$ISSUE" ]] && { echo "usage: $0 AT-GH-XXX issue_number" >&2; exit 2; }

LOWER=$(echo "$TASK" | tr '[:upper:]' '[:lower:]')
ATTEMPT=$(ls "$REPO_ROOT/artifacts/github-issue-scheduler/$TASK/" | grep '^attempt-' | head -1)
[[ -z "$ATTEMPT" ]] && { echo "$0: no attempt-* artifact dir for $TASK" >&2; exit 2; }
WT="$REPO_ROOT/.worktrees/$LOWER/$ATTEMPT"

cd "$WT"
git status --short
git add -A
git commit -m "$TASK: implement issue #$ISSUE (codex executor, pytest green, advisory reviewed)

Human-fallback publication; internal publication blocked by finding #8. Closes #$ISSUE."
BRANCH=$(git branch --show-current)
git push -u origin "$BRANCH"
gh pr create --base main --head "$BRANCH" --draft \
  --title "$TASK: $(gh issue view "$ISSUE" --json title -q .title)" \
  --body "Automated implementation for #$ISSUE. Evidence in artifacts/github-issue-scheduler/$TASK/$ATTEMPT (pytest green, advisory review). Published via human fallback (finding #8)."
