"""V0-EXECUTOR-LOCAL-PATH: the control plane commits a Ticket's output (OR-10 Q2).

After the executor ran, every validator passed and the evidence is present, the
dispatcher commits the Ticket worktree itself, with hooks disabled, so that
integration has a commit to rebase and publish. No test calls a real model: the
executor is ``tests/fake_claude_executable.py``.
"""

from __future__ import annotations

from contextlib import closing
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import agent_taskflow  # noqa: F401  installs the layered runtime path
import agent_taskflow.execution_policy as execution_policy
from agent_taskflow import integration_schema
from agent_taskflow.attempt_failure_class import read_attempt_failure_class
from agent_taskflow.dispatcher import CONTROL_PLANE_COMMIT_FILENAME, Dispatcher
from agent_taskflow.github_pr_adapter import GitHubPrAdapter
from agent_taskflow.integration_tick import IntegrationTickRequest, run_integration_tick
from agent_taskflow.models import TaskWorktreeRecord
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.ticket_success_gate import control_plane_commit_refusal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from execution_policy_support import policy_block, project_entry, write_registry  # noqa: E402
from step5_support import RecordingExecutor, RecordingValidator, git, make_fixture  # noqa: E402
from v1_step2_fixtures import FakeGhRunner, isolate_integration_lock_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
_IDENTITY_ENV = (
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL",
)


class ControlPlaneCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        self.key = self.fx.create_ticket("Add a note file.\n\nOne line is enough.").task_key
        # The host's global identity only: the fixture's repo-local identity is
        # removed and a controlled global config stands in for ~/.gitconfig.
        git(self.fx.repo, "config", "--unset", "user.name")
        git(self.fx.repo, "config", "--unset", "user.email")
        self.global_config = self.fx.root / "global.gitconfig"
        self.global_config.write_text(
            "[user]\n\tname = Control Plane\n\temail = control-plane@example.invalid\n",
            encoding="utf-8",
        )
        env = mock.patch.dict(os.environ, {
            "FAKE_CLAUDE_WRITE": "1", "FAKE_CLAUDE_EXIT_CODE": "0", "FAKE_CLAUDE_COMMIT": "0",
            "GIT_CONFIG_GLOBAL": str(self.global_config), "GIT_CONFIG_NOSYSTEM": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        for name in _IDENTITY_ENV:
            os.environ.pop(name, None)

    def dispatch(self):
        dispatcher = Dispatcher(
            db_path=self.fx.db_path,
            validator_registry={"pytest": RecordingValidator("pytest")},
            default_executor="manual",
        )
        try:
            return dispatcher.dispatch_task(self.key)
        finally:
            dispatcher.store.shutdown_runtime_supervisors()

    def worktree(self) -> Path:
        return Path(self.fx.task_row(self.key)["worktree_path"])

    def base(self) -> str:
        return self.fx.attempt_resources(self.key)[-1]["base_sha"]

    def head(self) -> str:
        return git(self.worktree(), "rev-parse", "HEAD").strip()

    def record(self) -> dict:
        root = Path(self.fx.attempts(self.key)[-1]["artifact_root"])
        return json.loads((root / CONTROL_PLANE_COMMIT_FILENAME).read_text(encoding="utf-8"))

    def assert_failed(self, result, needle: str) -> None:
        self.assertEqual(result.status, "failed", result.summary)
        self.assertIn(needle, result.summary)
        self.assertEqual(self.fx.status(self.key), "failed")
        attempt = self.fx.attempts(self.key)[-1]["attempt_id"]
        # FAILURE_GOVERNANCE's class.
        self.assertEqual(read_attempt_failure_class(self.fx.db_path, attempt)["failure_class"], "unknown")
        with closing(self.fx.connect()) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'integration_queue'").fetchone():
                queued = conn.execute(
                    "SELECT COUNT(*) FROM integration_queue WHERE task_key = ?", (self.key,)
                ).fetchone()[0]
                self.assertEqual(queued, 0)

    def test_uncommitted_output_is_committed_by_the_control_plane(self) -> None:
        result = self.dispatch()
        self.assertEqual(result.status, "ready_for_integration", result.summary)
        worktree, base, head = self.worktree(), self.base(), self.head()
        self.assertNotEqual(head, base)
        self.assertEqual(git(worktree, "rev-list", "--count", f"{base}..HEAD").strip(), "1")
        self.assertEqual(git(worktree, "status", "--porcelain").strip(), "")
        title = self.fx.task_row(self.key)["title"]
        self.assertEqual(git(worktree, "log", "-1", "--format=%s").strip(), f"{self.key}: {title}")
        self.assertEqual(
            git(worktree, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip(),
            "Control Plane <control-plane@example.invalid>|Control Plane <control-plane@example.invalid>",
        )
        self.assertIn("fake-claude-change.txt", git(worktree, "show", "--name-only", "--format=", "HEAD"))
        self.assertEqual(self.record(), {
            "base_sha": base, "head_sha": head, "committed_by_control_plane": True,
            "message": f"{self.key}: {title}",
        })

    def test_an_executor_commit_is_not_committed_again(self) -> None:
        os.environ["FAKE_CLAUDE_COMMIT"] = "1"
        result = self.dispatch()
        self.assertEqual(result.status, "ready_for_integration", result.summary)
        base = self.base()
        self.assertEqual(git(self.worktree(), "rev-list", "--count", f"{base}..HEAD").strip(), "1")
        self.assertEqual(git(self.worktree(), "log", "-1", "--format=%s").strip(), "fake change")
        self.assertEqual(self.record(), {
            "base_sha": base, "head_sha": self.head(), "committed_by_control_plane": False,
            "message": None,
        })

    def test_repository_commit_hooks_do_not_run(self) -> None:
        sentinel = self.fx.root / "hook-ran"
        script = f"#!/bin/sh\necho \"$0\" >> '{sentinel}'\nexit 1\n"
        hooks = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit")
        configured = self.fx.root / "configured-hooks"
        configured.mkdir()
        for directory in (self.fx.repo / ".git" / "hooks", configured):
            for name in hooks:
                path = directory / name
                path.write_text(script, encoding="utf-8")
                path.chmod(0o755)
        for hooks_path in (None, str(configured)):
            with self.subTest(core_hooks_path=hooks_path):
                if hooks_path is not None:
                    self.key = self.fx.create_ticket("A second Ticket.").task_key
                    git(self.fx.repo, "config", "core.hooksPath", hooks_path)
                result = self.dispatch()
                self.assertEqual(result.status, "ready_for_integration", result.summary)
                self.assertNotEqual(self.head(), self.base())
                self.assertFalse(sentinel.exists(), sentinel.read_text() if sentinel.exists() else "")

    def test_a_git_commit_failure_fails_the_ticket(self) -> None:
        # No configured email. $EMAIL would let git guess one on any host, and
        # user.useConfigOnly must refuse that guess.
        self.global_config.write_text("[user]\n\tname = Control Plane\n", encoding="utf-8")
        os.environ["EMAIL"] = "guessed@example.invalid"
        result = self.dispatch()
        self.assert_failed(result, "Control-plane git commit failed")
        self.assertEqual(self.head(), self.base())

    def test_head_still_at_base_fails_the_ticket(self) -> None:
        # Were the diff gate ever to pass a run that changed nothing, the
        # commit step would still refuse it.
        os.environ["FAKE_CLAUDE_WRITE"] = "0"
        with mock.patch("agent_taskflow.dispatcher.worktree_diff_refusal", return_value=None):
            result = self.dispatch()
        self.assert_failed(result, "HEAD is still the base")

    def test_legacy_task_is_not_committed(self) -> None:
        key = "AT-LEGACY-COMMIT"
        self.fx.add_legacy_task(key)
        worktree = self.fx.repo / ".worktrees" / key
        git(self.fx.repo, "worktree", "add", "-b", f"task/{key}", str(worktree), "main")
        base = git(worktree, "rev-parse", "HEAD").strip()
        TaskMirrorStore(self.fx.db_path).upsert_task_worktree(TaskWorktreeRecord(
            task_key=key, repo_path=self.fx.repo, worktree_path=worktree,
            branch=f"task/{key}", base_branch="main", base_sha=base, status="active",
        ))
        executor = RecordingExecutor("completed", write_file="legacy.txt")
        result = self.fx.dispatch(key, executor)
        self.assertEqual(result.status, "waiting_approval", result.summary)
        # The legacy run's own (Attempt-scoped) worktree keeps its output uncommitted.
        ran_in = Path(executor.contexts[0].worktree_path)
        self.assertEqual(git(ran_in, "rev-parse", "HEAD").strip(), base)
        self.assertEqual(git(ran_in, "status", "--porcelain").strip(), "?? legacy.txt")
        self.assertEqual(list(self.fx.artifacts.rglob(CONTROL_PLANE_COMMIT_FILENAME)), [])


class ControlPlaneCommitHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="v0-commit-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "helper@example.invalid")
        git(self.repo, "config", "user.name", "Helper")
        (self.repo / "README.md").write_text("x\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("evil/\n", encoding="utf-8")
        git(self.repo, "add", "README.md", ".gitignore")
        git(self.repo, "commit", "-m", "initial")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.branch = "task/T1"
        self.wt = self.repo / ".worktrees" / "T1"
        git(self.repo, "worktree", "add", "-q", "-b", self.branch, str(self.wt), "main")
        self.record = self.root / "record.json"

    def refusal(self, base: str | None = None) -> str | None:
        return control_plane_commit_refusal(
            self.wt, base or self.base, "AT-1: t", self.record,
            repo_path=self.repo, branch=self.branch,
        )

    def branch_head(self) -> str:
        return git(self.repo, "rev-parse", f"refs/heads/{self.branch}").strip()

    def test_a_change_is_committed_onto_the_task_branch(self) -> None:
        (self.wt / "new.txt").write_text("y\n", encoding="utf-8")
        self.assertIsNone(self.refusal())
        record = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(record["head_sha"], self.branch_head())
        self.assertNotEqual(record["head_sha"], self.base)

    def test_a_clean_tree_at_base_is_refused_and_records_nothing(self) -> None:
        self.assertIn("HEAD is still the base", self.refusal())
        self.assertFalse(self.record.exists())

    def test_a_redirected_git_pointer_is_refused_before_git_runs_in_it(self) -> None:
        # Review r1 (exp-gitdir-redirect): files the executor can write inside its
        # worktree point `.git` at a rogue git dir whose config defines a clean
        # filter. Unbound, `git add` ran the filter and the commit landed in the
        # rogue git dir while the task branch stayed at base.
        evil = self.wt / "evil"
        (evil / "objects" / "info").mkdir(parents=True)
        (evil / "refs" / "heads").mkdir(parents=True)
        (evil / "objects" / "info" / "alternates").write_text(
            f"{self.repo / '.git' / 'objects'}\n", encoding="utf-8")
        (evil / "HEAD").write_text(f"{self.base}\n", encoding="utf-8")
        sentinel = self.root / "PWNED-filter"
        (self.wt / "x.sh").write_text(f"touch {sentinel}\ncat\n", encoding="utf-8")
        (evil / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
            "[user]\n\tname = E\n\temail = e@x.invalid\n"
            f"[filter \"p\"]\n\tclean = sh {self.wt / 'x.sh'}\n",
            encoding="utf-8",
        )
        (self.wt / ".gitattributes").write_text("*.txt filter=p\n", encoding="utf-8")
        (self.wt / ".git").write_text("gitdir: evil\n", encoding="utf-8")
        (self.wt / "new.txt").write_text("change\n", encoding="utf-8")

        refusal = self.refusal()
        self.assertIsNotNone(refusal)
        self.assertIn("is not bound to the managed repository", refusal)
        self.assertFalse(sentinel.exists(), "a Ticket-defined filter ran")
        self.assertFalse(self.record.exists())
        self.assertEqual(self.branch_head(), self.base)

    def test_a_commit_off_the_task_branch_is_refused(self) -> None:
        git(self.wt, "checkout", "-q", "--detach")
        (self.wt / "new.txt").write_text("y\n", encoding="utf-8")
        refusal = self.refusal()
        self.assertIsNotNone(refusal)
        self.assertIn(f"refs/heads/{self.branch} in", refusal)
        self.assertIn("not the worktree HEAD", refusal)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.branch_head(), self.base)

    def test_git_failures_are_refusals_not_success(self) -> None:
        (self.wt / "new.txt").write_text("y\n", encoding="utf-8")
        failed = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="boom")
        real = subprocess.run

        def run(argv, *args, **kwargs):
            if "commit" in argv:
                return failed
            return real(argv, *args, **kwargs)

        with mock.patch("agent_taskflow.ticket_success_gate.subprocess.run", side_effect=run):
            self.assertEqual(self.refusal(), "Control-plane git commit failed: boom")
        with mock.patch(
            "agent_taskflow.ticket_success_gate.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 60),
        ):
            self.assertIn("could not be made", self.refusal())
        self.assertIn("no base commit", control_plane_commit_refusal(
            self.wt, None, "m", self.record, repo_path=self.repo, branch=self.branch,
        ))
        self.assertFalse(self.record.exists())
        self.assertEqual(self.branch_head(), self.base)


class ControlPlaneCommitIntegrationTests(unittest.TestCase):
    """A control-plane commit is what the integration tick rebases and publishes."""

    def setUp(self) -> None:
        isolate_integration_lock_dir(self)
        self.fx = make_fixture()
        self.addCleanup(self.fx.cleanup)
        origin = self.fx.root / "origin.git"
        git(self.fx.root, "init", "--bare", "-b", "main", str(origin))
        git(self.fx.repo, "remote", "add", "origin", str(origin))
        git(self.fx.repo, "push", "origin", "main")
        self.origin = origin
        check = "from pathlib import Path; assert Path('fake-claude-change.txt').is_file()"
        write_registry(self.fx.registry_path, {"step5": project_entry(
            self.fx.repo, github_repo="owner/repo", execution=policy_block(
                integration_validators=[
                    {"name": "committed", "command": [sys.executable, "-c", check], "timeout_seconds": 60},
                ],
            ),
        )})
        self.fx.repository = dataclasses.replace(self.fx.repository, github_repo="owner/repo")
        env = mock.patch.dict(os.environ, {
            "FAKE_CLAUDE_WRITE": "1", "FAKE_CLAUDE_EXIT_CODE": "0", "FAKE_CLAUDE_COMMIT": "0",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_the_committed_ticket_is_rebased_and_published(self) -> None:
        key = self.fx.create_ticket("Add a note file.").task_key
        dispatcher = Dispatcher(
            db_path=self.fx.db_path, validator_registry={"pytest": RecordingValidator("pytest")},
        )
        try:
            result = dispatcher.dispatch_task(key)
        finally:
            dispatcher.store.shutdown_runtime_supervisors()
        self.assertEqual(result.status, "ready_for_integration", result.summary)
        # The target moves on, so integration must really rebase the commit.
        git(self.fx.repo, "commit", "--allow-empty", "-m", "target moved")
        git(self.fx.repo, "push", "origin", "main")

        gh = FakeGhRunner()
        policy = execution_policy.resolve_execution_policy("step5")
        tick = run_integration_tick(IntegrationTickRequest(
            repo="owner/repo", repo_path=self.fx.repo, db_path=self.fx.db_path,
            validator_specs=policy.integration_validator_specs(),
            dry_run=False, confirm_integration=True,
        ), github=GitHubPrAdapter("owner/repo", runner=gh))

        self.assertTrue(tick["ok"], tick)
        self.assertEqual([o["task_key"] for o in tick["outcomes"]], [key])
        self.assertEqual(self.fx.status(key), integration_schema.NEEDS_REVIEW)
        creates = [c for c in gh.calls if c[:3] == ["gh", "pr", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn("--draft", creates[0])
        branch = self.fx.task_row(key)["branch"]
        published = git(self.origin, "rev-parse", branch).strip()
        target = git(self.origin, "rev-parse", "main").strip()
        self.assertEqual(git(self.origin, "rev-parse", f"{published}^").strip(), target)
        title = self.fx.task_row(key)["title"]
        self.assertEqual(git(self.origin, "log", "-1", "--format=%s", branch).strip(), f"{key}: {title}")


class CommittedPolicyTests(unittest.TestCase):
    """The committed agent-taskflow policy (local-path-gap §4, OR-10 Q3)."""

    def test_the_committed_policy_loads_and_resolves(self) -> None:
        path = REPO_ROOT / "config" / "projects.yaml"
        policy = execution_policy.resolve_execution_policy("agent-taskflow", config_path=path)
        argv = policy.resolved_argv()
        self.assertEqual((policy.executor, policy.model, policy.effort), (
            "claude-code", "claude-opus-5-5", "medium",
        ))
        # A version-pinned, root-owned binary, not the self-updating ~/.local/bin/claude.
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertRegex(argv[0], r"^/opt/claude-code/\d+\.\d+\.\d+/claude$")
        self.assertEqual(argv[1:6], ("--print", "--model", "claude-opus-5-5", "--effort", "medium"))

        def value(flag: str) -> str:
            return argv[argv.index(flag) + 1]

        self.assertEqual(value("--permission-mode"), "acceptEdits")
        self.assertEqual(value("--permission-prompts"), "none")
        self.assertNotIn("Bash", value("--tools").split(","))
        self.assertEqual(value("--setting-sources"), "project")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(policy.implementation_validator_names, ("pytest",))
        (spec,) = policy.integration_validator_specs()
        self.assertTrue(Path(spec.command[0]).is_absolute())
        self.assertEqual(spec.command[1:], ("-m", "pytest", "-q"))


if __name__ == "__main__":
    unittest.main()
