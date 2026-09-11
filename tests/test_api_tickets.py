"""Mission Control Ticket API tests (SPEC §10, §11, §12.1, §12.2).

A Ticket is a row in the canonical `tasks` table (PR #195 ruling), so every
Ticket created here is also visible through the legacy `/api/tasks` routes.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.models import utc_now_iso
from agent_taskflow.store import init_db as init_task_db
from agent_taskflow.ticket_fields_schema import migrate_ticket_fields
from agent_taskflow.ticket_metadata import (
    derive_branch_name,
    fallback_title_from_prompt,
    slugify_branch_component,
)
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
        # The API fails closed until the explicit Step 1 migration has run.
        init_task_db(self.db_path)
        migrate_ticket_fields(self.db_path)

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



class BranchCollisionRouteTests(TicketApiTestCase):
    """PR #195 ruling 4b: 409 naming the existing branch; nothing written."""

    def setUp(self) -> None:
        super().setUp()
        self.branch = derive_branch_name(
            "task/",
            "AT-0001",
            slugify_branch_component(fallback_title_from_prompt(PROMPT)),
        )

    def counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
            )

    def post_forbidding_subprocess(self):
        def forbidden(*args: object, **kwargs: object):
            raise AssertionError(f"Ticket creation must not spawn a process: {args}")

        with mock.patch.object(subprocess, "Popen", forbidden), mock.patch.object(
            subprocess, "run", forbidden
        ), mock.patch.object(os, "system", forbidden):
            return self.create()

    def assert_409_naming_the_branch(self, source: str) -> dict:
        counts_before = self.counts()
        paths_before = {path for path in self.sandbox.rglob("*") if path.is_dir()}

        response = self.post_forbidding_subprocess()

        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["branch"], self.branch)
        self.assertIn(self.branch, body["detail"])
        self.assertEqual(body["conflict_source"], source)
        self.assertEqual(self.counts(), counts_before, "refusal wrote a row or event")
        self.assertEqual(
            {path for path in self.sandbox.rglob("*") if path.is_dir()},
            paths_before,
            "refusal created a directory",
        )
        self.assertEqual(self.client.get("/api/tickets/AT-0001").status_code, 404)
        return body

    def test_branch_recorded_in_tasks_is_409(self) -> None:
        now = utc_now_iso()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO tasks (task_key, project, board, title, status,"
                " repo_path, created_at, updated_at, branch)"
                " VALUES ('AT-MANUAL', 'forms', 'forms', 'Manual', 'queued', ?, ?, ?, ?)",
                (str(self.repo_path), now, now, self.branch),
            )
        body = self.assert_409_naming_the_branch("tasks")
        self.assertEqual(body["existing"], "AT-MANUAL")

    def test_existing_repository_branch_is_409(self) -> None:
        env = {
            **os.environ,
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        for args in (
            ["init", "-q", "-b", "main"],
            ["commit", "-q", "--allow-empty", "-m", "init"],
            ["branch", self.branch],
        ):
            subprocess.run(
                ["git", "-C", str(self.repo_path), *args],
                check=True,
                capture_output=True,
                env=env,
            )
        body = self.assert_409_naming_the_branch("repository")
        self.assertEqual(body["existing"], f"refs/heads/{self.branch}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
