"""L2-M2-B2: every required evidence file of a Ticket lives under its Attempt root.

Level 2 M2 Exit Gate row 3 ("all required evidence is attempt-scoped"; Roadmap
§2.2 and §2.4). The end-to-end test drives the real path: a Ticket is created
through the Ticket service, dispatched through the real Dispatcher with the
real ``changed-files`` and ``policy`` validators, handed off through the real
integration handoff, and integrated by a real ``integrate_task`` run against a
local bare ``origin``. Only GitHub is faked (``FakeGhRunner``). Nothing here
writes evidence by hand.

The fallback tests pin where the task-level directory is still used, and the
compatibility tests pin that evidence written there before this change is
still found and never moved.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5_support import RecordingExecutor, git, make_fixture  # noqa: E402
from v1_step2_fixtures import FakeGhRunner, isolate_integration_lock_dir  # noqa: E402

from agent_taskflow import attempt_scoped_runtime_path  # noqa: E402
from agent_taskflow import integration_schema as schema  # noqa: E402
from agent_taskflow.api.main import create_app  # noqa: E402
from agent_taskflow.attempt_store import AttemptStore  # noqa: E402
from agent_taskflow.dispatcher import Dispatcher  # noqa: E402
from agent_taskflow.evidence_coverage import (  # noqa: E402
    COVERAGE_ARTIFACT_NAME,
    EVIDENCE_CHANGED_FILES_AUDIT,
    EVIDENCE_COMPILEALL_LOG,
    EVIDENCE_POLICY_VALIDATE_LOG,
    EVIDENCE_PREFLIGHT_PR_CHECK,
    PREFLIGHT_NOT_APPLICABLE_REASON,
    ROOT_ATTEMPT,
    ROOT_ATTEMPT_ELSEWHERE,
    ROOT_NO_ATTEMPT,
    RunnerEvidenceCollector,
)
from agent_taskflow.github_pr_adapter import GitHubPrAdapter  # noqa: E402
from agent_taskflow.integration_controller import (  # noqa: E402
    IntegrationRequest,
    integrate_task,
)
from agent_taskflow.integration_evidence_root import (  # noqa: E402
    REASON_ATTEMPT_ROOT,
    REASON_ATTEMPT_ROOT_UNAVAILABLE,
    REASON_ATTEMPT_UNREADABLE,
    REASON_NO_ATTEMPT_ROOT,
    REASON_NO_PRODUCER,
    SCOPE_ATTEMPT,
    SCOPE_NONE,
    SCOPE_TASK,
    TASK_LEVEL_FROM_ATTEMPT_BASE,
    TASK_LEVEL_RECORDED,
    TASK_LEVEL_UNRESOLVED,
    resolve_integration_evidence_root,
)
from agent_taskflow.integration_handoff import (  # noqa: E402
    BINDING_NONE,
    BINDING_PRODUCER_HANDOFF,
    REASON_BOUND,
    REASON_NO_ENTRY,
    ProducerAttemptBinding,
)
from agent_taskflow.integration_queue import remove_from_queue  # noqa: E402
from agent_taskflow.integration_store import IntegrationStore  # noqa: E402
from agent_taskflow.integration_validators import IntegrationValidatorSpec  # noqa: E402
from agent_taskflow.store import TaskMirrorStore  # noqa: E402
from agent_taskflow.validation_summary import ValidationSummaryRecorder  # noqa: E402
from agent_taskflow.validators.changed_files import ChangedFilesValidator  # noqa: E402
from agent_taskflow.validators.policy import PolicyCheckValidator  # noqa: E402


REPO = "owner/alpha"
GREEN = (IntegrationValidatorSpec(name="unit", command=("true",)),)


def _is_under(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(root)
    except ValueError:
        return False
    return True


class _Pipeline:
    """One real Ticket, dispatched and integrated, in a disposable tree."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        isolate_integration_lock_dir(test)
        self.fx = make_fixture()
        test.addCleanup(self.fx.cleanup)
        self.fx.repository = replace(self.fx.repository, github_repo=REPO)
        origin = self.fx.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(origin)],
            check=True, capture_output=True,
        )
        git(self.fx.repo, "remote", "add", "origin", str(origin))
        git(self.fx.repo, "push", "-q", "origin", "main")
        self.store = TaskMirrorStore(self.fx.db_path)
        self.integration = IntegrationStore(self.fx.db_path)
        self.integration.init_db()
        self.github = GitHubPrAdapter(REPO, runner=FakeGhRunner(repo=REPO))

    def dispatch(self, prompt: str) -> str:
        key = self.fx.create_ticket(prompt).task_key
        dispatcher = Dispatcher(
            db_path=self.fx.db_path,
            executor_registry={"fake": RecordingExecutor(write_file="feature.txt")},
            validator_registry={
                "changed-files": ChangedFilesValidator(),
                "policy": PolicyCheckValidator(),
            },
            validators=("changed-files", "policy"),
            default_executor="fake",
        )
        self.last_store = dispatcher.store
        try:
            result = dispatcher.dispatch_task(key)
        finally:
            dispatcher.store.shutdown_runtime_supervisors()
        self.test.assertEqual(result.status, schema.READY_FOR_INTEGRATION, result.summary)
        return key

    def retry_that_fails(self, key: str) -> None:
        """A second Attempt runs after the producer handed off, and fails.

        The operator re-queues the Ticket for another run; the real
        Dispatcher claims it, allocates a new Attempt (which points
        ``tasks.artifact_dir`` at the new root) and the executor fails, so
        nothing is handed off and the queue entry stays bound to the first
        Attempt. The operator then returns the Ticket to
        ``ready_for_integration`` to integrate that queued work. The two
        status moves are the only scaffolding; each Attempt is real.
        """
        self.store.update_task_status(key, "queued", source="operator")
        dispatcher = Dispatcher(
            db_path=self.fx.db_path,
            executor_registry={"fake": RecordingExecutor(status="failed")},
            validator_registry={
                "changed-files": ChangedFilesValidator(),
                "policy": PolicyCheckValidator(),
            },
            validators=("changed-files", "policy"),
            default_executor="fake",
        )
        try:
            result = dispatcher.dispatch_task(key)
        finally:
            dispatcher.store.shutdown_runtime_supervisors()
        self.test.assertEqual(result.status, "failed", result.summary)
        self.store.update_task_status(
            key, schema.READY_FOR_INTEGRATION, source="operator"
        )

    def commit_implementation(self, key: str) -> None:
        """The executor's change becomes the task branch's commit."""
        worktree = Path(self.store.get_task_worktree(key).worktree_path)
        git(worktree, "add", "feature.txt")
        git(worktree, "commit", "-q", "-m", f"{key}: implementation")

    def integrate(self, key: str, **overrides):
        kwargs = dict(
            task_key=key,
            repo=REPO,
            db_path=self.fx.db_path,
            target_branch="main",
            remote="origin",
            validator_specs=GREEN,
            dry_run=False,
            confirm_integration=True,
            owner="test-runtime",
        )
        kwargs.update(overrides)
        return integrate_task(
            IntegrationRequest(**kwargs),
            store=self.store,
            integration_store=self.integration,
            github=self.github,
        )

    def attempt(self, key: str) -> dict:
        attempts = self.fx.attempts(key)
        self.test.assertEqual(len(attempts), 1)
        return attempts[0]

    def task_dir(self, key: str) -> Path:
        return self.fx.artifacts / key


