"""V1-EXECUTOR-CONTRACT review-r1 nits N1, N3, N5 and N6 (RULINGS 67).

N1: Ticket creation reads the same package-anchored registry as the policy,
    and the claim refuses a Ticket whose repo_path is not its registry entry's.
N3: the diff gate runs no external diff or textconv driver.
N5: the registry refuses duplicate mapping keys.
N6: the legacy-entry-point Ticket guard fails closed without a database path.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import agent_taskflow  # noqa: F401  installs the layered runtime path
import agent_taskflow.execution_policy as execution_policy
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.execution_policy import (
    POLICY_INVALID,
    POLICY_REPO_PATH_MISMATCH,
    ExecutionPolicyError,
    resolve_execution_policy,
    ticket_repo_path_refusal,
)
from agent_taskflow.ready_queue import eligible_tickets, policy_refused_tickets
from agent_taskflow.runtime_admission import RuntimeAdmissionStore, RuntimeExecutionPolicyError
from agent_taskflow.ticket_creation import TicketCreationRequest, create_ticket
from agent_taskflow.ticket_lifecycle import (
    LEGACY_ENTRYPOINT_REFUSED,
    legacy_entrypoint_ticket_refusal,
)
from agent_taskflow.ticket_repositories import (
    DEFAULT_PROJECTS_CONFIG_PATH,
    TicketRepositoryError,
    list_ticket_repositories,
    resolve_ticket_repository,
)
from agent_taskflow.ticket_store import TicketStore
from agent_taskflow.ticket_success_gate import worktree_diff_refusal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution_policy_support import policy_block, project_entry, write_registry  # noqa: E402
from step5_support import git, make_fixture  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class TicketCreationRegistryTests(unittest.TestCase):
    """N1: Ticket creation and the policy read one package-anchored registry."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        # A decoy registry in the working directory names another repository.
        self.decoy_repo = self.fx.root / "decoy-repo"
        self.decoy_repo.mkdir()
        decoy = self.fx.root / "cwd"
        write_registry(decoy / "config" / "projects.yaml", {
            "step5": project_entry(self.decoy_repo, execution=policy_block()),
        })
        previous = Path.cwd()
        os.chdir(decoy)
        self.addCleanup(os.chdir, previous)

    def test_the_default_registry_is_the_policy_registry_not_the_cwd(self) -> None:
        self.assertTrue(DEFAULT_PROJECTS_CONFIG_PATH.is_absolute())
        self.assertEqual(DEFAULT_PROJECTS_CONFIG_PATH, REPO_ROOT / "config" / "projects.yaml")
        # The resolver's registry (patched by the fixture) wins over the cwd one.
        self.assertEqual(resolve_ticket_repository("step5").repo_path, self.fx.repo)
        self.assertEqual(
            [r.repo_path for r in list_ticket_repositories() if r.repository == "step5"],
            [self.fx.repo],
        )

    def test_a_relative_registry_path_is_refused(self) -> None:
        with self.assertRaises(TicketRepositoryError) as caught:
            resolve_ticket_repository("step5", Path("config/projects.yaml"))
        self.assertIn("must be absolute", str(caught.exception))

    def test_a_ticket_created_by_name_gets_the_policy_registry_repo_path(self) -> None:
        ticket = create_ticket(
            TicketCreationRequest(repository="step5", prompt="Created from the registry"),
            store=TicketStore(self.fx.db_path),
        ).ticket
        self.assertEqual(ticket.repo_path, self.fx.repo)
        self.assertEqual(eligible_tickets(self.fx.db_path)[0].task_key, ticket.task_key)


