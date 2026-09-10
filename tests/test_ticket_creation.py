"""Step 1 acceptance gate for Ticket creation.

Covers SPEC §43 items 1-4 and the §44 invariants this step can affect:

* §43.1  Create Ticket from repository / prompt / priority only.
* §43.2  Python derives every other metadata field.
* §43.3  AI title failure cannot block creation.
* §43.4  Derived worktree path and branch are deterministic and unique
         (derivation only — creation is Step 2).
* §44    One Ticket = One Worktree.
* §44    All lifecycle mutations are auditable.
* §12.1  Initial status is `ready`, or `blocked`; never `queued`.
* Negative scope: creation runs no Git command and creates no directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_taskflow.ticket_ai_metadata import (
    TicketAIMetadataRequest,
    TicketAIMetadataSuggestion,
)
from agent_taskflow.ticket_creation import (
    CREATION_SAFETY_FLAGS,
    TicketCreationError,
    TicketCreationRequest,
    create_ticket,
    ticket_to_dict,
)
from agent_taskflow.ticket_metadata import TITLE_FALLBACK_MAX_CHARS
from agent_taskflow.ticket_models import (
    METADATA_SOURCE_AI,
    METADATA_SOURCE_FALLBACK,
    RESERVED_INITIAL_TICKET_STATUS,
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
    github_repo: example/bullet-journal
    default_branch: trunk
    branch_prefix: worktree/
"""


class TicketCreationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        # `sandbox` holds everything Ticket creation must never write into.
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
        self.store = TicketStore(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, **overrides: object) -> TicketCreationRequest:
        fields: dict[str, object] = {
            "repository": "forms",
            "prompt": PROMPT,
            "priority": "normal",
        }
        fields.update(overrides)
        return TicketCreationRequest(**fields)  # type: ignore[arg-type]

    def create(self, **kwargs: object):
        request = kwargs.pop("request", None) or self.request()
        return create_ticket(
            request,  # type: ignore[arg-type]
            store=self.store,
            projects_config_path=self.config_path,
            **kwargs,  # type: ignore[arg-type]
        )

    def sandbox_dirs(self) -> set[Path]:
        return {path for path in self.sandbox.rglob("*") if path.is_dir()}


class MinimalCreationInputTests(TicketCreationTestCase):
    """SPEC §43.1 / §10: repository, prompt and priority are the whole form."""

    def test_ticket_is_creatable_from_three_inputs(self) -> None:
        result = self.create()
        ticket = result.ticket

        self.assertEqual(ticket.repository, "forms")
        self.assertEqual(ticket.prompt, PROMPT)
        self.assertEqual(ticket.priority, "normal")

    def test_request_surface_is_exactly_repository_prompt_priority(self) -> None:
        user_supplied = set(TicketCreationRequest.__dataclass_fields__)
        self.assertEqual(
            user_supplied,
            {"repository", "prompt", "priority", "blocked_by"},
        )
        for forbidden in (
            "task_key",
            "ticket_id",
            "repo_path",
            "worktree_path",
            "branch",
            "artifact_dir",
            "base_branch",
        ):
            self.assertNotIn(forbidden, user_supplied)

    def test_priority_defaults_to_normal(self) -> None:
        self.assertEqual(TicketCreationRequest("forms", PROMPT).priority, "normal")

    def test_every_spec_priority_is_accepted(self) -> None:
        for priority in ("critical", "high", "normal", "low"):
            with self.subTest(priority=priority):
                result = self.create(request=self.request(priority=priority))
                self.assertEqual(result.ticket.priority, priority)

    def test_unknown_priority_is_rejected(self) -> None:
        with self.assertRaises(TicketCreationError):
            self.request(priority="urgent")

    def test_unknown_repository_is_rejected(self) -> None:
        with self.assertRaises(TicketCreationError):
            self.create(request=self.request(repository="not-registered"))

    def test_blank_prompt_is_rejected(self) -> None:
        with self.assertRaises(TicketCreationError):
            self.request(prompt="   \n\t ")