class DispatchedAndIntegratedTicketTests(unittest.TestCase):
    """Acceptance criteria 1, 2, 3 and 5 on one real Ticket."""

    def setUp(self) -> None:
        self.p = _Pipeline(self)
        self.key = self.p.dispatch("Attempt-scoped evidence")
        self.p.commit_implementation(self.key)
        self.result = self.p.integrate(self.key)
        self.assertEqual(self.result.status, "integrated", self.result.summary)
        self.attempt = self.p.attempt(self.key)
        self.root = Path(self.attempt["artifact_root"])

    def summaries(self) -> dict[str, dict]:
        by_phase: dict[str, dict] = {}
        for path in sorted(self.root.glob("validation-runs/*/validation-summary.json")):
            payload = json.loads(path.read_text())
            self.assertNotIn(payload["phase"], by_phase)
            by_phase[payload["phase"]] = {"path": path, "payload": payload}
        return by_phase

    def coverage(self, phase: str) -> dict:
        return json.loads(
            (self.summaries()[phase]["path"].parent / COVERAGE_ARTIFACT_NAME).read_text()
        )

    def item(self, coverage: dict, evidence: str) -> dict:
        (match,) = [e for e in coverage["items"] if e["evidence"] == evidence]
        return match

    def test_the_attempt_root_is_the_producer_attempt_directory(self) -> None:
        self.assertEqual(self.root, self.p.task_dir(self.key) / self.attempt["attempt_id"])
        self.assertTrue(self.root.is_dir())
        binding = self.result.producer_attempt_binding
        self.assertEqual(binding["attempt_id"], self.attempt["attempt_id"])
        self.assertEqual(binding["reason_code"], REASON_BOUND)

    def test_every_required_artifact_lives_under_the_attempt_root(self) -> None:
        summaries = self.summaries()
        self.assertEqual(
            set(summaries), {"implementation_validation", "integration_validation"}
        )
        run_id = self.result.integration_run_id
        required = [
            # §2.2 evidence pipeline, execution validation.
            summaries["implementation_validation"]["path"],
            summaries["implementation_validation"]["path"].parent / COVERAGE_ARTIFACT_NAME,
            self.root / EVIDENCE_CHANGED_FILES_AUDIT,
            self.root / EVIDENCE_POLICY_VALIDATE_LOG,
            self.root / "changed-files-validate.log",
            # §2.2 evidence pipeline, integration validation.
            summaries["integration_validation"]["path"],
            summaries["integration_validation"]["path"].parent / COVERAGE_ARTIFACT_NAME,
            # Integration report and integration run JSON.
            self.root / "integration" / f"validators-{run_id}.json",
            self.root / "integration" / f"integration-{run_id}.json",
            # §2.4: the resolved task spec the executor ran against.
            self.root / "mission_contract.json",
        ]
        for phase in summaries.values():
            for row in phase["payload"]["validators"]:
                required.append(Path(row["artifact_path"]))
        for path in required:
            self.assertTrue(path.is_file(), path)
            self.assertTrue(_is_under(path, self.root), path)
        self.assertEqual(self.result.integration_json_path, required[8])
        self.assertEqual(self.result.validation_report.evidence_path, required[7])

        # Every summary and index names this Attempt and only this root.
        for phase, entry in summaries.items():
            payload = entry["payload"]
            self.assertEqual(payload["attempt_id"], self.attempt["attempt_id"], phase)
            coverage = self.coverage(phase)
            self.assertEqual(coverage["recorder_artifact_root"], str(self.root), phase)
            self.assertEqual(coverage["artifact_roots"], [str(self.root)], phase)
            self.assertEqual(
                coverage["artifact_root_binding"]["reason_code"], ROOT_ATTEMPT, phase
            )
            for entry_item in coverage["items"]:
                for reference in entry_item["references"]:
                    self.assertTrue(
                        _is_under(Path(reference["path"]), self.root), reference
                    )
                    self.assertTrue(reference["admissible"], reference)

        # Every artifact the run indexed for readers points under the root.
        indexed = [Path(a.path) for a in self.p.store.list_task_artifacts(self.key)]
        self.assertTrue(indexed)
        for path in indexed:
            self.assertTrue(_is_under(path, self.root), path)

    def test_nothing_new_is_written_at_the_task_level(self) -> None:
        task_dir = self.p.task_dir(self.key)
        stray = [
            path for path in task_dir.rglob("*")
            if path.is_file() and not _is_under(path, self.root)
        ]
        self.assertEqual(stray, [])
        self.assertFalse((task_dir / "validation-runs").exists())
        self.assertFalse((task_dir / "integration").exists())

    def test_the_integration_run_records_where_its_evidence_went(self) -> None:
        evidence_root = self.result.evidence_root
        self.assertEqual(evidence_root["scope"], SCOPE_ATTEMPT)
        self.assertEqual(evidence_root["path"], str(self.root))
        self.assertEqual(evidence_root["attempt_id"], self.attempt["attempt_id"])
        self.assertEqual(evidence_root["reason_code"], REASON_ATTEMPT_ROOT)
        # Allocation points tasks.artifact_dir at the newest Attempt's root;
        # the run records it, but binds by the producer, not by that pointer.
        self.assertEqual(
            evidence_root["task_artifact_dir"],
            str(self.p.store.get_task(self.key).artifact_dir),
        )
        written = json.loads(Path(self.result.integration_json_path).read_text())
        self.assertEqual(written["evidence_root"], evidence_root)
        started = [
            json.loads(e.payload_json or "{}")
            for e in self.p.store.list_task_events(self.key)
            if e.event_type == "integration_started"
        ]
        self.assertEqual(started[0]["evidence_root"], evidence_root)

    def test_preflight_and_compileall_are_not_applicable_with_their_reasons(self) -> None:
        for phase in ("implementation_validation", "integration_validation"):
            coverage = self.coverage(phase)
            preflight = self.item(coverage, EVIDENCE_PREFLIGHT_PR_CHECK)
            self.assertEqual(preflight["applicability"], "not_applicable", phase)
            self.assertEqual(preflight["reason"], PREFLIGHT_NOT_APPLICABLE_REASON)
            compileall = self.item(coverage, EVIDENCE_COMPILEALL_LOG)
            self.assertEqual(compileall["applicability"], "not_applicable", phase)
            self.assertIn("No validator configured for this run", compileall["reason"])
        self.assertEqual(list(self.p.task_dir(self.key).rglob("preflight-pr-check.json")), [])
        self.assertEqual(list(self.p.task_dir(self.key).rglob("compileall.log")), [])

    def test_the_gate_and_lifecycle_are_unchanged(self) -> None:
        self.assertEqual(self.result.final_task_status, schema.NEEDS_REVIEW)
        self.assertTrue(self.result.validators_passed)
        self.assertFalse(self.result.merged)
        self.assertEqual(self.p.store.get_task(self.key).status, schema.NEEDS_REVIEW)


