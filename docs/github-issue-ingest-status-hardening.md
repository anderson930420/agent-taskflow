# GitHub Issue Ingest Status Hardening

## Purpose

This checkpoint hardens the existing GitHub Issue ingestion boundary before the one-task automation outer loop is introduced.

GitHub Issue ingestion mirrors input/spec evidence into the local task mirror. It is not dispatch, not scheduler execution, not workspace preparation, not runtime execution, not PR creation, not approval, not merge, and not cleanup.

## Status Preservation Contract

Re-ingesting a GitHub Issue for an existing task must preserve the task's current workflow status. In particular, a fresh GitHub Issue snapshot must not move an in-progress local task backward to `queued`, and a closed GitHub Issue snapshot must not turn an active local task into `blocked`.

The current store-level protection is `TaskMirrorStore.upsert_task(..., preserve_existing_status=True)`. The ingestion layer may refresh issue/spec evidence and record a new `github_issue_ingested` event, but it must not treat that refresh as permission to restart or downgrade task execution state.

## Covered Cases

The hardening tests cover:

- re-ingesting an open issue over `preparing`, `implementing`, `validating`, and `waiting_approval` tasks preserves each existing status;
- re-ingesting a closed issue over an active task preserves the active task status and does not add a blocked reason;
- ingesting a closed issue as a new task still maps to `blocked`.

## Safety Boundary

This phase is intentionally test/documentation only:

- no watcher integration;
- no outer automation loop;
- no scheduler loop;
- no background worker;
- no executor or validator change;
- no GitHub mutation;
- no branch push;
- no draft PR creation;
- no approval, merge, cleanup, branch deletion, or worktree deletion.

The next automation step may compose discovery, ingestion, and the one-task watcher, but it should rely on this status preservation boundary so issue refreshes cannot restart or downgrade tasks already in progress.
