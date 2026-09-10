"""Step 3 negative-scope gate.

Three required negatives:

1. No emitted payload, template, or API response contains a percentage, ETA,
   or completion estimate (SPEC §14.2).
2. No Step 3 code path writes a Ticket lifecycle status.
3. No Step 3 code path issues a GitHub or Git call.

The checks are source-level and deliberately blunt: Step 3 owns a small, fixed
set of modules, and none of them may grow a lifecycle, Git, or GitHub seam.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# Every module Step 3 creates. `execution_observability.py` is extended in
# place and is checked separately because it predates Step 3.
STEP3_PY_MODULES = (
    "agent_taskflow/runtime_progress.py",
    "agent_taskflow/runtime_progress_schema.py",
    "agent_taskflow/runtime_progress_store.py",
    "agent_taskflow/realtime_projection.py",
    "agent_taskflow/api/realtime.py",
)

STEP3_FRONTEND_FILES = (
    "mission-control/lib/realtime.ts",
    "mission-control/components/ExecutionStepList.tsx",
    "mission-control/components/LiveBoard.tsx",
    "mission-control/components/LiveTicketPanel.tsx",
    "mission-control/app/live/page.tsx",
)


def read(relative: str) -> str:
    path = REPO_ROOT / relative
    if not path.exists():
        raise FileNotFoundError(f"Step 3 file missing: {relative}")
    return path.read_text(encoding="utf-8")


class Step3FilesExistTests(unittest.TestCase):
    def test_every_declared_step3_file_exists(self) -> None:
        for relative in STEP3_PY_MODULES + STEP3_FRONTEND_FILES:
            with self.subTest(file=relative):
                self.assertTrue((REPO_ROOT / relative).exists(), relative)


class NoLifecycleWriteInStep3CodeTests(unittest.TestCase):
    """Forbidden layer — the Python control plane owns lifecycle (§2.1)."""

    LIFECYCLE_CALL_TOKENS = (
        "update_task_status",
        "record_approval_decision",
        "create_attempt",
        "close_attempt",
        "append_lifecycle_event",
        "register_task_identity",
        "upsert_task(",
        "upsert_task_with_level2_identity",
    )

    LIFECYCLE_SQL_PATTERNS = (
        r"\bUPDATE\s+tasks\b",
        r"\bINSERT\s+INTO\s+tasks\b",
        r"\bDELETE\s+FROM\s+tasks\b",
        r"\bUPDATE\s+attempts\b",
        r"\bINSERT\s+INTO\s+attempts\b",
        r"\bINSERT\s+INTO\s+lifecycle_events\b",
        r"\bINSERT\s+INTO\s+task_events\b",
        r"\bALTER\s+TABLE\s+tasks\b",
    )

    def test_no_step3_module_calls_a_lifecycle_mutator(self) -> None:
        for relative in STEP3_PY_MODULES:
            source = read(relative)
            for token in self.LIFECYCLE_CALL_TOKENS:
                with self.subTest(file=relative, token=token):
                    self.assertNotIn(token, source)

    def test_no_step3_module_writes_a_lifecycle_table(self) -> None:
        for relative in STEP3_PY_MODULES:
            source = read(relative)
            for pattern in self.LIFECYCLE_SQL_PATTERNS:
                with self.subTest(file=relative, pattern=pattern):
                    self.assertIsNone(
                        re.search(pattern, source, re.IGNORECASE), pattern
                    )

    def test_step3_modules_only_write_their_own_progress_tables(self) -> None:
        written: set[str] = set()
        # Uppercase-only on purpose: SQL in this repo is written in caps, so
        # prose like "update the current phase" is not a false positive.
        pattern = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)")
        for relative in STEP3_PY_MODULES:
            for match in pattern.finditer(read(relative)):
                written.add(match.group(1).lower())
        self.assertTrue(
            written <= {"attempt_progress", "attempt_observed_steps", "schema_migrations"},
            f"Step 3 wrote unexpected tables: {sorted(written)}",
        )


class NoGitOrGithubCallInStep3CodeTests(unittest.TestCase):
    """Forbidden layer — no Git operations, no GitHub writes, no polling."""

    FORBIDDEN_IMPORTS = frozenset(
        {
            "subprocess",
            "requests",
            "httpx",
            "urllib",
            "urllib.request",
            "http.client",
            "socket",
            "agent_taskflow.branch_push",
            "agent_taskflow.draft_pr",
            "agent_taskflow.dispatcher",
            "agent_taskflow.worktree",
            "agent_taskflow.workspace_manager",
            "agent_taskflow.github_issue_intake",
            "agent_taskflow.github_issue_ingestion",
            "agent_taskflow.github_issue_discovery",
            "agent_taskflow.executor_launch",
        }
    )

    FORBIDDEN_TOKENS = (
        "subprocess",
        "os.system",
        "os.popen",
        "urllib.request",
        "http.client",
        "gh pr ",
        "gh issue ",
        "gh api ",
        "git push",
        "git fetch",
        "git merge",
        "git rebase",
        "git commit",
        "git worktree",
        "git branch",
        "api.github.com",
    )

    def test_no_step3_module_imports_a_process_or_network_layer(self) -> None:
        for relative in STEP3_PY_MODULES:
            tree = ast.parse(read(relative), filename=relative)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            with self.subTest(file=relative):
                self.assertEqual(imported & self.FORBIDDEN_IMPORTS, set())

    def test_no_step3_module_shells_out_or_calls_github(self) -> None:
        for relative in STEP3_PY_MODULES:
            source = read(relative)
            for token in self.FORBIDDEN_TOKENS:
                with self.subTest(file=relative, token=token):
                    self.assertNotIn(token, source)

    def test_step3_frontend_never_calls_a_mutation_endpoint(self) -> None:
        forbidden = (
            "/approve",
            "/reject",
            "/block",
            "/start",
            "/validate",
            "/prepare-workspace",
            'method: "POST"',
            "method: 'POST'",
            "gh pr",
            "git push",
        )
        for relative in STEP3_FRONTEND_FILES:
            source = read(relative)
            for token in forbidden:
                with self.subTest(file=relative, token=token):
                    self.assertNotIn(token, source)


class NoProgressEstimateAnywhereTests(unittest.TestCase):
    """§14.2 — no percentage, ETA, or completion estimate in code or template."""

    WORD_PATTERNS = (
        r"\bpercent(?:age)?\b",
        r"\bpct\b",
        r"\bETA\b",
        r"\bprogress[ _-]?bar\b",
        r"<progress\b",
        r"\btime[ _-]remaining\b",
        r"\b(?:seconds|minutes)[ _-]remaining\b",
        r"\bcompletion[ _-]estimate\b",
        r"\bestimated[ _-](?:completion|finish|time|remaining)\b",
    )

    NUMERIC_PERCENT = re.compile(r"\d\s*%")

    def _assert_clean(self, relative: str, *, allow_regex_source: bool) -> None:
        source = read(relative)
        if allow_regex_source:
            # runtime_progress.py necessarily *names* the forbidden tokens in
            # order to detect them; only assert on the numeric-percent form.
            self.assertIsNone(self.NUMERIC_PERCENT.search(source), relative)
            return
        for pattern in self.WORD_PATTERNS:
            with self.subTest(file=relative, pattern=pattern):
                self.assertIsNone(re.search(pattern, source, re.IGNORECASE), pattern)
        with self.subTest(file=relative, pattern="numeric percent"):
            self.assertIsNone(self.NUMERIC_PERCENT.search(source))

    def test_step3_python_modules_emit_no_estimate(self) -> None:
        for relative in STEP3_PY_MODULES:
            self._assert_clean(
                relative,
                allow_regex_source=relative.endswith("runtime_progress.py"),
            )

    def test_step3_frontend_renders_no_estimate(self) -> None:
        for relative in STEP3_FRONTEND_FILES:
            self._assert_clean(relative, allow_regex_source=False)

    def test_step3_frontend_has_no_dag_visualization(self) -> None:
        # §16 / §41 — V1 does not do DAG visualization.
        for relative in STEP3_FRONTEND_FILES:
            source = read(relative).lower()
            for token in ("dag", "graphviz", "cytoscape", "d3-", "mermaid"):
                with self.subTest(file=relative, token=token):
                    self.assertNotIn(token, source)


class Step3StaysWithinItsAllowedLayersTests(unittest.TestCase):
    def test_step3_does_not_introduce_a_scheduler_or_webhook_seam(self) -> None:
        forbidden = (
            r"scheduler_tick",
            r"run_scheduler",
            r"webhook",
            r"integration_lock",
            r"acquire_lock",
            r"max_concurrent_tasks",
            r"atomic_claim",
            r"\blease\b",
        )
        for relative in STEP3_PY_MODULES:
            source = read(relative)
            for pattern in forbidden:
                with self.subTest(file=relative, pattern=pattern):
                    self.assertIsNone(re.search(pattern, source, re.IGNORECASE))

    def test_step3_does_not_add_a_new_python_dependency(self) -> None:
        declared = read("pyproject.toml")
        self.assertNotIn("sse-starlette", declared)
        self.assertNotIn("aiosqlite", declared)
        for relative in STEP3_PY_MODULES:
            tree = ast.parse(read(relative), filename=relative)
            roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
            allowed = {
                "__future__",
                "agent_taskflow",
                "anyio",
                "asyncio",
                "functools",
                "collections",
                "contextlib",
                "dataclasses",
                "datetime",
                "fastapi",
                "json",
                "pathlib",
                "re",
                "sqlite3",
                "starlette",
                "time",
                "types",
                "typing",
            }
            with self.subTest(file=relative):
                self.assertEqual(roots - allowed, set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
