# Post-Dogfood Cleanup Plan

**Document version**: Phase 57 — Post-v0.1.0 Evidence Decision
**Execution date**: 2026-05-13
**Executed by**: phase-43 automated cleanup

## Staging Clone Archive Decision

**Phase**: 44
**Date**: 2026-05-13
**Path**: `/tmp/agent-taskflow-v0.1.0-rc1-staging/`
**Size**: ~500 MB (actual: 500M)

### Purpose

Isolated checkout from `v0.1.0-rc1` tag (`2039aab`) used in Phase 37 to verify release reproducibility in a clean staging environment. Contains full smoke task evidence separate from source repo.

### Evidence Contained

| Item | Path | Description |
|---|---|---|
| Staging repo | `repo/` | Detached checkout at `2039aab`, Pi executor run verified |
| Staging DB | `agent-taskflow-staging-rc1.db` | SQLite DB with 1 task (AT-PI-STAGING-RC1), 7 events |
| Artifacts | `artifacts/AT-PI-STAGING-RC1/` | 7 files: mission_contract.json, pi-executor.log (10905B), pi_mission_plan.json, pi_mission_prompt.md, policy-validate.log, handoff_summary.md, implementation_prompt.md |
| Smoke result | `repo/.worktrees/AT-PI-STAGING-RC1/pi_smoke_result.txt` | Content: `pi-real-run-smoke-ok` |

### Archive Decision

**Decision**: Preserve until v0.1.0 final release

**Rationale**:
- It is the strongest local reproducibility evidence for the `v0.1.0-rc1` release candidate
- Shows that the tagged commit (`2039aab`) passes governance smoke when checked out in isolation
- Contains both DB evidence (task state) and artifact evidence (executor logs, policy validation)
- If deleted before a formal v0.1.0 release, there would be no local copy of the isolated staging smoke proof

### Deletion Condition

This staging clone MAY be deleted after EITHER:

1. `v0.1.0` final release is cut and verified, OR
2. Equivalent evidence summary is archived in docs (e.g., a release sign-off doc confirming staging smoke passes)

### Explicit Non-Action

**No deletion performed in Phase 44.** Staging clone intentionally preserved.

### Future Cleanup Command

```bash
# DO NOT RUN before final release sign-off
rm -rf /tmp/agent-taskflow-v0.1.0-rc1-staging/
```

### Warning

Do not run the above command until v0.1.0 final release sign-off is complete.

## Cleanup Execution Status

**Executed**: 2026-05-13 (Phase 43)
**Commit**: `b0b8a95` → cleanup execution commit (pending push)

### Cleaned Paths (deleted)

| Path | Reason |
|---|---|
| `/home/ubuntu/agent-taskflow/.worktrees/AT-PI-SMOKE-28/` | stale worktree, not git-tracked |
| `/home/ubuntu/agent-taskflow/.worktrees/AT-PI-SMOKE-28-R2/` | stale worktree, not git-tracked |
| `/home/ubuntu/agent-taskflow/.worktrees/AT-DOGFOOD-API-DB-PATH/` | dogfood worktree merged to main, not git-tracked |
| `/tmp/agent-taskflow-pi-gov-smoke-28.db` | failed smoke attempt, superseded by R2 |
| `/tmp/agent-taskflow-pi-gov-artifacts-28/` | failed smoke attempt artifacts, superseded by R2 |
| `/tmp/agent-taskflow-pi-smoke-artifacts/` | old 4K smoke artifacts, verified empty before delete |

### Preserved Paths (intentionally retained)

| Path | Task | Reason |
|---|---|---|
| `/tmp/agent-taskflow-pi-gov-smoke-28-r2.db` | AT-PI-SMOKE-28-R2 | passed smoke, policy passed, audit evidence |
| `/tmp/agent-taskflow-pi-gov-artifacts-28-r2/` | AT-PI-SMOKE-28-R2 | passed smoke, policy passed, audit evidence |
| `/tmp/agent-taskflow-dogfood-api-db-path.db` | AT-DOGFOOD-API-DB-PATH | approved, merged, audit evidence |
| `/tmp/agent-taskflow-dogfood-api-db-path-artifacts/` | AT-DOGFOOD-API-DB-PATH | approved, merged, audit evidence |
| `/tmp/agent-taskflow-v0.1.0-rc1-staging/` | AT-PI-STAGING-RC1 | v0.1.0-rc1 smoke verification, staging clone |

### Verification Result

- All cleaned paths confirmed removed
- All preserved paths confirmed intact
- No unapproved paths were deleted
- No source repo files modified
- v0.1.0-rc1 tag unchanged
- Source repo clean after cleanup

### Compliance Notes

