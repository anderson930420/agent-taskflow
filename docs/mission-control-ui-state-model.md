# Mission Control UI State Model

## Overview

Mission Control is a governance control plane UI for the agent-taskflow system. It provides task state visibility, action controls via backend API, and review evidence integration.

## Core Principles

1. **UI is a control/review layer, not an executor.** The UI never directly executes Pi, OpenCode, or Shell commands.
2. **All UI actions go through backend FastAPI endpoints only.**
3. **Human approval is the final gate.** Workers cannot self-approve.
4. **No destructive actions from UI:** No push, merge, cleanup, worktree deletion, or branch deletion.
5. **State machine is backend-controlled.** The UI displays state but does not modify the state machine logic.

## Task States

Each task status maps to a category with specific meaning and allowed actions.

### `queued`

- **Category:** `not_started`
- **Meaning:** Task is registered and waiting to be dispatched. Executor has not started.
- **Allowed actions:** `start`, `block`
- **UI behavior:** Start button enabled. Approve/reject disabled.

### `preparing`

- **Category:** `running`
- **Meaning:** Dispatcher is setting up worktree, artifact directory, and mission contract.
- **Allowed actions:** (none)
- **UI behavior:** All action buttons disabled. Timeline shows active step.

### `implementing`

- **Category:** `running`
- **Meaning:** Executor backend is actively running inside the assigned worktree.
- **Allowed actions:** (none)
- **UI behavior:** All action buttons disabled. Executor metadata shown.

### `validating`

- **Category:** `running`
- **Meaning:** Deterministic validators (e.g., pytest, openspec) are running against the output.
- **Allowed actions:** (none)
- **UI behavior:** All action buttons disabled. Validation results section active.

### `waiting_approval`

- **Category:** `review`
- **Meaning:** Validation passed. Task requires human review and approval before it can be accepted.
- **Allowed actions:** `approve`, `reject`, `block`
- **UI behavior:** Approve/reject/block all enabled. Review evidence must be loaded before approving.

### `waiting_for_review`

- **Category:** `terminal_skipped`
- **Meaning:** Task has paused for review. No action pending.
- **Allowed actions:** (none)
- **UI behavior:** All buttons disabled.

### `accepted`

- **Category:** `terminal_success`
- **Meaning:** Human approval has accepted the task output. Task is complete.
- **Allowed actions:** (none)
- **UI behavior:** Terminal state. No action panel shown.

### `completed`

- **Category:** `terminal_success`
- **Meaning:** Task completed successfully.
- **Allowed actions:** (none)
- **UI behavior:** Terminal state. No action panel shown.

### `rejected`

- **Category:** `terminal_failure`
- **Meaning:** Human review rejected the task output.
- **Allowed actions:** (none)
- **UI behavior:** Terminal state. Reject reason shown if available.

### `failed`

- **Category:** `terminal_failure`
- **Meaning:** Executor or validator reported failure.
- **Allowed actions:** `block`
- **UI behavior:** Block enabled. Failure reason shown. May show retry option in future.

### `blocked`

- **Category:** `terminal_blocked`
- **Meaning:** Task was manually blocked or hit a governance error. No executor action will run.
- **Allowed actions:** (none)
- **UI behavior:** Blocked reason prominently displayed. No action panel shown.

### `cleaned`

- **Category:** `terminal_skipped`
- **Meaning:** Task was cleaned up.
- **Allowed actions:** (none)
- **UI behavior:** Terminal state.

### `canceled`

- **Category:** `terminal_skipped`
- **Meaning:** Task was canceled.
- **Allowed actions:** (none)
- **UI behavior:** Terminal state.

## UI Never Performs

The Mission Control UI explicitly does NOT provide:

- **No push:** No git push, no force push
- **No merge:** No PR merge, no branch merge
- **No cleanup:** No worktree cleanup, no artifact cleanup, no temporary file deletion
- **No delete:** No branch deletion, no worktree deletion, no artifact deletion
- **No direct executor execution:** No direct Pi CLI call, no OpenCode execution, no Shell execution
- **No auto-approval:** Approve button requires human operator identity
- **No autonomous loop:** No multi-round goal loop, no AI reviewer replacing deterministic validators

## Action Controls

UI exposes only these safe action controls, gated by current task state:

| Action | Endpoint | Enable condition |
|---|---|---|
| Start task | `POST /api/tasks/{key}/start` | `queued`, `blocked`, `preparing` |
| Approve task | `POST /api/tasks/{key}/approve` | `waiting_approval` only |
| Reject task | `POST /api/tasks/{key}/reject` | `waiting_approval`, `blocked` |
| Block task | `POST /api/tasks/{key}/block` | `queued`, `preparing`, `implementing`, `validating`, `waiting_approval` |

### Approval Confirmation

When user clicks "Approve task", the UI shows a confirmation dialog with:

