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

`configured_attempt` is that persisted configuration, and
`configured_attempt.model` does not attest the runtime's selected model.
`requested_launch` is a separate group describing what the runner actually
selected for this launch: `executor` and `timeout_seconds` come from the launch
spec, while `model`, `base_commit`, `policy_version`, `permission_profile` and
`tools` come from the optional provenance a runner captured. The two groups are
never merged, and neither is substituted for the other when one is null.
`observed_model` is still always null, with the reason
`backend_effective_model_not_attested`; nothing here observes a backend's
effective model.

`runner_provenance` carries that whole normalized capture, including the
`*_source` labels naming where each value came from. Its
`canonical_execution_path`, `prompt_reference`, `spec_reference` and
`config_snapshot_reference` entries are the same values published at the top
level. The runners populate it as described below. A launch with no runner
observation, such as a direct adapter or launch-spec caller, keeps those fields
null.

`canonical_execution_path` is `dispatcher` from `Dispatcher.dispatch_task`,
`approved_task_runner` from the approved runner, or `execution_engine` when that
runner observes the existing engine invocation scope for its exact task key and
database, with `path_source`
`approved_task_runner.direct_engine_authority_scope`. A bound Attempt alone is
not engine attestation. The observation reads a context variable, so a runner
crossing a thread boundary reports the narrower path instead; it under-claims
rather than over-claims.

`base_commit` is the dispatcher's prepared worktree base SHA
(`prepared_worktree.base_sha`) or the approved runner's prepared workspace base
SHA (`prepared_workspace.base_sha`). When the executor-process runtime store
binds the context, it replaces that with the claimed Attempt resource's base SHA
(`claimed_attempt_resource.base_sha`), which is therefore the usual published
value for a bound managed launch.

`prompt_reference` records the selected prompt's path, plus the UTF-8 SHA-256
and byte length of the text the adapter had already chosen. It hashes those
bytes rather than reopening the path, so a file replaced after selection does
not change the digest. Its `source` is `pi_mission_rendered` for Pi's rendered mission prompt,
`claude_code_implementer_rendered` for the prompt Claude Code writes to stdin,
and `input_prompt_text` for OpenCode and for Pi without a mission contract.
Shell passes no prompt and leaves the field null.

`spec_reference` is captured only when the approved runner reads `issue_spec.md`
to render a missing implementation prompt, with `source`
`issue_spec_text_used_for_prompt_rendering`. An already existing prompt does not
establish which spec produced it, so that spec reference stays unknown.

`config_snapshot_reference` digests a canonical JSON projection of fields the
runner explicitly enumerates: executor, model, provider, tools, validators and a
timeout, plus the approved runner's base branch. Its reference name is
`dispatcher.resolved_configuration` or
`approved_task_runner.resolved_configuration`, and its `source` is
`runner_selected_fields_json`. It is a digest of those selected values only. It
is neither a hash of a configuration file nor the persisted
`configured_attempt.config_snapshot_hash`.

`requested_launch.model` is Pi's constructor model (`PiExecutor.model`, which
does not use the context model) or OpenCode's constructor-or-context selection
(`OpenCodeExecutor.model_or_context`). Claude Code and shell run opaque
commands, so both report a null model with `model_source`
`opaque_command_model_not_resolved`. `requested_launch.tools` currently comes
from Pi's constructor tools only. No current runner selects `policy_version` or
`permission_profile`, so both stay unknown on real runs while their persisted
values remain in `configured_attempt`; the fields publish if a runner begins
selecting them.

`allowed_tools`, `environment_allowlist`, `network_policy` and `credential_policy`
are always null: requested tools are not an enforcement attestation, and
environment, network and credential enforcement policies are not observed here.
This evidence slice still establishes no M2 enforcement and no Level 2
eligibility.

`unknown_field_reasons` explains every null with a specific reason. A captured
field the runner never set is `not_observed_by_runner`, and one supplied with a
wrong type or a malformed digest is `invalid_runner_metadata`; malformed metadata
becomes null and never blocks publication or changes the subprocess outcome. The
fields this boundary cannot attest state why individually:
`backend_effective_model_not_attested`,
`requested_tools_are_not_enforcement_attestation`, and
`environment_enforcement_policy_not_observed`,
`network_enforcement_policy_not_observed` and
`credential_enforcement_policy_not_observed`. The generic
`not_attested_at_managed_launch_boundary` is the construction default for those
keys and is in practice always replaced by one of the reasons above; it remains
the value of the unchanged top-level `unknown_field_provenance`. These reasons
are keyed by provenance field name, so the facts published as
`requested_launch.model` and `requested_launch.tools` appear there as
`requested_model` and `requested_tools`, and `runner_provenance`-only labels such
as `path_source`, `base_source` and `model_source` appear with no matching
top-level key.

`missing_fields` is built conditionally rather than always listing the unknown
keys. It contains, in order: the null entries among `observed_model`,
`canonical_execution_path`, `prompt_reference`, `spec_reference`,
`config_snapshot_reference`, `allowed_tools`, `environment_allowlist`,
`network_policy` and `credential_policy`; the null `requested_launch.*` entries
except `timeout_seconds`, where null means no timeout was configured rather than
a missing observation; the null `configured_attempt.*` entries;
`observed_process_executable` when it was not observed; and
`preflight.resolved_executable` when argv[0] is redacted. A populated reference
is absent from the list. `requested_launch.executor` is never listed, because the
launch spec rejects an empty executor name. Nothing listed is ever replaced with
an inferred value.

`requested_launch`, `runner_provenance`, `unknown_field_reasons` and
`credential_policy` were added, and the four reference fields became
conditionally populated, without changing `schema_version`, which remains
`resolved_launch_evidence.v1`. `model_provenance` now reads
`requested_and_configured_models_only; backend_effective_model_not_attested`.
No runtime, API or UI consumer in this repository reads these keys.

The launch command uses the existing argv redaction, and neither prompt/stdin
text nor environment values are passed to the writer. The references hold a path
or reference name, a digest and a byte length only; no prompt, spec,
configuration, environment or credential content is copied into this evidence.

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