class RetriedTicketTests(unittest.TestCase):
    """Criterion 1 when the producer is not the newest Attempt.

    With one Attempt, the producer's root and the newest Attempt's root (which
    ``tasks.artifact_dir`` points at after allocation) are the same directory,
    so placement alone cannot tell producer binding from "whatever the task
    row points at". Here a later Attempt exists, so they differ.
    """

    def setUp(self) -> None:
        self.p = _Pipeline(self)
        self.key = self.p.dispatch("Retried after handoff")
        self.p.commit_implementation(self.key)
        self.p.retry_that_fails(self.key)
        producer, newest = self.p.fx.attempts(self.key)
        self.producer_root = Path(producer["artifact_root"])
        self.newest_root = Path(newest["artifact_root"])
        self.producer_id = producer["attempt_id"]
        self.newest_id = newest["attempt_id"]
        self.result = self.p.integrate(self.key)
        self.assertEqual(self.result.status, "integrated", self.result.summary)

    def test_the_fixture_has_a_newer_attempt_than_the_producer(self) -> None:
        self.assertNotEqual(self.producer_root, self.newest_root)
        self.assertTrue(self.newest_root.is_dir())
        # The task row points at the newest Attempt; the queue entry does not.
        self.assertEqual(
            Path(self.p.store.get_task(self.key).artifact_dir), self.newest_root
        )
        binding = self.result.producer_attempt_binding
        self.assertEqual(binding["reason_code"], REASON_BOUND)
        self.assertEqual(binding["attempt_id"], self.producer_id)

    def test_integration_evidence_lands_under_the_producer_attempt_root(self) -> None:
        run_id = self.result.integration_run_id
        integration_json = self.producer_root / "integration" / f"integration-{run_id}.json"
        report = self.producer_root / "integration" / f"validators-{run_id}.json"
        where = f"producer {self.producer_id}, newest {self.newest_id}"
        self.assertEqual(
            str(self.result.integration_json_path), str(integration_json), where
        )
        self.assertEqual(
            str(self.result.validation_report.evidence_path), str(report), where
        )
        self.assertTrue(integration_json.is_file())
        self.assertTrue(report.is_file())

        summaries = [
            path for path in self.producer_root.glob("validation-runs/*/validation-summary.json")
            if json.loads(path.read_text())["phase"] == "integration_validation"
        ]
        self.assertEqual(len(summaries), 1)
        summary = json.loads(summaries[0].read_text())
        self.assertEqual(summary["attempt_id"], self.producer_id)
        coverage = json.loads((summaries[0].parent / COVERAGE_ARTIFACT_NAME).read_text())
        self.assertEqual(coverage["artifact_roots"], [str(self.producer_root)])
        self.assertEqual(coverage["artifact_root_binding"]["reason_code"], ROOT_ATTEMPT)
        for row in summary["validators"]:
            self.assertTrue(_is_under(Path(row["artifact_path"]), self.producer_root), row)

        self.assertEqual(self.result.evidence_root["scope"], SCOPE_ATTEMPT)
        self.assertEqual(self.result.evidence_root["attempt_id"], self.producer_id)
        self.assertEqual(self.result.evidence_root["path"], str(self.producer_root))

    def test_nothing_from_the_integration_run_lands_under_the_newer_attempt(self) -> None:
        self.assertFalse(
            (self.newest_root / "integration").exists(),
            f"integration evidence under the newer Attempt {self.newest_id}",
        )
        phases = [
            json.loads(path.read_text())["phase"]
            for path in self.newest_root.glob("validation-runs/*/validation-summary.json")
        ]
        self.assertNotIn("integration_validation", phases)
        integration_rows = [
            Path(a.path) for a in self.p.store.list_task_artifacts(self.key)
            if a.artifact_type.startswith("integration")
        ]
        self.assertTrue(integration_rows)
        for path in integration_rows:
            self.assertTrue(_is_under(path, self.producer_root), path)


