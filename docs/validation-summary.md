# Runner validation summaries

`Dispatcher`, `run_approved_task`, and `run_integration_validators` now use
`ValidationSummaryRecorder`. Each invocation writes a new artifact:

```text
<recorded-artifact-root>/validation-runs/<validation-run-uuid>/validation-summary.json
```

The existing task artifact index records it as `other`; its JSON `kind` is
`validation_summary`, with `schema_version: 1`. No schema migration is required.
Per-validator snapshots live beside the summary. A repeated integration run id,
legacy artifact root, or retry cannot overwrite an earlier summary or snapshot.
The artifact-list API retains indexed metadata and appends safely discovered,
unindexed regular files from the artifact root. It deduplicates by resolved path
and keeps the existing preview and secret filtering. Registering a summary
therefore does not hide older files that were recorded only in run events.

## Identity and observations

The JSON records task key, source entrypoint, phase, validation run id,
executor run id and integration run id where applicable. Execution callers pass
the exact Attempt id retained from their own runtime claim. They do not query
the latest Attempt after releasing the claim. Existing runtime resource binding
continues to determine the artifact root and owns all resources.
The recorder resolves that root from the store's existing resource lookup and
checks it against the captured claim, because ApprovedTaskRunner's local task
value may still contain its pre-claim root. It does not mutate that task value.
The approved runner also exposes the summary in its returned artifact list, so
the ExecutionEngine facade can carry the reference alongside existing evidence.

Integration currently carries no authoritative execution Attempt reference.
Its summary therefore has `attempt_id: null` and `attempt_binding: unbound`,
alongside its distinct `integration_run_id`. An unbound legacy caller is marked
the same way. This does not create an executor Attempt or infer history from
an active/latest pointer. Full M2 Attempt-bound evidence remains unfinished.

Each configured validator row records its name, command or config reference,
observed start/end times, exit code, result, outcome kind, and actual evidence
path. Execution validator callbacks expose no universal command contract, so
their rows reference the caller's ordered validator configuration. Integration
rows include the actual command from `IntegrationValidatorSpec.command`.

Timestamps surround the runner invocation; they are not claims about the
subprocess's internal start time. Unrun rows keep null timestamps, exit code,
and artifact path. Summaries are initialized before executor invocation and
record stopped execution before validation, interrupted validation, and earlier
completed rows when a later validator raises. Dry-run and refusal paths do not
start a validation run. A hard process kill leaves the last atomic partial
record, without a fabricated end time.

## Evidence and results

The recorder copies an actual returned log or evidence artifact below the
recorded root. It rejects path escape, symlink components, and nonregular files,
using directory descriptors and `O_NOFOLLOW` while reading. Snapshots are capped
at 1,000,000 bytes; truncation is explicit and prevents summary completeness.
Integration snapshots contain its captured outcome/output, with the existing
20,000-character integration capture limit. Timeout output is retained, including
non-UTF-8 bytes decoded with replacement. Raised exceptions are recorded as
runner error evidence and re-raised. Missing or unsafe evidence keeps a null
artifact path; no output is invented to fill a missing log.
Destination allocation preserves the requested absolute root pathname and uses
directory descriptors and `O_NOFOLLOW` for every component, including root
ancestors. Initial root aliases and symlink chains are refused rather than
resolved to another directory. Path construction and directory opening are
inside the observation error boundary, so an I/O failure cannot skip the worker
or interrupt executor terminalization.
Each atomic write opens and checks the recorded root/run directory identities,
then uses Linux `/proc/self/fd` to anchor the existing atomic-write helper to
that directory. A symlink substituted before a write is refused; a rename after
the directory is opened cannot redirect the actual write to another directory.
After publication it reopens the claimed namespace and checks directory and
summary-owned file identities (device, inode, size and modification time).
Missing or replaced references prevent a successful summary. When a failure is
detected with the destination descriptor still open, the recorder attempts an
atomic incomplete/error summary in that retained directory and emits the error
audit; replacement directories receive no summary-owned output. A recovery
write can itself fail. The audit includes that recovery error; if a successful
summary was already written, the recorder then attempts to unlink only its
summary name through the retained descriptor, preserving validator evidence.
If the filesystem also refuses invalidation, the audit includes that failure
and the on-disk summary may remain stale. These are publication-time checks,
not a guarantee against later filesystem tampering. Legacy integration report
destination policy and the original integration validator gate are unchanged.

`reported_exit_code` preserves a validator adapter's value. `exit_code` is null
for skipped/blocked results and known integration tool errors: integration's
existing synthetic 124/126/127 codes are not observed subprocess exits.
Execution validators that return `failed` without an exit code do not expose
whether they failed an in-process gate or swallowed a tool exception. The row
preserves their summary and uses `unclassified_failure`; it does not guess from
free text. Extending those validator result contracts is outside this slice.

`complete` requires a nonempty observed sequence with evidence for each row.
`passed` additionally requires all rows to have passed. An empty legacy gate or
a skipped validator can still produce its existing lifecycle result, while the
summary refuses to claim all validators passed. Exceptions, incomplete runs,
missing evidence, and unavailable artifact roots cannot produce a successful
summary. Runtime recording failures are audited as `validation_summary_error`
task events with `complete: false` and `passed: false`; a warning is also logged,
including if the event store is unavailable. Recording stops for that sequence,
retaining an incomplete summary when the destination remains writable. The caller still runs
its existing executor/validator terminal handling and preserves its original
verdict or exception. This prevents an observation failure from leaving an
executor run unfinished or replacing its real error. The standalone recorder
raises recording errors when no runtime error sink is supplied.
Existing lifecycle statuses, validator verdicts, human gates, resource
ownership and integration admission rules remain the callers' responsibility.

Neither completeness nor `passed` grants merge eligibility or human approval.
Advisory review evidence after the configured validators is outside this summary.
When the optional integration artifact root is absent, existing database-only
integration behavior remains available and no on-disk summary is claimed.

## Review and rollback

Focused coverage lives in `tests/test_validation_summary.py` and the existing
dispatcher, approved runner and integration validator tests. It includes real
subprocess success/failure/timeout fixtures, partial errors, skipped/empty gates,
exact caller identity, unbound compatibility, retry namespace preservation,
unsafe evidence paths, bounded snapshots, destination substitution/races, and
construction, registration, and closeout I/O failures through real callers.
`tests/test_validation_summary_boundaries.py` additionally asserts actual setup
fault injection in both callers' success and executor exception paths, and runs
real subprocess validators against final run replacement, missing/replaced
evidence, initial root aliases, symlink chains and parent aliases.

This is the bounded M2.2 summary slice. It does not finish M2 launch restrictions,
the outcome ledger, all required evidence types, or the docs backlog, and it
adds no F9/F10 callers. Rollback consists of reverting the additive recorder and
caller wiring; retain existing evidence directories for review.