- No wildcard cleanup used
- Only exact approved paths deleted
- git worktree remove attempted first (paths were not git-tracked, fell back to rm -rf)
- No DB schema changes
- No source code changes
- No dispatcher state machine changes
- No approval semantics changes
- No DEFAULT_VALIDATORS changes
- No tags pushed

## Current Repository State

| Item | Value |
|---|---|
| main hash | `a20265bdcba5aade0aa1d5076abcd4dff0386187` |
| origin/main hash | `a20265bdcba5aade0aa1d5076abcd4dff0386187` (synced) |
| v0.1.0-rc1 tag | `2039aab` (immutable, pushed) |
| Python tests | 662 passed |
| compileall | clean |
| frontend build | clean |
| git status | clean |

## Worktrees

| Worktree | Status | Recommendation |
|---|---|---|
| AT-PI-SMOKE-28 | empty (20 bytes pi_smoke_result.txt) — not tracked by git | safe to remove |
| AT-PI-SMOKE-28-R2 | empty (20 bytes pi_smoke_result.txt) — not tracked by git | safe to remove |
| AT-DOGFOOD-API-DB-PATH | contains scripts/tests from dogfood run — not tracked by git; merged content now on main | safe to remove |

Git worktree list shows only main repo (no tracked worktrees):
```
/home/ubuntu/agent-taskflow  a20265b [main]
```

**Note**: Worktree directories exist on disk but are not tracked by git (no `.git` file inside them). They are safe to remove via `git worktree remove`.

## SQLite Databases

| DB Path | Task | Status | Size | Recommendation |
|---|---|---|---|---|
| `/tmp/agent-taskflow-pi-gov-smoke-28.db` | AT-PI-SMOKE-28 | first attempt FAILED | 32K | safe to delete |
| `/tmp/agent-taskflow-pi-gov-smoke-28-r2.db` | AT-PI-SMOKE-28-R2 | passed, policy passed | 32K | preserve as audit evidence |
| `/tmp/agent-taskflow-dogfood-api-db-path.db` | AT-DOGFOOD-API-DB-PATH | approved, merged | 44K | preserve as audit evidence |

## Artifact Directories

| Directory | Task | Status | Size | Recommendation |
|---|---|---|---|---|
| `/tmp/agent-taskflow-pi-smoke-artifacts/` | unknown | old smoke artifacts | 4K | verify contents before delete |
| `/tmp/agent-taskflow-pi-gov-artifacts-28/` | AT-PI-SMOKE-28 | first attempt FAILED | 52K | safe to delete |
| `/tmp/agent-taskflow-pi-gov-artifacts-28-r2/` | AT-PI-SMOKE-28-R2 | passed, policy passed | 52K | preserve as audit evidence |
| `/tmp/agent-taskflow-dogfood-api-db-path-artifacts/` | AT-DOGFOOD-API-DB-PATH | approved, merged | 64K | preserve as audit evidence |
| `/tmp/agent-taskflow-v0.1.0-rc1-staging/` | AT-PI-STAGING-RC1 | v0.1.0-rc1 smoke verification | 500M | preserve as audit evidence (staging clone) |

## Preserved Evidence (Do Not Delete)

These resources are audit evidence for the governance pipeline and should be retained until formal archival:

1. **AT-PI-SMOKE-28-R2** — Smoke test from Phase 28, policy validator passed, human approval verified
   - DB: `/tmp/agent-taskflow-pi-gov-smoke-28-r2.db`
   - Artifacts: `/tmp/agent-taskflow-pi-gov-artifacts-28-r2/`

2. **AT-DOGFOOD-API-DB-PATH** — Dogfood task from Phase 38, approved and merged in Phase 40, pushed in Phase 41
   - DB: `/tmp/agent-taskflow-dogfood-api-db-path.db`
   - Artifacts: `/tmp/agent-taskflow-dogfood-api-db-path-artifacts/`
   - Worktree: `/home/ubuntu/agent-taskflow/.worktrees/AT-DOGFOOD-API-DB-PATH`

3. **AT-PI-STAGING-RC1** — Staging smoke from Phase 37, verified v0.1.0-rc1 tag in staging clone
   - Staging clone: `/tmp/agent-taskflow-v0.1.0-rc1-staging/`
   - Staging DB: `/tmp/agent-taskflow-v0.1.0-rc1-staging/agent-taskflow-staging-rc1.db`
   - Staging artifacts: `/tmp/agent-taskflow-v0.1.0-rc1-staging/artifacts/`

## Safe to Delete Later

These resources are superseded, failed, or redundant. Delete in Phase 43:

1. **AT-PI-SMOKE-28 first attempt** — Failed on policy validator false positive, superseded by R2
   - `/tmp/agent-taskflow-pi-gov-smoke-28.db`
   - `/tmp/agent-taskflow-pi-gov-artifacts-28/`

