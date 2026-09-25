"""Shared runner evidence coverage for the Level 2 M2.2 evidence pipeline.

The roadmap names one evidence set per attempt:

```text
validation-summary.json
changed-files-audit.json
compileall.log (when applicable)
policy-validate.log
validator-specific logs
preflight-pr-check.json
executor-launch-spec.json
dual-write-consistency.json (during migration)
```

This module publishes the index that says, for one validation run, which of
those exist, where they actually are, and why the rest do not. Three rules keep
it from becoming a second narrative:

* **Only observed facts.** Every reference comes from a path the run itself
  reported (a validator artifact, an executor artifact, the recorder's own
  output) or from a file discovered in the artifact roots this run was given.
  A reference carries the origin it came from, so a discovered file is never
  presented as something this run produced.
* **No hardcoded framework gate.** Applicability is derived from the run's own
  configured validators — the artifacts they reported and the commands they
  actually ran. A run that configures no compileall step records
  ``not_applicable`` with the validator list that makes that true; it does not
  assert that compileall should have run.
* **Absence is stated, never filled.** ``not_run``, ``not_applicable``,
  ``missing`` and ``unknown`` each carry a reason. Nothing is invented for an
  evidence kind that did not happen, and an unreadable reference stays
  unreadable rather than being dropped from the index.

The collector is fed by the runner seams as a run proceeds and resolved once,
against :class:`~agent_taskflow.validation_summary.ValidationSummaryRecorder`'s
own validator rows, when that recorder finishes. Coverage is an observation of
evidence; it is not a verdict, and it grants nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping, Sequence


__all__ = [
    "EVIDENCE_CHANGED_FILES_AUDIT",
    "EVIDENCE_COMPILEALL_LOG",
    "EVIDENCE_DUAL_WRITE_CONSISTENCY",
    "EVIDENCE_EXECUTOR_LAUNCH_SPEC",
    "EVIDENCE_KINDS",
    "EVIDENCE_POLICY_VALIDATE_LOG",
    "EVIDENCE_PREFLIGHT_PR_CHECK",
    "EVIDENCE_VALIDATION_SUMMARY",
    "EVIDENCE_VALIDATOR_LOGS",
    "COVERAGE_ARTIFACT_NAME",
    "COVERAGE_KIND",
    "PREFLIGHT_NOT_APPLICABLE_REASON",
    "ROOT_ATTEMPT",
    "ROOT_ATTEMPT_ELSEWHERE",
    "ROOT_NO_ATTEMPT",
    "ROOT_NONE",
    "RunnerEvidenceCollector",
    "validator_config_identity",
]


COVERAGE_ARTIFACT_NAME = "evidence-coverage.json"
COVERAGE_KIND = "runner_evidence_coverage"
COVERAGE_SCHEMA_VERSION = 1

EVIDENCE_VALIDATION_SUMMARY = "validation-summary.json"
EVIDENCE_CHANGED_FILES_AUDIT = "changed-files-audit.json"
EVIDENCE_COMPILEALL_LOG = "compileall.log"
EVIDENCE_POLICY_VALIDATE_LOG = "policy-validate.log"
EVIDENCE_VALIDATOR_LOGS = "validator-specific-logs"
EVIDENCE_PREFLIGHT_PR_CHECK = "preflight-pr-check.json"
EVIDENCE_EXECUTOR_LAUNCH_SPEC = "executor-launch-spec.json"
EVIDENCE_DUAL_WRITE_CONSISTENCY = "dual-write-consistency.json"

# L2-M2-B2: nothing in this repository writes preflight-pr-check.json and no
# validation run performs a PR preflight check, so a run that neither observed
# one nor found one is not_applicable, with this reason, rather than unknown. A
# seam that gains a real check reports it through
# RunnerEvidenceCollector.note_operation; nothing here writes the file.
PREFLIGHT_NOT_APPLICABLE_REASON = (
    "No validation run performs a PR preflight check, and nothing in Taskflow "
    "writes preflight-pr-check.json. Execution validation never opens or checks "
    "a PR. V1 integration gates the PR it publishes with the Taskflow "
    "integration validators recorded in the validation summary (SPEC §29, "
    "§30), and GitHub CI has no lifecycle authority. The legacy path's PR "
    "preparation preflight (pr_preparation_pipeline.py) is a separate explicit "
    "operator command after waiting_approval and writes no such file. No check "
    "ran in this run and none was found in the artifact root."
)

# Why the recorder's artifact root is where it is (L2-M2-B2). Attempt-scoped
# resources always put an Attempt's root at ``<artifact base>/<attempt_id>``
# (attempt_resources.allocate), so a root named after the run's own Attempt is
# that Attempt's root.
ROOT_ATTEMPT = "attempt_artifact_root"
ROOT_ATTEMPT_ELSEWHERE = "attempt_bound_outside_attempt_root"
ROOT_NO_ATTEMPT = "no_attempt_bound"
ROOT_NONE = "no_artifact_root"

EVIDENCE_KINDS = (
    EVIDENCE_VALIDATION_SUMMARY,
    EVIDENCE_CHANGED_FILES_AUDIT,
    EVIDENCE_COMPILEALL_LOG,
    EVIDENCE_POLICY_VALIDATE_LOG,
    EVIDENCE_VALIDATOR_LOGS,
    EVIDENCE_PREFLIGHT_PR_CHECK,
    EVIDENCE_EXECUTOR_LAUNCH_SPEC,
    EVIDENCE_DUAL_WRITE_CONSISTENCY,
)

# Artifact keys the managed launch path already reports for a launch spec.
LAUNCH_SPEC_ARTIFACT_KEYS = ("executor_launch_spec", "launch_spec")

APPLICABLE = "applicable"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"

STATUS_PRESENT = "present"
STATUS_PARTIAL = "partial"
STATUS_MISSING = "missing"
STATUS_NOT_RUN = "not_run"
STATUS_NOT_APPLICABLE = NOT_APPLICABLE
STATUS_UNKNOWN = UNKNOWN

ORIGIN_RECORDER = "validation_summary_recorder"
ORIGIN_VALIDATOR = "reported_by_validator"
ORIGIN_EXECUTOR = "reported_by_executor"
ORIGIN_DISCOVERED = "discovered_in_artifact_root"
ORIGIN_OPERATION = "reported_by_observed_operation"

MAX_HASHED_BYTES = 64 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validator_config_identity(
    validator: Any,
    *,
    name: str,
    index: int,
    config_source: str,
    resolution: str,
) -> dict[str, Any]:
    """Describe the validator object this run actually resolved and invoked.

    Execution-path validators expose no universal command contract: some run a
    subprocess and publish its argv, others check in process. The identity
    therefore records the implementation that was resolved, where the name came
    from in the run's own configuration, and the argv only when the resolved
    object exposes one. ``command_reference`` names the attribute it was read
    from so a reviewer can re-derive it; nothing is guessed from the log text.
    """
    chain = _implementation_chain(validator)
    identity: dict[str, Any] = {
        "configured_name": name,
        "config_source": config_source,
        "config_index": index,
        "resolution": resolution,
        # The object the runner invoked, then anything it delegates to. The
        # runtime paths wrap validators in proxies, and the innermost entry is
        # the validator that does the work.
        "implementation": chain[0],
        "implementation_chain": chain,
        "validator_name": getattr(validator, "name", None),
    }
    reference = f"{chain[-1].rsplit('.', 1)[-1]}.command"
    try:
        command = getattr(validator, "command")
    except AttributeError:
        identity["command_reference"] = None
        identity["command_availability"] = "in_process_validator_exposes_no_command"
        return identity
    except Exception as exc:  # noqa: BLE001 - identity capture is never fatal.
        identity["command_reference"] = reference
        identity["command_availability"] = "unreadable"
        identity["command_error"] = f"{type(exc).__name__}: {exc}"
        return identity
    identity["command_reference"] = reference
    identity["command_availability"] = "resolved_before_invocation"
    identity["command"] = [str(part) for part in command]
    return identity


def _implementation_chain(validator: Any, *, limit: int = 8) -> list[str]:
    chain: list[str] = []
    current = validator
    seen: set[int] = set()
    while current is not None and len(chain) < limit and id(current) not in seen:
        seen.add(id(current))
        implementation = type(current)
        chain.append(f"{implementation.__module__}.{implementation.__qualname__}")
        current = getattr(current, "_validator", None)
    return chain


def _identity_command(identity: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not identity:
        return ()
    command = identity.get("command")
    if not isinstance(command, (list, tuple)):
        return ()
    return tuple(str(part) for part in command)


@dataclass(frozen=True)
class _Reference:
    """One actual path this run pointed at, with where the pointer came from.

    ``recorder_rejection`` carries the recorder's own refusal for this path,
    when it made one. A reference the recorder refused is kept here as a
    diagnostic: readable is not the same as admissible, and the index must not
    present a path the publication boundary rejected as accepted evidence.
    """

    path: Path
    origin: str
    detail: str | None = None
    recorder_rejection: str | None = None

    def resolve(
        self, roots: Sequence[Path], recorder_root: Path | None
    ) -> dict[str, Any]:
        inside_recorder_root = (
            None if recorder_root is None else _is_within(self.path, recorder_root)
        )
        record: dict[str, Any] = {
            "path": str(self.path),
            "origin": self.origin,
            "inside_artifact_root": any(_is_within(self.path, root) for root in roots),
            "inside_recorder_root": inside_recorder_root,
            "readable": False,
            "regular_file": None,
            "size_bytes": None,
            "sha256": None,
            "error": None,
            # Admissible means this run may count the path as its own accepted
            # evidence: readable, and inside the boundary the recorder enforces.
            "admissible": False,
            "rejection_reason": None,
        }
        if self.detail is not None:
            record["detail"] = self.detail
        if self.recorder_rejection is not None:
            record["recorder_rejection"] = self.recorder_rejection
        digest = hashlib.sha256()
        size = 0
        try:
            # O_NOFOLLOW on the final component: an evidence reference that has
            # become a symlink is reported as such, never followed elsewhere.
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                record["regular_file"] = stat.S_ISREG(info.st_mode)
                if not record["regular_file"]:
                    record["error"] = "not_a_regular_file"
                    return _admissibility(record)
                while size <= MAX_HASHED_BYTES:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as exc:
            record["error"] = f"{type(exc).__name__}:{exc.errno}"
            return _admissibility(record)
        if size > MAX_HASHED_BYTES:
            record["size_bytes"] = info.st_size
            record["error"] = "too_large_to_hash"
            record["readable"] = True
            return _admissibility(record)
        record.update(readable=True, size_bytes=size, sha256=digest.hexdigest())
        return _admissibility(record)


def _admissibility(record: dict[str, Any]) -> dict[str, Any]:
    """Decide admissibility exactly where the recorder's boundary decides it.

    The recorder refuses to snapshot evidence outside its artifact root, and
    marks that run's summary incomplete. Counting the same path as present here
    would let the index disagree with the publication safeguard that rejected
    it, so the digest stays for diagnosis and the entry does not become present.
    """
    rejection = record.get("recorder_rejection")
    if not record["readable"]:
        record["rejection_reason"] = record["error"] or "unreadable"
    elif rejection is not None:
        record["rejection_reason"] = f"recorder_rejected:{rejection}"
    elif record["inside_recorder_root"] is False:
        record["rejection_reason"] = "outside_recorder_artifact_root"
    elif record["error"] is not None:
        record["rejection_reason"] = record["error"]
    else:
        record["admissible"] = True
    return record


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass
class _Item:
    evidence: str
    applicability: str
    reason: str
    references: list[_Reference] = field(default_factory=list)
    producers: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    ran: bool = True

    def render(
        self, roots: Sequence[Path], recorder_root: Path | None
    ) -> dict[str, Any]:
        resolved = [
            reference.resolve(roots, recorder_root) for reference in self.references
        ]
        # Status follows admissibility, not mere readability: a path the
        # recorder refused stays visible with its digest, but cannot make this
        # evidence kind present (review finding M22-R1-N1).
        admissible = [item for item in resolved if item["admissible"]]
        rejected = [item for item in resolved if not item["admissible"]]
        if self.applicability == NOT_APPLICABLE:
            status = STATUS_NOT_APPLICABLE
        elif self.applicability == UNKNOWN:
            status = STATUS_UNKNOWN
        elif not resolved:
            status = STATUS_NOT_RUN if not self.ran else STATUS_MISSING
        elif len(admissible) == len(resolved):
            status = STATUS_PRESENT
        elif admissible:
            status = STATUS_PARTIAL
        else:
            status = STATUS_MISSING
        payload: dict[str, Any] = {
            "evidence": self.evidence,
            "applicability": self.applicability,
            "status": status,
            "reason": self.reason,
            "producers": list(self.producers),
            "references": resolved,
            "admissible_references": len(admissible),
            "rejected_references": len(rejected),
        }
        detail = dict(self.detail)
        if rejected:
            detail["rejected_reference_reasons"] = [
                {"path": item["path"], "reason": item["rejection_reason"]}
                for item in rejected
            ]
        if detail:
            payload["detail"] = detail
        return payload


class RunnerEvidenceCollector:
    """Collect one run's observed evidence facts and resolve them once.

    The seam feeds facts as they happen; nothing is inferred later. The
    collector never runs a validator, never writes an artifact of its own and
    never changes a verdict.
    """

    def __init__(
        self,
        *,
        source: str,
        artifact_roots: Sequence[Path | None] = (),
    ) -> None:
        self.source = source
        self.artifact_roots: list[Path] = []
        for root in artifact_roots:
            if root is None:
                continue
            candidate = Path(root).absolute()
            if candidate not in self.artifact_roots:
                self.artifact_roots.append(candidate)
        self._validator_identities: dict[int, dict[str, Any]] = {}
        self._executor: dict[str, Any] | None = None
        self._executor_artifacts: dict[str, Path] = {}
        self._operations: dict[str, dict[str, Any]] = {}

    # -- facts -------------------------------------------------------------

    def note_validator_identity(self, index: int, identity: Mapping[str, Any]) -> None:
        """Record the resolved identity of the validator at ``index``."""
        self._validator_identities[int(index)] = dict(identity)

    def note_executor(
        self,
        executor: str,
        *,
        ran: bool,
        artifacts: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        """Record the executor invocation and the artifacts it reported."""
        self._executor = {"executor": executor, "ran": bool(ran), "reason": reason}
        for key, value in dict(artifacts or {}).items():
            if value is not None:
                self._executor_artifacts[str(key)] = Path(value)

    def note_operation(
        self,
        evidence: str,
        *,
        observed: bool,
        reason: str,
        applicable: bool | None = None,
        path: Path | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Record an evidence-producing operation this run actually observed.

        ``observed=False`` states that the operation did not run, with the
        reason. It never turns into a reference; an operation that did not
        happen cannot produce evidence. ``applicable`` is the seam's own
        statement about whether the operation belongs to this run at all;
        ``None`` leaves that unknown rather than assuming either answer.
        """
        self._operations[evidence] = {
            "observed": bool(observed),
            "applicable": applicable,
            "reason": reason,
            "path": None if path is None else Path(path),
            "detail": dict(detail or {}),
        }

    # -- resolution --------------------------------------------------------

    def coverage(self, recorder: Any) -> dict[str, Any]:
        """Build the coverage index for ``recorder``'s validation run."""
        payload = recorder.payload
        rows: Sequence[Mapping[str, Any]] = payload.get("validators", ())
        roots = list(self.artifact_roots)
        recorder_root = getattr(recorder, "artifact_dir", None)
        recorder_root = None if recorder_root is None else Path(recorder_root)
        if recorder_root is not None and recorder_root not in roots:
            roots.insert(0, recorder_root)

        items = {
            item.evidence: item
            for item in (
                self._summary_item(recorder),
                *self._validator_items(rows),
                self._preflight_item(roots),
                self._launch_spec_item(rows),
                self._dual_write_item(roots),
            )
        }
        # One entry per required evidence kind, always in the roadmap's order.
        rendered = [items[kind].render(roots, recorder_root) for kind in EVIDENCE_KINDS]
        unresolved = [
            entry["evidence"]
            for entry in rendered
            if entry["applicability"] == APPLICABLE
            and entry["status"] not in {STATUS_PRESENT}
        ]
        rejected = [
            {
                "evidence": entry["evidence"],
                "path": reference["path"],
                "reason": reference["rejection_reason"],
            }
            for entry in rendered
            for reference in entry["references"]
            if not reference["admissible"]
        ]
        return {
            "schema_version": COVERAGE_SCHEMA_VERSION,
            "kind": COVERAGE_KIND,
            "task_key": payload.get("task_key"),
            "attempt_id": payload.get("attempt_id"),
            "attempt_binding": payload.get("attempt_binding"),
            "validation_run_id": payload.get("validation_run_id"),
            "integration_run_id": payload.get("integration_run_id"),
            "executor_run_id": payload.get("executor_run_id"),
            "source": payload.get("source", self.source),
            "phase": payload.get("phase"),
            "generated_at": _now(),
            "artifact_roots": [str(root) for root in roots],
            "recorder_artifact_root": None if recorder_root is None else str(recorder_root),
            "artifact_root_binding": _artifact_root_binding(
                payload.get("attempt_id"), recorder_root
            ),
            "executor": self._executor,
            # complete means every applicable kind resolved to a reference this
            # run may claim: readable and inside the recorder's own boundary.
            "complete": not unresolved,
            "unresolved": unresolved,
            "rejected_references": rejected,
            "items": rendered,
        }

    # -- individual evidence kinds ----------------------------------------

    def _summary_item(self, recorder: Any) -> _Item:
        path = getattr(recorder, "path", None)
        if path is None:
            return _Item(
                evidence=EVIDENCE_VALIDATION_SUMMARY,
                applicability=APPLICABLE,
                reason=(
                    "This validation run has no published summary path; the "
                    "recorder reported a destination or publication failure."
                ),
                producers=[self.source],
                ran=True,
            )
        return _Item(
            evidence=EVIDENCE_VALIDATION_SUMMARY,
            applicability=APPLICABLE,
            reason="Published by this validation run.",
            references=[_Reference(Path(path), ORIGIN_RECORDER)],
            producers=[self.source],
            detail={
                "note": (
                    "The summary is still being written when coverage is built; "
                    "its recorded size and digest are the state before the "
                    "closing flush."
                )
            },
        )

    def _validator_items(self, rows: Sequence[Mapping[str, Any]]) -> list[_Item]:
        """Validator logs plus the three validator-produced evidence kinds."""
        configured = [str(row.get("validator")) for row in rows]
        logs = _Item(
            evidence=EVIDENCE_VALIDATOR_LOGS,
            applicability=APPLICABLE if rows else NOT_APPLICABLE,
            reason=(
                f"{len(rows)} validator(s) configured for this run: "
                f"{', '.join(configured)}."
                if rows
                else "This run configured no validators, so it produced no validator logs."
            ),
            producers=configured,
        )
        per_validator: list[dict[str, Any]] = []
        named: dict[str, _Item] = {}

        for index, row in enumerate(rows):
            name = str(row.get("validator"))
            identity = self._validator_identities.get(index)
            command = _identity_command(identity) or tuple(
                str(part) for part in (row.get("command") or ())
            )
            artifacts = _row_artifacts(row)
            ran = row.get("started_at") is not None
            # The recorder's own refusal for the path it was handed. Carried
            # onto every reference to that path, so the index cannot present
            # evidence the publication boundary rejected (M22-R1-N1).
            rejection = _recorder_rejection(row)
            entry = {
                "index": index,
                "validator": name,
                "result": row.get("result"),
                "outcome_kind": row.get("outcome_kind"),
                "exit_code": row.get("exit_code"),
                "started_at": row.get("started_at"),
                "ended_at": row.get("ended_at"),
                "config_reference": row.get("config_reference"),
                "command": list(command) or None,
                "evidence_error": row.get("evidence_error"),
                "artifact_path": row.get("artifact_path"),
                "source_artifacts": {key: str(path) for key, path in artifacts.items()},
            }
            per_validator.append(entry)

            if row.get("artifact_path") is not None:
                logs.references.append(
                    _Reference(
                        Path(str(row["artifact_path"])),
                        ORIGIN_RECORDER,
                        detail=f"validator {name} evidence recorded by this run",
                    )
                )
            elif ran:
                logs.detail.setdefault("validators_without_evidence", []).append(
                    {
                        "validator": name,
                        "reason": row.get("evidence_error") or "no evidence path recorded",
                    }
                )
                # The run could not record its own copy. The path the validator
                # reported is still indexed, so the gap is visible rather than
                # silently absent.
                logs.references.extend(
                    _Reference(
                        path,
                        ORIGIN_VALIDATOR,
                        detail=f"validator {name} reported {key}; not recorded by this run",
                        recorder_rejection=_rejection_for(rejection, row, path),
                    )
                    for key, path in artifacts.items()
                )

            for kind in (EVIDENCE_CHANGED_FILES_AUDIT, EVIDENCE_POLICY_VALIDATE_LOG):
                matched = [path for path in artifacts.values() if path.name == kind]
                if matched:
                    named.setdefault(
                        kind,
                        _Item(
                            evidence=kind,
                            applicability=APPLICABLE,
                            reason="",
                            producers=[],
                        ),
                    )
                    item = named[kind]
                    item.producers.append(name)
                    item.references.extend(
                        _Reference(
                            path,
                            ORIGIN_VALIDATOR,
                            detail=f"validator {name}",
                            recorder_rejection=_rejection_for(rejection, row, path),
                        )
                        for path in matched
                    )
                    item.reason = (
                        f"Reported by validator(s) {', '.join(item.producers)} in this run."
                    )

            compileall_paths = [
                path for path in artifacts.values() if path.name == EVIDENCE_COMPILEALL_LOG
            ]
            by_command = any("compileall" in part for part in command)
            if compileall_paths or by_command:
                item = named.setdefault(
                    EVIDENCE_COMPILEALL_LOG,
                    _Item(
                        evidence=EVIDENCE_COMPILEALL_LOG,
                        applicability=APPLICABLE,
                        reason="",
                        producers=[],
                        ran=ran,
                    ),
                )
                item.producers.append(name)
                item.ran = item.ran or ran
                if compileall_paths:
                    item.references.extend(
                        _Reference(
                            path,
                            ORIGIN_VALIDATOR,
                            detail=f"validator {name}",
                            recorder_rejection=_rejection_for(rejection, row, path),
                        )
                        for path in compileall_paths
                    )
                    item.reason = (
                        f"Reported by validator(s) {', '.join(item.producers)} in this run."
                    )
                else:
                    snapshot = row.get("artifact_path")
                    if snapshot is not None:
                        item.references.append(
                            _Reference(
                                Path(str(snapshot)),
                                ORIGIN_RECORDER,
                                detail=(
                                    f"validator {name} ran compileall; its own log was "
                                    "recorded as this run's validator evidence"
                                ),
                            )
                        )
                    item.reason = (
                        f"Validator {name} runs compileall in its configured command "
                        f"({' '.join(command)}), but reported no compileall.log artifact."
                    )
                    item.detail.setdefault("matched_by", "validator_command")

        logs.detail["validators"] = per_validator

        items = [logs]
        for kind in (
            EVIDENCE_CHANGED_FILES_AUDIT,
            EVIDENCE_COMPILEALL_LOG,
            EVIDENCE_POLICY_VALIDATE_LOG,
        ):
            if kind in named:
                items.append(named[kind])
                continue
            items.append(
                _Item(
                    evidence=kind,
                    applicability=NOT_APPLICABLE,
                    reason=(
                        "No validator configured for this run produced or invoked "
                        f"{kind}. Configured validators: "
                        f"{', '.join(configured) if configured else 'none'}."
                    ),
                    producers=[],
                )
            )
        return items

    def _preflight_item(self, roots: Sequence[Path]) -> _Item:
        observation = self._operations.get(EVIDENCE_PREFLIGHT_PR_CHECK)
        if observation is not None and observation["observed"]:
            references = (
                [_Reference(observation["path"], ORIGIN_OPERATION)]
                if observation["path"] is not None
                else []
            )
            return _Item(
                evidence=EVIDENCE_PREFLIGHT_PR_CHECK,
                applicability=APPLICABLE,
                reason=observation["reason"],
                references=references,
                producers=[self.source],
                detail=observation["detail"],
            )
        discovered = _discover(roots, EVIDENCE_PREFLIGHT_PR_CHECK)
        if discovered:
            return _Item(
                evidence=EVIDENCE_PREFLIGHT_PR_CHECK,
                applicability=APPLICABLE,
                reason=(
                    "This run observed no PR preflight check. The referenced file "
                    "was found in the artifact root and is attributed to whichever "
                    "earlier operation wrote it, not to this run."
                ),
                references=[_Reference(path, ORIGIN_DISCOVERED) for path in discovered],
                producers=[],
            )
        if observation is not None and observation["applicable"] is not None:
            return _Item(
                evidence=EVIDENCE_PREFLIGHT_PR_CHECK,
                applicability=(
                    APPLICABLE if observation["applicable"] else NOT_APPLICABLE
                ),
                reason=observation["reason"],
                producers=[],
                detail={**observation["detail"], "observed": False},
                ran=False,
            )
        if observation is not None:
            return _Item(
                evidence=EVIDENCE_PREFLIGHT_PR_CHECK,
                applicability=UNKNOWN,
                reason=observation["reason"],
                producers=[],
                detail={"observed": False},
                ran=False,
            )
        return _Item(
            evidence=EVIDENCE_PREFLIGHT_PR_CHECK,
            applicability=NOT_APPLICABLE,
            reason=PREFLIGHT_NOT_APPLICABLE_REASON,
            producers=[],
            detail={"observed": False, "producer": None},
            ran=False,
        )

    def _launch_spec_item(self, rows: Sequence[Mapping[str, Any]]) -> _Item:
        references: list[_Reference] = []
        producers: list[str] = []
        for key, path in self._executor_artifacts.items():
            if key in LAUNCH_SPEC_ARTIFACT_KEYS:
                references.append(
                    _Reference(path, ORIGIN_EXECUTOR, detail=f"executor artifact {key}")
                )
                producers.append(str((self._executor or {}).get("executor")))
        for row in rows:
            name = str(row.get("validator"))
            for key, path in _row_artifacts(row).items():
                if key in LAUNCH_SPEC_ARTIFACT_KEYS:
                    references.append(
                        _Reference(path, ORIGIN_VALIDATOR, detail=f"validator {name}")
                    )
                    producers.append(name)
        if references:
            return _Item(
                evidence=EVIDENCE_EXECUTOR_LAUNCH_SPEC,
                applicability=APPLICABLE,
                reason=(
                    "Reported by the managed launch path for "
                    f"{', '.join(sorted(set(producers)))}."
                ),
                references=references,
                producers=sorted(set(producers)),
            )
        if self._executor is None:
            return _Item(
                evidence=EVIDENCE_EXECUTOR_LAUNCH_SPEC,
                applicability=NOT_APPLICABLE,
                reason=(
                    "This validation run launches no executor, so it produces no "
                    "executor launch spec."
                ),
                producers=[],
            )
        return _Item(
            evidence=EVIDENCE_EXECUTOR_LAUNCH_SPEC,
            applicability=APPLICABLE,
            reason=(
                f"Executor {self._executor.get('executor')} reported no managed "
                "launch spec artifact"
                + (
                    f" ({self._executor['reason']})."
                    if self._executor.get("reason")
                    else "; it ran through a path that publishes none."
                )
            ),
            producers=[str(self._executor.get("executor"))],
            # The executor ran; the managed launch that publishes a spec did
            # not, which is what this evidence kind is about.
            ran=False,
            detail={"executor_ran": bool(self._executor.get("ran"))},
        )

    def _dual_write_item(self, roots: Sequence[Path]) -> _Item:
        observation = self._operations.get(EVIDENCE_DUAL_WRITE_CONSISTENCY)
        if observation is not None and observation["observed"]:
            references = (
                [_Reference(observation["path"], ORIGIN_OPERATION)]
                if observation["path"] is not None
                else []
            )
            return _Item(
                evidence=EVIDENCE_DUAL_WRITE_CONSISTENCY,
                applicability=APPLICABLE,
                reason=observation["reason"],
                references=references,
                producers=[self.source],
                detail=observation["detail"],
            )
        if observation is not None and observation["applicable"] is False:
            return _Item(
                evidence=EVIDENCE_DUAL_WRITE_CONSISTENCY,
                applicability=NOT_APPLICABLE,
                reason=observation["reason"],
                producers=[],
                detail={**observation["detail"], "observed": False},
            )
        discovered = _discover(roots, EVIDENCE_DUAL_WRITE_CONSISTENCY)
        if discovered:
            return _Item(
                evidence=EVIDENCE_DUAL_WRITE_CONSISTENCY,
                applicability=APPLICABLE,
                reason=(
                    "A dual-write consistency observation exists in the artifact "
                    "root. This run did not produce it and does not interpret it."
                ),
                references=[_Reference(path, ORIGIN_DISCOVERED) for path in discovered],
                producers=[],
            )
        return _Item(
            evidence=EVIDENCE_DUAL_WRITE_CONSISTENCY,
            applicability=UNKNOWN,
            reason=(
                observation["reason"]
                if observation is not None
                else (
                    "Dual-write consistency is required only during a migration "
                    "window. This runner seam observes no migration state and found "
                    "no observation in the artifact root, so applicability is "
                    "unknown rather than assumed."
                )
            ),
            producers=[],
        )