class ValidatorContextBindingTests(unittest.TestCase):
    """Criterion 2: the Attempt root does not depend on the validator proxy."""

    def test_the_dispatcher_context_is_attempt_bound_without_the_proxy(self) -> None:
        seen = []

        def passthrough(store, context):
            # The proxy's own rebinding is disabled; what arrives is exactly
            # the context the dispatcher built.
            seen.append(context)
            return context

        p = _Pipeline(self)
        with mock.patch.object(
            attempt_scoped_runtime_path.AttemptScopedRuntimeTaskStore,
            "bind_validator_context",
            passthrough,
        ):
            key = p.dispatch("Proxy disabled")
        root = Path(p.attempt(key)["artifact_root"])
        self.assertEqual(len(seen), 2)
        for context in seen:
            self.assertEqual(context.artifact_dir, root)
            self.assertEqual(context.attempt_id, p.attempt(key)["attempt_id"])
        self.assertTrue((root / EVIDENCE_CHANGED_FILES_AUDIT).is_file())
        self.assertTrue((root / EVIDENCE_POLICY_VALIDATE_LOG).is_file())
        self.assertFalse((p.task_dir(key) / EVIDENCE_CHANGED_FILES_AUDIT).exists())

    def test_the_bound_task_values_are_released_with_the_attempt(self) -> None:
        p = _Pipeline(self)
        key = p.dispatch("Released")
        # The terminal status released the Attempt, so a value read for the
        # next run is not rebound to this Attempt's root.
        self.assertNotIn(key, p.last_store._task_objects)


