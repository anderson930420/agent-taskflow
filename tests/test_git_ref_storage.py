"""Read-only branch lookup in Git ref storage (PR #195 ruling 4b).

The fixtures build real repositories with `git` in setUp. The lookups under
test then run with every process-spawning entry point patched to raise: the
lookup must answer from the ref files alone and write nothing.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_taskflow.git_ref_storage import (
    GitRefStorageError,
    find_existing_branch_refs,
    resolve_git_common_dir,
)


BRANCH = "task/AT-0001-separate-the-ending-page-image"


def forbidden(*args: object, **kwargs: object):
    raise AssertionError(f"ref lookup must not spawn a process: {args}")


def no_subprocess():
    return mock.patch.multiple(subprocess, Popen=forbidden, run=forbidden), mock.patch.object(
        os, "system", forbidden
    )


class GitRefStorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.env = {
            **os.environ,
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)],
            check=True,
            env=self.env,
        )
        self.git("commit", "-q", "--allow-empty", "-m", "init")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args],
            check=True,
            capture_output=True,
            env=self.env,
        )

    def lookup(self, repo: Path, branch: str = BRANCH) -> tuple[str, ...]:
        patch_subprocess, patch_system = no_subprocess()
        with patch_subprocess, patch_system:
            return find_existing_branch_refs(repo, branch)


class NoRepositoryTests(GitRefStorageTestCase):
    def test_plain_directory_has_no_branches(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(self.lookup(plain), ())

    def test_missing_path_has_no_branches(self) -> None:
        self.assertEqual(self.lookup(self.root / "absent"), ())
        self.assertFalse((self.root / "absent").exists())

    def test_absent_branch_is_not_found(self) -> None:
        self.assertEqual(self.lookup(self.repo), ())


class LocalBranchTests(GitRefStorageTestCase):
    def test_loose_local_branch_is_found(self) -> None:
        self.git("branch", BRANCH)
        self.assertEqual(self.lookup(self.repo), (f"refs/heads/{BRANCH}",))

    def test_packed_local_branch_is_found(self) -> None:
        self.git("branch", BRANCH)
        self.git("pack-refs", "--all")
        self.assertFalse((self.repo / ".git" / "refs" / "heads" / BRANCH).exists())
        self.assertEqual(self.lookup(self.repo), (f"refs/heads/{BRANCH}",))

    def test_default_branch_is_found(self) -> None:
        self.assertEqual(self.lookup(self.repo, "main"), ("refs/heads/main",))

    def test_similar_names_do_not_match(self) -> None:
        self.git("branch", BRANCH + "-longer")
        self.git("branch", "task/AT-0001")
        self.assertEqual(self.lookup(self.repo), ())


class RemoteTrackingBranchTests(GitRefStorageTestCase):
    def test_loose_remote_tracking_branch_is_found(self) -> None:
        self.git("update-ref", f"refs/remotes/origin/{BRANCH}", "HEAD")
        self.assertEqual(self.lookup(self.repo), (f"refs/remotes/origin/{BRANCH}",))

    def test_packed_remote_tracking_branch_is_found(self) -> None:
        self.git("update-ref", f"refs/remotes/upstream/{BRANCH}", "HEAD")
        self.git("pack-refs", "--all")
        self.assertEqual(self.lookup(self.repo), (f"refs/remotes/upstream/{BRANCH}",))

    def test_local_and_remote_are_both_reported(self) -> None:
        self.git("branch", BRANCH)
        self.git("update-ref", f"refs/remotes/origin/{BRANCH}", "HEAD")
        self.assertEqual(
            self.lookup(self.repo),
            (f"refs/heads/{BRANCH}", f"refs/remotes/origin/{BRANCH}"),
        )


class LinkedWorktreeTests(GitRefStorageTestCase):
    def test_linked_worktree_reads_the_common_ref_store(self) -> None:
        worktree = self.root / "wt"
        self.git("worktree", "add", "-q", "-b", BRANCH, str(worktree))
        self.assertTrue((worktree / ".git").is_file())
        self.assertEqual(
            resolve_git_common_dir(worktree),
            Path(os.path.normpath(self.repo / ".git")),
        )
        self.assertEqual(self.lookup(worktree), (f"refs/heads/{BRANCH}",))
        self.assertEqual(self.lookup(worktree, "main"), ("refs/heads/main",))


class FailClosedTests(GitRefStorageTestCase):
    def test_reftable_config_fails_closed(self) -> None:
        fake = self.root / "reftable-repo"
        (fake / ".git").mkdir(parents=True)
        (fake / ".git" / "config").write_text(
            "[extensions]\n\trefStorage = reftable\n",
            encoding="utf-8",
        )
        with self.assertRaises(GitRefStorageError):
            self.lookup(fake)

    def test_reftable_directory_fails_closed(self) -> None:
        fake = self.root / "reftable-dir"
        (fake / ".git" / "reftable").mkdir(parents=True)
        with self.assertRaises(GitRefStorageError):
            self.lookup(fake)

    def test_malformed_git_file_fails_closed(self) -> None:
        fake = self.root / "broken"
        fake.mkdir()
        (fake / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        with self.assertRaises(GitRefStorageError):
            self.lookup(fake)

    def test_unsafe_branch_name_is_rejected(self) -> None:
        for branch in ("../escape", "task//x", "", "task/./x"):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    find_existing_branch_refs(self.repo, branch)


class ReadOnlyTests(GitRefStorageTestCase):
    def snapshot(self) -> dict[str, tuple[bool, int, int]]:
        result: dict[str, tuple[bool, int, int]] = {}
        for path in sorted(self.repo.rglob("*")):
            stat = path.stat()
            result[str(path)] = (path.is_dir(), stat.st_mtime_ns, stat.st_size)
        return result

    def test_lookup_spawns_no_process_and_writes_nothing(self) -> None:
        self.git("branch", BRANCH)
        self.git("update-ref", f"refs/remotes/origin/{BRANCH}", "HEAD")
        self.git("pack-refs", "--all")
        self.git("branch", "loose-after-pack")
        before = self.snapshot()

        found = self.lookup(self.repo)
        missing = self.lookup(self.repo, "task/AT-9999-nothing")

        self.assertEqual(len(found), 2)
        self.assertEqual(missing, ())
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