class DerivedMetadataTests(TicketCreationTestCase):
    """SPEC §43.2 / §10.1: Python derives all remaining metadata."""

    def test_all_metadata_is_derived_by_python(self) -> None:
        ticket = self.create().ticket

        self.assertEqual(ticket.ticket_id, "AT-001")
        self.assertEqual(ticket.ticket_prefix, "AT")
        self.assertEqual(ticket.repo_path, self.repo_path)
        self.assertEqual(ticket.github_repo, "example/forms")
        self.assertEqual(ticket.base_branch, "main")
        self.assertEqual(
            ticket.worktree_path,
            self.worktrees_dir / "AT-001",
        )
        self.assertEqual(ticket.artifact_dir, self.artifacts_root / "AT-001")
        self.assertTrue(ticket.branch.startswith("task/AT-001-"))

    def test_registry_drives_base_branch_and_branch_prefix(self) -> None:
        ticket = self.create(request=self.request(repository="bullet_journal")).ticket

        self.assertEqual(ticket.ticket_id, "BJ-001")
        self.assertEqual(ticket.base_branch, "trunk")
        self.assertTrue(ticket.branch.startswith("worktree/BJ-001-"))
        # No worktrees_dir / artifacts_root configured: both fall back under
        # the repository path rather than being asked of the user.
        self.assertEqual(
            ticket.worktree_path,
            self.other_repo_path / ".worktrees" / "BJ-001",
        )
        self.assertTrue(
            ticket.artifact_dir.is_relative_to(self.other_repo_path)
        )

    def test_task_ids_are_allocated_per_repository_prefix(self) -> None:
        first = self.create().ticket
        second = self.create().ticket
        other = self.create(request=self.request(repository="bullet_journal")).ticket

        self.assertEqual(first.ticket_id, "AT-001")
        self.assertEqual(second.ticket_id, "AT-002")
        self.assertEqual(other.ticket_id, "BJ-001")

    def test_ticket_serializes_without_leaking_path_objects(self) -> None:
        payload = ticket_to_dict(self.create().ticket)
        json.dumps(payload)
        self.assertEqual(payload["ticket_id"], "AT-001")
        self.assertEqual(payload["worktree_path"], str(self.worktrees_dir / "AT-001"))


