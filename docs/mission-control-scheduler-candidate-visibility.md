# Mission Control Scheduler Candidate Visibility (Phase I)

## Purpose

Phase I exposes the Phase H read-only scheduler candidate readback API in
Mission Control. It is a **visibility layer only**: operators can see which
tasks the discovery layer classifies as scheduler candidates, what gate they
sit at, and what evidence is missing, without any execution control.

Candidate visibility is **NOT execution permission**. Mission Control remains
read-only. Human/operator confirmation remains required for every downstream
action.

## UI surfaces

### Dashboard candidate overview

Renders on the main board (`mission-control/app/page.tsx` →
`components/TaskBoard.tsx`) in a `Scheduler Candidates` section that shows:

- total candidate count
- ready candidate count
- discovery note from the API
- a read-only candidate list table:
  - task key (navigation link to the task detail page only — no action)
  - status
  - recommended command kind
  - required next gate
  - candidate ready
  - safety labels (`read_only`, `proposal_created=false`,
    `confirmation_created=false`, `handoff_created=false`,
    `runtime_started=false`, `approved_task_runner_called=false`,
    `github_mutated=false`, `approved=false`, `merged=false`,
    `cleanup_performed=false`, `background_worker_started=false`)

If the candidates endpoint fails, the dashboard renders an inline error state
inside the same section. The dashboard never converts that failure into an
action surface.

### Task detail Scheduler Candidate panel

Renders on `mission-control/app/tasks/[taskKey]/page.tsx` as a
`Scheduler Candidate` section backed by
`components/SchedulerCandidatePanel.tsx#TaskSchedulerCandidatePanel`.

It loads through `getTaskSchedulerCandidate(taskKey)` (best-effort): if the
endpoint fails, the rest of the task detail bundle continues to load and the
panel shows `Scheduler candidate readback unavailable`.

## What it displays

The per-task panel shows:

- `candidate_ready`
- `recommended_command_kind`
- `current_phase_label`
- `required_next_gate`
- `required_operator_action`
- `missing_evidence`
- `consistency_warnings`
- `reason`
- `blocked_reason`
- `severity`
- `confidence`
- `discovery_note`
- `safety` flags

If no candidate is associated with the task, the panel renders an empty
state: `No scheduler candidate available for this task.`

## What it does not do

This phase introduces no execution surface and no mutation surface. The
visibility layer specifically does **not**:

- create scheduler proposals
- create scheduler confirmations
- create verifier reports
- create intake-to-runner handoffs
- start runtime executions
- invoke `approved_task_runner`
- run any scheduler loop or background worker
- mutate the local mirror DB
- write artifacts
- approve, reject, merge, or clean up tasks
- mutate GitHub (no PR creation, no comments, no merges)

No new API backend endpoints were added. No `POST`/`PATCH`/`DELETE` calls
were introduced. The Mission Control frontend only calls the two Phase H
endpoints:

- `GET /api/scheduler/candidates`
- `GET /api/tasks/{task_key}/scheduler-candidate`

No `execution_allowed` field is read from the API and no such field is
emitted in the UI.

## Safety boundary

- Candidate visibility is not execution permission.
- Operator confirmation remains required for any downstream action.
- Mission Control remains read-only.
- `validation_result` remains the authoritative validation record; the
  candidate panel does not imply that any task is approved or ready to merge.
- All safety flags from the API (`CANDIDATE_SAFETY_FLAGS` and
  `DISCOVERY_SAFETY_FLAGS`) are surfaced to the operator verbatim.

## Relationship to future phases

A later phase may add explicit, operator-gated proposal-creation controls.
This phase deliberately stops at visibility. Adding action controls — even
for ready candidates — requires a separate phase with a corresponding
governance gate and validator coverage.

## Validation

- `tests/test_mission_control_frontend_source.py` includes
  `TestSchedulerCandidateVisibilityFrontendSource`, which enforces:
  - candidate types are present
  - both Phase H endpoints are called
  - the task detail bundle integrates candidates best-effort
  - the panel surfaces the required safety labels
  - the panel introduces no action buttons, no forms, no mutation requests,
    and no `execution_allowed` field
- `cd mission-control && npm run build` confirms the frontend compiles.
