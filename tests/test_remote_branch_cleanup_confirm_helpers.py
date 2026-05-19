"""Unit tests for remote_branch_cleanup_confirm_helpers pure helper functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import unittest

from agent_taskflow.remote_branch_cleanup_confirm_helpers import (
    PROTECTED_BRANCHES,
    LOCAL_ARTIFACT_KIND,
    LOCAL_CONFIRM_FLAG,
    LOCAL_EVENT_TYPE,
    build_git_ls_remote_heads_command,
    build_git_push_delete_command,
    cleanup_recommendation_snapshot,
    dedupe_preserve_order,
    empty_cleanup_recommendation,
    empty_draft_pr_evidence,
    empty_local_cleanup_evidence,
    empty_remote_branch,
    latest_event_payload,
    normalize_branch_name,
    safety_block,
    validate_branch_name,
)


class TestProtectedBranches(unittest.TestCase):
    def test_contains_main_master_trunk(self) -> None:
        self.assertIn("main", PROTECTED_BRANCHES)
        self.assertIn("master", PROTECTED_BRANCHES)
        self.assertIn("trunk", PROTECTED_BRANCHES)

    def test_task_branch_not_protected(self) -> None:
        self.assertNotIn("task/AT-001", PROTECTED_BRANCHES)
        self.assertNotIn("feature/my-feature", PROTECTED_BRANCHES)


class TestNormalizeBranchName(unittest.TestCase):
    def test_strips_whitespace(self) -> None:
        self.assertEqual(normalize_branch_name("  task/AT-001  "), "task/AT-001")

    def test_returns_none_for_non_string(self) -> None:
        self.assertIsNone(normalize_branch_name(None))
        self.assertIsNone(normalize_branch_name(123))
        self.assertIsNone(normalize_branch_name([]))

    def test_returns_none_for_empty_string(self) -> None:
        self.assertIsNone(normalize_branch_name(""))
        self.assertIsNone(normalize_branch_name("   "))

    def test_returns_plain_string(self) -> None:
        self.assertEqual(normalize_branch_name("main"), "main")


class TestValidateBranchName(unittest.TestCase):
    def test_valid_task_branch_passes(self) -> None:
        self.assertIsNone(validate_branch_name("task/AT-001"))

    def test_valid_simple_branch_passes(self) -> None:
        self.assertIsNone(validate_branch_name("feature-branch"))
        self.assertIsNone(validate_branch_name("fix.something"))

    def test_empty_string_fails(self) -> None:
        self.assertIsNotNone(validate_branch_name(""))

    def test_leading_dash_fails(self) -> None:
        error = validate_branch_name("-bad-branch")
        self.assertIsNotNone(error)
        self.assertIn("'-'", error)

    def test_whitespace_fails(self) -> None:
        error = validate_branch_name("bad branch")
        self.assertIsNotNone(error)
        self.assertIn("whitespace", error)

    def test_double_dot_fails(self) -> None:
        error = validate_branch_name("branch..ref")
        self.assertIsNotNone(error)
        self.assertIn("..", error)

    def test_colon_fails(self) -> None:
        error = validate_branch_name("branch:ref")
        self.assertIsNotNone(error)
        self.assertIn(":", error)

    def test_asterisk_fails(self) -> None:
        error = validate_branch_name("branch*glob")
        self.assertIsNotNone(error)

    def test_unsupported_chars_fail(self) -> None:
        for ch in "?[]\\^~":
            with self.subTest(ch=ch):
                self.assertIsNotNone(validate_branch_name(f"branch{ch}name"))

    def test_dot_lock_suffix_fails(self) -> None:
        error = validate_branch_name("branch.lock")
        self.assertIsNotNone(error)
        self.assertIn(".lock", error)

    def test_protected_branch_names_pass_validation(self) -> None:
        # validate_branch_name does not enforce protection; that is done upstream
        self.assertIsNone(validate_branch_name("main"))
        self.assertIsNone(validate_branch_name("master"))
        self.assertIsNone(validate_branch_name("trunk"))


class TestDedupePreserveOrder(unittest.TestCase):
    def test_removes_duplicates(self) -> None:
        self.assertEqual(dedupe_preserve_order(["a", "b", "a", "c"]), ["a", "b", "c"])

    def test_preserves_order(self) -> None:
        self.assertEqual(dedupe_preserve_order(["c", "a", "b"]), ["c", "a", "b"])

    def test_removes_empty_strings(self) -> None:
        self.assertEqual(dedupe_preserve_order(["a", "", "b", ""]), ["a", "b"])

    def test_empty_list(self) -> None:
        self.assertEqual(dedupe_preserve_order([]), [])

    def test_all_duplicates(self) -> None:
        self.assertEqual(dedupe_preserve_order(["x", "x", "x"]), ["x"])


class TestBuildGitLsRemoteHeadsCommand(unittest.TestCase):
    def test_targets_correct_remote_and_branch(self) -> None:
        cmd = build_git_ls_remote_heads_command("origin", "task/AT-001")
        self.assertEqual(cmd, ["git", "ls-remote", "--heads", "origin", "task/AT-001"])

    def test_custom_remote(self) -> None:
        cmd = build_git_ls_remote_heads_command("upstream", "feature/x")
        self.assertIn("upstream", cmd)
        self.assertIn("feature/x", cmd)
        self.assertEqual(cmd[0], "git")

    def test_does_not_contain_delete_flag(self) -> None:
        cmd = build_git_ls_remote_heads_command("origin", "task/AT-001")
        self.assertNotIn("--delete", cmd)
        self.assertNotIn("push", cmd)


class TestBuildGitPushDeleteCommand(unittest.TestCase):
    def test_targets_correct_remote_and_branch(self) -> None:
        cmd = build_git_push_delete_command("origin", "task/AT-001")
        self.assertEqual(cmd, ["git", "push", "origin", "--delete", "task/AT-001"])

    def test_does_not_force_push(self) -> None:
        cmd = build_git_push_delete_command("origin", "task/AT-001")
        self.assertNotIn("--force", cmd)
        self.assertNotIn("--force-with-lease", cmd)
        self.assertNotIn("-f", cmd)

    def test_does_not_push_to_protected_branch(self) -> None:
        # The command is built from the branch argument; callers are responsible
        # for not passing protected branches, but the builder itself must not
        # silently alter the target.
        cmd = build_git_push_delete_command("origin", "task/AT-001")
        self.assertNotIn("main", cmd)
        self.assertNotIn("master", cmd)
        self.assertNotIn("trunk", cmd)

    def test_branch_is_last_argument(self) -> None:
        cmd = build_git_push_delete_command("origin", "task/AT-001")
        self.assertEqual(cmd[-1], "task/AT-001")


class TestSafetyBlock(unittest.TestCase):
    def test_false_fields_when_not_performed(self) -> None:
        block = safety_block(
            human_confirmation_confirmed=False,
            remote_branch_cleanup_performed=False,
            remote_branch_deleted=False,
        )
        self.assertFalse(block["human_confirmation_confirmed"])
        self.assertFalse(block["remote_branch_cleanup_performed"])
        self.assertFalse(block["remote_branch_deleted"])
        self.assertFalse(block["issue_closed"])
        self.assertFalse(block["task_status_changed"])
        self.assertFalse(block["task_archived"])
        self.assertFalse(block["task_completed"])
        self.assertFalse(block["merged"])
        self.assertFalse(block["approved"])
        self.assertFalse(block["force_delete"])

    def test_human_confirmation_required_always_true(self) -> None:
        for confirmed in (True, False):
            block = safety_block(
                human_confirmation_confirmed=confirmed,
                remote_branch_cleanup_performed=False,
                remote_branch_deleted=False,
            )
            self.assertTrue(block["human_confirmation_required"])

    def test_performed_fields_set_correctly(self) -> None:
        block = safety_block(
            human_confirmation_confirmed=True,
            remote_branch_cleanup_performed=True,
            remote_branch_deleted=True,
        )
        self.assertTrue(block["human_confirmation_confirmed"])
        self.assertTrue(block["remote_branch_cleanup_performed"])
        self.assertTrue(block["remote_branch_deleted"])

    def test_no_background_worker_or_webhook(self) -> None:
        block = safety_block(
            human_confirmation_confirmed=True,
            remote_branch_cleanup_performed=True,
            remote_branch_deleted=True,
        )
        self.assertFalse(block["background_worker_started"])
        self.assertFalse(block["webhook_started"])
        self.assertFalse(block["polling_loop_started"])


class TestEmptyDicts(unittest.TestCase):
    def test_empty_cleanup_recommendation_shape(self) -> None:
        rec = empty_cleanup_recommendation()
        self.assertFalse(rec["available"])
        self.assertFalse(rec["merged"])
        self.assertFalse(rec["remote_branch_cleanup_recommended"])
        self.assertIsNone(rec["status"])
        self.assertIsInstance(rec["recommended_cleanup"], list)
        self.assertIsInstance(rec["blocking_warnings"], list)

    def test_empty_draft_pr_evidence_shape(self) -> None:
        evidence = empty_draft_pr_evidence()
        self.assertFalse(evidence["available"])
        self.assertFalse(evidence["artifact_recorded"])
        self.assertFalse(evidence["event_recorded"])
        self.assertIsNone(evidence["repo"])
        self.assertIsNone(evidence["pr_number"])
        self.assertIsNone(evidence["head_branch"])
        self.assertIsInstance(evidence["warnings"], list)
        self.assertTrue(len(evidence["warnings"]) > 0)

    def test_empty_local_cleanup_evidence_shape(self) -> None:
        evidence = empty_local_cleanup_evidence()
        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["event_type"], LOCAL_EVENT_TYPE)
        self.assertEqual(evidence["artifact_kind"], LOCAL_ARTIFACT_KIND)
        self.assertEqual(evidence["confirmation_flag"], LOCAL_CONFIRM_FLAG)
        self.assertIsNone(evidence["local_branch"])
        self.assertIsInstance(evidence["payload"], dict)
        self.assertIsInstance(evidence["warnings"], list)
        self.assertTrue(len(evidence["warnings"]) > 0)

    def test_empty_remote_branch_shape(self) -> None:
        rb = empty_remote_branch("origin")
        self.assertFalse(rb["available"])
        self.assertEqual(rb["remote"], "origin")
        self.assertIsNone(rb["name"])
        self.assertFalse(rb["exists_before"])
        self.assertFalse(rb["exists_after"])
        self.assertFalse(rb["safe_to_delete"])
        self.assertFalse(rb["deleted"])
        self.assertFalse(rb["delete_attempted"])
        self.assertFalse(rb["protected"])

    def test_empty_remote_branch_with_branch_name(self) -> None:
        rb = empty_remote_branch("origin", branch="task/AT-001")
        self.assertEqual(rb["name"], "task/AT-001")


class TestLatestEventPayload(unittest.TestCase):
    @dataclass
    class FakeEvent:
        payload_json: str | None

    def test_empty_events_returns_empty_dict(self) -> None:
        self.assertEqual(latest_event_payload([]), {})

    def test_none_payload_json_returns_empty_dict(self) -> None:
        event = self.FakeEvent(payload_json=None)
        self.assertEqual(latest_event_payload([event]), {})

    def test_empty_payload_json_returns_empty_dict(self) -> None:
        event = self.FakeEvent(payload_json="")
        self.assertEqual(latest_event_payload([event]), {})

    def test_malformed_json_returns_empty_dict_safely(self) -> None:
        event = self.FakeEvent(payload_json="{not valid json")
        result = latest_event_payload([event])
        self.assertEqual(result, {})

    def test_valid_json_object_returned(self) -> None:
        import json
        payload = {"kind": "test", "value": 42}
        event = self.FakeEvent(payload_json=json.dumps(payload))
        result = latest_event_payload([event])
        self.assertEqual(result["kind"], "test")
        self.assertEqual(result["value"], 42)

    def test_json_array_returns_empty_dict(self) -> None:
        event = self.FakeEvent(payload_json='["a", "b"]')
        result = latest_event_payload([event])
        self.assertEqual(result, {})

    def test_uses_last_event(self) -> None:
        import json
        e1 = self.FakeEvent(payload_json=json.dumps({"seq": 1}))
        e2 = self.FakeEvent(payload_json=json.dumps({"seq": 2}))
        result = latest_event_payload([e1, e2])
        self.assertEqual(result["seq"], 2)


class TestCleanupRecommendationSnapshot(unittest.TestCase):
    @dataclass
    class FakeRecommendation:
        ok: bool
        status: str
        recommended_cleanup: list[Any]
        blocking_warnings: list[str]
        non_blocking_warnings: list[str]
        next_allowed_actions: list[str]
        actions_not_performed: list[str]
        summary: dict[str, Any]
        safety: dict[str, Any]

    def _make_rec(
        self,
        *,
        ok: bool = True,
        merged: bool = True,
        remote_branch_recommended: bool = True,
    ) -> "TestCleanupRecommendationSnapshot.FakeRecommendation":
        cleanup_item = {
            "action": "delete_remote_branch",
            "recommended": remote_branch_recommended,
        }
        return self.FakeRecommendation(
            ok=ok,
            status="cleanup_recommended" if ok else "blocked",
            recommended_cleanup=[cleanup_item],
            blocking_warnings=[],
            non_blocking_warnings=[],
            next_allowed_actions=["confirm remote branch cleanup"],
            actions_not_performed=["merge", "close issue"],
            summary={"merged": merged},
            safety={"force_delete": False},
        )

    def test_available_when_ok(self) -> None:
        snap = cleanup_recommendation_snapshot(self._make_rec(ok=True))
        self.assertTrue(snap["available"])

    def test_not_available_when_not_ok(self) -> None:
        snap = cleanup_recommendation_snapshot(self._make_rec(ok=False))
        self.assertFalse(snap["available"])

    def test_merged_from_summary(self) -> None:
        snap = cleanup_recommendation_snapshot(self._make_rec(merged=True))
        self.assertTrue(snap["merged"])
        snap2 = cleanup_recommendation_snapshot(self._make_rec(merged=False))
        self.assertFalse(snap2["merged"])

    def test_remote_branch_cleanup_recommended_true(self) -> None:
        snap = cleanup_recommendation_snapshot(self._make_rec(remote_branch_recommended=True))
        self.assertTrue(snap["remote_branch_cleanup_recommended"])

    def test_remote_branch_cleanup_recommended_false(self) -> None:
        snap = cleanup_recommendation_snapshot(self._make_rec(remote_branch_recommended=False))
        self.assertFalse(snap["remote_branch_cleanup_recommended"])

    def test_snapshot_keys_present(self) -> None:
        snap = cleanup_recommendation_snapshot(self._make_rec())
        expected_keys = {
            "available", "status", "merged", "remote_branch_cleanup_recommended",
            "recommended_cleanup", "blocking_warnings", "non_blocking_warnings",
            "next_allowed_actions", "actions_not_performed", "summary", "safety",
        }
        self.assertTrue(expected_keys.issubset(snap.keys()))


if __name__ == "__main__":
    unittest.main()
