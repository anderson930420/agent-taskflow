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

## Create Task and Dispatch UI

Mission Control provides two governance-driven interaction flows: creating a task record and dispatching it through the backend executor abstraction.

### Create Task Form

The `/tasks/new` page provides a Create Task form that calls `POST /api/tasks` (existing backend endpoint). Key properties:

- **Required fields:** task_key, project, repo_path, worktree_path, artifact_dir
- **Executor selector:** OpenCode, Pi (governance mission contract), Shell, Manual. Selecting Pi shows a note that Pi uses mission contract/mission plan but does not self-approve.
- **Validator selector:** single validator field (mapping to backend `validator` column). Default validators are `pytest` and `openspec`. Optional validators (policy, typecheck, lint) are opt-in.
- **Optional fields:** model, title, board, hermes_task_id, branch, base_branch, pr_url, pr_number
- **Inline validation:** task_key format, absolute path enforcement for required path fields
- **Governance warning:** form shows a warning box explaining that creating a task does not start a worker, workers run after the Start/Dispatch action, and the UI does not push/merge/cleanup.
- **Confirmation dialog:** requires explicit user confirmation before submitting.
- **On success:** navigates to task detail page after 1.2s delay.
- **On error:** shows backend validation error from the API.

### Start / Dispatch UI

The task detail page provides a `StartDispatchPanel` component above the state timeline. It calls `POST /api/tasks/{key}/start` (existing backend endpoint).

- **Enable condition:** task is in `queued`, `blocked`, or `preparing` state only.
- **Disabled for terminal states:** waiting_approval, accepted, rejected, etc. — shows a message directing the user to the approval action instead.
- **Options panel (collapsible):** executor selector, model input, validator multi-select (checkboxes), dry_run toggle.
- **Default validators:** pytest and openspec are always pre-selected and disabled (cannot be unchecked). Optional validators (policy, typecheck, lint) can be toggled.
- **Confirmation dialog:** shows clear governance warning: UI does not execute Pi/OpenCode/Shell directly, workers cannot approve or push, deterministic validators remain required, human approval is the final gate.
- **Result banner:** shows success/failure message with resulting task status.
- **On failure:** shows backend error message.


### Executor Selector

The UI shows four executor choices:

- **OpenCode:** default, maps to `opencode` executor
- **Pi (governance mission contract):** maps to `pi` executor, uses mission contract/mission plan when available, does not self-approve
- **Shell:** maps to `shell` executor
- **Manual:** maps to `manual` executor

Executor selection is passed to the backend dispatcher via the start endpoint. The UI does not directly invoke any executor.

### Validator Selector

Validators shown in both create form and dispatch panel:


- **Default (always required):** `pytest` (runs project test suite), `openspec` (checks spec consistency)
- **Optional (opt-in per task):** `policy` (checks governance artifacts/logs), `typecheck` (runs mypy/TypeScript checks), `lint` (runs ruff/flake8)

No validator can replace human approval. The UI enforces that default validators are always selected and cannot be disabled.


### UI Does Not Directly Execute Pi/OpenCode/Shell

Workers run through the agent-taskflow backend dispatcher abstraction only. The UI:
- Does not spawn subprocesses
- Does not invoke Pi CLI directly
- Does not run Shell commands directly
- Does not bypass the executor abstraction layer

### No Push/Merge/Cleanup/Delete

Neither the create task flow nor the dispatch flow produces side effects beyond what the backend API records. Specifically:
- No git push, force push, or branch push
- No PR merge or branch merge
- No worktree cleanup or artifact cleanup
- No worktree deletion or branch deletion

### Create Task Does Not Auto-Approve

Creating a task record only registers it in the local mirror store. It does not:
- Start a worker
- Approve the task
- Bypass validators
- Create a PR

### Human Approval Remains Final Gate

The dispatch flow ends with the task reaching `waiting_approval`. The task detail page directs the user to the **Approve / Reject** action in the ActionPanel. Workers cannot approve themselves — human review is required.

### Task Detail Remains Approval Surface

The task detail page is where full review evidence is available. The dispatch panel feeds into the same state machine that ultimately requires human approval. No approval action is available from the board or the dispatch panel itself.

## Artifact Review and Full Preview UX

Mission Control provides a comprehensive artifact review surface on the task detail page, with structured viewers, inline previews, and a full preview modal.

### Artifact Review Panel

The `ArtifactReviewPanel` component displays all task artifacts from the review evidence endpoint. It provides:

- **Summary bar:** total artifacts, previewable count, secret warning count, executor/validator log counts.
- **Category filter pills:** All, Mission, Pi Protocol, Executor, Validator, Prompts, Other.
- **Special structured viewers** for known artifact types.
- **Artifact list** with expandable rows and inline preview.


All previews use the existing `GET /api/tasks/{key}/artifacts/{name}` endpoint. No filesystem access from the UI.

### Artifact Classification

Artifacts are classified by examining artifact metadata from review evidence:

- **mission:** `is_mission_contract: true` or name === `mission_contract.json`
- **pi_protocol:** name === `pi_mission_plan.json` or `pi_mission_prompt.md`
- **executor_logs:** `is_executor_log: true`
- **validator_logs:** `is_validator_log: true`
- **prompts:** name === `implementation_prompt.md` or `*_prompt.md`
- **other:** all remaining artifacts

### MissionContractViewer

For `mission_contract.json`, a structured viewer shows:

- Status badge (present / missing / invalid)
- Schema version
- Task key, executor, goal, human_approval_required
- Required validators (as colored pills)
- Forbidden actions (red rows with ✕ icon)
- Expected artifacts
- Governance rules

