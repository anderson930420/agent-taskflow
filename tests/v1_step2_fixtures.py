"""Shared fixtures for the V1 Step 2 Integration Controller tests.

Not collected as a test module (the name does not match ``test*.py``).

The Step 2 tests use *real* throwaway git repositories rather than a faked
git runner: rebase, merge, ``behind_count`` and ancestry containment are the
behaviours under test, and a fake runner would only prove that the fake
agrees with itself. GitHub is faked, because it is a remote service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import subprocess
from pathlib import Path
from typing import Any


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Step2 Test",
    "GIT_AUTHOR_EMAIL": "step2@example.invalid",
    "GIT_COMMITTER_NAME": "Step2 Test",
    "GIT_COMMITTER_EMAIL": "step2@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd: Path, *args: str, extra_env: dict[str, str] | None = None) -> str:
    """Run a git command in ``cwd`` and return stdout, raising on failure."""
    import os

    env = dict(os.environ)
    env.update(GIT_ENV)
    env.update(extra_env or {})
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}: {completed.stderr.strip()}"
        )
    return completed.stdout


@dataclass
class GitFixture:
    """An origin bare repo, a clone, and one task worktree on a task branch."""

    root: Path
    target_branch: str = "main"
    origin: Path = field(init=False)
    repo: Path = field(init=False)

    def __post_init__(self) -> None:
        self.origin = self.root / "origin.git"
        self.repo = self.root / "repo"
        self.origin.mkdir(parents=True, exist_ok=True)
        git(self.origin.parent, "init", "--bare", "--initial-branch", self.target_branch, str(self.origin))
        seed = self.root / "seed"
        seed.mkdir()
        git(seed, "init", "--initial-branch", self.target_branch)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        (seed / "shared.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "seed")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "origin", self.target_branch)
        git(self.root, "clone", str(self.origin), str(self.repo))

    def create_task_worktree(self, task_key: str, *, branch: str | None = None) -> Path:
        """Create ``<repo>/.worktrees/<task_key>`` on a fresh task branch."""
        branch_name = branch or f"task/{task_key}"
        worktree_path = self.repo / ".worktrees" / task_key
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        git(
            self.repo,
            "worktree",
            "add",
            str(worktree_path),
            "-b",
            branch_name,
            f"origin/{self.target_branch}",
        )
        return worktree_path

    def commit_in(self, worktree: Path, relative_path: str, contents: str, message: str) -> str:
        target = worktree / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        git(worktree, "add", "-A")
        git(worktree, "commit", "-m", message)
        return git(worktree, "rev-parse", "HEAD").strip()

    def advance_target(self, relative_path: str = "advanced.txt", contents: str = "advanced\n") -> str:
        """Push a new commit onto the target branch of origin."""
        staging = self.root / f"staging-{relative_path.replace('/', '-')}"
        if staging.exists():
            import shutil

            shutil.rmtree(staging)
        git(self.root, "clone", str(self.origin), str(staging))
        (staging / relative_path).write_text(contents, encoding="utf-8")
        git(staging, "add", "-A")
        git(staging, "commit", "-m", f"advance target: {relative_path}")
        git(staging, "push", "origin", self.target_branch)
        return git(staging, "rev-parse", "HEAD").strip()

    def target_sha(self) -> str:
        return git(self.origin, "rev-parse", self.target_branch).strip()

    def merge_branch_into_target(self, branch: str, *, method: str = "merge") -> str:
        """Simulate a human GitHub merge, returning the resulting merge SHA.

        Supports the three GitHub merge methods of §36.1. ``squash`` and
        ``rebase`` deliberately produce commit SHAs that do not exist on the
        task branch.
        """
        import shutil

        staging = self.root / f"merge-{branch.replace('/', '-')}-{method}"
        if staging.exists():
            shutil.rmtree(staging)
        git(self.root, "clone", str(self.origin), str(staging))
        git(staging, "fetch", "origin", f"{branch}:{branch}")
        if method == "merge":
            git(staging, "merge", "--no-ff", "--no-edit", branch)
        elif method == "squash":
            git(staging, "merge", "--squash", branch)
            git(staging, "commit", "-m", f"squash merge {branch}")
        elif method == "rebase":
            # GitHub's rebase merge replays the commits with a fresh committer,
            # so the resulting SHAs never match the original task commits. The
            # committer-date override reproduces that faithfully.
            git(
                staging,
                "cherry-pick",
                f"{self.target_branch}..{branch}",
                extra_env={"GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00"},
            )
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown merge method: {method}")
        git(staging, "push", "origin", self.target_branch)
        return git(staging, "rev-parse", "HEAD").strip()


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class FakeGhRunner:
    """A scripted ``gh`` runner that records every argv it is handed.

    It refuses to implement ``gh pr merge`` at all: if production code ever
    reaches for it the call surfaces as an explicit failure rather than a
    silently successful merge.
    """

    def __init__(self, *, repo: str = "owner/repo", start_number: int = 41) -> None:
        self.repo = repo
        self.calls: list[list[str]] = []
        self._next_number = start_number
        self.pulls: dict[int, dict[str, Any]] = {}
        self.create_returncode = 0
        self.create_stderr = ""

    # -- scripting helpers -------------------------------------------------
    def set_pr(self, number: int, **fields: Any) -> dict[str, Any]:
        payload = self.pulls.setdefault(number, self._blank_pr(number))
        payload.update(fields)
        return payload

    def _blank_pr(self, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "url": f"https://github.com/{self.repo}/pull/{number}",
            "state": "OPEN",
            "isDraft": True,
            "merged": False,
            "mergedAt": None,
            "mergeCommit": None,
            "headRefName": "",
            "baseRefName": "",
            "headRefOid": "",
            "reviewDecision": "",
            "statusCheckRollup": [],
            "reviews": [],
            "title": "",
            "body": "",
        }

    # -- runner protocol ---------------------------------------------------
    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append(list(args))
        if args[:3] == ["gh", "pr", "create"]:
            return self._create(args)
        if args[:3] == ["gh", "pr", "edit"]:
            return self._edit(args)
        if args[:3] == ["gh", "pr", "view"]:
            return self._view(args)
        if args[:3] == ["gh", "pr", "merge"]:
            raise AssertionError("gh pr merge must never be invoked by Taskflow")
        return FakeCompletedProcess(returncode=97, stderr=f"unexpected gh call: {args}")

    def _flag(self, args: list[str], flag: str) -> str | None:
        if flag in args:
            return args[args.index(flag) + 1]
        return None

    def _create(self, args: list[str]) -> FakeCompletedProcess:
        if self.create_returncode != 0:
            return FakeCompletedProcess(
                returncode=self.create_returncode, stderr=self.create_stderr
            )
        self._next_number += 1
        number = self._next_number
        payload = self._blank_pr(number)
        payload.update(
            {
                "headRefName": self._flag(args, "--head") or "",
                "baseRefName": self._flag(args, "--base") or "",
                "title": self._flag(args, "--title") or "",
                "body": self._flag(args, "--body") or "",
                "isDraft": "--draft" in args,
            }
        )
        self.pulls[number] = payload
        return FakeCompletedProcess(returncode=0, stdout=f"{payload['url']}\n")

    def _edit(self, args: list[str]) -> FakeCompletedProcess:
        number = int(args[3])
        payload = self.pulls[number]
        body = self._flag(args, "--body")
        if body is not None:
            payload["body"] = body
        title = self._flag(args, "--title")
        if title is not None:
            payload["title"] = title
        return FakeCompletedProcess(returncode=0, stdout=f"{payload['url']}\n")

    def _view(self, args: list[str]) -> FakeCompletedProcess:
        number = int(args[3])
        payload = self.pulls.get(number)
        if payload is None:
            return FakeCompletedProcess(returncode=1, stderr="no such PR")
        requested = self._flag(args, "--json")
        fields = requested.split(",") if requested else list(payload)
        return FakeCompletedProcess(
            returncode=0,
            stdout=json.dumps({key: payload.get(key) for key in fields}),
        )
