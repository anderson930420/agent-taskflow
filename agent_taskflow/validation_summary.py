"""Runner observations of validation, independent of lifecycle and approval.

Each recorder owns a new run directory. Pending rows are durable before a
validator starts, and actual evidence is snapshotted before a row finishes.
Write errors are explicit observations at runtime and cannot replace the caller's
terminal handling. A standalone recorder without an error sink raises them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterator, Sequence, TypeVar
from uuid import uuid4

from agent_taskflow.atomic_write import atomic_write_bytes, atomic_write_json
from agent_taskflow.tasks import normalize_task_key


MAX_EVIDENCE_BYTES = 1_000_000
T = TypeVar("T")
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(exc: BaseException) -> dict[str, Any]:
    detail: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    for name in ("stdout", "stderr"):
        output = getattr(exc, name, None)
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if isinstance(output, str):
            detail[name] = output[:MAX_EVIDENCE_BYTES]
    return detail


def artifact_root_for_claim(
    task_store: Any, task_key: str, attempt_id: str | None, fallback: Path | None,
) -> Path | None:
    """Use the same recorded resource as runtime context proxies, if present.

    ApprovedTaskRunner's local task value can predate its claim. Do not mutate
    that value or query the latest Attempt to obtain an evidence destination.
    """
    resource_lookup = getattr(task_store, "attempt_resource", None)
    if attempt_id is None or resource_lookup is None:
        return fallback
    resource = resource_lookup(task_key)
    if resource is None:
        return fallback  # Compatibility stores need not provision resources.
    if resource.attempt_id != attempt_id or resource.task_key != normalize_task_key(task_key):
        raise ValueError("Validation summary resource does not match the captured runtime claim")
    return resource.artifact_root


def recording_error_sink(task_store: Any) -> Callable[[dict[str, Any]], None]:
    """Audit an observation failure without taking over lifecycle ownership."""
    def report(payload: dict[str, Any]) -> None:
        logger.warning("Validation summary recording failed: %s", payload["error"])
        if payload.get("recovery") is not None:
            logger.warning("Validation summary recovery failed: %s", payload["recovery"])
        try:
            task_store.record_task_event(
                payload["task_key"], "note", "validation_summary",
                message="Validation summary recording failed; evidence is incomplete",
                payload=payload,
            )
        except Exception as exc:
            logger.warning("Validation summary error audit failed: %s", exc)
    return report


class ValidationSummaryRecorder:
    """Record the exact caller identity; never infer an Attempt from history.

    ``attempt_id`` is the caller's captured runtime claim, or None for an
    unbound caller. Integration run ids are separate from execution Attempts.
    ``complete`` concerns evidence; ``passed`` requires every configured check
    to pass. Neither field is a lifecycle decision or merge eligibility.
    """

    def __init__(
        self,
        *,
        task_key: str,
        artifact_dir: Path | None,
        source: str,
        phase: str,
        validators: Sequence[str],
        config_reference: str | None = None,
        attempt_id: str | None = None,
        executor_run_id: int | str | None = None,
        integration_run_id: str | None = None,
        on_error: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.on_error = on_error
        self.recording_failed = False
        self._published = False
        self._published_complete = False
        self.artifact_dir: Path | None = None
        self.directory: Path | None = None
        self.path: Path | None = None
        self._file_identities: dict[str, tuple[int, int, int, int]] = {}
        run_id = uuid4().hex
        self.payload: dict[str, Any] = {
            "schema_version": 1,
            "kind": "validation_summary",
            "task_key": normalize_task_key(task_key),
            "attempt_id": attempt_id,
            "attempt_binding": "runtime_claim" if attempt_id is not None else "unbound",
            "attempt_binding_reason": (
                "Captured by the execution caller from its own runtime claim."
                if attempt_id is not None else
                "Caller has no authoritative execution Attempt binding; no historical inference."
            ),
            "executor_run_id": executor_run_id,
            "integration_run_id": integration_run_id,
            "validation_run_id": run_id,
            "source": source,
            "phase": phase,
            "started_at": _now(),
            "ended_at": None,
            "state": "running",
            "complete": False,
            "passed": False,
            "validators": [
                {
                    "validator": name,
                    "config_reference": f"{config_reference or source + '.validators'}[{index}] ({name})",
                    "started_at": None,
                    "ended_at": None,
                    "exit_code": None,
                    "result": "not_run",
                    "outcome_kind": "not_run",
                    "artifact_path": None,
                }
                for index, name in enumerate(validators)
            ],
        }
        if artifact_dir is not None:
            try:
                # Preserve the requested namespace. resolve() would silently
                # bind an initial symlink to a different artifact root.
                self.artifact_dir = Path(artifact_dir).absolute()
                if ".." in self.artifact_dir.parts:
                    raise ValueError("Validation artifact root contains parent traversal")
                self.directory = self.artifact_dir / "validation-runs" / run_id
                self.path = self.directory / "validation-summary.json"
                self._allocate_directory()
            except Exception as exc:
                self._recording_failure(exc)
        self._flush()

    def register(self, task_store: Any) -> None:
        if self.path is not None and not self.recording_failed:
            # Reuse the existing artifact index vocabulary, without a migration.
            try:
                task_store.record_task_artifact(self.payload["task_key"], "other", self.path)
            except Exception as exc:
                self._recording_failure(exc)

    @staticmethod
    def _identity(fd: int) -> tuple[int, int]:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino

    def _allocate_directory(self) -> None:
        assert self.artifact_dir is not None and self.directory is not None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = self._open_root(create=True)
        try:
            self._root_identity = self._identity(root_fd)
            try:
                os.mkdir("validation-runs", dir_fd=root_fd)
            except FileExistsError:
                pass
            runs_fd = os.open("validation-runs", flags, dir_fd=root_fd)
            try:
                # A new namespace, even for repeated integration ids or legacy roots.
                os.mkdir(self.directory.name, dir_fd=runs_fd)
                run_fd = os.open(self.directory.name, flags, dir_fd=runs_fd)
                try:
                    self._run_identity = self._identity(run_fd)
                finally:
                    os.close(run_fd)
            finally:
                os.close(runs_fd)
        finally:
            os.close(root_fd)

    def _open_root(self, *, create: bool = False) -> int:
        """Walk the requested absolute path without following any component."""
        assert self.artifact_dir is not None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(self.artifact_dir.anchor, flags)
        try:
            for part in self.artifact_dir.parts[1:]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int, int]:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Validation evidence is no longer a regular file")
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _verify_publication(self) -> None:
        """Check the claimed namespace and previously published evidence."""
        with self._destination(verify_after=False) as anchored:
            for name, identity in self._file_identities.items():
                if self._file_identity(anchored / name) != identity:
                    raise OSError(f"Validation evidence was replaced: {name}")

    @contextmanager
    def _destination(self, *, verify_after: bool = True) -> Iterator[Path]:
        """Anchor atomic_write to a verified directory descriptor on Linux.

        O_NOFOLLOW rejects substituted parents, inode checks reject replacement
        directories, and /proc/self/fd keeps a concurrent rename from redirecting
        the actual write. Artifact metadata retains the normal recorded path.
        """
        assert self.artifact_dir is not None and self.directory is not None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        opened: list[int] = []
        try:
            root_fd = self._open_root()
            opened.append(root_fd)
            if self._identity(root_fd) != self._root_identity:
                raise OSError("Validation artifact root was replaced")
            runs_fd = os.open("validation-runs", flags, dir_fd=root_fd)
            opened.append(runs_fd)
            run_fd = os.open(self.directory.name, flags, dir_fd=runs_fd)
            opened.append(run_fd)
            if self._identity(run_fd) != self._run_identity:
                raise OSError("Validation run directory was replaced")
            anchored = Path(f"/proc/self/fd/{run_fd}")
            try:
                yield anchored
                if verify_after:
                    self._verify_publication()
            except Exception as exc:
                if verify_after:
                    # The normal path may now name a replacement. Retain an
                    # honest failure in the directory we actually wrote to,
                    # while its descriptor still anchors the recovery write.
                    self.payload.update(complete=False, passed=False, recording_error=_error(exc))
                    retained = dict(self.payload, state="error")
                    try:
                        atomic_write_json(anchored / "validation-summary.json", retained, sort_keys=True)
                        self._published_complete = False
                    except Exception as recovery_exc:
                        recovery: dict[str, Any] = {"error": _error(recovery_exc)}
                        self.payload["recording_recovery"] = recovery
                        if self._published_complete:
                            # Do not leave a known successful summary after a
                            # detected publication failure if recovery cannot
                            # replace it. Only our summary name is invalidated;
                            # validator evidence and replacement paths remain.
                            try:
                                os.unlink("validation-summary.json", dir_fd=run_fd)
                                recovery["invalidated_summary_path"] = str(self.path)
                                self.path = None
                                self._published_complete = False
                            except OSError as invalidation_exc:
                                recovery["invalidation_error"] = _error(invalidation_exc)
                raise
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def _recording_failure(self, exc: Exception) -> None:
        self.recording_failed = True
        self.payload.update(complete=False, passed=False, recording_error=_error(exc))
        if not self._published:
            self.path = None
        if self.on_error is None:
            raise exc
        self.on_error({
            "kind": "validation_summary_error", "task_key": self.payload["task_key"],
            "attempt_id": self.payload["attempt_id"], "phase": self.payload["phase"],
            "entrypoint": self.payload["source"],
            "validation_run_id": self.payload["validation_run_id"],
            "executor_run_id": self.payload["executor_run_id"],
            "integration_run_id": self.payload["integration_run_id"],
            "artifact_path": str(self.path) if self.path is not None else None,
            "complete": False, "passed": False, "error": _error(exc),
            "recovery": self.payload.get("recording_recovery"),
        })

    def _write_json(self, name: str, payload: Any) -> bool:
        if self.recording_failed or self.directory is None:
            return False
        try:
            with self._destination() as anchored:
                atomic_write_json(anchored / name, payload, sort_keys=True)
                if name == "validation-summary.json":
                    self._published_complete = bool(payload.get("complete"))
                self._file_identities[name] = self._file_identity(anchored / name)
        except Exception as exc:
            self._recording_failure(exc)
            return False
        return True

    def _flush(self) -> None:
        if self.path is not None and self._write_json(self.path.name, self.payload):
            self._published = True

    def _snapshot(self, source: Path, index: int) -> tuple[str | None, str | None]:
        """Read a regular artifact beneath the trusted root, without symlinks.

        Open directory components with dir_fd/O_NOFOLLOW so a validator cannot
        race a checked parent into a symlink to an unrelated file.
        """
        if self.recording_failed or self.artifact_dir is None or self.directory is None:
            return None, "artifact_root_missing"
        try:
            relative = source.relative_to(self.artifact_dir)
        except ValueError:
            return None, "evidence_outside_artifact_root"
        if not relative.parts or ".." in relative.parts:
            return None, "invalid_evidence_path"
        directory_fd = -1
        try:
            directory_fd = self._open_root()
            if self._identity(directory_fd) != self._root_identity:
                return None, "evidence_artifact_root_replaced"
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None, "evidence_not_regular_file"
                captured = stream.read(MAX_EVIDENCE_BYTES + 1)
        except OSError as exc:
            return None, f"evidence_unreadable:{type(exc).__name__}:{exc.errno}"
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)
        target = self.directory / f"validator-{index:03d}.log"
        try:
            with self._destination() as anchored:
                atomic_write_bytes(anchored / target.name, captured[:MAX_EVIDENCE_BYTES])
                self._file_identities[target.name] = self._file_identity(anchored / target.name)
        except Exception as exc:
            self._recording_failure(exc)
            return None, "evidence_write_failed"
        return str(target), "evidence_truncated" if len(captured) > MAX_EVIDENCE_BYTES else None

    def observe(
        self,
        index: int,
        run: Callable[[], T],
        *,
        evidence: Callable[[T], dict[str, Any]] | None = None,
    ) -> T:
        """Observe an actual invocation without changing its returned verdict.

        ``evidence`` serializes an integration outcome that already contains
        captured output. Executor-path validators supply actual artifact paths.
        """
        row = self.payload["validators"][index]
        row.update(started_at=_now(), result="running", outcome_kind="running")
        self._flush()
        try:
            result = run()
        except BaseException as exc:
            row.update(
                ended_at=_now(), result="tool_error", outcome_kind="tool_error",
                error=_error(exc),
            )
            if self.directory is not None:
                target = self.directory / f"validator-{index:03d}-error.json"
                if self._write_json(target.name, row["error"]):
                    row["artifact_path"] = str(target)
            self.finish(state="error", reason=f"Validator {row['validator']} raised {type(exc).__name__}")
            raise

        row["ended_at"] = _now()
        try:
            self._record_result(index, result, evidence=evidence)
        except BaseException as exc:
            row["recording_error"] = _error(exc)
            self.finish(state="error", reason="Validator result evidence could not be recorded")
            raise
        return result

    def _record_result(
        self, index: int, result: Any,
        *, evidence: Callable[[Any], dict[str, Any]] | None,
    ) -> None:
        row = self.payload["validators"][index]
        status = result.status
        tool_error = getattr(result, "tool_error", None)
        row.update(
            result=status,
            exit_code=(
                result.exit_code
                if status not in {"skipped", "blocked"} and tool_error is None else None
            ),
            reported_exit_code=result.exit_code,
            summary=getattr(result, "summary", None),
            outcome_kind=(
                "tool_error" if tool_error is not None or status == "blocked" else
                "skipped" if status == "skipped" else
                "unclassified_failure" if status == "failed" and result.exit_code is None else
                "validator_verdict"
            ),
        )
        if tool_error is not None:
            row["error"] = tool_error
        if evidence is not None:
            details = evidence(result)
            if "command" in details:
                row["command"] = details["command"]
                row["config_reference"] = f"IntegrationValidatorSpec[{index}].command"
            if self.directory is not None:
                target = self.directory / f"validator-{index:03d}-evidence.json"
                if self._write_json(target.name, details):
                    row["artifact_path"] = str(target)
        else:
            source = getattr(result, "log_path", None)
            if source is None:
                sources = list(getattr(result, "artifacts", {}).values())
                source = sources[0] if sources else None
            if source is not None:
                row["source_artifact_path"] = str(source)
                row["artifact_path"], problem = self._snapshot(Path(source), index)
                if problem is not None:
                    row["evidence_error"] = problem
        if row["artifact_path"] is None:
            row.setdefault("evidence_error", "evidence_missing")
        self._flush()

    def finish(self, *, state: str = "finished", reason: str | None = None) -> None:
        self.payload.update(state=state, ended_at=_now())
        if reason is not None:
            self.payload["reason"] = reason
        rows = self.payload["validators"]
        complete = bool(rows) and self.path is not None and all(
            row["ended_at"] is not None
            and row["artifact_path"] is not None
            and row["result"] not in {"running", "not_run", "tool_error"}
            and "evidence_error" not in row
            for row in rows
        )
        self.payload["complete"] = complete and state == "finished" and not self.recording_failed
        self.payload["passed"] = self.payload["complete"] and all(
            row["result"] == "passed" and row["outcome_kind"] == "validator_verdict"
            and row["exit_code"] in (None, 0)
            for row in rows
        )
        self._flush()
