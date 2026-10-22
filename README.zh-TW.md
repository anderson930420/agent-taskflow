# Agent Taskflow

[English](README.md) | 繁體中文

Agent Taskflow 把 AI 程式工作當成 **Ticket** 來執行。每張 Ticket 在自己的 git
worktree 中執行一個有邊界的 AI executor，通過 deterministic validators 後，
與最新的 target branch 整合，並開成 GitHub pull request。由 owner 在 GitHub
審查並 merge；之後 Taskflow 驗證 merge 結果並清理。

Lifecycle 由 deterministic Python 掌管：claim、lease、worktree、validator、
integration、evidence 與 cleanup。AI 工具只是單一 worktree 裡的 worker，
不會變更 lifecycle 狀態，也不會 push、merge 或清理。

## Ticket 流程

```text
create     POST /api/tickets
execute    execution tick：claim Ticket、準備 worktree、執行專案的 executor 與
           validators、commit 結果 -> ready_for_integration
integrate  integration tick，每次處理一個 repository：rebase 到最新 target、
           重跑 validators、push task branch、建立或更新 PR -> needs_review
review     owner 在 GitHub 審查並 merge PR
close      下一輪 integration 記錄 merge、在 target history 中驗證，
           並移除 worktree -> completed
```

每個專案的 executor 與 validators 來自 `config/projects.yaml` 裡該專案的
`execution:` policy；agent-taskflow 本身使用 `claude-code` executor 與
`compile` validator。過期的 `needs_review` branch 會回到
`ready_for_integration` 並重新整合。無法繼續的 Ticket 會停在
`needs_decision`（validator 失敗、衝突、要求修改）、`failed`（runtime 失敗）
或 `cancelled`（例如 PR 未 merge 就被關閉）。

## 執行

Operator UI 是 owner 的 Personal AI Workspace 裡的 Taskflow 模組，那是一個從
server 端呼叫這個 API 的獨立 app；本 repository 沒有 UI。

```bash
python scripts/run_api.py --db-path /abs/path/state.db      # API 在 127.0.0.1:8100
python scripts/run_parallel_scheduler_tick.py --db-path /abs/path/state.db --jsonl
python scripts/run_integration_tick.py --db-path /abs/path/state.db \
  --repo owner/name --repo-path /abs/path/checkout --jsonl
```

兩個 tick 都只做一輪有界的處理就結束，由 cron 以短間隔執行。
`deploy/cron/v1-execution-tick.cron.example` 與
`deploy/cron/v1-integration-tick.cron.example` 列出 cron 設定，並說明每個 flag
與 exit code。每個 integration phase 預設都是唯讀預覽，除非傳入它自己的
`--confirm-*` flag。兩個 tick 都持有 non-overlap lock：重疊的執行會記錄
`skipped_overlap` 並以 75 結束。Schema 變更都是明確的 `scripts/migrate_*.py`
指令。安裝或變更 cron 是 owner 的操作。

## 安全邊界

- Taskflow 從不 merge pull request：`agent_taskflow/github_pr_adapter.py`
  會拒絕 `gh pr merge` 及其等效的 API 呼叫。
- 唯一的 push 是 `git push origin <task-branch>`。Taskflow 從不 force-push，
  從不 push `main`、`master`、`trunk` 或 target branch，也從不刪除 remote
  branch（`agent_taskflow/integration_git.py`）。
- 每個 PR 都由 owner 在 GitHub 審查並 merge。任何 agent 都不會 merge，也不會
  啟用 auto-merge。
- 只有在 target history 中驗證 merge 之後才會清理，而且只清理該 Ticket 自己
  乾淨的 worktree（`agent_taskflow/merge_verification.py`、
  `agent_taskflow/integration_cleanup.py`）。
- 每次 lifecycle 變更都是 SQLite store 裡可稽核的 task event。

## 開發

```bash
python -m venv .venv
.venv/bin/pip install -e . -c constraints.txt
.venv/bin/pip install pytest pytest-xdist
PYTHONPATH=. .venv/bin/python -m pytest tests -q -n 4
```

請使用可拋棄的 database；絕不對 live state 執行測試或 scripts。
Coding agents 從 `CLAUDE.md` 開始。`docs/v1/` 記錄每個 V1 step 的建置與端到端
測試，`docs/v0-supported-surface.md` 標示每個 entrypoint。