def _artifact_root_binding(
    attempt_id: str | None, recorder_root: Path | None
) -> dict[str, Any]:
    """Say whether the recorder's root is the run's Attempt root, and why."""
    if recorder_root is None:
        return {
            "scope": "none",
            "reason_code": ROOT_NONE,
            "reason": "The run had no artifact root, so nothing was written.",
        }
    if attempt_id is None:
        return {
            "scope": "task",
            "reason_code": ROOT_NO_ATTEMPT,
            "reason": (
                "The run is bound to no Attempt (it held no runtime claim, or it "
                "is an integration run without a producer Attempt), so no Attempt "
                f"root exists and the evidence is written to {recorder_root}."
            ),
        }
    if recorder_root.name == attempt_id:
        return {
            "scope": "attempt",
            "reason_code": ROOT_ATTEMPT,
            "reason": f"Written under the artifact root of Attempt {attempt_id}.",
        }
    return {
        "scope": "task",
        "reason_code": ROOT_ATTEMPT_ELSEWHERE,
        "reason": (
            f"The run is bound to Attempt {attempt_id}, but that Attempt has no "
            f"usable Attempt-scoped artifact root, so the evidence is written to "
            f"{recorder_root}. An integration run records the exact reason in "
            "its evidence_root."
        ),
    }


