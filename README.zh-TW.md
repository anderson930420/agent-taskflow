# Agent Taskflow

[English](README.md) | 繁體中文

Agent Taskflow 是用於 AI-assisted GitHub 工作的 Practical V1 control plane。
Deterministic Python lifecycle 負責 Ticket、attempt、lease、isolated worktree、
validator gate、integration evidence 與 cleanup eligibility；AI 只是 bounded
implementation executor，不擁有 delivery lifecycle 的決策權。

產品承諾刻意保持狹窄：把 Ticket 變成可在 GitHub 審查的 PR，同時保留證據，
並把最終 merge 留給人類。

## 目前的 Practical V1 流程

```text
Create Ticket
  -> runtime admission、one worktree、bounded executor
  -> deterministic validation 與 persisted evidence
  -> ready_for_integration 與 per-repository FIFO queue
  -> 在明確 invoke 時：對 latest target integration、validate、push/update PR
  -> GitHub human review 與 human merge
  -> 在明確 invoke 時：偵測並驗證 GitHub merge result，然後 cleanup
  -> completed
```

已實作的 producer half 是自動的：成功的 Ticket 不會經過 legacy
`waiting_approval` gate，而是從 validation 轉到 `ready_for_integration`，
並只入自己 repository 的 queue 一次。這是目前成功 implementation path，
不是多一層 approval。

公開顯示 vocabulary 使用 `needs_review`、`completed` 等名稱；既有 persisted
vocabulary 仍有 legacy spelling（例如 `waiting_for_review`、`cleaned`、
`canceled`）。唯一 mapping boundary 是 `agent_taskflow/status_vocab.py`。

### 現在已自動化的部分

* Ticket admission 強制 dependency、lease 與 configured concurrency。
* 每張 Ticket 有自己的 isolated worktree；attempt 與 lifecycle evidence 寫入 SQLite。
* Executor path 記錄 runtime progress 並跑 deterministic validators。
* 成功 implementation 轉到 `ready_for_integration`，並恰好一次進入
  repository 的 FIFO integration queue。
* 在 caller 明確確認時，integration controller 能取得 latest target、initial
  integration 或 no-force-push re-integration、重跑 validators，並建立或更新同一個
  GitHub PR。
* Stale PR detection、PR outcome polling、GitHub merge commit verification、
  gated cleanup 的 component 都已存在。

### 現在仍由 operator 或人類把關的部分

F9（integration-queue consumer tick）尚未實作。因此目前沒有 non-test caller
會 drain `ready_for_integration`；operator 必須明確以 `dry_run=False` 與
`confirm_integration=True` 呼叫 integration controller。這是 integration
trigger，不是 merge approval；controller 預設 dry-run／需要 confirmation。

Integration 後，Taskflow 可以 push task branch 並建立或更新 draft PR；但
**GitHub review 與 merge 一律由 human 完成**。Taskflow 不會呼叫 `gh pr merge`，
不能 self-approve，也不會啟用 auto-merge。

F10 也尚未完成。目前 scope 是其餘 V1 invocation surface：target-freshness
polling、PR-outcome polling、verified-merge cleanup，以及 parallel execution tick
的 scheduling。這些 component 不能被描述為已安裝的 autonomous service。Ruling 65
記錄 missing-invoker history；FOLLOWUPS.md 的 execution-vehicle draft 依 SPEC §47
propose short、idempotent cron tick，human installation 仍是另一個動作。F10 必須
決定 deployment surface。

## 安全邊界與 enforcement points

| 邊界 | Enforcement point |
| --- | --- |
| AI 是 bounded worker，不是 lifecycle authority | Runtime admission 與 dispatcher（`agent_taskflow/runtime_admission.py`、`agent_taskflow/dispatcher.py`） |
| One Ticket = one worktree；retry 保留可稽核的 Ticket context | Worktree 與 attempt resources（`agent_taskflow/ticket_worktree.py`、`agent_taskflow/attempt_resources.py`） |
| Execution 有上限且遵守 dependency | Atomic claim 與 capacity controls（`agent_taskflow/runtime_admission.py`、`agent_taskflow/runtime_capacity.py`） |
| Blocked 或 paused Ticket 不會執行 | Claim 與 transition guards（`agent_taskflow/runtime_admission.py`、`agent_taskflow/ticket_lifecycle.py`） |
| Integration per-repository serialized | Integration lock 與 queue（`agent_taskflow/integration_controller.py`、`agent_taskflow/integration_queue.py`） |
| 已發布 PR branch 不可 force-push | Integration Git policy（`agent_taskflow/integration_git.py`） |
| Protected 或 target branch 不是 task push target | Branch normalization 與 push allowlist（`agent_taskflow/integration_git.py`） |
| 是 validators，不是 GitHub CI，決定 `needs_review` | `agent_taskflow/integration_validators.py` |
| 只有 GitHub human review 可以 merge | `agent_taskflow/integration_controller.py` 的 manual-merge invariant；不會發出 merge command |
| Cleanup 需要 verified merge（或 explicit cancelled-work approval） | `agent_taskflow/integration_cleanup.py` |
| Lifecycle mutation 可供檢視 | SQLite task events 與 integration evidence |

