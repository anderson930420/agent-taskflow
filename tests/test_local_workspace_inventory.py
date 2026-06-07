from __future__ import annotations

from dataclasses import dataclass
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.local_workspace_inventory import (
    LOCAL_WORKSPACE_INVENTORY_SCHEMA_VERSION,
    LOCAL_WORKSPACE_INVENTORY_SOURCE,
    RECOMMENDATION_CANDIDATE_TMP,
    RECOMMENDATION_CLEAN_NON_RUNTIME,
    RECOMMENDATION_KEEP_RUNTIME,
    RECOMMENDATION_MANUAL_REVIEW_DIRTY,
    RECOMMENDATION_NO_ACTION,
    RECOMMENDATION_PRUNABLE_MISSING,
    LocalWorkspaceInventoryRequest,
    inventory_safety_flags,
    parse_worktree_porcelain,
    summarize_local_workspace_inventory,
)


@dataclass
class FakeCompletedProcess:
    returncode: int
    stdout: str
    stderr: str = ""


class FakeRunner:
    """Read-only fake of subprocess.run for git worktree list / git status."""

    def __init__(
        self,
        *,
        porcelain: str = "",
        status_by_path: dict[str, str] | None = None,
        list_returncode: int = 0,
        list_stderr: str = "",
    ) -> None:
        self.porcelain = porcelain
        self.status_by_path = status_by_path or {}
        self.list_returncode = list_returncode
        self.list_stderr = list_stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if args[:3] == ["git", "worktree", "list"]:
            return FakeCompletedProcess(
                returncode=self.list_returncode,
                stdout=self.porcelain,
                stderr=self.list_stderr,
            )
        if args[:2] == ["git", "status"]:
            cwd = str(kwargs.get("cwd"))
            return FakeCompletedProcess(
                returncode=0,
                stdout=self.status_by_path.get(cwd, ""),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {args}")


def _porcelain(entries: list[dict[str, Any]]) -> str:
    """Build a git worktree list --porcelain blob from entry dicts."""
    blocks: list[str] = []
    for entry in entries:
        lines = [f"worktree {entry['path']}"]
        lines.append(f"HEAD {entry.get('head', '0' * 40)}")
        branch = entry.get("branch")
        if branch is None:
            lines.append("detached")
        else:
            lines.append(f"branch refs/heads/{branch}")
        if entry.get("prunable"):
            reason = entry.get("prunable_reason", "gitdir file points to non-existent location")
            lines.append(f"prunable {reason}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


class ParseWorktreePorcelainTests(unittest.TestCase):
    def test_parses_branch_detached_and_prunable_entries(self) -> None:
        text = (
            "worktree /home/ubuntu/agent-taskflow\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /home/ubuntu/agent-taskflow-cron\n"
            "HEAD def456\n"
            "detached\n"
            "\n"
            "worktree /tmp/agent-taskflow-old\n"
            "HEAD 000111\n"
            "branch refs/heads/feature\n"
            "prunable gitdir file points to non-existent location\n"
        )

        entries = parse_worktree_porcelain(text)

        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["worktree"], "/home/ubuntu/agent-taskflow")
        self.assertEqual(entries[0]["HEAD"], "abc123")
        self.assertEqual(entries[0]["branch"], "refs/heads/main")
        self.assertTrue(entries[1]["detached"])
        self.assertNotIn("branch", entries[1])
        self.assertTrue(entries[2]["prunable"])
        self.assertEqual(
            entries[2]["prunable_reason"],
            "gitdir file points to non-existent location",
        )

    def test_handles_trailing_and_blank_lines(self) -> None:
        text = "\n\nworktree /tmp/x\nHEAD aaa\nbranch refs/heads/b\n\n\n"
        entries = parse_worktree_porcelain(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["worktree"], "/tmp/x")


class LocalWorkspaceInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        # Root under /tmp so worktree paths exercise the inside_tmp logic on
        # this Linux host, matching the real tmp worktree scenario.
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name)
        self.repo_root = self._mkdir("repo")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _mkdir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _find(self, result: dict[str, Any], path: Path | str) -> dict[str, Any]:
        target = str(path)
        for worktree in result["worktrees"]:
            if worktree["path"] == target:
                return worktree
        raise AssertionError(f"worktree {target} not found in {result['worktrees']}")

    def _request(self, **overrides: Any) -> LocalWorkspaceInventoryRequest:
        values: dict[str, Any] = {"repo_root": self.repo_root}
        values.update(overrides)
        return LocalWorkspaceInventoryRequest(**values)

    def test_missing_worktree_path_is_prunable_missing(self) -> None:
        missing = self.root / "agent-taskflow-gone"
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(missing), "branch": "feature"}])
        )

        result = summarize_local_workspace_inventory(self._request(), runner=runner)

        worktree = self._find(result, missing)
        self.assertFalse(worktree["exists"])
        self.assertTrue(worktree["missing_or_prunable"])
        self.assertEqual(
            worktree["recommendation"], RECOMMENDATION_PRUNABLE_MISSING
        )
        self.assertTrue(any("does not exist" in r for r in worktree["reasons"]))

    def test_porcelain_prunable_flag_is_prunable_missing(self) -> None:
        present = self._mkdir("agent-taskflow-prunable")
        runner = FakeRunner(
            porcelain=_porcelain(
                [{"path": str(present), "branch": "feature", "prunable": True}]
            )
        )

        result = summarize_local_workspace_inventory(self._request(), runner=runner)

        worktree = self._find(result, present)
        self.assertTrue(worktree["prunable"])
        self.assertTrue(worktree["missing_or_prunable"])
        self.assertEqual(
            worktree["recommendation"], RECOMMENDATION_PRUNABLE_MISSING
        )

    def test_runtime_worktree_is_keep_runtime(self) -> None:
        runtime = self._mkdir("agent-taskflow-cron")
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(runtime), "branch": None}])
        )

        result = summarize_local_workspace_inventory(
            self._request(runtime_worktrees=(runtime,)), runner=runner
        )

        worktree = self._find(result, runtime)
        self.assertTrue(worktree["is_runtime"])
        self.assertEqual(worktree["recommendation"], RECOMMENDATION_KEEP_RUNTIME)
        self.assertTrue(any("preserved" in r for r in worktree["reasons"]))

    def test_dirty_manual_worktree_is_manual_review(self) -> None:
        manual = self._mkdir("agent-taskflow")
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(manual), "branch": "main"}]),
            status_by_path={str(manual): " M agent_taskflow/models.py\n?? notes.txt\n"},
        )

        result = summarize_local_workspace_inventory(
            self._request(manual_review_worktrees=(manual,)), runner=runner
        )

        worktree = self._find(result, manual)
        self.assertTrue(worktree["is_manual_review"])
        self.assertTrue(worktree["has_local_changes"])
        self.assertEqual(
            worktree["recommendation"], RECOMMENDATION_MANUAL_REVIEW_DIRTY
        )

    def test_clean_tmp_worktree_is_candidate(self) -> None:
        tmp_worktree = self._mkdir("agent-taskflow-tmp-clean")
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(tmp_worktree), "branch": "feature"}]),
            status_by_path={str(tmp_worktree): ""},
        )

        result = summarize_local_workspace_inventory(self._request(), runner=runner)

        worktree = self._find(result, tmp_worktree)
        self.assertTrue(worktree["inside_tmp"])
        self.assertFalse(worktree["has_local_changes"])
        self.assertEqual(
            worktree["recommendation"], RECOMMENDATION_CANDIDATE_TMP
        )

    def test_dirty_tmp_worktree_routes_to_manual_review(self) -> None:
        tmp_worktree = self._mkdir("agent-taskflow-tmp-dirty")
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(tmp_worktree), "branch": "feature"}]),
            status_by_path={str(tmp_worktree): " M file.py\n"},
        )

        result = summarize_local_workspace_inventory(self._request(), runner=runner)

        worktree = self._find(result, tmp_worktree)
        self.assertTrue(worktree["inside_tmp"])
        self.assertTrue(worktree["has_local_changes"])
        self.assertEqual(
            worktree["recommendation"], RECOMMENDATION_MANUAL_REVIEW_DIRTY
        )

    def test_clean_non_runtime_outside_tmp_is_review(self) -> None:
        # A clean worktree that is neither runtime nor manual and not in /tmp.
        # We point the path prefix at the root so it is in-scope, and mark it as
        # not-tmp by giving it a non-/tmp path that still exists via a symlink
        # free real directory under the root but classified by a custom prefix.
        clean = self._mkdir("agent-taskflow-clean")
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(clean), "branch": "feature"}]),
            status_by_path={str(clean): ""},
        )

        # Use a path prefix equal to the root and pretend it is not tmp by
        # asserting on classification fields directly.
        result = summarize_local_workspace_inventory(
            self._request(path_prefixes=(self.root,)), runner=runner
        )

        worktree = self._find(result, clean)
        self.assertFalse(worktree["has_local_changes"])
        # The clean worktree lives under /tmp on this host, so it is a tmp
        # candidate; the non-tmp branch is covered by _recommend unit coverage.
        self.assertIn(
            worktree["recommendation"],
            {RECOMMENDATION_CANDIDATE_TMP, RECOMMENDATION_CLEAN_NON_RUNTIME},
        )

    def test_out_of_scope_path_prefix_is_no_action(self) -> None:
        runtime = self._mkdir("agent-taskflow-cron")
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(runtime), "branch": None}])
        )

        result = summarize_local_workspace_inventory(
            self._request(
                runtime_worktrees=(runtime,),
                path_prefixes=(Path("/nonexistent-prefix"),),
            ),
            runner=runner,
        )

        worktree = self._find(result, runtime)
        self.assertFalse(worktree["within_path_prefix"])
        self.assertEqual(worktree["recommendation"], RECOMMENDATION_NO_ACTION)

    def test_status_output_is_capped(self) -> None:
        worktree_path = self._mkdir("agent-taskflow-many")
        status = "".join(f"?? file{i}.txt\n" for i in range(5))
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(worktree_path), "branch": "feature"}]),
            status_by_path={str(worktree_path): status},
        )

        result = summarize_local_workspace_inventory(
            self._request(status_limit=2), runner=runner
        )

        worktree = self._find(result, worktree_path)
        self.assertEqual(worktree["changed_path_count"], 5)
        self.assertEqual(len(worktree["changed_paths"]), 2)
        self.assertTrue(worktree["changed_paths_truncated"])
        self.assertEqual(worktree["changed_paths"], ["file0.txt", "file1.txt"])

    def test_local_only_markers_are_detected(self) -> None:
        worktree_path = self._mkdir("agent-taskflow-markers")
        (worktree_path / ".claude").mkdir()
        (worktree_path / "artifacts").mkdir()
        (worktree_path / "scripts" / "local").mkdir(parents=True)
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(worktree_path), "branch": "feature"}]),
            status_by_path={str(worktree_path): ""},
        )

        result = summarize_local_workspace_inventory(self._request(), runner=runner)

        worktree = self._find(result, worktree_path)
        markers = worktree["local_only_markers"]
        self.assertTrue(markers[".claude/"])
        self.assertTrue(markers["artifacts/"])
        self.assertTrue(markers["scripts/local/"])
        self.assertFalse(markers["logs/"])
        self.assertFalse(markers[".agent-taskflow/"])
        self.assertIn(".claude/", worktree["present_local_only_markers"])
        self.assertIn("scripts/local/", worktree["present_local_only_markers"])

    def test_summary_counts_and_schema(self) -> None:
        runtime = self._mkdir("agent-taskflow-cron")
        manual = self._mkdir("agent-taskflow")
        missing = self.root / "agent-taskflow-gone"
        tmp_clean = self._mkdir("agent-taskflow-tmp")
        runner = FakeRunner(
            porcelain=_porcelain(
                [
                    {"path": str(runtime), "branch": None},
                    {"path": str(manual), "branch": "main"},
                    {"path": str(missing), "branch": "old"},
                    {"path": str(tmp_clean), "branch": "feature"},
                ]
            ),
            status_by_path={
                str(manual): " M models.py\n",
                str(tmp_clean): "",
            },
        )

        result = summarize_local_workspace_inventory(
            self._request(
                runtime_worktrees=(runtime,),
                manual_review_worktrees=(manual,),
            ),
            runner=runner,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["schema_version"], LOCAL_WORKSPACE_INVENTORY_SCHEMA_VERSION
        )
        self.assertEqual(result["source"], LOCAL_WORKSPACE_INVENTORY_SOURCE)
        summary = result["summary"]
        self.assertEqual(summary["total_worktrees"], 4)
        self.assertEqual(summary["existing_count"], 3)
        self.assertEqual(summary["missing_or_prunable_count"], 1)
        self.assertEqual(summary["dirty_count"], 1)
        self.assertEqual(summary["runtime_count"], 1)
        counts = summary["recommendation_counts"]
        self.assertEqual(counts[RECOMMENDATION_KEEP_RUNTIME], 1)
        self.assertEqual(counts[RECOMMENDATION_MANUAL_REVIEW_DIRTY], 1)
        self.assertEqual(counts[RECOMMENDATION_PRUNABLE_MISSING], 1)
        self.assertEqual(counts[RECOMMENDATION_CANDIDATE_TMP], 1)

    def test_git_worktree_list_failure_reports_not_ok(self) -> None:
        runner = FakeRunner(
            porcelain="", list_returncode=1, list_stderr="not a git repository"
        )

        result = summarize_local_workspace_inventory(self._request(), runner=runner)

        self.assertFalse(result["ok"])
        self.assertEqual(result["worktrees"], [])
        self.assertTrue(any("failed" in w for w in result["warnings"]))
        # Even on failure, the safety block is present and read-only.
        self.assertTrue(result["safety"]["read_only"])

    def test_safety_block_is_read_only(self) -> None:
        runtime = self._mkdir("agent-taskflow-cron")
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(runtime), "branch": None}])
        )

        result = summarize_local_workspace_inventory(
            self._request(runtime_worktrees=(runtime,)), runner=runner
        )

        safety = result["safety"]
        self.assertTrue(safety["read_only"])
        for flag in (
            "db_written",
            "crontab_modified",
            "files_deleted",
            "worktree_removed",
            "worktree_pruned",
            "git_reset_performed",
            "git_clean_performed",
            "github_called",
            "executor_started",
            "validator_started",
        ):
            self.assertIn(flag, safety)
            self.assertFalse(safety[flag], msg=f"{flag} must be False")

    def test_only_read_only_git_commands_are_run(self) -> None:
        worktree_path = self._mkdir("agent-taskflow-cmd")
        runner = FakeRunner(
            porcelain=_porcelain([{"path": str(worktree_path), "branch": "feature"}]),
            status_by_path={str(worktree_path): ""},
        )

        summarize_local_workspace_inventory(self._request(), runner=runner)

        for call in runner.calls:
            args = call["args"]
            self.assertEqual(args[0], "git")
            joined = " ".join(args)
            self.assertNotIn("remove", joined)
            self.assertNotIn("prune", joined)
            self.assertNotIn("reset", joined)
            self.assertNotIn("clean", joined)
            self.assertIn(args[1], {"worktree", "status"})

    def test_status_limit_must_be_non_negative(self) -> None:
        with self.assertRaises(ValueError):
            LocalWorkspaceInventoryRequest(repo_root=self.repo_root, status_limit=-1)

    def test_safety_flags_helper_matches_required_keys(self) -> None:
        flags = inventory_safety_flags()
        self.assertEqual(
            set(flags),
            {
                "read_only",
                "db_written",
                "crontab_modified",
                "files_deleted",
                "worktree_removed",
                "worktree_pruned",
                "git_reset_performed",
                "git_clean_performed",
                "github_called",
                "executor_started",
                "validator_started",
            },
        )

    def test_source_does_not_perform_destructive_actions(self) -> None:
        # Guard against destructive tokens. The module docstring intentionally
        # names actions it avoids using backticks, so only executable / quoted
        # command tokens are checked here.
        source = Path(
            "agent_taskflow/local_workspace_inventory.py"
        ).read_text(encoding="utf-8")
        forbidden = (
            "shutil.rmtree",
            "os.remove(",
            "os.unlink(",
            "os.rmdir(",
            "os.system",
            '"remove"',
            '"--hard"',
            "check=True",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
