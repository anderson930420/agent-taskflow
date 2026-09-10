"""Mission Control Ticket API tests (SPEC §10, §11, §12.1, §12.2).

A Ticket is a row in the canonical `tasks` table (PR #195 ruling), so every
Ticket created here is also visible through the legacy `/api/tasks` routes.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.ticket_ai_metadata import (
    TicketAIMetadataRequest,
    TicketAIMetadataSuggestion,
)


PROMPT = "Separate the ending page image from the shared hero component"

PROJECTS_YAML = """\
projects:
  forms:
    project_slug: forms
    task_key_prefix: FM
    repo_path: {repo_path}
    github_repo: example/forms
    artifacts_root: {artifacts_root}
    worktrees_dir: {worktrees_dir}
    default_branch: main
    branch_prefix: task/
  bullet_journal:
    project_slug: bullet_journal
    task_key_prefix: BJ
    repo_path: {other_repo_path}
    default_branch: trunk
    branch_prefix: worktree/
"""


class TicketApiTestCase(unittest.TestCase):
    ai_adapter = None

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sandbox = self.root / "sandbox"
        self.repo_path = self.sandbox / "forms"
        self.other_repo_path = self.sandbox / "bullet-journal"
        self.worktrees_dir = self.repo_path / ".worktrees"
        self.artifacts_root = self.sandbox / "artifacts"
        self.repo_path.mkdir(parents=True)
        self.other_repo_path.mkdir(parents=True)

        self.config_path = self.root / "projects.yaml"
        self.config_path.write_text(
            PROJECTS_YAML.format(
                repo_path=self.repo_path,
                artifacts_root=self.artifacts_root,
                worktrees_dir=self.worktrees_dir,
                other_repo_path=self.other_repo_path,
            ),
            encoding="utf-8",
        )

        db_dir = self.root / "db"
        db_dir.mkdir()
        self.db_path = db_dir / "state.db"

        self.app = create_app(
            self.db_path,
            projects_config_path=self.config_path,
            ticket_ai_adapter=self.ai_adapter,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def create(self, **overrides: object):
        payload: dict[str, object] = {
            "repository": "forms",
            "prompt": PROMPT,
            "priority": "normal",
        }
        payload.update(overrides)
        return self.client.post("/api/tickets", json=payload)


class RepositoryRegistryRouteTests(TicketApiTestCase):
    """SPEC §11: the repository dropdown reads the existing registry."""

    def test_lists_registered_repositories(self) -> None:
        response = self.client.get("/api/repositories")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(body["count"], 2)
        names = [item["repository"] for item in body["items"]]
        self.assertEqual(names, ["bullet_journal", "forms"])

        forms = body["items"][1]
        self.assertEqual(forms["repo_path"], str(self.repo_path))
        self.assertEqual(forms["base_branch"], "main")
        self.assertEqual(forms["branch_prefix"], "task/")
        self.assertEqual(forms["github_repo"], "example/forms")
        # Task keys come from one global counter, not a per-repo prefix.
        self.assertNotIn("ticket_prefix", forms)


class CreateTicketRouteTests(TicketApiTestCase):
    """SPEC §10: repository + prompt + priority is the whole request body."""

    def test_creates_a_ticket_from_three_fields(self) -> None:
        response = self.create()
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertTrue(body["ok"])
        self.assertEqual(body["task_key"], "AT-0001")
        self.assertEqual(body["status"], "created")
        self.assertEqual(body["display_status"], "ready")

        item = body["item"]
        self.assertEqual(item["repository"], "forms")
        self.assertEqual(item["prompt"], PROMPT)
        self.assertEqual(item["priority"], "normal")
        self.assertEqual(item["base_branch"], "main")
        self.assertEqual(item["repo_path"], str(self.repo_path))
        self.assertEqual(item["worktree_path"], str(self.worktrees_dir / "AT-0001"))
        self.assertEqual(item["artifact_dir"], str(self.artifacts_root / "AT-0001"))
        self.assertTrue(item["branch"].startswith("task/AT-0001-"))
        self.assertEqual(item["ai_title_status"], "not_attempted")

    def test_priority_is_optional_and_defaults_to_normal(self) -> None:
        response = self.client.post(
            "/api/tickets",
            json={"repository": "forms", "prompt": PROMPT},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["priority"], "normal")

    def test_unknown_repository_is_rejected(self) -> None:
        response = self.create(repository="nope")
        self.assertEqual(response.status_code, 422)
        self.assertIn("nope", response.json()["detail"])

    def test_unknown_priority_is_rejected(self) -> None:
        self.assertEqual(self.create(priority="urgent").status_code, 422)

    def test_blank_prompt_is_rejected(self) -> None:
        self.assertEqual(self.create(prompt="   ").status_code, 422)

    def test_route_creates_nothing_on_disk(self) -> None:
        before = {path for path in self.sandbox.rglob("*")}
        self.create()
        self.assertEqual({path for path in self.sandbox.rglob("*")}, before)
        self.assertFalse(self.worktrees_dir.exists())

    def test_unknown_blocker_is_rejected(self) -> None:
        self.assertEqual(self.create(blocked_by="AT-0404").status_code, 422)

    def test_known_blocker_creates_a_blocked_ticket(self) -> None:
        blocker = self.create().json()["task_key"]
        response = self.create(blocked_by=blocker)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "blocked")
        self.assertEqual(response.json()["display_status"], "blocked")


class TicketReadbackRouteTests(TicketApiTestCase):
    """SPEC §17: the detail page reads Ticket state and its audit trail."""

    def test_ticket_detail_includes_metadata_and_events(self) -> None:
        task_key = self.create().json()["task_key"]
        response = self.client.get(f"/api/tickets/{task_key}")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(body["item"]["task_key"], task_key)
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["event_type"], "created")
        self.assertEqual(body["events"][0]["source"], "mission_control")

    def test_missing_ticket_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/tickets/AT-9999").status_code, 404)

    def test_unsafe_task_key_is_404_not_500(self) -> None:
        self.assertEqual(self.client.get("/api/tickets/..%2Fetc").status_code, 404)

    def test_list_filters_by_repository_priority_and_display_status(self) -> None:
        self.create(priority="high")
        self.create(repository="bullet_journal", priority="low")

        everything = self.client.get("/api/tickets").json()
        self.assertEqual(everything["count"], 2)

        forms = self.client.get("/api/tickets?repository=forms").json()
        self.assertEqual([item["task_key"] for item in forms["items"]], ["AT-0001"])

        low = self.client.get("/api/tickets?priority=low").json()
        self.assertEqual([item["task_key"] for item in low["items"]], ["AT-0002"])

        ready = self.client.get("/api/tickets?status=ready").json()
        self.assertEqual(ready["count"], 2)

        blocked = self.client.get("/api/tickets?status=blocked").json()
        self.assertEqual(blocked["count"], 0)

    def test_status_filter_takes_display_names_not_persisted_values(self) -> None:
        self.create()
        # `created` is the persisted spelling; the filter speaks §12.
        self.assertEqual(self.client.get("/api/tickets?status=created").status_code, 400)

    def test_invalid_status_filter_is_400(self) -> None:
        self.assertEqual(
            self.client.get("/api/tickets?status=not-a-status").status_code,
            400,
        )


class FailingAiAdapterRouteTests(TicketApiTestCase):
    """SPEC §43.3: AI title failure cannot block creation through the API."""

    @staticmethod
    def ai_adapter(request: TicketAIMetadataRequest) -> TicketAIMetadataSuggestion:
        raise RuntimeError("model unavailable")

    def test_creation_still_succeeds_with_the_fallback_title(self) -> None:
        response = self.create()
        self.assertEqual(response.status_code, 200)
        item = response.json()["item"]
        self.assertEqual(item["title"], PROMPT[:60])
        self.assertEqual(item["ai_title_status"], "fallback")


class TicketsAreTasksTests(TicketApiTestCase):
    """PR #195 ruling: `tasks` is the one canonical Ticket entity."""

    def test_health_is_unchanged(self) -> None:
        self.assertEqual(self.client.get("/health").json()["status"], "ok")

    def test_created_ticket_appears_in_the_task_list(self) -> None:
        self.assertEqual(self.client.get("/api/tasks").json()["items"], [])
        task_key = self.create().json()["task_key"]

        tasks = self.client.get("/api/tasks").json()
        self.assertEqual(tasks["count"], 1)
        self.assertEqual(tasks["items"][0]["task_key"], task_key)
        self.assertEqual(tasks["items"][0]["status"], "created")

    def test_created_ticket_is_readable_through_the_task_route(self) -> None:
        task_key = self.create().json()["task_key"]
        response = self.client.get(f"/api/tasks/{task_key}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["project"], "forms")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