class EvidenceRootFallbackTests(unittest.TestCase):
    """Where the task level remains, and that each case says why."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.p = _Pipeline(self)
        self.task_dir = self.root / "AT-900"

    def binding(self, attempt_id: str | None, reason_code: str = REASON_BOUND):
        return ProducerAttemptBinding(
            task_key="AT-900",
            kind=BINDING_PRODUCER_HANDOFF if attempt_id else BINDING_NONE,
            reason_code=reason_code,
            reason="test",
            attempt_id=attempt_id,
        )

    def resolve(self, binding):
        return resolve_integration_evidence_root(
            self.p.fx.db_path,
            task_key="AT-900",
            task_artifact_dir=self.task_dir,
            producer_binding=binding,
        )

    def test_an_unbound_run_stays_at_the_task_level_with_the_binding_reason(self) -> None:
        root = self.resolve(self.binding(None, REASON_NO_ENTRY))
        self.assertEqual(root.scope, SCOPE_TASK)
        self.assertEqual(root.path, self.task_dir)
        self.assertEqual(root.reason_code, REASON_NO_PRODUCER)
        self.assertIn(REASON_NO_ENTRY, root.reason)
        self.assertIsNone(root.attempt_id)
        self.assertEqual(root.task_level_reason_code, TASK_LEVEL_RECORDED)

    def test_a_database_path_with_uri_characters_is_read(self) -> None:
        # '?', '#' and '%41' would be read as a query, a fragment and an
        # escape by a raw 'file:' URI; the lookup must still find the row.
        key = self.p.dispatch("Odd database path")
        attempt = self.p.attempt(key)
        odd = self.root / "state?mode=rw#frag%41" / "state.db"
        odd.parent.mkdir()
        with closing(sqlite3.connect(self.p.fx.db_path)) as source, closing(
            sqlite3.connect(odd)
        ) as target:
            source.backup(target)
        resolved = resolve_integration_evidence_root(
            odd,
            task_key=key,
            task_artifact_dir=Path(attempt["artifact_root"]),
            producer_binding=None,
        )
        self.assertEqual(resolved.reason_code, REASON_NO_PRODUCER)
        self.assertEqual(resolved.task_level_reason_code, TASK_LEVEL_FROM_ATTEMPT_BASE)
        self.assertEqual(resolved.task_artifact_dir_attempt_id, attempt["attempt_id"])
        self.assertEqual(resolved.path, self.p.task_dir(key))

    def test_a_failed_task_level_lookup_is_reported_not_silent(self) -> None:
        not_a_database = self.root / "not-a-database.db"
        not_a_database.write_bytes(b"this is not sqlite\n" * 64)
        resolved = resolve_integration_evidence_root(
            not_a_database,
            task_key="AT-900",
            task_artifact_dir=self.task_dir,
            producer_binding=None,
        )
        self.assertEqual(resolved.scope, SCOPE_TASK)
        self.assertEqual(resolved.reason_code, REASON_NO_PRODUCER)
        self.assertEqual(resolved.task_level_reason_code, TASK_LEVEL_UNRESOLVED)
        self.assertIn("could not be read", resolved.reason)
        self.assertEqual(
            resolved.to_dict()["task_level_reason_code"], TASK_LEVEL_UNRESOLVED
        )

    def test_no_binding_and_no_task_directory_writes_nowhere(self) -> None:
        root = resolve_integration_evidence_root(
            self.p.fx.db_path, task_key="AT-900", task_artifact_dir=None, producer_binding=None
        )
        self.assertEqual(root.scope, SCOPE_NONE)
        self.assertIsNone(root.path)

    def test_an_unknown_attempt_is_reported_not_guessed(self) -> None:
        root = self.resolve(self.binding("attempt-that-does-not-exist"))
        self.assertEqual(root.scope, SCOPE_TASK)
        self.assertEqual(root.reason_code, REASON_NO_ATTEMPT_ROOT)
        self.assertEqual(root.attempt_id, "attempt-that-does-not-exist")

    def test_an_unreadable_attempt_store_is_reported(self) -> None:
        root = resolve_integration_evidence_root(
            self.root / "no-attempt-tables.db",
            task_key="AT-900",
            task_artifact_dir=self.task_dir,
            producer_binding=self.binding("attempt-x"),
        )
        self.assertEqual(root.scope, SCOPE_TASK)
        self.assertEqual(root.reason_code, REASON_ATTEMPT_UNREADABLE)

    def test_a_removed_attempt_root_is_reported_not_recreated(self) -> None:
        key = self.p.dispatch("Root removed")
        attempt = self.p.attempt(key)
        root = Path(attempt["artifact_root"])
        moved = root.with_name(root.name + ".moved")
        root.rename(moved)
        resolved = resolve_integration_evidence_root(
            self.p.fx.db_path,
            task_key=key,
            task_artifact_dir=self.p.task_dir(key),
            producer_binding=self.binding(attempt["attempt_id"]),
        )
        self.assertEqual(resolved.scope, SCOPE_TASK)
        self.assertEqual(resolved.reason_code, REASON_ATTEMPT_ROOT_UNAVAILABLE)
        self.assertFalse(root.exists())

    def test_a_symlinked_attempt_root_is_refused(self) -> None:
        key = self.p.dispatch("Root symlinked")
        attempt = self.p.attempt(key)
        root = Path(attempt["artifact_root"])
        elsewhere = self.root / "elsewhere"
        root.rename(elsewhere)
        root.symlink_to(elsewhere, target_is_directory=True)
        resolved = resolve_integration_evidence_root(
            self.p.fx.db_path,
            task_key=key,
            task_artifact_dir=self.p.task_dir(key),
            producer_binding=self.binding(attempt["attempt_id"]),
        )
        self.assertEqual(resolved.reason_code, REASON_ATTEMPT_ROOT_UNAVAILABLE)

    def test_a_manual_integration_run_keeps_task_level_evidence(self) -> None:
        key = self.p.dispatch("Manual run")
        self.p.commit_implementation(key)
        # Nothing handed this run off: the queue entry is gone, as after a
        # manual or watcher-driven run.
        remove_from_queue(self.p.integration, key)
        result = self.p.integrate(key)
        self.assertEqual(result.status, "integrated", result.summary)
        self.assertEqual(result.evidence_root["scope"], SCOPE_TASK)
        self.assertEqual(result.evidence_root["reason_code"], REASON_NO_PRODUCER)
        # The task's recorded directory is the dispatched Attempt's root, but
        # an unbound run is not filed under it: it goes to the task level.
        attempt = self.p.attempt(key)
        self.assertEqual(result.evidence_root["task_artifact_dir"], attempt["artifact_root"])
        self.assertEqual(
            result.evidence_root["task_artifact_dir_attempt_id"], attempt["attempt_id"]
        )
        task_dir = self.p.task_dir(key)
        self.assertEqual(result.evidence_root["path"], str(task_dir))
        self.assertEqual(result.integration_json_path.parent, task_dir / "integration")
        self.assertTrue(any((task_dir / "validation-runs").iterdir()))
        root = Path(attempt["artifact_root"])
        self.assertFalse((root / "integration").exists())
        # Only the execution run's own summary is under the Attempt root.
        phases = [
            json.loads(path.read_text())["phase"]
            for path in root.glob("validation-runs/*/validation-summary.json")
        ]
        self.assertEqual(phases, ["implementation_validation"])


class ArtifactRootBindingTests(unittest.TestCase):
    """Every coverage index says why its root is, or is not, an Attempt root."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "AT-1"

    def binding(self, root: Path, attempt_id: str | None) -> dict:
        root.mkdir(parents=True)
        collector = RunnerEvidenceCollector(source="test", artifact_roots=(root,))
        recorder = ValidationSummaryRecorder(
            task_key="AT-1",
            artifact_dir=root,
            source="test",
            phase="implementation_validation",
            validators=(),
            attempt_id=attempt_id,
            coverage_builder=collector.coverage,
        )
        recorder.finish()
        published = json.loads(recorder.path.read_text())
        coverage = json.loads(Path(published["evidence_coverage"]["path"]).read_text())
        return coverage["artifact_root_binding"]

    def test_a_run_without_a_runtime_claim_names_its_task_level_root(self) -> None:
        # Fallback case 3: no claim, so artifact_root_for_claim keeps the
        # task's directory. The index now says so with a reason code.
        binding = self.binding(self.base, None)
        self.assertEqual(binding["scope"], "task")
        self.assertEqual(binding["reason_code"], ROOT_NO_ATTEMPT)
        self.assertIn(str(self.base), binding["reason"])

    def test_an_attempt_root_is_named_as_one(self) -> None:
        binding = self.binding(self.base / "attempt-1", "attempt-1")
        self.assertEqual(binding["scope"], "attempt")
        self.assertEqual(binding["reason_code"], ROOT_ATTEMPT)

    def test_an_attempt_without_its_own_root_says_so(self) -> None:
        # A compatibility store without Attempt resources.
        binding = self.binding(self.base, "attempt-1")
        self.assertEqual(binding["scope"], "task")
        self.assertEqual(binding["reason_code"], ROOT_ATTEMPT_ELSEWHERE)


