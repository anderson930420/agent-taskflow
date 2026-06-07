"""Tests for the ExecutionEngine architecture inventory documentation (P4-a).

The doc is documentation-only and behavior-preserving. These tests assert that
the doc exists and pins the current real scheduled execution path, the safety
boundaries that must be preserved, the proposed ExecutionEngine ownership
boundary, the explicit non-ownership list, the P4-a through P4-f roadmap, and
the documentation-only / behavior-preserving scope. P4-a adds no runtime code,
so the doc must read as an inventory and boundary contract, not as an executable
migration.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "execution-engine-architecture-inventory.md"


class ExecutionEngineArchitectureInventoryDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = DOC.read_text(encoding="utf-8")
        cls.doc_lower = cls.doc.lower()

    def test_doc_exists(self) -> None:
        self.assertTrue(DOC.is_file())

    def test_mentions_key_modules(self) -> None:
        for module in (
            "run_github_issue_one_task_scheduler_tick.py",
            "github_issue_one_task_scheduler_tick.py",
            "github_issue_one_task_automation.py",
            "approved_task_runner.py",
            "executor adapter",
            "validator layer",
            "runtime handoff execution",
            "intake runner handoff",
        ):
            self.assertIn(
                module, self.doc_lower, f"doc missing key module {module!r}"
            )

    def test_mentions_current_execution_flow_stages(self) -> None:
        for stage in (
            "proposal creation",
            "scheduler confirmation creation",
            "scheduler confirmation verifier report",
            "runtime preflight",
            "artifact recording",
            "waiting_approval",
            "blocked",
        ):
            self.assertIn(
                stage, self.doc_lower, f"doc missing execution flow stage {stage!r}"
            )

    def test_mentions_status_transitions(self) -> None:
        for status in ("queued", "preparing", "implementing", "validating"):
            self.assertIn(
                status, self.doc_lower, f"doc missing status transition {status!r}"
            )

    def test_mentions_safety_boundaries(self) -> None:
        for phrase in (
            "no auto-approval",
            "no auto-merge",
            "no auto-cleanup",
            "no branch deletion",
            "no worktree deletion",
            "no daemon",
            "no webhook",
            "no multi-task batch",
            "one issue / one task / one tick",
            "human confirmation",
        ):
            self.assertIn(
                phrase, self.doc_lower, f"doc missing safety boundary {phrase!r}"
            )

    def test_defines_what_execution_engine_owns(self) -> None:
        self.assertIn("proposed executionengine responsibility", self.doc_lower)
        for owned in (
            "consuming an approved task",
            "resolving the effective executor profile",
            "enforcing preflight inputs",
            "preparing or resolving workspace context",
            "dispatching the executor",
            "capturing executor artifacts",
            "dispatching deterministic validators",
            "producing a proof-of-work summary",
            "returning the next operator action",
        ):
            self.assertIn(
                owned, self.doc_lower, f"doc missing ExecutionEngine ownership {owned!r}"
            )

    def test_defines_what_execution_engine_must_not_own(self) -> None:
        self.assertIn("must not own", self.doc_lower)
        for not_owned in (
            "github issue discovery",
            "github issue ingestion",
            "scheduler candidate selection",
            "proposal creation",
            "human confirmation creation",
            "confirmation verifier authority",
            "merge",
            "cleanup",
            "archive disposition",
            "task closeout disposition",
            "cron scheduling",
            "mission control ui mutation",
        ):
            self.assertIn(
                not_owned,
                self.doc_lower,
                f"doc missing ExecutionEngine non-ownership {not_owned!r}",
            )

    def test_includes_p4_roadmap(self) -> None:
        for phase in ("p4-a", "p4-b", "p4-c", "p4-d", "p4-e", "p4-f"):
            self.assertIn(phase, self.doc_lower, f"doc missing roadmap phase {phase!r}")

    def test_includes_migration_constraints(self) -> None:
        for constraint in (
            "existing tests",
            "dry-run semantics",
            "confirmed flag semantics",
            "current json shapes",
            "no new automation capability",
        ):
            self.assertIn(
                constraint,
                self.doc_lower,
                f"doc missing migration constraint {constraint!r}",
            )

    def test_states_documentation_only_and_behavior_preserving_scope(self) -> None:
        self.assertIn("documentation and tests only", self.doc_lower)
        self.assertIn("documentation-only and behavior-preserving", self.doc_lower)


if __name__ == "__main__":
    unittest.main()