class AiTitleFallbackTests(TicketCreationTestCase):
    """SPEC §43.3 / §10.1: AI metadata failure cannot block creation."""

    def expect_fallback(self, adapter, **kwargs: object) -> None:
        result = self.create(ai_adapter=adapter, **kwargs)
        ticket = result.ticket

        self.assertEqual(ticket.title, PROMPT[:TITLE_FALLBACK_MAX_CHARS])
        self.assertEqual(ticket.title_source, METADATA_SOURCE_FALLBACK)
        self.assertEqual(ticket.branch_slug_source, METADATA_SOURCE_FALLBACK)
        self.assertFalse(result.used_ai_title)
        self.assertIsNotNone(self.store.get_ticket(ticket.ticket_id))

    def test_adapter_raising_falls_back(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            raise RuntimeError("model unavailable")

        self.expect_fallback(adapter)

    def test_adapter_timing_out_falls_back(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            time.sleep(5)
            raise AssertionError("should never be awaited")

        started = time.monotonic()
        self.expect_fallback(adapter, ai_timeout_seconds=0.05)
        self.assertLess(time.monotonic() - started, 4)

    def test_adapter_raising_timeout_error_falls_back(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            raise TimeoutError("deadline exceeded")

        self.expect_fallback(adapter)

    def test_adapter_returning_empty_title_falls_back(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            return TicketAIMetadataSuggestion(title="")

        self.expect_fallback(adapter)

    def test_adapter_returning_whitespace_title_falls_back(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            return TicketAIMetadataSuggestion(title="   \n\t  ")

        self.expect_fallback(adapter)

    def test_adapter_returning_nothing_falls_back(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            return None

        self.expect_fallback(adapter)

    def test_ai_failure_is_recorded_on_the_audit_event(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            raise RuntimeError("model unavailable")

        ticket = self.create(ai_adapter=adapter).ticket
        payload = json.loads(
            self.store.list_ticket_events(ticket.ticket_id)[0].payload_json or "{}"
        )
        self.assertTrue(payload["ai_attempted"])
        self.assertIn("model unavailable", payload["ai_error"])
        self.assertEqual(payload["title_source"], METADATA_SOURCE_FALLBACK)

    def test_successful_adapter_metadata_is_used(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            self.assertEqual(request.prompt, PROMPT)
            self.assertEqual(request.repository, "forms")
            return TicketAIMetadataSuggestion(
                title="Separate ending-page image",
                branch_slug="Separate Ending Image",
                commit_message="feat: separate ending-page image",
            )

        result = self.create(ai_adapter=adapter)
        ticket = result.ticket

        self.assertEqual(ticket.title, "Separate ending-page image")
        self.assertEqual(ticket.title_source, METADATA_SOURCE_AI)
        self.assertEqual(ticket.branch, "task/AT-001-separate-ending-image")
        self.assertEqual(ticket.branch_slug_source, METADATA_SOURCE_AI)
        self.assertEqual(
            ticket.commit_message_suggestion,
            "feat: separate ending-page image",
        )
        self.assertTrue(result.used_ai_title)

    def test_no_adapter_means_no_ai_attempt(self) -> None:
        result = self.create()
        self.assertFalse(result.metadata.ai_attempted)
        self.assertIsNone(result.metadata.ai_error)
        self.assertEqual(result.ticket.title, PROMPT[:TITLE_FALLBACK_MAX_CHARS])


class OneTicketOneWorktreeTests(TicketCreationTestCase):
    """SPEC §43.4 and §44: derived identity is unique per Ticket."""

    def test_identical_prompts_derive_distinct_worktrees_and_branches(self) -> None:
        first = self.create().ticket
        second = self.create().ticket

        self.assertNotEqual(first.ticket_id, second.ticket_id)
        self.assertNotEqual(first.worktree_path, second.worktree_path)
        self.assertNotEqual(first.branch, second.branch)

    def test_identical_ai_titles_derive_distinct_worktrees_and_branches(self) -> None:
        def adapter(request: TicketAIMetadataRequest):
            return TicketAIMetadataSuggestion(
                title="Exactly the same title",
                branch_slug="exactly-the-same-slug",
            )

        first = self.create(ai_adapter=adapter).ticket
        second = self.create(ai_adapter=adapter).ticket

        self.assertEqual(first.title, second.title)
        self.assertNotEqual(first.branch, second.branch)
        self.assertNotEqual(first.worktree_path, second.worktree_path)
        self.assertIn(first.ticket_id, first.branch)
        self.assertIn(second.ticket_id, second.branch)

    def test_worktree_path_ends_with_the_task_id(self) -> None:
        ticket = self.create().ticket
        self.assertEqual(ticket.worktree_path.name, ticket.ticket_id)
        self.assertEqual(ticket.worktree_path.parent, self.worktrees_dir)

    def test_derived_identity_is_stable_across_readback(self) -> None:
        ticket = self.create().ticket
        stored = self.store.get_ticket(ticket.ticket_id)
        assert stored is not None
        self.assertEqual(stored.branch, ticket.branch)
        self.assertEqual(stored.worktree_path, ticket.worktree_path)
        self.assertEqual(stored.artifact_dir, ticket.artifact_dir)


class InitialStatusTests(TicketCreationTestCase):
    """SPEC §12.1: `ready`, or `blocked`; V1 never writes `queued`."""

    def test_new_ticket_is_ready(self) -> None:
        self.assertEqual(self.create().ticket.status, "ready")

    def test_ticket_created_with_blocked_by_is_blocked(self) -> None:
        blocker = self.create().ticket
        dependent = self.create(
            request=self.request(blocked_by=blocker.ticket_id)
        ).ticket

        self.assertEqual(dependent.status, "blocked")
        self.assertEqual(dependent.blocked_by, blocker.ticket_id)

    def test_unknown_blocker_is_rejected(self) -> None:
        with self.assertRaises(TicketCreationError):
            self.create(request=self.request(blocked_by="AT-404"))

    def test_creation_never_writes_queued(self) -> None:
        statuses = {
            self.create().ticket.status,
            self.create(request=self.request(priority="critical")).ticket.status,
        }
        self.assertNotIn(RESERVED_INITIAL_TICKET_STATUS, statuses)


class AuditabilityTests(TicketCreationTestCase):
    """SPEC §44: all lifecycle mutations are auditable."""

    def test_creation_writes_an_audit_event(self) -> None:
        ticket = self.create().ticket
        events = self.store.list_ticket_events(ticket.ticket_id)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_type, "ticket_created")
        self.assertEqual(event.ticket_id, ticket.ticket_id)
        self.assertTrue(event.created_at)

        payload = json.loads(event.payload_json or "{}")
        self.assertEqual(payload["repository"], "forms")
        self.assertEqual(payload["priority"], "normal")
        self.assertEqual(payload["initial_status"], "ready")

    def test_audit_event_records_that_creation_touched_no_git(self) -> None:
        ticket = self.create().ticket
        payload = json.loads(
            self.store.list_ticket_events(ticket.ticket_id)[0].payload_json or "{}"
        )
        self.assertEqual(payload["safety_flags"], dict(CREATION_SAFETY_FLAGS))
        self.assertFalse(any(payload["safety_flags"].values()))


class NegativeScopeTests(TicketCreationTestCase):
    """Step 1 derives strings. It must not run Git or touch the filesystem."""

    def test_creation_runs_no_subprocess(self) -> None:
        def forbidden(*args: object, **kwargs: object):
            raise AssertionError(f"Ticket creation must not spawn a process: {args}")

        with mock.patch.object(subprocess, "Popen", forbidden), mock.patch.object(
            subprocess, "run", forbidden
        ), mock.patch.object(os, "system", forbidden):
            ticket = self.create().ticket

        self.assertEqual(ticket.ticket_id, "AT-001")

    def test_creation_creates_no_directory(self) -> None:
        before = self.sandbox_dirs()
        ticket = self.create().ticket
        after = self.sandbox_dirs()

        self.assertEqual(before, after)
        self.assertFalse(ticket.worktree_path.exists())
        self.assertFalse(ticket.artifact_dir.exists())
        self.assertFalse(self.worktrees_dir.exists())
        self.assertFalse(self.artifacts_root.exists())

    def test_creation_writes_no_file_inside_the_repository(self) -> None:
        files_before = {path for path in self.sandbox.rglob("*") if path.is_file()}
        self.create()
        files_after = {path for path in self.sandbox.rglob("*") if path.is_file()}
        self.assertEqual(files_before, files_after)

    def test_creation_does_not_require_the_repository_to_exist(self) -> None:
        # Step 1 never inspects the repository working tree.
        missing = self.root / "sandbox" / "forms-gone"
        self.config_path.write_text(
            PROJECTS_YAML.format(
                repo_path=missing,
                artifacts_root=self.artifacts_root,
                worktrees_dir=missing / ".worktrees",
                other_repo_path=self.other_repo_path,
            ),
            encoding="utf-8",
        )
        ticket = self.create().ticket
        self.assertEqual(ticket.repo_path, missing)
        self.assertFalse(missing.exists())


class RequestNormalizationTests(TicketCreationTestCase):
    def test_prompt_whitespace_is_normalized_before_storage(self) -> None:
        request = self.request(prompt="  Fix\tthe\n\nlogin  flow  ")
        ticket = self.create(request=request).ticket
        self.assertEqual(ticket.prompt, "Fix the login flow")
        self.assertEqual(ticket.title, "Fix the login flow")

    def test_blank_blocked_by_is_treated_as_absent(self) -> None:
        ticket = self.create(request=self.request(blocked_by="   ")).ticket
        self.assertIsNone(ticket.blocked_by)
        self.assertEqual(ticket.status, "ready")

    def test_request_is_immutable(self) -> None:
        request = self.request()
        with self.assertRaises(Exception):
            request.priority = "high"  # type: ignore[misc]
        self.assertEqual(replace(request, priority="high").priority, "high")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