GitHub CI 是可見的 review information，不是另一個 Taskflow lifecycle authority。
在 Ticket execution path，validator failure 停在 `needs_decision`，runtime failure
使用 `failed`；integration failure（包含 integration-validator failure）停在
`needs_decision`。Legacy GitHub-issue path 保留舊的 `blocked`/
`waiting_approval` vocabulary。PR 若 closed 但未 merge，Ticket 為 cancelled，
worktree 會保留直到 explicit cleanup confirmation。

## Operation facts 與 source records

記錄中的 production capacity 是 **2 active executor leases**，不是假設的 config
file 值。Operator deployment record 位於 read-only
`~/agent-taskflow-ops/v1/RULINGS.md` 52；同一筆也記錄 default 為 1、enforcement
在 claim transaction。提高到 1 以上需要 commit-bound rehearsal evidence。Ruling 48
與 50 的 production preparation 在 backup database 後執行了下列 explicit
migrations：

```text
scripts/migrate_ticket_fields.py
scripts/migrate_runtime_progress.py
scripts/migrate_ticket_worktree_resources.py
```

這些是 deployment evidence，不是要讀者對任意 database 執行 migration 的說明。
Migration script 是 explicit、idempotent；startup 不會靜默套用 Ticket-field migration。

Implementation 與 real-GitHub evidence 的位置：

* `docs/v1/handoff-step1.md`：Ticket fields 與 creation。
* `docs/v1/handoff-step3.md`：runtime progress。
* `docs/v1/handoff-step4.md`、`docs/v1/handoff-step5.md`：capacity、admission、
  attempts 與 parallel execution。
* `docs/v1/handoff-step2.md`、`docs/v1/handoff-step2-e2e.md`：integration、PR
  handling、merge verification、cleanup components。
* `docs/v1/handoff-f4.md`、`docs/v1/handoff-f8.md`、`docs/v1/handoff-f8-e2e.md`：
  automatic handoff 到 `ready_for_integration`、queue evidence 與剩餘的 manual trigger。

權威且 read-only 的 control records 是 `~/agent-taskflow-ops/v1/SPEC.md`、
`~/agent-taskflow-ops/v1/RULINGS.md` 與 `~/agent-taskflow-ops/v1/FOLLOWUPS.md`。
較高階 Master Spec 與 Level 2 Roadmap 只在不與 Practical V1 或較新的 human ruling
衝突時作為設計輸入。尤其兩者都不授權 auto-merge，也不授權恢復 legacy
`waiting_approval` success path。

## Canonical ExecutionEngine authority

Confirmed Level 2 scheduler execution 經由
`SchedulerExecutionEngineAuthority` 與其 canonical `ExecutionEngine` result。
`--use-execution-engine` 是 compatibility flag；它不會恢復 legacy scheduler
fallback。此 authority 在所有 confirmed Level 2 execution 仍是 canonical；它不會
改變 F8 指定的 Ticket success status 或 human GitHub merge gate。

## 邁向 unattended V1 round 前的剩餘工作

* **F7（本文件更新）：** 使 public description 與已 merge 的 F4/F8 behavior 一致。
* **F9：** 建立一個 idempotent integration tick，依 FIFO drain 各 repository queue。
  它必須呼叫既有 controller，不得更動 controller 的 lock 或 merge semantics。
* **F10：** 呼叫其餘既有 consumer components，並確定 cron deployment surface，
  包括 non-overlap locks。
* **F2：** 完成 deferred repository-wide status vocabulary 與 explicit migration
  cleanup。
* **F9/F10 後：** 跑第一張真實 Ticket。由它的 observed friction，而不是這份
  documentation，排定 Step 6、F6、F5 與後續工作。Open PR 在真正 merge 前仍是
  human-review dependency。

## Development validation

請在 disposable development database 與 worktree 執行 repository validators，
不要把本 README 當作 production operator runbook：

```bash
PYTHONPATH=. .venv/bin/python scripts/validate_workflow_contract.py
PYTHONPATH=. .venv/bin/python scripts/validate_workflow_policy.py
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```

不要用 production database 做 local validation 或實驗。
