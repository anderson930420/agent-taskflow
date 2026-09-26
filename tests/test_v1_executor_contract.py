"""V1-EXECUTOR-CONTRACT (OWNER RULING 67, D1) acceptance tests.

Covers the resolver, claim-path enforcement (OR-8.3), the dispatcher's
allowlist success with a fake ``claude`` executable configured through the
policy, the prompt writer, and the Attempt's policy record. No test calls a
real model: the executor is ``tests/fake_claude_executable.py``.
"""

from __future__ import annotations

from contextlib import closing
import dataclasses
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import agent_taskflow  # noqa: F401  installs the layered runtime path
import agent_taskflow.execution_policy as execution_policy
from agent_taskflow.attempt_failure_class import read_attempt_failure_class
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.execution_policy import (
    EXECUTOR_ALLOWLIST,
    POLICY_CHANGED,
    POLICY_EXECUTOR_NOT_ALLOWED,
    POLICY_INVALID,
    POLICY_MISSING,
    POLICY_OVERRIDE_REFUSED,
    POLICY_PROJECT_NOT_REGISTERED,
    POLICY_REGISTRY_UNAVAILABLE,
    POLICY_TIMEOUT_MISSING,
    POLICY_VALIDATORS_EMPTY,
    ExecutionPolicyError,
    resolve_execution_policy,
)
from agent_taskflow.executors.claude_code import ClaudeCodeExecutor
from agent_taskflow.parallel_scheduler import run_scheduler_tick
from agent_taskflow.ready_queue import eligible_tickets, policy_refused_tickets
from agent_taskflow.runtime_admission import (
    RuntimeAdmissionStore,
    RuntimeExecutionPolicyError,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution_policy_support import (  # noqa: E402
    TEST_EFFORT,
    TEST_MODEL,
    policy_block,
    project_entry,
    registry,
    write_registry,
)
from step5_support import RecordingValidator, make_fixture  # noqa: E402
from test_integration_tick import VALIDATORS, TickFixture  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="v1-policy-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "projects.yaml"

    def resolve(self, execution, *, project: str = "demo"):
        entry = project_entry(self.root / "repo", execution=execution)
        write_registry(self.path, {"demo": entry})
        return resolve_execution_policy(project, config_path=self.path)

    def assert_refused(self, code: str, execution, *, project: str = "demo") -> ExecutionPolicyError:
        with self.assertRaises(ExecutionPolicyError) as caught:
            self.resolve(execution, project=project)
        self.assertEqual(caught.exception.reason_code, code, str(caught.exception))
        self.assertIn(code, str(caught.exception))
        return caught.exception

    def test_valid_policy_is_frozen_and_hashed(self) -> None:
        policy = self.resolve(policy_block())
        self.assertEqual(policy.executor, "claude-code")
        self.assertEqual(policy.model, TEST_MODEL)
        self.assertEqual(policy.effort, TEST_EFFORT)
        self.assertEqual(policy.timeout_seconds, 120)
        self.assertEqual(policy.implementation_validator_names, ("pytest",))
        self.assertEqual(policy.integration_validators[0].command, ("python3", "-c", "pass"))
        self.assertEqual(
            policy.resolved_argv()[-4:], ("--model", TEST_MODEL, "--effort", TEST_EFFORT)
        )
        self.assertRegex(policy.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(policy.snapshot()["policy_sha256"], policy.sha256)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.model = "other"  # type: ignore[misc]
        # The hash is of the canonical form: stable across key order, and it
        # moves with every field, policy_version included.
        reordered = dict(reversed(list(policy_block().items())))
        self.assertEqual(self.resolve(reordered).sha256, policy.sha256)
        self.assertNotEqual(self.resolve(policy_block(policy_version="test-2")).sha256, policy.sha256)

    def test_missing_policy_and_unknown_project(self) -> None:
        self.assert_refused(POLICY_MISSING, None)
        self.assert_refused(POLICY_PROJECT_NOT_REGISTERED, policy_block(), project="other")

    def test_disallowed_executors(self) -> None:
        self.assertEqual(EXECUTOR_ALLOWLIST, frozenset({"claude-code"}))
        for executor in ("manual", "noop", "shell", "opencode", "pi"):
            with self.subTest(executor=executor):
                self.assert_refused(POLICY_EXECUTOR_NOT_ALLOWED, policy_block(executor=executor))

    def test_empty_validator_lists(self) -> None:
        for key in ("implementation_validators", "integration_validators"):
            with self.subTest(key=key, value="[]"):
                self.assert_refused(POLICY_VALIDATORS_EMPTY, policy_block(**{key: []}))
            with self.subTest(key=key, value="absent"):
                block = policy_block()
                del block[key]
                self.assert_refused(POLICY_VALIDATORS_EMPTY, block)

    def test_missing_timeouts(self) -> None:
        self.assert_refused(POLICY_TIMEOUT_MISSING, policy_block(timeout_seconds=None))
        self.assert_refused(
            POLICY_TIMEOUT_MISSING,
            policy_block(implementation_validators=[{"name": "pytest"}]),
        )
        self.assert_refused(
            POLICY_TIMEOUT_MISSING,
            policy_block(integration_validators=[{"name": "unit", "command": ["true"]}]),
        )

    def test_invalid_fields_refused(self) -> None:
        cases = {
            "unknown key": policy_block(env={"TOKEN": "x"}),
            "no model placeholder": policy_block(argv=["claude", "--effort", "{effort}"]),
            "twice": policy_block(argv=["claude", "{model}", "{model}", "{effort}"]),
            "other placeholder": policy_block(argv=["claude", "{model}", "{effort}", "{home}"]),
            "placeholder argv0": policy_block(argv=["{model}", "{effort}"]),
            "empty argv part": policy_block(argv=["claude", "", "{model}", "{effort}"]),
            "model with space": policy_block(model="a b"),
            "bool timeout": policy_block(timeout_seconds=True),
            "huge timeout": policy_block(timeout_seconds=10**9),
            "unknown implementation validator": policy_block(
                implementation_validators=[{"name": "nope", "timeout_seconds": 5}]
            ),
            "duplicate validators": policy_block(
                implementation_validators=[
                    {"name": "pytest", "timeout_seconds": 5},
                    {"name": "pytest", "timeout_seconds": 5},
                ]
            ),
            "integer policy_version": policy_block(policy_version=3),
            "not a mapping": ["claude-code"],
        }
        for label, block in cases.items():
            with self.subTest(label):
                self.assert_refused(POLICY_INVALID, block)

    def test_registry_is_read_by_absolute_path_never_the_cwd(self) -> None:
        self.assertTrue(execution_policy.PROJECTS_REGISTRY_PATH.is_absolute())
        self.assertEqual(
            execution_policy.PROJECTS_REGISTRY_PATH, REPO_ROOT / "config" / "projects.yaml"
        )
        with self.assertRaises(ExecutionPolicyError) as caught:
            resolve_execution_policy("demo", config_path=Path("config/projects.yaml"))
        self.assertEqual(caught.exception.reason_code, POLICY_REGISTRY_UNAVAILABLE)

        # A decoy registry in the working directory grants a policy; the
        # resolver still reads the patched absolute path, which grants none.
        decoy = self.root / "cwd"
        write_registry(decoy / "config" / "projects.yaml", {
            "demo": project_entry(self.root / "repo", execution=policy_block()),
        })
        previous = Path.cwd()
        os.chdir(decoy)
        self.addCleanup(os.chdir, previous)
        with registry(self.path, {"demo": project_entry(self.root / "repo")}):
            self.assertEqual(
                execution_policy.execution_policy_refusal("demo").reason_code, POLICY_MISSING
            )
        with registry(self.path, {"demo": project_entry(self.root / "repo", execution=policy_block())}):
            self.assertIsNone(execution_policy.execution_policy_refusal("demo"))

    def test_shipped_registry_makes_only_agent_taskflow_runnable(self) -> None:
        # RULINGS 67: a project without a policy is not runnable. V0 (OR-10 Q3)
        # gives agent-taskflow the only policy; its content is checked in
        # tests/test_v0_executor_local_path.py.
        _, projects = execution_policy.load_registry_projects(
            REPO_ROOT / "config" / "projects.yaml"
        )
        self.assertIn("agent-taskflow", projects)
        for name in set(projects) - {"agent-taskflow"}:
            refusal = execution_policy.execution_policy_refusal(
                name, config_path=REPO_ROOT / "config" / "projects.yaml"
            )
            self.assertIsNotNone(refusal, name)
            self.assertEqual(refusal.reason_code, POLICY_MISSING)


def _no_policy_registry(fx) -> None:
    write_registry(fx.registry_path, {"step5": project_entry(fx.repo)})


class ClaimPathEnforcementTests(unittest.TestCase):
    """OR-8.3: a project with no valid policy is never claimed, on any path."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.key = self.fx.create_ticket("Claim path").task_key

    def snapshot(self) -> tuple:
        return (
            self.fx.task_row(self.key)["status"],
            self.fx.attempts(self.key),
            self.fx.leases(self.key),
            len(self.fx.events(self.key)),
        )

    def test_eligible_tickets_excludes_and_names_the_reason(self) -> None:
        self.assertEqual([t.task_key for t in eligible_tickets(self.fx.db_path)], [self.key])
        _no_policy_registry(self.fx)
        self.assertEqual(eligible_tickets(self.fx.db_path), [])
        refused = policy_refused_tickets(self.fx.db_path)
        self.assertEqual([(r.task_key, r.reason_code) for r in refused], [(self.key, POLICY_MISSING)])

    def test_direct_claim_is_refused_inside_the_transaction(self) -> None:
        _no_policy_registry(self.fx)
        before = self.snapshot()
        with self.assertRaises(RuntimeExecutionPolicyError) as caught:
            RuntimeAdmissionStore(self.fx.db_path).claim(self.key, owner_id="direct-test")
        self.assertEqual(caught.exception.reason_code, POLICY_MISSING)
        self.assertEqual(self.snapshot(), before)

    def test_disallowed_executor_refused_at_claim(self) -> None:
        write_registry(self.fx.registry_path, {
            "step5": project_entry(self.fx.repo, execution=policy_block(executor="manual")),
        })
        with self.assertRaises(RuntimeExecutionPolicyError) as caught:
            RuntimeAdmissionStore(self.fx.db_path).claim(self.key, owner_id="direct-test")
        self.assertEqual(caught.exception.reason_code, POLICY_EXECUTOR_NOT_ALLOWED)

    def test_claim_records_the_policy_and_refuses_overrides(self) -> None:
        store = RuntimeAdmissionStore(self.fx.db_path)
        for field, value in (("executor", "manual"), ("model", "other-model"),
                             ("policy_version", "other"), ("permission_profile", "root")):
            with self.subTest(field=field):
                with self.assertRaises(RuntimeExecutionPolicyError) as caught:
                    store.claim(self.key, owner_id="override-test", **{field: value})
                self.assertEqual(caught.exception.reason_code, POLICY_OVERRIDE_REFUSED)
        with self.assertRaises(RuntimeExecutionPolicyError) as caught:
            store.claim(self.key, owner_id="stale-test", config_snapshot_hash="0" * 64)
        self.assertEqual(caught.exception.reason_code, POLICY_CHANGED)
        self.assertEqual(self.fx.attempts(self.key), [])

        policy = resolve_execution_policy("step5")
        claim = store.claim(self.key, owner_id="direct-test")
        attempt = self.fx.attempts(self.key)[0]
        self.assertEqual(attempt["attempt_id"], claim.attempt_id)
        self.assertEqual(
            (attempt["executor"], attempt["model"], attempt["policy_version"],
             attempt["config_snapshot_hash"], attempt["permission_profile"]),
            ("claude-code", TEST_MODEL, "test-1", policy.sha256, "test-bounded-implementer"),
        )

    def test_dispatcher_claim_refused_and_nothing_written(self) -> None:
        _no_policy_registry(self.fx)
        before = self.snapshot()
        dispatcher = Dispatcher(db_path=self.fx.db_path)
        try:
            result = dispatcher.dispatch_task(self.key)
            self.assertEqual(result.status, "blocked")
            self.assertIn(POLICY_MISSING, result.summary)
            self.assertEqual(self.snapshot(), before)
            # Even a store transition that skips the dispatcher's own check is
            # refused by the claim transaction.
            with self.assertRaises(RuntimeExecutionPolicyError):
                dispatcher.store.update_task_status(self.key, "preparing", source="direct")
        finally:
            dispatcher.store.shutdown_runtime_supervisors()
        self.assertEqual(self.snapshot(), before)

    def test_scheduler_tick_starts_nothing_and_reports_why(self) -> None:
        _no_policy_registry(self.fx)

        def launcher(db_path, task_key):  # pragma: no cover - must not be called
            raise AssertionError(f"worker launched for {task_key}")

        result = run_scheduler_tick(self.fx.db_path, launcher=launcher, wait=True)
        self.assertEqual(result.started, ())
        self.assertEqual(result.candidates, ())
        self.assertEqual(
            [(r["task_key"], r["reason_code"]) for r in result.to_dict()["policy_refused"]],
            [(self.key, POLICY_MISSING)],
        )
        self.assertEqual(self.fx.status(self.key), "created")

    def test_reset_reserved_retry_adoption_is_gated_too(self) -> None:
        from agent_taskflow.reset_lineage import ResetLineageStore
        from agent_taskflow.reset_runtime_path import ResetAwareRuntimeAdmissionStore
        from agent_taskflow.store import TaskMirrorStore
        from step5_support import RecordingExecutor

        self.fx.dispatch(self.key, RecordingExecutor("failed"))
        TaskMirrorStore(self.fx.db_path).update_task_status(
            self.key, "blocked", source="test", blocked_reason="hold for retry"
        )
        ResetLineageStore(self.fx.db_path).reserve_retry(self.key, reason="retry", actor="test")
        reserved = self.fx.attempts(self.key)[-1]
        self.assertEqual((self.fx.status(self.key), reserved["status"]), ("queued", "created"))

        _no_policy_registry(self.fx)
        before = self.snapshot()
        with self.assertRaises(RuntimeExecutionPolicyError) as caught:
            ResetAwareRuntimeAdmissionStore(self.fx.db_path).claim(self.key, owner_id="retry-test")
        self.assertEqual(caught.exception.reason_code, POLICY_MISSING)
        self.assertEqual(self.snapshot(), before)

        self.fx.set_policy_validators(("pytest",))
        claim = ResetAwareRuntimeAdmissionStore(self.fx.db_path).claim(self.key, owner_id="retry-test")
        self.assertEqual(claim.attempt_id, reserved["attempt_id"])
        adopted = self.fx.attempts(self.key)[-1]
        self.assertEqual(
            (adopted["executor"], adopted["config_snapshot_hash"]),
            ("claude-code", resolve_execution_policy("step5").sha256),
        )

    def test_legacy_task_claim_needs_no_policy(self) -> None:
        _no_policy_registry(self.fx)
        self.fx.add_legacy_task("AT-9001")
        claim = RuntimeAdmissionStore(self.fx.db_path).claim("AT-9001", owner_id="legacy-test")
        attempt = self.fx.attempts("AT-9001")[0]
        self.assertEqual(attempt["attempt_id"], claim.attempt_id)
        self.assertEqual(attempt["executor"], "fake")
        self.assertIsNone(attempt["policy_version"])


class _DuckResult:
    """An executor result whose status is outside the executor vocabulary."""

    def __init__(self, status: str) -> None:
        self.executor = "claude-code"
        self.status = status
        self.exit_code = 0
        self.summary = f"duck {status}"
        self.log_path = None
        self.artifacts = {}


class _StaticExecutor:
    name = "claude-code"

    def __init__(self, result) -> None:
        self.result = result

    def run(self, context):
        return self.result


class DispatcherPolicyEndToEndTests(unittest.TestCase):
    """The real claude-code adapter, invoked through the policy's argv."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.prompt = "Add a CHANGELOG entry for the V1 executor contract.\n\nKeep it short."
        self.key = self.fx.create_ticket(self.prompt).task_key
        self.policy = resolve_execution_policy("step5")
        env = mock.patch.dict(os.environ, {
            "FAKE_CLAUDE_WRITE": "1", "FAKE_CLAUDE_EXIT_CODE": "0", "FAKE_CLAUDE_COMMIT": "0",
        })
        env.start()
        self.addCleanup(env.stop)

    def dispatch(self, *, executor=None, validator_status: str = "passed", **kwargs):
        dispatcher = Dispatcher(
            db_path=self.fx.db_path,
            executor_registry={"claude-code": executor} if executor is not None else None,
            validator_registry={"pytest": RecordingValidator("pytest", status=validator_status)},
            # Ignored for a Ticket: the policy decides.
            validators=("openspec",),
            default_executor="manual",
        )
        try:
            return dispatcher.dispatch_task(self.key, **kwargs)
        finally:
            dispatcher.store.shutdown_runtime_supervisors()

    def attempt(self) -> dict:
        return self.fx.attempts(self.key)[-1]

    def failure_class(self) -> str | None:
        record = read_attempt_failure_class(self.fx.db_path, self.attempt()["attempt_id"])
        return record["failure_class"] if record else None

    def assert_not_ready(self, result, status: str, needle: str, failure_class: str) -> None:
        self.assertEqual(result.status, status, result.summary)
        self.assertIn(needle, result.summary)
        self.assertEqual(self.fx.status(self.key), status)
        self.assertEqual(self.failure_class(), failure_class)
        with closing(self.fx.connect()) as conn:
            queued = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'integration_queue'"
            ).fetchone()[0]
            if queued:
                count = conn.execute(
                    "SELECT COUNT(*) FROM integration_queue WHERE task_key = ?", (self.key,)
                ).fetchone()[0]
                self.assertEqual(count, 0)

    def test_completed_validators_passed_and_diff_is_ready(self) -> None:
        result = self.dispatch()
        self.assertEqual(result.status, "ready_for_integration", result.summary)
        self.assertEqual(result.validator_statuses, {"pytest": "passed"})
        root = Path(self.attempt()["artifact_root"])

        # The prompt writer: the Ticket's stored prompt, verbatim, is the file
        # and the contract's goal (not the title).
        stored = self.fx.task_row(self.key)["prompt"]
        self.assertTrue(stored.startswith("Add a CHANGELOG entry"))
        self.assertEqual((root / "implementation_prompt.md").read_text(encoding="utf-8"), stored)
        contract = json.loads((root / "mission_contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["goal"], stored)
        self.assertNotEqual(contract["goal"], self.fx.task_row(self.key)["title"])
        self.assertEqual(contract["executor"], "claude-code")
        self.assertEqual(contract["required_validators"], ["pytest"])
        self.assertIn(stored, (root / "claude-code-implementer-prompt.md").read_text(encoding="utf-8"))

        # The executor received the policy's argv: model and effort included.
        stdout = json.loads((root / "claude-code-stdout.log").read_text(encoding="utf-8").strip())
        self.assertEqual(stdout["argv"], ["--model", TEST_MODEL, "--effort", TEST_EFFORT])
        self.assertTrue((Path(self.fx.task_row(self.key)["worktree_path"]) / "fake-claude-change.txt").is_file())

        # The Attempt record carries the policy.
        attempt = self.attempt()
        self.assertEqual(
            (attempt["executor"], attempt["model"], attempt["policy_version"],
             attempt["config_snapshot_hash"], attempt["permission_profile"]),
            ("claude-code", TEST_MODEL, "test-1", self.policy.sha256, "test-bounded-implementer"),
        )
        snapshot = json.loads((root / "execution_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["policy_sha256"], self.policy.sha256)
        self.assertEqual(snapshot["policy"]["effort"], TEST_EFFORT)
        self.assertEqual(snapshot["policy"]["timeout_seconds"], 120)
        self.assertEqual(set(snapshot["policy"]), {
            "executor", "argv", "model", "effort", "timeout_seconds", "permission_profile",
            "policy_version", "implementation_validators", "integration_validators",
        })

        # ... and so does the launch provenance.
        launch = [json.loads(p.read_text(encoding="utf-8")) for p in root.glob("resolved-launch-*.json")]
        self.assertEqual(len(launch), 1)
        requested = launch[0]["requested_launch"]
        self.assertEqual(requested["policy_version"], "test-1")
        self.assertEqual(requested["permission_profile"], "test-bounded-implementer")
        self.assertEqual(requested["model"], TEST_MODEL)
        self.assertEqual(requested["timeout_seconds"], 120)
        self.assertEqual(launch[0]["config_snapshot_reference"]["sha256"], self.policy.sha256)
        self.assertEqual(launch[0]["configured_attempt"]["config_snapshot_hash"], self.policy.sha256)

    def test_committed_change_counts_as_a_diff(self) -> None:
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        result = self.dispatch()
        self.assertEqual(result.status, "ready_for_integration", result.summary)

    def test_completed_without_diff_is_not_ready(self) -> None:
        os.environ["FAKE_CLAUDE_WRITE"] = "0"
        result = self.dispatch()
        self.assert_not_ready(result, "failed", "produced no change", "execution_failure")

    def test_nonzero_exit_is_not_ready(self) -> None:
        os.environ["FAKE_CLAUDE_EXIT_CODE"] = "3"
        result = self.dispatch()
        self.assert_not_ready(result, "failed", "exit code 3", "execution_failure")

    def test_skipped_executor_is_not_ready(self) -> None:
        from agent_taskflow.executors.base import ExecutorResult

        result = self.dispatch(executor=_StaticExecutor(
            ExecutorResult(executor="claude-code", status="skipped", summary="skipped")
        ))
        self.assert_not_ready(result, "failed", "'skipped'", "execution_failure")

    def test_dry_run_executor_is_not_ready(self) -> None:
        # The real adapter in its prompt-only mode reports `completed` with no
        # process; that is not an invocation.
        result = self.dispatch(executor=ClaudeCodeExecutor(
            command=self.policy.resolved_argv(), enable_invocation=False,
        ))
        self.assert_not_ready(result, "failed", "exit code was None", "execution_failure")

    def test_unknown_executor_status_is_not_ready(self) -> None:
        result = self.dispatch(executor=_StaticExecutor(_DuckResult("unknown")))
        self.assert_not_ready(result, "failed", "'unknown'", "execution_failure")

    def test_completed_claim_without_managed_launch_is_not_ready(self) -> None:
        from agent_taskflow.executors.base import ExecutorResult

        result = self.dispatch(executor=_StaticExecutor(
            ExecutorResult(executor="claude-code", status="completed", exit_code=0)
        ))
        self.assert_not_ready(result, "failed", "managed executor launch", "execution_failure")

    def test_launch_of_another_argv_is_not_ready(self) -> None:
        other = (*self.policy.resolved_argv(), "--dangerously-extra")
        result = self.dispatch(executor=ClaudeCodeExecutor(command=other, enable_invocation=True))
        self.assert_not_ready(result, "failed", "argv differs", "execution_failure")

    def test_failing_validator_is_not_ready(self) -> None:
        result = self.dispatch(validator_status="failed")
        self.assert_not_ready(result, "needs_decision", "fake validator failed", "validation_failure")

    def test_skipped_validator_is_not_ready(self) -> None:
        result = self.dispatch(validator_status="skipped")
        self.assert_not_ready(result, "failed", "'skipped'", "tool_error")

    def test_caller_executor_or_model_is_refused_without_writing(self) -> None:
        before = (self.fx.status(self.key), self.fx.attempts(self.key), len(self.fx.events(self.key)))
        for kwargs in ({"executor_name": "manual"}, {"model": "other"}, {"executor_name": "claude-code"}):
            with self.subTest(**kwargs):
                result = self.dispatch(**kwargs)
                self.assertEqual(result.status, "blocked")
                self.assertIn(POLICY_OVERRIDE_REFUSED, result.summary)
        self.assertEqual(
            (self.fx.status(self.key), self.fx.attempts(self.key), len(self.fx.events(self.key))),
            before,
        )

    def test_policy_changed_between_resolution_and_claim_fails(self) -> None:
        stale = dataclasses.replace(self.policy, sha256="f" * 64)
        with mock.patch("agent_taskflow.dispatcher.resolve_execution_policy", return_value=stale):
            result = self.dispatch()
        self.assert_not_ready(result, "failed", POLICY_CHANGED, "unknown")
        self.assertFalse(
            (Path(self.attempt()["artifact_root"]) / "claude-code-stdout.log").exists(),
            "the executor must not run under a policy the claim did not record",
        )



class LegacyAndApiOverrideTests(unittest.TestCase):
    """RULINGS 67: no CLI, API or legacy path overrides a V1 Ticket's policy."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.key = self.fx.create_ticket("Refuse the legacy paths").task_key
        self.fx.add_legacy_task("AT-9100")

    def snapshot(self, key: str) -> tuple:
        return (self.fx.task_row(key)["status"], self.fx.attempts(key), len(self.fx.events(key)))

    def test_api_start_refuses_overrides_for_a_ticket(self) -> None:
        from fastapi.testclient import TestClient
        from agent_taskflow.api.main import create_app
        from agent_taskflow.ticket_lifecycle import LEGACY_ENTRYPOINT_REFUSED

        before = self.snapshot(self.key)
        with TestClient(create_app(self.fx.db_path)) as client:
            # RULINGS 80/81 (G5): /start refuses every V1 Ticket before the
            # override check, so no override reaches the policy.
            for body in ({"executor": "manual"}, {"model": "other"}, {"validators": []},
                         {"validators": ["pytest"], "dry_run": True}):
                with self.subTest(body=body):
                    response = client.post(f"/api/tasks/{self.key}/start", json=body)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertFalse(response.json()["ok"])
                    self.assertIn(LEGACY_ENTRYPOINT_REFUSED, response.json()["message"])
            # Without an override the Ticket is refused too: it runs only
            # through the execution tick, under its policy.
            _no_policy_registry(self.fx)
            response = client.post(f"/api/tasks/{self.key}/start", json={})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertFalse(response.json()["ok"])
            self.assertIn(LEGACY_ENTRYPOINT_REFUSED, response.json()["message"])
            # A legacy task keeps its per-request selection: it reaches the
            # dispatcher, which refuses it only for its own (missing) worktree.
            legacy = client.post("/api/tasks/AT-9100/start", json={"executor": "noop", "dry_run": True})
            self.assertEqual(legacy.status_code, 200, legacy.text)
            self.assertIn("Task worktree not found", legacy.json()["message"])
            self.assertNotIn("execution_policy", legacy.text)
        self.assertEqual(self.snapshot(self.key), before)

    def test_run_dispatcher_script_fails_closed_on_a_ticket(self) -> None:
        import importlib.util
        import io
        from contextlib import redirect_stdout

        from agent_taskflow.ticket_lifecycle import LEGACY_ENTRYPOINT_REFUSED

        spec = importlib.util.spec_from_file_location("v1ec_run_dispatcher", REPO_ROOT / "scripts/run_dispatcher.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        before = self.snapshot(self.key)
        with redirect_stdout(io.StringIO()) as output:
            code = cli.main(["--task-key", self.key, "--db-path", str(self.fx.db_path)])
        self.assertEqual(code, 2)
        self.assertIn(LEGACY_ENTRYPOINT_REFUSED, json.loads(output.getvalue())["summary"])
        self.assertEqual(self.snapshot(self.key), before)
        with redirect_stdout(io.StringIO()) as output:
            cli.main(["--task-key", "AT-9100", "--db-path", str(self.fx.db_path),
                      "--executor", "noop", "--dry-run"])
        # A legacy task still reaches the dispatcher (here, refused for its worktree).
        self.assertEqual(json.loads(output.getvalue())["summary"], "Task worktree not found: AT-9100")

    def test_approved_task_runner_fails_closed_on_a_ticket(self) -> None:
        from agent_taskflow.approved_task_runner import ApprovedTaskRunRequest, run_approved_task
        from agent_taskflow.ticket_lifecycle import LEGACY_ENTRYPOINT_REFUSED

        before = self.snapshot(self.key)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = run_approved_task(ApprovedTaskRunRequest(
                    task_key=self.key, executor="claude-code", repo_path=self.fx.repo,
                    db_path=self.fx.db_path, confirm_approved_task=True, dry_run=dry_run,
                ))
                self.assertFalse(result.ok)
                self.assertIn(LEGACY_ENTRYPOINT_REFUSED, json.dumps(result.to_dict()))
        self.assertEqual(self.snapshot(self.key), before)

    def test_queued_task_handoff_fails_closed_on_a_ticket(self) -> None:
        from agent_taskflow.queued_task_handoff import QueuedTaskHandoffRequest, run_queued_task_handoff
        from agent_taskflow.ticket_lifecycle import LEGACY_ENTRYPOINT_REFUSED

        before = self.snapshot(self.key)
        result = run_queued_task_handoff(QueuedTaskHandoffRequest(
            task_key=self.key, executor="claude-code", repo_path=self.fx.repo,
            db_path=self.fx.db_path, dry_run=True,
        ))
        self.assertFalse(result.ok)
        self.assertIn(LEGACY_ENTRYPOINT_REFUSED, json.dumps(result.to_dict()))
        self.assertEqual(self.snapshot(self.key), before)

    def test_execution_engine_fails_closed_on_a_ticket(self) -> None:
        from agent_taskflow.execution_engine_approved_task_adapter import (
            ApprovedTaskRunnerExecutionEngineAdapter,
        )
        from agent_taskflow.execution_engine_manual_runtime import (
            build_manual_execution_engine_request,
        )
        from agent_taskflow.ticket_lifecycle import LEGACY_ENTRYPOINT_REFUSED

        request = dataclasses.replace(
            build_manual_execution_engine_request(
                task_key=self.key, repo_path=self.fx.repo, artifact_dir=self.fx.artifacts,
                executor="claude-code", validators=("pytest",), dry_run=False,
            ),
            lifecycle_db_path=self.fx.db_path,
        )
        before = self.snapshot(self.key)
        result = ApprovedTaskRunnerExecutionEngineAdapter().execute(request)
        self.assertFalse(result.ok)
        self.assertIn(LEGACY_ENTRYPOINT_REFUSED, result.summary)
        self.assertEqual(self.snapshot(self.key), before)



class IntegrationDrainPolicyTests(TickFixture):
    """The drain runs a Ticket's policy's integration validators and nothing else."""

    def drain_outcome(self, **overrides) -> dict:
        ticket = self.make_ticket()
        with mock.patch("agent_taskflow.integration_tick.integrate_task") as controller:
            result = self.tick(**overrides)
        controller.assert_not_called()
        self.assertFalse(result["ok"])
        (outcome,) = result["outcomes"]
        self.assertEqual(outcome["task_key"], ticket.task_key)
        self.assertFalse(outcome["controller_called"])
        self.assertEqual(self.status(ticket), "ready_for_integration")
        return outcome

    def test_request_validators_that_differ_from_the_policy_are_refused(self) -> None:
        from agent_taskflow.integration_tick import POLICY_INTEGRATION_VALIDATORS_MISMATCH
        from agent_taskflow.integration_validators import IntegrationValidatorSpec

        other = (IntegrationValidatorSpec("other", ("true",), 120),)
        outcome = self.drain_outcome(validator_specs=other)
        self.assertEqual(outcome["reason"], POLICY_INTEGRATION_VALIDATORS_MISMATCH)

    def test_a_ticket_whose_project_has_no_policy_is_not_integrated(self) -> None:
        write_registry(self.registry_path, {"fixture": project_entry(self.fixture.repo, github_repo="owner/repo")})
        self.assertEqual(self.drain_outcome()["reason"], POLICY_MISSING)

    def test_the_policy_validators_reach_the_controller(self) -> None:
        self.make_ticket()
        seen = []

        def controller(request, **_kwargs):
            seen.append(request.validator_specs)
            raise RuntimeError("stop after observing the request")

        with mock.patch("agent_taskflow.integration_tick.integrate_task", side_effect=controller):
            self.tick()
        self.assertEqual(seen, [VALIDATORS])


if __name__ == "__main__":
    unittest.main()
