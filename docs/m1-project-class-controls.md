# M1-D Project Admission and Task-Class Governance Controls

M1-D adds the minimum durable control-plane foundation for the final Milestone
1 gate. It does not implement automatic merge, promotion policy, project-wide
application configuration, multi-project scheduling, or a task-class taxonomy.

## Existing identities remain authoritative

No new project or class model is introduced. A persisted Task supplies both
identities:

```text
task_key -> tasks.project
         -> tasks.task_class
```

`TaskRecord.project`, `config/projects.yaml`, and `agent_taskflow.projects`
remain the existing mirror/config surfaces. Admission and governance decisions
resolve the persisted Task row through `AttemptStore`; caller-supplied project
or class strings do not override it.

## Two separate control planes

Project pause is execution admission control. A paused project denies a new
Level 2 claim at the canonical atomic admission boundary. It does not suspend,
abort, or signal an active Attempt. Clear restores future admission only.

Task-class disable is class-global governance control. It denies only the class
control term of a future automatic merge/promotion decision. It neither kills
active execution nor claims that the Task is auto-merge eligible. Clearing it
restores only that control-plane permission.

```text
actual_auto_merge =
    later_milestone_enabled
    AND promotion_policy_passed
    AND project/class governance permits
    AND all other required gates
```

M1-D implements only the class-governance term. `actual_auto_merge_enabled`
and the M1 audit's `auto_merge_eligible` remain false.

## Persistence and precedence

The valid persisted scopes are:

```text
global, project, task_class, task, attempt
```

Execution controls evaluate global, project, task, and Attempt scopes. Kill
outranks pause; running rows are neutral and cannot override a pause/kill at
another scope. Task-class governance is evaluated separately and never enters
the cooperative kill path.

The task-class scope is intentionally global by class for M1-D. A later
`(project, task_class)` promotion policy can combine the independent identities
without requiring a composite runtime-control scope.

## Migration and evidence

`level2_project_class_controls_v1` transactionally rebuilds
`runtime_controls` because SQLite cannot edit its CHECK constraint in place.
It preserves current controls, generations, attribution, timestamps, metadata,
append-only events, triggers, and indexes.

`project-class-control-rehearsal.json` uses schema
`m1_project_class_controls.v1`. The M1 audit reports:

- `BLOCKED` when the deployed scope schema/migration is missing;
- `PARTIAL` when schema is deployed but matching rehearsal evidence is absent;
- `PASSED` only when schema and DB-/SHA-bound evidence both pass.

Implementation rehearsals must use a disposable database. Production migration
and final M1 closeout are separate reviewed deployment steps.
