"""Tests for the per-repo integration queue and lock (spec §22, §22.1, §23.1)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_taskflow import integration_schema as schema
from agent_taskflow.integration_queue import (
    IntegrationLock,
    IntegrationLockUnavailable,
    enqueue_for_integration,
    next_for_repo,
    queue_for_repo,
    remove_from_queue,
)
from agent_taskflow.integration_store import IntegrationStore
from agent_taskflow.models import TaskRecord
from agent_taskflow.store import TaskMirrorStore


class QueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.store = TaskMirrorStore(self.db_path)
        self.store.init_db()
        self.integration = IntegrationStore(self.db_path)
        self.integration.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _task(self, key: str, *, status: str = schema.READY_FOR_INTEGRATION) -> None:
        self.store.upsert_task(
            TaskRecord(
                task_key=key,
                project="demo",
                status=status,
                repo_path=self.root / "repo",
            )
        )


class FifoOrderingTests(QueueTestCase):
    def test_queue_is_fifo_by_entry_timestamp(self) -> None:
        for key, at in (("AT-A", "2026-09-10T01:00:00Z"), ("AT-B", "2026-09-10T00:00:00Z"), ("AT-C", "2026-09-10T02:00:00Z")):
            self._task(key)
            enqueue_for_integration(self.integration, key, repo="owner/forms", enqueued_at=at)
        self.assertEqual(
            [entry.task_key for entry in queue_for_repo(self.integration, "owner/forms")],
            ["AT-B", "AT-A", "AT-C"],
        )

    def test_priority_never_affects_integration_order(self) -> None:
        """§22.1 — priority orders execution, never integration."""
        self._task("AT-LOW")
        self._task("AT-CRITICAL")
        enqueue_for_integration(
            self.integration, "AT-LOW", repo="owner/forms", enqueued_at="2026-09-10T00:00:00Z", priority="Low"
        )
        enqueue_for_integration(
            self.integration,
            "AT-CRITICAL",
            repo="owner/forms",
            enqueued_at="2026-09-10T00:00:01Z",
            priority="Critical",
        )
        self.assertEqual(
            [entry.task_key for entry in queue_for_repo(self.integration, "owner/forms")],
            ["AT-LOW", "AT-CRITICAL"],
        )

    def test_identical_timestamps_fall_back_to_insertion_order(self) -> None:
        for key in ("AT-1", "AT-2", "AT-3"):
            self._task(key)
            enqueue_for_integration(
                self.integration, key, repo="owner/forms", enqueued_at="2026-09-10T00:00:00Z"
            )
        self.assertEqual(
            [entry.task_key for entry in queue_for_repo(self.integration, "owner/forms")],
            ["AT-1", "AT-2", "AT-3"],
        )

    def test_queues_are_isolated_per_repo(self) -> None:
        self._task("AT-FORMS")
        self._task("AT-JOURNAL")
        enqueue_for_integration(self.integration, "AT-FORMS", repo="owner/forms")
        enqueue_for_integration(self.integration, "AT-JOURNAL", repo="owner/journal")
        self.assertEqual([e.task_key for e in queue_for_repo(self.integration, "owner/forms")], ["AT-FORMS"])
        self.assertEqual([e.task_key for e in queue_for_repo(self.integration, "owner/journal")], ["AT-JOURNAL"])

    def test_enqueue_is_idempotent_and_keeps_the_original_timestamp(self) -> None:
        self._task("AT-A")
        enqueue_for_integration(self.integration, "AT-A", repo="owner/forms", enqueued_at="2026-09-10T00:00:00Z")
        enqueue_for_integration(self.integration, "AT-A", repo="owner/forms", enqueued_at="2026-09-10T09:00:00Z")
        entries = queue_for_repo(self.integration, "owner/forms")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].enqueued_at, "2026-09-10T00:00:00Z")

    def test_next_and_remove(self) -> None:
        self._task("AT-A")
        self._task("AT-B")
        enqueue_for_integration(self.integration, "AT-A", repo="owner/forms", enqueued_at="2026-09-10T00:00:00Z")
        enqueue_for_integration(self.integration, "AT-B", repo="owner/forms", enqueued_at="2026-09-10T00:00:01Z")
        self.assertEqual(next_for_repo(self.integration, "owner/forms").task_key, "AT-A")
        remove_from_queue(self.integration, "AT-A")
        self.assertEqual(next_for_repo(self.integration, "owner/forms").task_key, "AT-B")
        remove_from_queue(self.integration, "AT-B")
        self.assertIsNone(next_for_repo(self.integration, "owner/forms"))


class IntegrationLockTests(QueueTestCase):
    def test_same_repo_lock_is_exclusive(self) -> None:
        with IntegrationLock(self.integration, "owner/forms", owner="runtime-a"):
            with self.assertRaises(IntegrationLockUnavailable):
                with IntegrationLock(self.integration, "owner/forms", owner="runtime-b"):
                    self.fail("second holder must not acquire the same repo lock")

    def test_different_repos_integrate_concurrently(self) -> None:
        with IntegrationLock(self.integration, "owner/forms", owner="runtime-a"):
            with IntegrationLock(self.integration, "owner/journal", owner="runtime-b"):
                self.assertIsNotNone(self.integration.get_integration_lock("owner/forms"))
                self.assertIsNotNone(self.integration.get_integration_lock("owner/journal"))

    def test_lock_is_released_on_exit(self) -> None:
        with IntegrationLock(self.integration, "owner/forms", owner="runtime-a"):
            pass
        self.assertIsNone(self.integration.get_integration_lock("owner/forms"))

    def test_lock_is_released_when_the_body_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with IntegrationLock(self.integration, "owner/forms", owner="runtime-a"):
                raise RuntimeError("integration blew up")
        self.assertIsNone(self.integration.get_integration_lock("owner/forms"))

    def test_only_the_holder_can_release(self) -> None:
        self.integration.acquire_integration_lock("owner/forms", owner="runtime-a")
        self.integration.release_integration_lock("owner/forms", owner="runtime-b")
        self.assertIsNotNone(self.integration.get_integration_lock("owner/forms"))
        self.integration.release_integration_lock("owner/forms", owner="runtime-a")
        self.assertIsNone(self.integration.get_integration_lock("owner/forms"))


if __name__ == "__main__":
    unittest.main()
