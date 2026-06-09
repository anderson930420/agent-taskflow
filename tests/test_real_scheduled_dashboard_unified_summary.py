"""P4-h tests: the real scheduled dashboard reads a UnifiedExecutionSummary.

These tests cover the summarizer reading an embedded ``observability_summary``
(a normalized ``UnifiedExecutionSummary``) from a scheduler tick log line, while
preserving the legacy fallback for old logs and malformed summaries. The
summarizer stays strictly read-only: no cron change, no DB write, no GitHub
call, no executor/validator run, no merge/approve/cleanup/archive/closeout, no
branch/worktree deletion, no daemon or scheduler loop.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_taskflow.execution_observability import (
    EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION,
    SUMMARY_SOURCE_SCHEDULER_TICK,
    summarize_scheduler_tick_payload,
    to_observability_dict,
)
from agent_taskflow.real_scheduled_execution_observability import (
    RealScheduledExecutionObservabilityRequest,
    render_real_scheduled_execution_summary,
    summarize_real_scheduled_execution,
)


def _scheduler_tick(
    *,
    status: str = "execution_completed",
    ok: bool = True,
    selected_task_key: str | None = "AT-GH-123",
    executor: str = "opencode",
    model: str = "minimax-coding-plan/MiniMax-M2.7",
    validators: list[str] | None = None,
) -> dict[str, Any]:
    """Build a realistic scheduler tick payload (without observability_summary)."""

    return {
        "ok": ok,
        "schema_version": "github_issue_one_task_scheduler_tick.v1",
        "source": "github_issue_one_task_scheduler_tick",
        "status": status,
        "mode": "confirmed",
        "repo": "anderson930420/agent-taskflow",
        "selected_task_key": selected_task_key,
        "runner_config": {
            "executor": executor,
            "model": model,
            "validators": validators if validators is not None else ["policy"],
            "worktree_root": "/home/ubuntu/agent-taskflow-cron/.worktrees",
        },
        "publication_config": {
            "publish_after_execution": False,
            "mode": "execution_only",
        },
        "lock": {"acquired": True, "contended": False, "released": True},
        "safety": {"human_review_required": True, "dry_run": False},
    }


def _with_observability_summary(tick: dict[str, Any]) -> dict[str, Any]:
    """Embed a real observability_summary, exactly as the P4-g cron tick would."""

    summary = to_observability_dict(summarize_scheduler_tick_payload(tick))
    return {**tick, "observability_summary": summary}


class RealScheduledDashboardUnifiedSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log_path = self.root / "tick.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_ticks(self, ticks: list[dict[str, Any]]) -> None:
        self.log_path.write_text(
            "\n".join(json.dumps(t, sort_keys=True) for t in ticks) + "\n",
            encoding="utf-8",
        )

    def _summarize(self, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {"log_path": self.log_path}
        values.update(overrides)
        return summarize_real_scheduled_execution(
            RealScheduledExecutionObservabilityRequest(**values)
        )

    # -- A. legacy fallback -------------------------------------------------

    def test_legacy_log_without_summary_preserves_core_fields(self) -> None:
        self._write_ticks(
            [
                _scheduler_tick(status="no_eligible_issues", selected_task_key=None),
                _scheduler_tick(status="execution_completed"),
            ]
        )

        result = self._summarize()

        # Legacy last_tick fields still work.
        self.assertEqual(result["last_tick"]["status"], "execution_completed")
        self.assertEqual(result["last_tick"]["selected_task_key"], "AT-GH-123")
        self.assertEqual(
            result["last_tick"]["runner_config"]["executor"], "opencode"
        )
        # No unified-summary marker for legacy logs.
        self.assertFalse(result["last_tick_uses_observability_summary"])
        self.assertIsNone(result["last_tick_observability_summary"])
        # Recent counts unchanged.
        recent = result["recent_ticks"]
        self.assertEqual(recent["total_parsed"], 2)
        self.assertEqual(recent["execution_completed_count"], 1)
        self.assertEqual(recent["no_eligible_count"], 1)
        self.assertEqual(recent["observability_summary_count"], 0)
        self.assertEqual(recent["malformed_observability_summary_count"], 0)

    def test_legacy_log_emits_no_unified_summary_warning(self) -> None:
        self._write_ticks([_scheduler_tick(status="no_eligible_issues")])

        result = self._summarize()

        self.assertFalse(
            any("observability_summary" in w for w in result["warnings"]),
            msg=f"warnings: {result['warnings']!r}",
        )

    # -- B. valid unified summary ------------------------------------------

    def test_valid_summary_is_read_and_exposed(self) -> None:
        tick = _with_observability_summary(
            _scheduler_tick(
                status="execution_completed",
                selected_task_key="AT-GH-777",
                executor="opencode",
                model="minimax-coding-plan/MiniMax-M2.7",
                validators=["policy", "tests"],
            )
        )
        self._write_ticks([tick])

        result = self._summarize()

        self.assertTrue(result["last_tick_uses_observability_summary"])
        obs = result["last_tick_observability_summary"]
        self.assertIsInstance(obs, dict)
        self.assertEqual(
            obs["schema_version"], EXECUTION_OBSERVABILITY_SUMMARY_SCHEMA_VERSION
        )
        self.assertEqual(obs["schema_version"], "execution_observability_summary.v1")
        self.assertEqual(obs["source"], SUMMARY_SOURCE_SCHEDULER_TICK)
        self.assertEqual(obs["source"], "scheduler_tick")
        # status maps from the unified summary.
        self.assertEqual(obs["status"], "execution_completed")
        self.assertEqual(obs["task_key"], "AT-GH-777")
        self.assertTrue(obs["ok"])
        # profile maps from the unified summary.
        self.assertEqual(obs["profile"]["executor"], "opencode")
        self.assertEqual(
            obs["profile"]["model"], "minimax-coding-plan/MiniMax-M2.7"
        )
        self.assertEqual(obs["profile"]["validators"], ["policy", "tests"])
        # publication mode maps from the unified summary.
        self.assertEqual(obs["publication_mode"], "execution_only")
        # safety maps from the unified summary.
        self.assertTrue(obs["safety"]["human_review_required"])
        self.assertFalse(obs["safety"]["merged"])

    def test_recent_counts_use_unified_summary_status(self) -> None:
        self._write_ticks(
            [
                _with_observability_summary(
                    _scheduler_tick(status="execution_completed")
                ),
                _with_observability_summary(
                    _scheduler_tick(
                        status="no_eligible_issues", selected_task_key=None
                    )
                ),
                _with_observability_summary(
                    _scheduler_tick(status="execution_completed")
                ),
            ]
        )

        result = self._summarize()
        recent = result["recent_ticks"]

        self.assertEqual(recent["total_parsed"], 3)
        self.assertEqual(recent["observability_summary_count"], 3)
        self.assertEqual(recent["malformed_observability_summary_count"], 0)
        self.assertEqual(recent["execution_completed_count"], 2)
        self.assertEqual(recent["no_eligible_count"], 1)
        self.assertEqual(recent["statuses"].get("execution_completed"), 2)
        self.assertEqual(recent["statuses"].get("no_eligible_issues"), 1)

    def test_human_readable_output_mentions_observability_summary(self) -> None:
        self._write_ticks(
            [_with_observability_summary(_scheduler_tick(status="execution_completed"))]
        )

        rendered = render_real_scheduled_execution_summary(self._summarize())

        self.assertIn("observability summary:", rendered)
        self.assertIn("source=scheduler_tick", rendered)

    # -- C. malformed unified summary --------------------------------------

    def test_summary_not_a_mapping_falls_back_to_legacy(self) -> None:
        tick = _scheduler_tick(status="execution_completed")
        tick["observability_summary"] = "not-a-mapping"
        self._write_ticks([tick])

        result = self._summarize()

        # Did not crash; legacy fallback used.
        self.assertTrue(result["ok"])
        self.assertFalse(result["last_tick_uses_observability_summary"])
        self.assertIsNone(result["last_tick_observability_summary"])
        self.assertEqual(result["last_tick"]["status"], "execution_completed")
        # Tick still counts as parsed.
        recent = result["recent_ticks"]
        self.assertEqual(recent["total_parsed"], 1)
        self.assertEqual(recent["execution_completed_count"], 1)
        self.assertEqual(recent["malformed_observability_summary_count"], 1)
        self.assertTrue(
            any("malformed observability_summary" in w for w in result["warnings"]),
            msg=f"warnings: {result['warnings']!r}",
        )

    def test_summary_with_wrong_schema_version_falls_back(self) -> None:
        tick = _scheduler_tick(status="no_eligible_issues", selected_task_key=None)
        tick["observability_summary"] = {
            "schema_version": "some_other_schema.v9",
            "status": "definitely_wrong",
        }
        self._write_ticks([tick])

        result = self._summarize()

        self.assertFalse(result["last_tick_uses_observability_summary"])
        self.assertIsNone(result["last_tick_observability_summary"])
        # Legacy status drives counting, not the malformed summary's status.
        self.assertEqual(result["last_tick"]["status"], "no_eligible_issues")
        recent = result["recent_ticks"]
        self.assertEqual(recent["no_eligible_count"], 1)
        self.assertEqual(recent["statuses"].get("definitely_wrong"), None)
        self.assertEqual(recent["malformed_observability_summary_count"], 1)

    # -- D. read-only boundary ----------------------------------------------

    def test_safety_block_remains_read_only(self) -> None:
        self._write_ticks(
            [_with_observability_summary(_scheduler_tick(status="execution_completed"))]
        )

        result = self._summarize()
        safety = result["safety"]

        self.assertTrue(safety["read_only"])
        for flag in (
            "cron_modified",
            "db_written",
            "github_called",
            "executor_started",
            "validator_started",
            "issue_ingested",
            "branch_pushed",
            "draft_pr_created",
            "merged",
            "approved",
            "cleanup_performed",
            "branch_deleted",
            "worktree_deleted",
            "daemon_started",
            "scheduler_loop_started",
        ):
            self.assertIn(flag, safety)
            self.assertFalse(safety[flag], msg=f"{flag} must be False")

    def test_summary_output_adds_no_mutating_action_keys(self) -> None:
        self._write_ticks(
            [_with_observability_summary(_scheduler_tick(status="execution_completed"))]
        )

        result = self._summarize()

        # P4-h is read-only: it must not surface any action/handle that implies a
        # merge, cleanup, archive, closeout, PR publication, issue close, or
        # branch/worktree deletion was performed or is offered by this tool.
        forbidden_substrings = (
            "merge",
            "cleanup",
            "archive",
            "closeout",
            "publish_pr",
            "issue_close",
            "branch_delete",
            "worktree_delete",
        )
        top_level_keys = set(result.keys()) | set(result["recent_ticks"].keys())
        for key in top_level_keys:
            for needle in forbidden_substrings:
                self.assertNotIn(
                    needle,
                    key.lower(),
                    msg=f"unexpected mutating-action key surfaced: {key}",
                )

    def test_module_source_performs_no_automation(self) -> None:
        source = Path(
            "agent_taskflow/real_scheduled_execution_observability.py"
        ).read_text(encoding="utf-8")
        forbidden = (
            "while True",
            "import subprocess",
            "subprocess.",
            "os.system",
            "threading.Thread",
            "record_approval_decision(",
            "update_task_status(",
            "ingest_github_issue(",
            "merge_pull_request",
            "create_draft_pr",
        )
        for needle in forbidden:
            self.assertNotIn(needle, source, needle)


if __name__ == "__main__":
    unittest.main()
