"""Guard the README safety-boundary statements and the paths both READMEs name."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
README_ZH_TW = REPO_ROOT / "README.zh-TW.md"

_REPO_PATH = re.compile(r"`((?:agent_taskflow|scripts|deploy|docs|config)/[^`\s]*)`")


class ReadmeSafetyBoundaryTests(unittest.TestCase):
    _SAFETY_CLAUSE_PATTERNS = {
        "en": (
            r"Taskflow\s+never\s+merges\s+a\s+pull\s+request",
            r"refuses\s+`gh pr merge`",
            r"Its\s+only\s+push\s+is\s+`git push origin <task-branch>`",
            r"Taskflow\s+never\s+force-pushes",
            r"never\s+pushes\s+`main`,\s+`master`,\s+`trunk`\s+or\s+the\s+target\s+branch",
            r"never\s+deletes\s+a\s+remote\s+branch",
            r"owner\s+reviews\s+and\s+merges\s+every\s+PR\s+on\s+GitHub",
            r"No\s+agent\s+merges\s+or\s+enables\s+auto-merge",
            r"Cleanup\s+runs\s+only\s+after\s+the\s+merge\s+is\s+verified",
        ),
        "zh_tw": (
            r"Taskflow\s*從不\s*merge\s+pull\s+request",
            r"會拒絕\s*`gh pr merge`",
            r"唯一的\s*push\s*是\s*`git push origin <task-branch>`",
            r"Taskflow\s*從不\s*force-push",
            r"從不\s*push\s*`main`、`master`、`trunk`\s*或\s*target\s+branch",
            r"從不刪除\s*remote\s+branch",
            r"每個\s*PR\s*都由\s*owner\s*在\s*GitHub\s*審查並\s*merge",
            r"任何\s*agent\s*都不會\s*merge，也不會\s*啟用\s*auto-merge",
            r"只有在\s*target\s+history\s*中驗證\s*merge\s*之後才會清理",
        ),
    }
    _UNSAFE_INVERSIONS = (
        ("en", "Taskflow never merges", "Taskflow merges"),
        ("en", "refuses `gh pr merge`", "runs `gh pr merge`"),
        ("en", "Its only push is", "One push is"),
        ("en", "Taskflow never force-pushes", "Taskflow force-pushes"),
        ("en", "never pushes `main`", "pushes `main`"),
        ("en", "never deletes", "deletes"),
        ("en", "owner reviews and merges every PR", "agent reviews and merges every PR"),
        ("en", "No agent merges or enables", "An agent merges or enables"),
        ("en", "Cleanup runs only after", "Cleanup runs before"),
        ("zh_tw", "Taskflow 從不 merge", "Taskflow 會 merge"),
        ("zh_tw", "會拒絕 `gh pr merge`", "會執行 `gh pr merge`"),
        ("zh_tw", "唯一的 push", "其中一個 push"),
        ("zh_tw", "Taskflow 從不 force-push", "Taskflow 會 force-push"),
        ("zh_tw", "從不 push `main`", "會 push `main`"),
        ("zh_tw", "從不刪除 remote", "會刪除 remote"),
        ("zh_tw", "都由 owner 在 GitHub", "都由 agent 在 GitHub"),
        ("zh_tw", "都不會 merge，也不會", "都會 merge，也會"),
        ("zh_tw", "只有在 target history", "不必在 target history"),
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.readmes = {
            "en": README.read_text(encoding="utf-8"),
            "zh_tw": README_ZH_TW.read_text(encoding="utf-8"),
        }

    def _assert_safety_clauses(self, readme: str, language: str) -> None:
        for pattern in self._SAFETY_CLAUSE_PATTERNS[language]:
            self.assertRegex(readme, pattern)

    def test_safety_boundaries_are_stated_in_both_languages(self) -> None:
        for language, readme in self.readmes.items():
            with self.subTest(language=language):
                self._assert_safety_clauses(readme, language)

    def test_safety_clause_guards_reject_each_unsafe_inversion(self) -> None:
        for language, old, new in self._UNSAFE_INVERSIONS:
            with self.subTest(language=language, old=old):
                readme = self.readmes[language]
                changed = readme.replace(old, new, 1)
                self.assertNotEqual(changed, readme)
                with self.assertRaises(AssertionError):
                    self._assert_safety_clauses(changed, language)

    def test_every_repository_path_named_by_a_readme_exists(self) -> None:
        for language, readme in self.readmes.items():
            paths = _REPO_PATH.findall(readme)
            self.assertTrue(paths, language)
            for path in paths:
                with self.subTest(language=language, path=path):
                    if "*" in path:
                        self.assertTrue(list(REPO_ROOT.glob(path)), path)
                    else:
                        self.assertTrue((REPO_ROOT / path).exists(), path)


if __name__ == "__main__":
    unittest.main()