2. **Old smoke artifacts** (unknown origin)
   - `/tmp/agent-taskflow-pi-smoke-artifacts/` — 4K, verify contents before delete

3. **Stale worktrees** (not tracked by git)
   - `/home/ubuntu/agent-taskflow/.worktrees/AT-PI-SMOKE-28/` — empty dir
   - `/home/ubuntu/agent-taskflow/.worktrees/AT-PI-SMOKE-28-R2/` — empty dir
   - `/home/ubuntu/agent-taskflow/.worktrees/AT-DOGFOOD-API-DB-PATH/` — merged content on main

## Do Not Delete

- Source repo: `/home/ubuntu/agent-taskflow/`
- Source repo `.git/`
- `origin/main` (synced, pushed)
- `v0.1.0-rc1` tag at `2039aab` (immutable, pushed to GitHub)
- GitHub Release: `https://github.com/anderson930420/agent-taskflow/releases/tag/v0.1.0-rc1`
- All tracked files (scripts/, tests/, docs/, agent_taskflow/, etc.)

## Proposed Cleanup Commands

**Do NOT run in this phase. Listed for Phase 43 reference only.**

```bash
# === Stale worktrees (not git-tracked, safe to remove) ===
git worktree remove /home/ubuntu/agent-taskflow/.worktrees/AT-PI-SMOKE-28 || true
git worktree remove /home/ubuntu/agent-taskflow/.worktrees/AT-PI-SMOKE-28-R2 || true
git worktree remove /home/ubuntu/agent-taskflow/.worktrees/AT-DOGFOOD-API-DB-PATH || true

# === Failed smoke attempt artifacts (superseded by R2) ===
rm -rf /tmp/agent-taskflow-pi-gov-artifacts-28
rm -f /tmp/agent-taskflow-pi-gov-smoke-28.db

# === Old smoke artifacts (verify before delete) ===
# ls /tmp/agent-taskflow-pi-smoke-artifacts/  # confirm before deleting
rm -rf /tmp/agent-taskflow-pi-smoke-artifacts/

# === Staging clone (preserve until v0.1.0 formal release) ===
# DO NOT DELETE YET — staging evidence needed for v0.1.0 release verification
# rm -rf /tmp/agent-taskflow-v0.1.0-rc1-staging/

# === Dogfood evidence (preserve until Phase 43 formal approval) ===
# DO NOT DELETE YET — dogfood approval evidence
# rm -rf /tmp/agent-taskflow-dogfood-api-db-path-artifacts/
# rm -f /tmp/agent-taskflow-dogfood-api-db-path.db
```

## Recommended Cleanup Order

When executing Phase 43:

1. **Verify staged evidence** — confirm AT-PI-STAGING-RC1 and AT-DOGFOOD-API-DB-PATH evidence is not needed before deletion
2. **Remove stale worktrees** — `git worktree remove` for 3 smoke/dogfood worktrees
3. **Delete failed smoke artifacts** — `rm -rf` for AT-PI-SMOKE-28 DB and artifacts
4. **Delete old smoke artifacts** — `rm -rf` for `/tmp/agent-taskflow-pi-smoke-artifacts/` (4K)
5. **Archive or delete staging clone** — only after v0.1.0 formal release is confirmed
6. **Archive or delete dogfood evidence** — only after Phase 40 merge is pushed and verified
7. **Verify source repo clean** — `git status`, worktree list, no untracked files in main
8. **Rerun tests if needed** — confirm 662 tests still pass after cleanup

## Risks

1. **Deleting audit evidence too early** — governance pipeline evidence needed for v0.1.0 release sign-off
2. **Deleting active worktree accidentally** — verify worktree is not tracked before removal
3. **Removing source repo paths by mistake** — `rm -rf /home/ubuntu/agent-taskflow/` would destroy everything
4. **Losing reproducibility data** — staging clone at 500M contains full v0.1.0-rc1 smoke evidence
5. **Tag immutability** — v0.1.0-rc1 tag must never be moved; deletion of tag files does not delete tag on GitHub

## Post-v0.1.0 Evidence Decision

**Phase**: 57
**Date**: 2026-05-13
**v0.1.0 Final Release**: https://github.com/anderson930420/agent-taskflow/releases/tag/v0.1.0

### v0.1.0 Release State

| Item | Value |
|---|---|
| v0.1.0 tag commit | `eee67f3` |
| v0.1.0-rc1 tag commit | `2039aab` (unchanged) |
| main hash | `eee67f3` |
| origin/main hash | `eee67f3` (synced) |
| Python tests | 853 passed |
| compileall | clean |
| frontend build | clean |
| GitHub Release | non-prerelease, published 2026-05-13 |

### Evidence Decision Table