class PreexistingTaskLevelEvidenceTests(unittest.TestCase):
    """Criterion 4: evidence written before this change is still found."""

    def test_old_task_level_evidence_stays_put_and_readers_still_list_it(self) -> None:
        p = _Pipeline(self)
        key = p.dispatch("Old evidence")
        task_dir = p.task_dir(key)
        # What an integration run wrote before this change, and indexed.
        old_json = task_dir / "integration" / "integration-oldrun00000.json"
        old_report = task_dir / "integration" / "validators-oldrun00000.json"
        old_summary = task_dir / "validation-runs" / "old" / "validation-summary.json"
        for path, artifact_type in (
            (old_json, "integration_result"),
            (old_report, "integration_validator_evidence"),
            (old_summary, "other"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
            p.store.record_task_artifact(key, artifact_type, path)
        before = {path: path.read_bytes() for path in (old_json, old_report, old_summary)}

        p.commit_implementation(key)
        result = p.integrate(key)
        self.assertEqual(result.status, "integrated", result.summary)

        # Nothing moved or rewritten.
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        root = Path(p.attempt(key)["artifact_root"])
        self.assertTrue(_is_under(result.integration_json_path, root))

        # The API artifact listing (Mission Control's source) finds both.
        with TestClient(create_app(p.fx.db_path)) as client:
            response = client.get(f"/api/tasks/{key}/artifacts")
            self.assertEqual(response.status_code, 200)
            paths = {item.get("path") for item in response.json()["items"]}
            evidence = client.get(f"/api/tasks/{key}/evidence")
            self.assertEqual(evidence.status_code, 200)
        for path in (old_json, old_report, old_summary, result.integration_json_path):
            self.assertIn(str(path), paths)
        self.assertIn(str(old_json), json.dumps(evidence.json()))
        self.assertIn(str(result.integration_json_path), json.dumps(evidence.json()))

    def test_the_attempt_store_still_reads_the_attempt_root(self) -> None:
        p = _Pipeline(self)
        key = p.dispatch("Ledger root")
        attempt = p.attempt(key)
        stored = AttemptStore(p.fx.db_path).get_attempt(attempt["attempt_id"])
        self.assertEqual(str(stored.artifact_root), attempt["artifact_root"])


if __name__ == "__main__":  # pragma: no cover - direct unittest execution
    unittest.main()
