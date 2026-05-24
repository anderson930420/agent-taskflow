"""Tests for docs/semi-automatic-scheduler-readiness-checkpoint.md.

Phase F adds a documentation-only readiness checkpoint that describes
what the merged Phase A–E chain has proven, what "semi-automatic
scheduler" means in this repo, and what must remain operator-gated.

These tests verify that the checkpoint document exists and contains the
governance statements required by the Phase F acceptance criteria.
They do not execute any scheduler, runtime, or smoke run.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_DOC = (
    REPO_ROOT / "docs" / "semi-automatic-scheduler-readiness-checkpoint.md"
)


class SemiAutomaticSchedulerReadinessCheckpointTests(unittest.TestCase):
    """Verify the Phase F readiness checkpoint contains required statements."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = CHECKPOINT_DOC.read_text(encoding="utf-8")

    def test_doc_exists(self) -> None:
        self.assertTrue(
            CHECKPOINT_DOC.is_file(),
            f"Phase F checkpoint doc must exist at {CHECKPOINT_DOC}",
        )

    def test_defines_semi_automatic_scheduler(self) -> None:
        self.assertIn("Semi-automatic scheduler", self.doc)

    def test_states_ready_for_levels_1_through_4(self) -> None:
        self.assertIn("ready for Level 1–4", self.doc)

    def test_states_not_yet_ready_for_level_5(self) -> None:
        self.assertIn("NOT yet ready for Level 5", self.doc)

    def test_states_no_self_approval(self) -> None:
        self.assertIn("no self-approval", self.doc)

    def test_states_no_auto_merge(self) -> None:
        self.assertIn("no auto-merge", self.doc)

    def test_states_runtime_audit_is_not_validation_authority(self) -> None:
        self.assertIn("runtime audit is **not** validation authority", self.doc)

    def test_states_validation_result_remains_authoritative(self) -> None:
        self.assertIn("`validation_result` remains authoritative", self.doc)

    def test_names_phase_g_read_only_scheduler_candidate_discovery(self) -> None:
        self.assertIn(
            "Phase G — Read-only scheduler candidate discovery", self.doc
        )

    def test_states_no_scheduler_loop(self) -> None:
        self.assertIn("no scheduler loop", self.doc)

    def test_states_no_background_worker(self) -> None:
        self.assertIn("no background worker", self.doc)

    def test_states_no_automatic_task_picking(self) -> None:
        self.assertIn("no automatic task picking", self.doc)

    def test_states_no_github_mutation(self) -> None:
        self.assertIn("no GitHub mutation", self.doc)

    def test_states_human_review_remains_final(self) -> None:
        self.assertIn("human review remains final", self.doc)


if __name__ == "__main__":
    unittest.main()