Highlights: `human_approval_required: true` shown in yellow, forbidden actions in red.

### PiMissionPlanViewer

For `pi_mission_plan.json`, a structured viewer shows:

- Schema version, task key, executor, goal
- Human approval requirement
- Required validators
- Protocol steps with numbered circles and descriptions

A note is shown: "These are protocol steps, not autonomous agents. The UI does not execute steps. Deterministic validators run separately."

If JSON parse fails, falls back to raw text preview.

### PolicyLogViewer

For `policy-validate.log`, a log viewer shows:

- Policy check status badge (passed / failed / blocked / not_run)
- Status banner: "Policy check passed" (green) or "Policy check failed" (red with guidance)
- Log lines with syntax highlighting:
  - Green + left border: lines mentioning "passed", "✓", "no violations"
  - Red + left border: lines mentioning "failed", "✗", "violation"
  - Yellow: lines mentioning "warning", "secret", "api_key", "token"
  - Red + ⚠ prefix: forbidden action lines

The UI does NOT alter or rerun policy validation results.

### Inline Preview

Artifact rows expand on click (▶ toggle button). When expanded:

1. Calls `GET /api/tasks/{key}/artifacts/{name}` to load preview.
2. Shows monospaced content with preserved whitespace (max 240px height, scrollable).
3. Shows truncation notice if preview was truncated by backend.
4. Shows secret warning if secrets detected.
5. Shows preview unavailable reason if backend blocks preview.
6. Does NOT auto-fetch all previews (only on user action).

### ArtifactPreviewModal

Clicking "Modal" on any artifact row opens a full-screen modal:

- Shows artifact name, kind badge, size
- Shows secret warning if applicable
- "Load preview" button (only if not loaded and no secrets)
- Full monospaced content with unlimited scroll height
- Truncation notice if truncated
- Close button + Escape key support
- No external modal library used

### No Direct Filesystem Access

The UI never reads files directly. All artifact content is served through the backend artifact preview API. The backend enforces path traversal protection (artifacts must be inside the task artifact directory).

### No Rerun of Validators

The frontend never reruns any validator. All validator results are pre-recorded in the database. The Validator Summary Card and Policy Log Viewer read existing data only.

### No Push/Merge/Cleanup/Delete in Artifact Review

Artifact previews and structured viewers do not expose any push, merge, cleanup, or delete actions. They are read-only review surfaces.

## API Health, Loading, and Evidence Preview UX

Mission Control provides operator-experience improvements for API reachability, loading states, error display, validator summaries, and executor log previews.

### API Health / Reachability Indicator

The Mission Control dashboard shows an API status indicator in the top bar. It calls `GET /health` on the Agent Taskflow API every 30 seconds.

States:
- **Loading:** "Checking API…" with a muted pulsing dot.
- **Connected:** "API connected" with a green dot and service name.
- **Degraded:** "API error" with a red dot and the backend error message.

The indicator does not trigger any write action. It is purely read-only.


### Loading States

A `loading.tsx` file at `mission-control/app/loading.tsx` provides a simple spinner with accessible text "Loading Mission Control…" while Next.js Suspense resolves the dashboard data.


Task detail and create task pages use Next.js `loading.tsx` if data is slow. All loading states are minimal and non-blocking.


### API Error Panel

`ApiErrorPanel` is a reusable component for displaying backend API errors:
- Shows the error title and message
- Shows HTTP status if available
- Shows a retry guidance message (no stack traces)
- Optional retry button

`ApiErrorBanner` is a smaller inline version for inline action failures.

Both components never show raw stack traces. Errors are displayed as user-friendly messages with clear next steps.


### Validator Summary Card

The task detail page shows a `ValidatorSummaryCard` above the full Review Evidence section. It displays:

- **Pass/fail/blocked/other counts** as colored badges.
- **Policy check banner** highlighted by color (green/yellow/red) with policy warnings.
- **Per-validator rows** showing validator name, exit code, and status.
- **Loading skeleton** while review evidence is being fetched.
- **Empty state** if no validator evidence is loaded yet.

Data comes from the existing `/api/tasks/{key}/review-evidence` endpoint. No validators are rerun by the frontend.

### Executor Log Preview Panel

The task detail page shows an `ExecutorLogPanel` below the Validator Summary Card.

Executor logs are identified from the review evidence artifact list (`is_executor_log: true`). The panel shows:

- Log name (Pi executor log, OpenCode log, etc.)
- File name and size
- "Load preview" button if preview is available and no secrets detected

On clicking "Load preview":
1. Calls `GET /api/tasks/{key}/artifacts/{name}` (existing artifact preview endpoint).
2. Shows truncated preview (first 20 KB) with a warning if the file was truncated.
3. Shows secret warning if the file contains high-confidence secret patterns.
4. Shows preview unavailable message if the backend returns a reason.

If no executor log artifacts exist, shows an empty state with a description.

The panel does NOT read the filesystem directly. All preview content comes from the backend artifact preview API.

### No Rerun of Validators

The frontend never reruns validators. The Validator Summary Card reads from the existing `review-evidence` endpoint which includes pre-recorded validator results. No validator subprocess is spawned by the UI.

### No Direct Filesystem Access

The UI never reads files directly. All artifact previews, executor logs, and mission contracts are served through the backend API. The backend enforces path traversal protection (artifacts must be inside the task artifact directory).


### No Push/Merge/Cleanup/Delete in Evidence UX

The evidence preview components (ValidatorSummaryCard, ExecutorLogPanel, ReviewEvidenceSection) do not expose any push, merge, cleanup, or delete actions.