def _recorder_rejection(row: Mapping[str, Any]) -> str | None:
    """Return the recorder's refusal for this row's reported evidence, if any.

    ``evidence_error`` is the recorder's own word for why it would not accept
    the path a validator handed it — outside the artifact root, a symlink, a
    non-regular file. ``evidence_missing`` is not a refusal of a path, so it is
    not carried onto one.
    """
    error = row.get("evidence_error")
    if not isinstance(error, str) or error == "evidence_missing":
        return None
    return error


def _rejection_for(
    rejection: str | None, row: Mapping[str, Any], path: Path
) -> str | None:
    """Apply a row's refusal only to the path the recorder actually refused."""
    if rejection is None:
        return None
    source = row.get("source_artifact_path")
    if source is not None and Path(str(source)) != path:
        return None
    return rejection


def _row_artifacts(row: Mapping[str, Any]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for key, value in dict(row.get("source_artifacts") or {}).items():
        artifacts[str(key)] = Path(str(value))
    source = row.get("source_artifact_path")
    if source is not None and not any(path == Path(str(source)) for path in artifacts.values()):
        artifacts.setdefault("log_path", Path(str(source)))
    return artifacts


def _discover(roots: Iterable[Path], name: str) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        candidate = Path(root) / name
        try:
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
        except OSError:  # pragma: no cover - defensive: an unreadable root.
            continue
    return found
