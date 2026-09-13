# Resolved managed-launch evidence

The shared `run_managed_process` boundary emits `resolved_launch_evidence.v1` for
managed executor and validator processes. Each file is named
`resolved-launch-<process-id>.json` inside the exact Attempt artifact root. Repeated
commands with the same name have different process identities and keep their own
files. An existing sidecar is never overwritten. These are launch observations;
the process registry and its events retain lifecycle and verified-exit authority.

The existing launch-spec and PID filenames remain available and link to the
sidecar through `resolved_launch_evidence`. Allocation events also retain that
process-specific reference. Legacy filenames can be reused by later launches, so
the sidecar embeds its own redacted launch spec and identifies those legacy links
as mutable. The reference's `pending` status means publication has not yet been
observed in that artifact; readers must inspect the referenced file. It does not
mean that a completed sidecar exists.

Each immutable sidecar records one observed launch outcome: `preflight_failed`,
`start_failed`, or `started`. It contains the preflight result, timestamps, resolved
executable, errors and warnings, plus known process identity. A successful start
does not assert execution success. Timeout, operator kill, descendant cleanup and
exit continue to use the existing process records.
The preflight executable is the resolved command candidate. When readable,
`observed_process_executable` separately records Linux `/proc/<pid>/exe`; fast
exits can make that observation unavailable, in which case it stays null.
If argv[0] is explicitly redacted, both executable paths remain null with
`redacted_argv0` provenance. A private symlink target is never recovered into those
fields by resolving the redacted executable alias.

Configuration comes only from the exact bound persisted Attempt. A read-only
SQLite transaction verifies Task, Attempt, lease, owner, resource and path linkage
before selecting executor, model, base commit, policy version, configuration hash,
prompt-template version and permission profile. It never chooses the latest
Attempt, reads task-level defaults, initializes a store or runs a migration. A
missing column/database produces explicit metadata provenance. A mismatched or
unavailable binding produces a `not_written` reference and withholds configuration;
the writer cannot publish under an unverified artifact root.

`configured_attempt.model` is configuration, and does not attest the runtime's
selected model. `observed_model` remains null. Canonical ExecutionEngine path,
prompt/spec references, allowed tools, environment allowlist and network policy
also remain null because this boundary does not attest them. Null configuration
and unknown fields appear in `missing_fields`; they are never replaced with
inferred values. The launch command uses the existing argv redaction, and neither
prompt/stdin text nor environment values are passed to the writer.

Environment inheritance remains enabled. `environment_keys` is a list of reported
key names, and is explicitly **not an allowlist**. Network isolation and security
eligibility are false. This evidence slice does not establish M2 credential,
environment, filesystem or network enforcement and makes no Level 2 eligibility
claim.
`environment_source` distinguishes Popen's parent-environment inheritance from a
caller-supplied mapping. The latter may already contain inherited values; this
writer neither reads the mapping nor claims to know how its caller assembled it.

Publication uses the shared atomic JSON writer, then opens the staged file without
following symlinks and verifies its regular-file identity and exact runner payload.
An exclusive link from the pinned file descriptor avoids following a substituted
staging pathname. Checks around publication verify the staging/final entries and
confirm that the bound directory pathname still names the opened directory.
Existing destinations are never overwritten. A detected replacement or mismatch
produces a `write_failed` reference; cleanup removes the new link only when it still
names the writer's inode. Unrelated colliding artifacts remain intact. Evidence
failures do not change process lifecycle or its result. These checks detect
publication races; they do not make artifacts tamper-proof against arbitrary
same-UID filesystem writes or establish full M2 isolation.
