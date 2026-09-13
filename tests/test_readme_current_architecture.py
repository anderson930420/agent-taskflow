"""Guard practical-V1 README claims that would be unsafe if inverted."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
README_ZH_TW = REPO_ROOT / "README.zh-TW.md"


class ReadmeCurrentArchitectureTests(unittest.TestCase):
    _SAFETY_CLAUSE_PATTERNS = {
        "en": (
            r"Taskflow\s+never\s+calls\s+`gh pr merge`",
            r"cannot\s+self-approve",
            r"does\s+not\s+enable\s+auto-merge",
            r"\|\s*Published PR branches are never force-pushed\s*\|",
            r"\|\s*Protected or target branches are never task-push targets\s*\|",
            r"does not restore a\s+legacy scheduler fallback",
        ),
        "zh_tw": (
            r"Taskflow\s+不會呼叫\s+`gh pr merge`",
            r"不能\s+self-approve",
            r"不會啟用\s+auto-merge",
            r"\|\s*已發布 PR branch 不可 force-push\s*\|",
            r"\|\s*Protected 或 target branch 不是 task push target\s*\|",
            r"不會恢復 legacy scheduler\s+fallback",
        ),
    }
    _SAFETY_CLAUSE_SAMPLES = {
        "en": (
            "Taskflow never calls `gh pr merge`",
            "cannot self-approve",
            "does not enable auto-merge",
            "| Published PR branches are never force-pushed |",
            "| Protected or target branches are never task-push targets |",
            "does not restore a legacy scheduler fallback",
        ),
        "zh_tw": (
            "Taskflow 不會呼叫 `gh pr merge`",
            "不能 self-approve",
            "不會啟用 auto-merge",
            "| 已發布 PR branch 不可 force-push |",
            "| Protected 或 target branch 不是 task push target |",
            "不會恢復 legacy scheduler fallback",
        ),
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = README.read_text(encoding="utf-8")
        cls.readme_zh_tw = README_ZH_TW.read_text(encoding="utf-8")

    def test_both_readmes_exist(self) -> None:
        self.assertTrue(README.is_file())
        self.assertTrue(README_ZH_TW.is_file())

    def test_practical_v1_flow_is_described_in_both_languages(self) -> None:
        for readme in (self.readme, self.readme_zh_tw):
            for fact in (
                "ready_for_integration",
                "FIFO",
                "deterministic validation",
                "GitHub",
                "completed",
            ):
                self.assertIn(fact, readme)

    def test_success_path_keeps_legacy_waiting_approval_out_of_automation(self) -> None:
        for readme in (self.readme, self.readme_zh_tw):
            self.assertIn("`ready_for_integration`", readme)
        automated_sections = (
            self.readme.split("### What is automated today", 1)[1].split(
                "### Current operator and human gates", 1
            )[0],
            self.readme_zh_tw.split("### 現在已自動化的部分", 1)[1].split(
                "### 現在仍由 operator 或人類把關的部分", 1
            )[0],
        )
        for section in automated_sections:
            self.assertNotIn("waiting_approval", section)

    def _assert_safety_clauses(self, readme: str, language: str) -> None:
        for pattern in self._SAFETY_CLAUSE_PATTERNS[language]:
            self.assertRegex(readme, pattern)

    def test_human_merge_and_branch_safety_clauses_are_explicit(self) -> None:
        self._assert_safety_clauses(self.readme, "en")
        self._assert_safety_clauses(self.readme_zh_tw, "zh_tw")

    def test_safety_clause_guards_reject_each_unsafe_inversion(self) -> None:
        mutations = (
            ("en", "Taskflow never calls", "Taskflow calls"),
            ("en", "cannot self-approve", "can self-approve"),
            ("en", "does not enable auto-merge", "enables auto-merge"),
            ("en", "are never force-pushed", "are always force-pushed"),
            ("en", "are never task-push targets", "are valid task-push targets"),
            ("en", "does not restore a", "restores a"),
            ("zh_tw", "Taskflow 不會呼叫", "Taskflow 會呼叫"),
            ("zh_tw", "不能 self-approve", "能 self-approve"),
            ("zh_tw", "不會啟用 auto-merge", "會啟用 auto-merge"),
            ("zh_tw", "不可 force-push", "可 force-push"),
            ("zh_tw", "不是 task push target", "是 task push target"),
            ("zh_tw", "不會恢復 legacy scheduler", "會恢復 legacy scheduler"),
        )
        readmes = {
            language: "\n".join(clauses)
            for language, clauses in self._SAFETY_CLAUSE_SAMPLES.items()
        }
        for language, old, new in mutations:
            with self.subTest(language=language, old=old):
                changed = readmes[language].replace(old, new, 1)
                self.assertNotEqual(changed, readmes[language])
                with self.assertRaises(AssertionError):
                    self._assert_safety_clauses(changed, language)

    def test_current_unimplemented_callers_are_not_claimed_as_operational(self) -> None:
        for readme in (self.readme, self.readme_zh_tw):
            self.assertIn("F9", readme)
            self.assertIn("F10", readme)
            self.assertIn("`confirm_integration=True`", readme)
            self.assertIn("non-test caller", readme)
            self.assertIn("cron", readme)
            self.assertRegex(readme, r"(?is)(not implemented|尚未實作).{0,100}F9|F9.{0,100}(not implemented|尚未實作)")
        self.assertIn("must not be\ndescribed as an installed", self.readme)
        self.assertIn("不能被描述為已安裝", self.readme_zh_tw)

    def test_safety_boundaries_name_concrete_enforcement_points(self) -> None:
        for readme in (self.readme, self.readme_zh_tw):
            for path in (
                "agent_taskflow/runtime_admission.py",
                "agent_taskflow/integration_controller.py",
                "agent_taskflow/integration_git.py",
                "agent_taskflow/integration_cleanup.py",
            ):
                self.assertIn(path, readme)
            self.assertIn("force-push", readme)
            self.assertIn("GitHub CI", readme)
        for path in (
            "agent_taskflow/runtime_admission.py",
            "agent_taskflow/integration_controller.py",
            "agent_taskflow/integration_git.py",
            "agent_taskflow/integration_cleanup.py",
        ):
            self.assertTrue((REPO_ROOT / path).is_file(), path)

    def test_failure_vocabulary_is_scoped_to_its_execution_path(self) -> None:
        for readme in (self.readme, self.readme_zh_tw):
            self.assertIn("integration", readme)
            self.assertIn("needs_decision", readme)
            self.assertIn("failed", readme)
            self.assertIn("blocked", readme)
        self.assertIn("Ticket execution path", self.readme)
        self.assertIn("GitHub-issue path", self.readme)
        self.assertIn("Ticket execution path", self.readme_zh_tw)
        self.assertIn("Legacy GitHub-issue path", self.readme_zh_tw)

    def test_execution_engine_authority_and_ticket_status_are_not_conflated(self) -> None:
        for readme in (self.readme, self.readme_zh_tw):
            self.assertIn("SchedulerExecutionEngineAuthority", readme)
            self.assertIn("ExecutionEngine", readme)
            self.assertIn("--use-execution-engine", readme)
            self.assertIn("legacy scheduler", readme)
            self.assertIn("fallback", readme)
            self.assertIn("Ticket success", readme)

    def test_deployment_facts_and_evidence_locations_are_sourced(self) -> None:
        for readme in (self.readme, self.readme_zh_tw):
            self.assertIn("2 active executor leases", readme)
            self.assertIn("RULINGS.md` 52", readme)
            for migration in (
                "scripts/migrate_ticket_fields.py",
                "scripts/migrate_runtime_progress.py",
                "scripts/migrate_ticket_worktree_resources.py",
            ):
                self.assertIn(migration, readme)
            self.assertIn("docs/v1/handoff-f8-e2e.md", readme)

    def test_stale_dogfood_readme_contract_is_removed(self) -> None:
        for readme in (self.readme, self.readme_zh_tw):
            self.assertNotIn("Semi-Automatic Dogfood Loop", readme)
            self.assertNotIn("Optional Branch Push", readme)
            self.assertNotIn("Optional Draft PR Handoff", readme)


if __name__ == "__main__":
    unittest.main()