class ClaimRepoPathCrossCheckTests(unittest.TestCase):
    """N1: a Ticket whose repo_path is not its registry entry's is never claimed."""

    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.key = self.fx.create_ticket("Repo path cross-check").task_key
        # The project's entry now names another repository; the policy is valid.
        self.moved = self.fx.root / "moved-repo"
        self.moved.mkdir()
        write_registry(self.fx.registry_path, {
            "step5": project_entry(self.moved, execution=policy_block()),
        })

    def snapshot(self) -> tuple:
        return (
            self.fx.task_row(self.key)["status"],
            self.fx.attempts(self.key),
            self.fx.leases(self.key),
            len(self.fx.events(self.key)),
        )

    def test_ready_queue_excludes_it_with_the_reason(self) -> None:
        self.assertEqual(eligible_tickets(self.fx.db_path), [])
        self.assertEqual(
            [(r.task_key, r.reason_code) for r in policy_refused_tickets(self.fx.db_path)],
            [(self.key, POLICY_REPO_PATH_MISMATCH)],
        )

    def test_the_claim_transaction_refuses_it_and_writes_nothing(self) -> None:
        before = self.snapshot()
        with self.assertRaises(RuntimeExecutionPolicyError) as caught:
            RuntimeAdmissionStore(self.fx.db_path).claim(self.key, owner_id="cross-check")
        self.assertEqual(caught.exception.reason_code, POLICY_REPO_PATH_MISMATCH)
        self.assertEqual(self.snapshot(), before)

    def test_the_dispatcher_refuses_it_and_writes_nothing(self) -> None:
        before = self.snapshot()
        dispatcher = Dispatcher(db_path=self.fx.db_path)
        try:
            result = dispatcher.dispatch_task(self.key)
        finally:
            dispatcher.store.shutdown_runtime_supervisors()
        self.assertEqual(result.status, "blocked")
        self.assertIn(POLICY_REPO_PATH_MISMATCH, result.summary)
        self.assertEqual(self.snapshot(), before)

    def test_the_matching_repo_path_is_claimed(self) -> None:
        # The same repository through a symlink is the same repository.
        link = self.fx.root / "repo-link"
        link.symlink_to(self.fx.repo, target_is_directory=True)
        write_registry(self.fx.registry_path, {
            "step5": project_entry(link, execution=policy_block()),
        })
        claim = RuntimeAdmissionStore(self.fx.db_path).claim(self.key, owner_id="cross-check")
        self.assertEqual(self.fx.attempts(self.key)[0]["attempt_id"], claim.attempt_id)

    def test_the_helper_fails_closed(self) -> None:
        policy = resolve_execution_policy("step5")
        self.assertIsNotNone(ticket_repo_path_refusal(policy, None))
        self.assertIsNotNone(ticket_repo_path_refusal(policy, "relative/repo"))
        self.assertIsNotNone(
            ticket_repo_path_refusal(policy.__class__(**{**policy.__dict__, "repo_path": None}), self.moved)
        )
        self.assertIsNone(ticket_repo_path_refusal(policy, self.moved))


class DiffGateDriverTests(unittest.TestCase):
    """N3: no configured external diff or textconv driver runs in the gate."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="v1-diff-gate-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "gate@example.invalid")
        git(self.repo, "config", "user.name", "Gate")
        (self.repo / "tracked.txt").write_text("before\n", encoding="utf-8")
        (self.repo / ".gitattributes").write_text("*.txt diff=spy\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.markers = {name: self.root / f"{name}-ran" for name in ("external", "textconv")}
        for name, marker in self.markers.items():
            script = self.root / f"{name}.sh"
            script.write_text(f"#!/bin/sh\ntouch {marker}\ncat \"$1\" 2>/dev/null\nexit 0\n")
            script.chmod(0o755)
        # Ticket-controlled repository configuration naming both drivers.
        git(self.repo, "config", "diff.external", str(self.root / "external.sh"))
        git(self.repo, "config", "diff.spy.textconv", str(self.root / "textconv.sh"))
        (self.repo / "tracked.txt").write_text("after\n", encoding="utf-8")

    def ran(self) -> dict[str, bool]:
        return {name: marker.exists() for name, marker in self.markers.items()}

    def test_the_drivers_are_live_for_a_plain_git_diff(self) -> None:
        # Sanity: the configuration really would run each driver.
        subprocess.run(["git", "diff", self.base], cwd=self.repo, capture_output=True, check=False)
        self.assertTrue(self.ran()["external"])
        subprocess.run(["git", "diff", "--no-ext-diff", self.base],
                       cwd=self.repo, capture_output=True, check=False)
        self.assertTrue(self.ran()["textconv"])

    def test_the_gate_disables_drivers_hooks_and_fsmonitor_on_its_diff(self) -> None:
        # git 2.43's --quiet already skips both drivers (the behavioural test
        # below passes either way); the flags make that independent of git's
        # version, so the command line itself is pinned here.
        calls: list[list[str]] = []
        real_run = subprocess.run

        def spy(argv, *args, **kwargs):
            calls.append(list(argv))
            return real_run(argv, *args, **kwargs)

        with unittest.mock.patch("agent_taskflow.ticket_success_gate.subprocess.run", side_effect=spy):
            worktree_diff_refusal(self.repo, self.base)
        (diff_call,) = [argv for argv in calls if "diff" in argv]
        for flag in ("--no-ext-diff", "--no-textconv", "--quiet",
                     "core.fsmonitor=false", "core.hooksPath=/dev/null"):
            self.assertIn(flag, diff_call)

    def test_the_gate_sees_the_diff_without_running_either_driver(self) -> None:
        self.assertIsNone(worktree_diff_refusal(self.repo, self.base))
        self.assertEqual(self.ran(), {"external": False, "textconv": False})
        git(self.repo, "checkout", "--", "tracked.txt")
        self.assertIn("no change", worktree_diff_refusal(self.repo, self.base))
        self.assertEqual(self.ran(), {"external": False, "textconv": False})


class DuplicateKeyTests(unittest.TestCase):
    """N5: a duplicated key anywhere in the registry is a validation error."""

    ENTRY = """\
    repo_path: /srv/demo
