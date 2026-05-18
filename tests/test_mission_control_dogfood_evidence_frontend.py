"""Source-level tests for Mission Control dogfood evidence readback UI."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PANEL = REPO_ROOT / "mission-control" / "components" / "DogfoodEvidencePanel.tsx"
PAGE = REPO_ROOT / "mission-control" / "app" / "tasks" / "[taskKey]" / "page.tsx"
API = REPO_ROOT / "mission-control" / "lib" / "api.ts"
TYPES = REPO_ROOT / "mission-control" / "lib" / "types.ts"


class MissionControlDogfoodEvidenceFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.panel = PANEL.read_text(encoding="utf-8")
        cls.page = PAGE.read_text(encoding="utf-8")
        cls.types = TYPES.read_text(encoding="utf-8")
        cls.changed_frontend = "\n".join([cls.panel, cls.page, cls.types])
        cls.new_evidence_surface = "\n".join([cls.panel, cls.types])

    def test_task_detail_page_includes_evidence_summary_panel(self) -> None:
        self.assertIn("Evidence Summary", self.page)
        self.assertIn("DogfoodEvidencePanel", self.page)

    def test_ui_fetches_read_only_evidence_api(self) -> None:
        self.assertIn("requestJson<TaskDogfoodEvidenceBundle>", self.panel)
        self.assertIn("/evidence", self.panel)

    def test_ui_renders_required_artifact_groups(self) -> None:
        for label in ["Handoff", "Publication", "Draft PR"]:
            self.assertIn(label, self.panel)

    def test_ui_includes_read_only_safety_language(self) -> None:
        self.assertIn("Read-only evidence view", self.panel)
        self.assertIn("No push, PR creation, merge, approval, or", self.panel)
        self.assertIn("cleanup actions are available from Mission Control", self.panel)

    def test_no_new_action_button_labels_are_introduced(self) -> None:
        forbidden_labels = [
            "Push Branch",
            "Create PR",
            "Merge",
            "Approve",
            "Cleanup",
            "Delete Worktree",
            "Delete Branch",
        ]
        for label in forbidden_labels:
            with self.subTest(label=label):
                self.assertNotIn(label, self.changed_frontend)

    def test_changed_frontend_does_not_call_mutation_endpoints(self) -> None:
        forbidden = [
            "git push",
            "gh pr create",
            "gh pr merge",
            "gh pr review",
            "gh issue edit",
            "delete_branch",
            "delete_worktree",
            "worktree remove",
            "dispatcher start",
            "prepare workspace",
            "create draft pr",
            "push task branch",
            "/approve",
            "/reject",
            "/block",
            "/start",
            "/prepare-workspace",
        ]
        lowered = self.new_evidence_surface.lower()
        for needle in forbidden:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, lowered)


if __name__ == "__main__":
    unittest.main()
