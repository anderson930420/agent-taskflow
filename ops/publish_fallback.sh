#!/bin/bash
# Usage: publish_fallback.sh AT-GH-XXX <issue_number>
set -e
TASK="$1"; ISSUE="$2"
[[ -z "$TASK" || -z "$ISSUE" ]] && { echo "usage: $0 AT-GH-XXX issue_number"; exit 2; }

# Guard: only publish tasks the mirror says are waiting_approval
STATUS=$(python3 -c "
import sqlite3
c = sqlite3.connect('file:/home/ubuntu/.agent-taskflow/state/github_issue_scheduler.sqlite3?mode=ro', uri=True)
r = c.execute(\"SELECT status FROM tasks WHERE task_key='$TASK'\").fetchone()
print(r[0] if r else 'NOT_FOUND')
")
[[ "$STATUS" != "waiting_approval" ]] && { echo "REFUSE: $TASK is '$STATUS', not waiting_approval"; exit 3; }

LOWER=$(echo "$TASK" | tr '[:upper:]' '[:lower:]')
ATTEMPT=$(ls /home/ubuntu/agent-taskflow/artifacts/github-issue-scheduler/$TASK/ | grep '^attempt-' | head -1)
WT="/home/ubuntu/agent-taskflow/.worktrees/$LOWER/$ATTEMPT"
cd "$WT"
git status --short
git add -A
if git diff --cached --quiet; then
  echo "(already committed, skipping commit)"
else
  git commit -m "$TASK: implement issue #$ISSUE (codex executor, validator evidence in artifact dir)

Human-fallback publication. Closes #$ISSUE."
fi
BRANCH=$(git branch --show-current)
git push -u origin "$BRANCH"
gh pr create --base main --head "$BRANCH" --draft \
  --title "$TASK: $(gh issue view $ISSUE --json title -q .title)" \
  --body "Automated implementation for #$ISSUE. Evidence in artifacts/github-issue-scheduler/$TASK/$ATTEMPT. Published via human fallback." || echo "(PR may already exist — check gh pr list)"
