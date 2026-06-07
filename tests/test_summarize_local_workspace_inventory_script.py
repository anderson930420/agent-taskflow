from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "summarize_local_workspace_inventory.py"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


class SummarizeLocalWorkspaceInventoryScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        # Root under /tmp so the added worktree exercises the inside_tmp path.
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "main-repo"
        self.repo.mkdir()

        _git(self.repo, "init", "-b", "main")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "initial commit")

        self.feature_worktree = self.root / "wt-feature"
        _git(
            self.repo,
            "worktree",
            "add",
            str(self.feature_worktree),
            "-b",
            "feature",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo-root",
                str(self.repo),
                "--runtime-worktree",
                str(self.feature_worktree),
                "--manual-review-worktree",
                str(self.repo),
                *args,
            ],
            cwd=str(REPO_ROOT),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def test_help_runs(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=str(REPO_ROOT),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--status-limit", result.stdout)
        self.assertIn("--runtime-worktree", result.stdout)

    def test_json_emits_valid_json(self) -> None:
        result = self._run("--json")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        for key in (
            "ok",
            "schema_version",
            "source",
            "repo_root",
            "runtime_worktrees",
            "manual_review_worktrees",
            "worktrees",
            "summary",
            "safety",
        ):
            self.assertIn(key, payload)

        paths = {worktree["path"] for worktree in payload["worktrees"]}
        self.assertIn(str(self.repo), paths)
        self.assertIn(str(self.feature_worktree), paths)

        by_path = {w["path"]: w for w in payload["worktrees"]}
        self.assertEqual(by_path[str(self.feature_worktree)]["recommendation"], "keep_runtime")
        self.assertTrue(by_path[str(self.feature_worktree)]["is_runtime"])
        self.assertEqual(
            by_path[str(self.repo)]["recommendation"], "manual_review_dirty_checkout"
        )

        counts = payload["summary"]["recommendation_counts"]
        self.assertGreaterEqual(counts["keep_runtime"], 1)
        self.assertTrue(payload["safety"]["read_only"])
        self.assertFalse(payload["safety"]["db_written"])
        self.assertFalse(payload["safety"]["worktree_removed"])
        self.assertFalse(payload["safety"]["worktree_pruned"])
        self.assertFalse(payload["safety"]["git_reset_performed"])
        self.assertFalse(payload["safety"]["git_clean_performed"])

    def test_human_readable_output(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        out = result.stdout
        self.assertIn("Local Workspace Inventory", out)
        self.assertIn("Recommendation counts:", out)
        self.assertIn("keep_runtime:", out)
        # Read-only assurance is part of human output.
        self.assertIn("read-only", out.lower())
        self.assertIn("P2-b", out)

    def test_path_prefix_can_scope_out_worktrees(self) -> None:
        result = self._run("--path-prefix", "/nonexistent-prefix", "--json")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        for worktree in payload["worktrees"]:
            self.assertFalse(worktree["within_path_prefix"])
            self.assertEqual(worktree["recommendation"], "no_action")


if __name__ == "__main__":
    unittest.main()