| Evidence | Path | Size | Purpose | Decision | Cleanup Condition |
|---|---|---|---|---|---|
| R2 smoke DB | `/tmp/agent-taskflow-pi-gov-smoke-28-r2.db` | 32K | Passed governance smoke, policy validator, human approval verified (`decided_by="human"`, task status `accepted`) | **Keep** | Keep until post-v0.1.0 UI create/dispatch dogfood completes |
| R2 smoke artifacts | `/tmp/agent-taskflow-pi-gov-artifacts-28-r2/` | 52K | Same as above; executor logs, mission contract, Pi plan, policy validation | **Keep** | Keep until post-v0.1.0 UI create/dispatch dogfood completes |
| Dogfood DB | `/tmp/agent-taskflow-dogfood-api-db-path.db` | 44K | API runner dogfood, full approval flow verified | **Keep** | Keep until post-v0.1.0 UI create/dispatch dogfood completes |
| Dogfood artifacts | `/tmp/agent-taskflow-dogfood-api-db-path-artifacts/` | 64K | Dogfood approval evidence | **Keep** | Keep until post-v0.1.0 UI create/dispatch dogfood completes |
| v0.1.0-rc1 staging clone | `/tmp/agent-taskflow-v0.1.0-rc1-staging/` | 500M | Isolated v0.1.0-rc1 smoke verification, detached checkout at `2039aab`, release reproducibility proof | **Safe to delete** | May delete after v0.1.0 final release verified; tag and GitHub release preserve source state; does not affect reproducibility |

### Staging Clone Decision Rationale

The staging clone (`/tmp/agent-taskflow-v0.1.0-rc1-staging/`) is **safe to delete** because:

1. `v0.1.0` final release exists at `eee67f3` — tag and GitHub release preserve source state permanently
2. `v0.1.0-rc1` tag at `2039aab` is immutable and remains on GitHub
3. Staging smoke evidence (AT-PI-STAGING-RC1) is documented in Phase 37–44 docs
4. R2 smoke (AT-PI-SMOKE-28-R2) provides equivalent local governance evidence
5. 500M disk usage is significant; keeping indefinitely provides no additional governance value once v0.1.0 is released

The other 4 evidence items (R2 smoke DB/artifacts, dogfood DB/artifacts) should remain until at least one more post-v0.1.0 UI create/dispatch dogfood completes, because:

1. They prove the end-to-end human approval enforcement path
2. Browser approval dogfood was for approval only, not create/dispatch
3. Full create/dispatch UI flow has not been deeply verified with a real browser click
4. Keeping them provides audit evidence for the governance pipeline

### Explicit Non-Action

**No evidence deleted in Phase 57.** Only decision recorded.

### Staging Clone Cleanup Command (for future phase)

```bash
# Safe to run after v0.1.0 final release verified
# Do NOT run until UI create/dispatch dogfood completes or explicit sign-off
rm -rf /tmp/agent-taskflow-v0.1.0-rc1-staging/
```

## Proposed Next Cleanup Phase

**Phase 58 or later** (after post-v0.1.0 UI create/dispatch dogfood):

1. **Archive evidence summary** — document R2 smoke and dogfood results in a cleanup sign-off doc
2. **Delete staging clone** — `rm -rf /tmp/agent-taskflow-v0.1.0-rc1-staging/` (500M, safe after v0.1.0 verified)
3. **Optionally keep R2/dogfood evidence** — if UI create/dispatch dogfood passes, optionally delete small evidence (32K–64K each) to reduce clutter
4. **Rerun tests/build** — confirm 853 tests still pass, compileall clean, frontend build clean
5. **Verify git status clean** — no untracked files in main

## Next Phase Recommendation

**Phase 43: Executed ✓** — Stale worktrees, failed smoke attempt artifacts, and old smoke artifacts deleted. Evidence preserved.

**Phase 44: Staging Clone Archive Decision ✓** — documented, preserved until v0.1.0 final release.

**Phase 57: Post-v0.1.0 Evidence Decision ✓** — v0.1.0 final release confirmed, evidence decision table updated.

**Phase 58: Proposed** — Delete staging clone (500M, safe now), optionally archive R2/dogfood evidence summary, verify tests/build.

Remaining preserved evidence after Phase 58:
- `/tmp/agent-taskflow-pi-gov-smoke-28-r2.db` — keep until UI create/dispatch dogfood completes
- `/tmp/agent-taskflow-pi-gov-artifacts-28-r2/` — keep until UI create/dispatch dogfood completes
- `/tmp/agent-taskflow-dogfood-api-db-path.db` — keep until UI create/dispatch dogfood completes
- `/tmp/agent-taskflow-dogfood-api-db-path-artifacts/` — keep until UI create/dispatch dogfood completes

**Note**: Staging clone may be deleted in Phase 58 (500M, safe after v0.1.0 release verified).