"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="v1-dup-keys-")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "projects.yaml"

    def block(self, *, extra_field: str = "", repeat_block: bool = False) -> str:
        execution = (
            "    execution:\n"
            "      executor: claude-code\n"
            "      argv: [claude, --model, '{model}', --effort, '{effort}']\n"
            "      model: m-one\n"
            f"{extra_field}"
            "      effort: medium\n"
            "      timeout_seconds: 60\n"
            "      permission_profile: p\n"
            "      policy_version: '1'\n"
            "      implementation_validators: [{name: pytest, timeout_seconds: 60}]\n"
            "      integration_validators: [{name: unit, command: ['true'], timeout_seconds: 60}]\n"
        )
        return execution + (execution if repeat_block else "")

    def resolve(self, text: str):
        self.path.write_text(text, encoding="utf-8")
        return resolve_execution_policy("demo", config_path=self.path)

    def assert_duplicate(self, text: str) -> None:
        with self.assertRaises(ExecutionPolicyError) as caught:
            self.resolve(text)
        self.assertEqual(caught.exception.reason_code, POLICY_INVALID)
        self.assertIn("duplicate", str(caught.exception))

    def test_a_valid_registry_still_resolves(self) -> None:
        self.assertEqual(self.resolve("projects:\n  demo:\n" + self.ENTRY + self.block()).model, "m-one")

    def test_a_duplicated_execution_block_is_refused(self) -> None:
        self.assert_duplicate("projects:\n  demo:\n" + self.ENTRY + self.block(repeat_block=True))

    def test_a_duplicated_policy_field_is_refused(self) -> None:
        self.assert_duplicate(
            "projects:\n  demo:\n" + self.ENTRY + self.block(extra_field="      model: m-two\n")
        )

    def test_a_duplicated_project_is_refused_for_ticket_creation_too(self) -> None:
        text = "projects:\n  demo:\n" + self.ENTRY + "  demo:\n" + self.ENTRY
        self.assert_duplicate(text)
        with self.assertRaises(TicketRepositoryError):
            resolve_ticket_repository("demo", self.path)

    def test_merge_keys_are_not_duplicates(self) -> None:
        text = (
            "shared: &shared\n  repo_path: /srv/demo\n"
            "projects:\n  demo:\n    <<: *shared\n" + self.block()
        )
        self.assertEqual(self.resolve(text).repo_path, Path("/srv/demo"))


class LegacyGuardFailsClosedTests(unittest.TestCase):
    """N6: without a database path the guard refuses instead of allowing."""

    def test_no_database_path_is_refused(self) -> None:
        refusal = legacy_entrypoint_ticket_refusal(None, "AT-0001", entrypoint="a caller")
        self.assertIsNotNone(refusal)
        self.assertIn(LEGACY_ENTRYPOINT_REFUSED, refusal)
        self.assertIn("no database path", refusal)

    def test_an_unreadable_database_is_refused_and_a_missing_one_is_legacy(self) -> None:
        self.assertIn(
            LEGACY_ENTRYPOINT_REFUSED,
            legacy_entrypoint_ticket_refusal(Path("relative.db"), "AT-0001", entrypoint="x"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            # A database that does not exist holds no Ticket (is_ticket's rule).
            self.assertIsNone(
                legacy_entrypoint_ticket_refusal(Path(tmp) / "none.db", "AT-0001", entrypoint="x")
            )


if __name__ == "__main__":
    unittest.main()
