from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_taskflow.artifacts import artifact_dir_for
from agent_taskflow.governance import (
    assert_not_main_repo_write,
    assert_task_has_artifact_dir,
    assert_worktree_inside_repo_worktrees,
)
from agent_taskflow.projects import get_project_config, load_projects_config
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.worktree import worktree_path_for


class ProjectConfigTests(unittest.TestCase):
    def test_load_projects_config_and_get_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.yaml"
            config_path.write_text(
                """
projects:
  agent-taskflow:
    project_slug: agent-taskflow
    repo_path: /home/ubuntu/agent-taskflow
""".strip(),
                encoding="utf-8",
            )

            config = load_projects_config(config_path)
            project = get_project_config(config, "agent-taskflow")

            self.assertEqual(project["project_slug"], "agent-taskflow")
            self.assertEqual(project["repo_path"], "/home/ubuntu/agent-taskflow")

    def test_missing_project_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Project 'missing' not found"):
            get_project_config({"agent-taskflow": {}}, "missing")


class PathHelperTests(unittest.TestCase):
    def test_normalize_task_key_accepts_safe_key(self) -> None:
        self.assertEqual(normalize_task_key(" AT-0001 "), "AT-0001")

    def test_normalize_task_key_rejects_unsafe_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe characters"):
            normalize_task_key("../AT-0001")

    def test_worktree_path_for_uses_repo_worktrees_dir(self) -> None:
        self.assertEqual(
            worktree_path_for("/home/ubuntu/agent-taskflow", "AT-0001"),
            Path("/home/ubuntu/agent-taskflow/.worktrees/AT-0001"),
        )

    def test_worktree_path_for_rejects_relative_repo(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo_path must be absolute"):
            worktree_path_for("relative/repo", "AT-0001")

    def test_artifact_dir_for_uses_artifacts_root(self) -> None:
        self.assertEqual(
            artifact_dir_for("AT-0001", "/home/ubuntu/.hermes/task-artifacts"),
            Path("/home/ubuntu/.hermes/task-artifacts/AT-0001"),
        )


class GovernanceTests(unittest.TestCase):
    def test_rejects_main_repo_as_worktree(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be the main repo path"):
            assert_not_main_repo_write(
                "/home/ubuntu/agent-taskflow",
                "/home/ubuntu/agent-taskflow",
            )

    def test_requires_worktree_inside_repo_worktrees(self) -> None:
        assert_worktree_inside_repo_worktrees(
            "/home/ubuntu/agent-taskflow/.worktrees/AT-0001",
            "/home/ubuntu/agent-taskflow",
        )

        with self.assertRaisesRegex(ValueError, "must be inside"):
            assert_worktree_inside_repo_worktrees(
                "/home/ubuntu/other/AT-0001",
                "/home/ubuntu/agent-taskflow",
            )

    def test_assert_task_has_artifact_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert_task_has_artifact_dir(tmp)

        with self.assertRaisesRegex(ValueError, "Artifact directory does not exist"):
            assert_task_has_artifact_dir("/tmp/agent-taskflow-definitely-missing")


if __name__ == "__main__":
    unittest.main()