```
Approving accepts the task output for this governance workflow.
This does NOT push, merge, or cleanup any branch or worktree.
Human approval is the final gate.

Proceed to approve {task_key}?
```

### Reject Confirmation

```
Rejecting marks the task output as not accepted.
Provide a reason so the task can be reworked.
This does NOT delete any artifacts.

Proceed to reject {task_key}?
```

### Block Confirmation

```
Blocking stops the task immediately.
Provide a clear reason.
This does NOT cleanup or delete any files.

Proceed to block {task_key}?
```

## State Timeline

The task detail page shows a visual timeline of:

`queued → preparing → implementing → validating → waiting_approval → terminal`

Terminal states shown as final step: `Approved`, `Rejected`, `Failed`, `Blocked`, `Cleaned`, `Canceled`

## Review Evidence Integration

Before a human can approve a task in `waiting_approval` state:

1. **Mission Contract card** — Shows contract status, required validators, forbidden actions, expected artifacts
2. **Validator Results table** — Shows each validator's status, exit code, summary
3. **Artifacts table** — Shows each artifact with preview capability and secret warnings

If policy warnings exist, they are shown before the contract card in a yellow warning box.

## Forbidden Actions in Mission Contract

The mission contract `forbidden_actions` list explicitly prevents the worker from:
- self-approval
- pushing to main/master
- merging branches
- deleting worktrees
- running cleanup operations
- bypassing validators

The UI does not enforce these at the UI layer — enforcement is the backend dispatcher's responsibility.

## Backend API Endpoints Used

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks` | List tasks |
| GET | `/api/tasks/{taskKey}` | Get single task |
| GET | `/api/tasks/{taskKey}/runs` | Get executor runs |
| GET | `/api/tasks/{taskKey}/artifacts` | Get artifacts |
| GET | `/api/tasks/{taskKey}/validations` | Get validation results |
| GET | `/api/tasks/{taskKey}/approvals` | Get approval decisions |
| GET | `/api/tasks/{taskKey}/review-evidence` | Get review evidence |
| GET | `/api/tasks/{taskKey}/artifacts/{name}` | Get artifact preview |
| POST | `/api/tasks/{taskKey}/start` | Start/dispatch task |
| POST | `/api/tasks/{taskKey}/approve` | Human approval |
| POST | `/api/tasks/{taskKey}/reject` | Human rejection |
| POST | `/api/tasks/{taskKey}/block` | Block task |

## No New Endpoints in UI Phase

No new backend API endpoints are introduced in this UI improvement phase. All action controls call existing endpoints. No push/merge/cleanup/delete endpoints are added to the UI.

## Task Board State Grouping

The Mission Control dashboard (`/`) displays all tasks on a visual board grouped by state category.

### State Categories

Tasks are grouped into the following categories, mapped from the backend task status:


| Category | Statuses | Color |
|---|---|---|
| Not Started | `queued` | muted |
| Running | `preparing`, `implementing`, `validating` | blue |
| Needs Review | `waiting_approval`, `waiting_for_review` | yellow |
| Succeeded | `accepted`, `completed` | green |
| Failed | `rejected`, `failed` | red |
| Blocked | `blocked` | red |
| Closed | `cleaned`, `canceled` | muted-2 |

The board uses `taskState.ts` metadata — `TASK_CATEGORIES`, `getTaskCategory()`, `getCategoryForStatus()`, `countTasksByCategory()` — to derive colors, labels, and counts from the backend task list.

### Search and Filter Behavior

The board provides:

1. **Search** — Client-side filter by task key, title, executor, model, project, or provider. No backend query required.
2. **Category filter** — One-click category summary bar. Selecting a category shows only tasks in that category. "All" resets the filter.
3. **Combined** — Search and category filter can be applied together.

### Card Metadata

Each task card on the board shows:

- State badge (colored, with status label and category pill)
- Task key
- Title or task key if title is absent
- Executor / model / provider subtitle
- Last updated timestamp
- Quick link to task detail page

No push/merge/cleanup/delete actions are available from board cards. "View details" navigates to the task detail page where full review evidence and action controls are available.


### No Direct Executor Actions from Board

The board is read-only:
- No approve / reject / block from board cards
- No start task from board
- No direct Pi / OpenCode / Shell execution
- No push / merge / cleanup / delete

Approval actions remain in the task detail page ActionPanel where review evidence must be loaded before a human can approve.


### Empty / Loading / Error States

- **No tasks**: Shows a placeholder message with a "Create Task" CTA.
- **No results after filter**: Shows "No matching tasks."
- **API error**: Shows a full-page error panel with error message and API base URL.
- **Loading**: Handled by Next.js `loading.tsx` or Suspense boundary.

### Review Evidence Is the Basis for Human Approval

The board provides an overview. Full review evidence — mission contract, validator results, artifact previews — is available on the task detail page. Human approval requires reviewing this evidence through the backend `/review-evidence` API.