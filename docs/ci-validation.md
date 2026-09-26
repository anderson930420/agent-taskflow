# CI validation strategy

GitHub CI is a review signal, independent of Taskflow lifecycle validators. This
strategy changes neither task state nor approval, retry, integration, worktree,
or human merge authority. The existing required check remains `test`.

## Run locally

Use Python 3.12 and an isolated environment:

```bash
python -m pip install -e '.[test]' -c constraints.txt -c constraints-test.txt
python scripts/ci_validation.py select --base origin/main --output /tmp/ci-selection-001
python scripts/ci_validation.py run --base origin/main --output /tmp/ci-run-001
```

Each output directory must be new and outside the checkout. The base must resolve
to a commit that is an ancestor of HEAD. Fetch the intended target first; for a
PR, compare its merge-base against the checked-out HEAD. CI checks out GitHub's
PR merge ref with full history, so it validates the merge with the target. Main,
release branches/tags, published releases, nightly and manual CI always run full.
Local runs without a reliable target should use `--full` after resolving a base;
missing/invalid bases fail, rather than guessing a smaller diff.

`--full` and repeatable `--risk 'reason'` only escalate. Use `--risk` for known
flakiness, uncertain impact, uncovered behavior, or a semantic risk not apparent
from file paths. There is no force-fast override. After a failed preflight,
repair the environment and rerun with `--full` in a fresh evidence directory.
An unexplained/flaky failed gate remains a failed gate; a later passing suite
does not erase it. This CI runner makes no Taskflow retry decision.

## Gates

Every run first imports required runtime/test modules, verifies declared package
versions, runs `pip check`, collects the **entire** pytest suite, and discovers
the entire unittest suite without execution. Missing pytest is an unconditional
failure even where existing unittest tests would otherwise skip it. Add each
new test-only dependency to the `test` extra and test constraints, and add its
import to `tool.agent-taskflow.ci.required-imports`, especially if tests treat it
as optional. Collection catches additional import failures before CI unittest.

The fast gate compiles `agent_taskflow`, `scripts`, and `tests`, runs the existing
[workflow contract](../WORKFLOW.md) and machine-readable policy validators, then
executes the selected tests with pytest. Selection includes baseline contract,
policy, changed-files and selector regression tests, direct changed tests, and
transitive callers found through Python imports and literal module/script
references. The selection artifact shows a path chain for each caller. Deleted
source names remain in the graph so surviving caller tests are still selected.

Full validation runs the fast gate followed by **both serial suites**:

```bash
PYTHONPATH=. python -m pytest tests -q -p no:cacheprovider
PYTHONPATH=. python -m unittest discover -s tests
```

The second command is the original CI unittest command. Both suites run even if
the first fails. No xdist dependency or parallel suite execution is introduced.
Ambient `PYTEST_ADDOPTS` and automatically loaded third-party pytest plugins are
disabled to avoid accidental filtering or retry behavior. The workflow allows
60 minutes for collection, the fast gate and both serial full suites.

A fast-gate failure requires both full suites in the same invocation and leaves
the overall result failed. A dependency/collection failure blocks execution;
full is marked required but cannot be claimed executed. No success is reported
for an incomplete gate.

## Conservative impact selection

Full is mandatory for migrations/schema/persistence/SQLite; artifact/evidence
publication; lifecycle, Dispatcher, ApprovedTaskRunner, Integration, scheduler,
CLI and shared core; security, credentials, path policy, symlinks, filesystem
races or concurrency; workflow/policy/deployment/production boundaries; and CI,
dependencies, tests, discovery or unknown impact. Structural changes (tracked
additions, deletions, renames, type or mode changes) also escalate. The default
for every unadmitted product or script path is full, regardless of its name.

**The current runtime fast allowlist is empty.** All real product runtime changes
therefore require full today. A realistic pure display-label fixture exercises
admission and its direct, adjacent and transitive caller tests. This is proof of
the selection mechanism, not an exemption for existing safety-sensitive code.

To admit a future genuinely local pure module, review its responsibilities and
all callers first, then add an exact source path, direct tests and complete
transitive production caller set to `RUNTIME_FAST`. The graph must agree with
that set. New callers, absent tests, I/O imports or dynamic execution escalate.
Any CLI or named shared-core caller also requires full validation, even where an
otherwise pure local leaf and its tests meet the admission rule.
The static graph cannot prove the absence of computed dynamic references;
reviewers must inspect those before admission and use `--risk` when uncertain.
Changes to the allowlist itself are CI infrastructure and require full validation.
Do not exempt a module merely because its filename looks harmless.

Ordinary Markdown under `docs/` can receive the docs lane when every changed
path is ordinary documentation. All doc test modules, applicable baseline tests,
tests with an exact literal document path or unique document basename, local
Markdown link checks, compile and policy/contract validators still run. Literal
references use the same caller graph as source changes, so a helper that refers
to the document and a test that imports the helper are both recorded in the
selection chain. Sensitive document paths or content escalate, including
workflow, policy, security, deployment, runtime, permission or authentication
changes, access-control instructions, privileged commands, and system paths.
Base, HEAD, staged and current doc contents are inspected, so removing sensitive
text or reverting a staged edit cannot grant the docs exemption. Other document
locations are unclassified and require full. Link checks verify local
inline/reference destinations; they do not check remote availability or fragment
anchors. A successful docs lane is **not runtime full validation** and records
`runtime_full_validated: false`.

## Evidence and limits

`audit.json` records the resolved base, HEAD, all changed paths, content hashes,
file modes and separate committed/staged/unstaged/untracked layers. Raw NUL Git
records preserve both rename endpoints, deletions and unusual filenames. The
three tracked patches are saved with SHA-256 hashes. Untracked contents are
represented by their SHA-256 and mode; review them in the checkout as well.
Only untracked `agent_taskflow.egg-info/` installer metadata is explicitly
excluded and listed; tracked metadata is never excluded. Git-ignored generated
files are outside the source diff, as with ordinary Git review.

`selection.json` records full-escalation reasons and selected tests. Every command
has argv, working directory, timestamp, duration, exit code and a hashed log.
`result.json` records the actual fast/full outcomes, including failures. A final
source snapshot must match the initial source hash; edits during validation
invalidate the result and require a fresh run. This is a local evidence check,
not a hostile-filesystem security boundary or a substitute for independent
review and exact-head CI. The workflow uploads evidence even after failure.

Commands use fixed argv lists and Git's NUL-delimited paths. Diff text is never
evaluated as shell commands. Test fixtures may create disposable Git commits,
as the existing suite does; the runner does not publish or mutate Taskflow state.
