"""Mission Control Ticket API tests (SPEC §10, §11, §12.1)."""

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
from agent_taskflow.ticket_store import TicketStore


PROMPT = "Separate the ending page image from the shared hero component"

PROJECTS_YAML = """\
projects:
  forms:
    project_slug: forms
    task_key_prefix: AT
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
        self.ticket_store = TicketStore(self.db_path)

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
        self.assertEqual(forms["ticket_prefix"], "AT")
        self.assertEqual(forms["github_repo"], "example/forms")


class CreateTicketRouteTests(TicketApiTestCase):
    """SPEC §10: repository + prompt + priority is the whole request body."""

    def test_creates_a_ticket_from_three_fields(self) -> None:
        response = self.create()
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertTrue(body["ok"])
        self.assertEqual(body["ticket_id"], "AT-001")
        self.assertEqual(body["status"], "ready")

        item = body["item"]
        self.assertEqual(item["repository"], "forms")
        self.assertEqual(item["prompt"], PROMPT)
        self.assertEqual(item["priority"], "normal")
        self.assertEqual(item["base_branch"], "main")
        self.assertEqual(item["repo_path"], str(self.repo_path))
        self.assertEqual(item["worktree_path"], str(self.worktrees_dir / "AT-001"))
        self.assertEqual(item["artifact_dir"], str(self.artifacts_root / "AT-001"))
        self.assertTrue(item["branch"].startswith("task/AT-001-"))

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
        response = self.create(priority="urgent")
        self.assertEqual(response.status_code, 422)

    def test_blank_prompt_is_rejected(self) -> None:
        response = self.create(prompt="   ")
        self.assertEqual(response.status_code, 422)

    def test_route_creates_nothing_on_disk(self) -> None:
        before = {path for path in self.sandbox.rglob("*")}
        self.create()
        self.assertEqual({path for path in self.sandbox.rglob("*")}, before)
        self.assertFalse(self.worktrees_dir.exists())

    def test_unknown_blocker_is_rejected(self) -> None:
        response = self.create(blocked_by="AT-404")
        self.assertEqual(response.status_code, 422)

    def test_known_blocker_creates_a_blocked_ticket(self) -> None:
        blocker = self.create().json()["ticket_id"]
        response = self.create(blocked_by=blocker)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "blocked")


class TicketReadbackRouteTests(TicketApiTestCase):
    """SPEC §17: the detail page reads Ticket state and its audit trail."""

    def test_ticket_detail_includes_metadata_and_events(self) -> None:
        ticket_id = self.create().json()["ticket_id"]
        response = self.client.get(f"/api/tickets/{ticket_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(body["item"]["ticket_id"], ticket_id)
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["event_type"], "ticket_created")

    def test_missing_ticket_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/tickets/AT-999").status_code, 404)

    def test_unsafe_ticket_id_is_404_not_500(self) -> None:
        self.assertEqual(self.client.get("/api/tickets/..%2Fetc").status_code, 404)

    def test_list_filters_by_repository_priority_and_status(self) -> None:
        self.create(priority="high")
        self.create(repository="bullet_journal", priority="low")

        everything = self.client.get("/api/tickets").json()
        self.assertEqual(everything["count"], 2)

        forms = self.client.get("/api/tickets?repository=forms").json()
        self.assertEqual([item["ticket_id"] for item in forms["items"]], ["AT-001"])

        low = self.client.get("/api/tickets?priority=low").json()
        self.assertEqual([item["ticket_id"] for item in low["items"]], ["BJ-001"])

        ready = self.client.get("/api/tickets?status=ready").json()
        self.assertEqual(ready["count"], 2)

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
        self.assertEqual(item["title_source"], "fallback")


class ExistingRoutesStillWorkTests(TicketApiTestCase):
    """The legacy task mirror API is untouched by the Ticket routes."""

    def test_health_and_task_routes_are_unchanged(self) -> None:
        self.assertEqual(self.client.get("/health").json()["status"], "ok")
        self.assertEqual(self.client.get("/api/tasks").json()["items"], [])

    def test_tickets_are_not_mirrored_into_the_legacy_task_list(self) -> None:
        self.create()
        self.assertEqual(self.client.get("/api/tasks").json()["count"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
