"""Focused unit tests for the pure helpers in draft_pr_confirm_helpers."""

from __future__ import annotations

import json
import unittest

from agent_taskflow.draft_pr_confirm_helpers import (
    DraftPrConfirmError,
    body_preview,
    build_gh_create_command,
    build_gh_list_open_pr_command,
    build_gh_view_command,
    build_verification_result,
    command_preview,
    dedupe_preserve_order,
    empty_verification_preview,
    empty_verification_result,
    extract_pr_commit_oids,
    extract_pr_file_paths,
    extract_pr_url,
    normalize_branch_choice,
    normalize_repo,
    parse_event_payload,
    parse_json_array,
    parse_json_object,
    stringify_list,
)


def _expected(
    *,
    base: str = "main",
    head: str = "feature/x",
    title: str = "T",
    files: list[str] | None = None,
    commits: list[str] | None = None,
) -> dict:
    preview = empty_verification_preview()
    preview.update(
        {
            "expected_repo": "owner/repo",
            "expected_base": base,
            "expected_head": head,
            "expected_title": title,
            "expected_files": list(files or []),
            "expected_commits": list(commits or []),
        }
    )
    return preview


def _view_payload(
    *,
    number: int = 42,
    url: str = "https://github.com/owner/repo/pull/42",
    base: str = "main",
    head: str = "feature/x",
    title: str = "T",
    is_draft: bool = True,
    state: str = "OPEN",
    files: list[str] | None = None,
    commits: list[str] | None = None,
) -> dict:
    return {
        "number": number,
        "url": url,
        "baseRefName": base,
        "headRefName": head,
        "title": title,
        "isDraft": is_draft,
        "state": state,
        "files": [{"path": p} for p in (files or [])],
        "commits": [{"oid": c} for c in (commits or [])],
    }


class GhCommandBuildersTests(unittest.TestCase):
    def test_create_command_is_draft_and_uses_provided_values(self) -> None:
        cmd = build_gh_create_command(
            repo="owner/repo",
            base="main",
            head="feature/x",
            title="A title",
            body="A body",
        )
        self.assertEqual(cmd[:3], ["gh", "pr", "create"])
        self.assertIn("--draft", cmd)
        self.assertIn("owner/repo", cmd)
        self.assertIn("main", cmd)
        self.assertIn("feature/x", cmd)
        self.assertIn("A title", cmd)
        self.assertIn("A body", cmd)

    def test_view_command_requests_required_json_fields(self) -> None:
        cmd = build_gh_view_command("owner/repo", "https://example/pull/1")
        self.assertEqual(cmd[:3], ["gh", "pr", "view"])
        self.assertIn("--repo", cmd)
        self.assertIn("owner/repo", cmd)
        self.assertIn("https://example/pull/1", cmd)
        json_arg = cmd[cmd.index("--json") + 1]
        for field in (
            "url",
            "number",
            "headRefName",
            "baseRefName",
            "isDraft",
            "title",
            "state",
            "commits",
            "files",
        ):
            self.assertIn(field, json_arg)

    def test_list_open_pr_command_scopes_to_repo_head_and_state(self) -> None:
        cmd = build_gh_list_open_pr_command(repo="owner/repo", head="feature/x")
        self.assertEqual(cmd[:3], ["gh", "pr", "list"])
        self.assertIn("--state", cmd)
        self.assertEqual(cmd[cmd.index("--state") + 1], "open")
        self.assertEqual(cmd[cmd.index("--head") + 1], "feature/x")

    def test_command_preview_shell_quotes_arguments(self) -> None:
        rendered = command_preview(["gh", "pr", "create", "--title", "has space"])
        self.assertIn("'has space'", rendered)


class JsonParsingTests(unittest.TestCase):
    def test_parse_json_object_returns_dict(self) -> None:
        self.assertEqual(parse_json_object('{"a": 1}', source="x"), {"a": 1})

    def test_parse_json_object_rejects_array(self) -> None:
        with self.assertRaises(DraftPrConfirmError):
            parse_json_object("[1]", source="gh pr view")

    def test_parse_json_object_rejects_invalid_json(self) -> None:
        with self.assertRaises(DraftPrConfirmError):
            parse_json_object("not json", source="gh pr view")

    def test_parse_json_array_returns_list(self) -> None:
        self.assertEqual(parse_json_array("[1, 2]", source="x"), [1, 2])

    def test_parse_json_array_rejects_object(self) -> None:
        with self.assertRaises(DraftPrConfirmError):
            parse_json_array("{}", source="gh pr list")

    def test_parse_event_payload_filters_wrong_kind(self) -> None:
        payload = json.dumps({"kind": "other", "x": 1})
        self.assertEqual(parse_event_payload(payload, event_type="branch_push_completed"), {})

    def test_parse_event_payload_accepts_matching_kind(self) -> None:
        payload = json.dumps({"kind": "branch_push_completed", "x": 1})
        self.assertEqual(
            parse_event_payload(payload, event_type="branch_push_completed"),
            {"kind": "branch_push_completed", "x": 1},
        )

    def test_parse_event_payload_handles_missing_and_invalid(self) -> None:
        self.assertEqual(parse_event_payload(None, event_type="e"), {})
        self.assertEqual(parse_event_payload("not json", event_type="e"), {})
        self.assertEqual(parse_event_payload("[1]", event_type="e"), {})


class PrUrlExtractionTests(unittest.TestCase):
    def test_extracts_first_github_pr_url(self) -> None:
        text = "Created PR: https://github.com/owner/repo/pull/123\nmore text"
        self.assertEqual(extract_pr_url(text), "https://github.com/owner/repo/pull/123")

    def test_raises_when_no_url_present(self) -> None:
        with self.assertRaises(DraftPrConfirmError):
            extract_pr_url("gh did not print a URL")


class ListExtractorTests(unittest.TestCase):
    def test_extract_pr_file_paths_filters_non_dict_and_blank(self) -> None:
        files = [{"path": "a"}, "not a dict", {"path": "  "}, {"other": 1}, {"path": "b "}]
        self.assertEqual(extract_pr_file_paths(files), ["a", "b"])

    def test_extract_pr_file_paths_returns_empty_for_non_list(self) -> None:
        self.assertEqual(extract_pr_file_paths(None), [])

    def test_extract_pr_commit_oids_filters_non_dict_and_blank(self) -> None:
        commits = [{"oid": "abc"}, "x", {"oid": " "}, {"oid": "def "}]
        self.assertEqual(extract_pr_commit_oids(commits), ["abc", "def"])

    def test_stringify_list_filters_non_strings_and_blank(self) -> None:
        self.assertEqual(stringify_list(["a", 1, "", " b "]), ["a", "b"])
        self.assertEqual(stringify_list("not a list"), [])

    def test_dedupe_preserve_order(self) -> None:
        self.assertEqual(dedupe_preserve_order(["a", "b", "a", "c", "b"]), ["a", "b", "c"])


class RepoAndBranchNormalizationTests(unittest.TestCase):
    def test_normalize_repo_accepts_owner_slash_name(self) -> None:
        self.assertEqual(normalize_repo("  owner/repo  "), "owner/repo")

    def test_normalize_repo_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            normalize_repo("   ")

    def test_normalize_repo_rejects_flag_like(self) -> None:
        with self.assertRaises(ValueError):
            normalize_repo("--evil/repo")

    def test_normalize_repo_rejects_missing_slash(self) -> None:
        with self.assertRaises(ValueError):
            normalize_repo("ownerrepo")

    def test_normalize_branch_choice_uses_canonical_when_unprovided(self) -> None:
        self.assertEqual(
            normalize_branch_choice(provided=None, canonical="main", field_name="base"),
            "main",
        )

    def test_normalize_branch_choice_rejects_mismatch(self) -> None:
        with self.assertRaises(DraftPrConfirmError):
            normalize_branch_choice(provided="other", canonical="main", field_name="base")

    def test_normalize_branch_choice_rejects_empty_canonical(self) -> None:
        with self.assertRaises(DraftPrConfirmError):
            normalize_branch_choice(provided=None, canonical="", field_name="base")

    def test_normalize_branch_choice_rejects_empty_provided(self) -> None:
        with self.assertRaises(DraftPrConfirmError):
            normalize_branch_choice(provided="   ", canonical="main", field_name="base")


class BodyPreviewTests(unittest.TestCase):
    def test_collapses_whitespace_and_truncates(self) -> None:
        self.assertEqual(body_preview("  hello   world\n\nfoo "), "hello world foo")
        self.assertEqual(body_preview("abcdef", limit=3), "abc")


class VerificationResultTests(unittest.TestCase):
    def test_successful_verification_returns_expected_pr_metadata_shape(self) -> None:
        expected = _expected(files=["a.py", "b.py"], commits=["sha1", "sha2"])
        payload = _view_payload(
            files=["a.py", "b.py"],
            commits=["sha1", "sha2"],
        )

        result = build_verification_result(payload, expected=expected)

        self.assertTrue(result["passed"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["actual_number"], 42)
        self.assertEqual(result["actual_url"], "https://github.com/owner/repo/pull/42")
        self.assertEqual(result["actual_base"], "main")
        self.assertEqual(result["actual_head"], "feature/x")
        self.assertEqual(result["actual_title"], "T")
        self.assertTrue(result["draft_match"])
        self.assertTrue(result["state_match"])
        self.assertEqual(result["expected_files"], ["a.py", "b.py"])
        self.assertEqual(result["actual_files"], ["a.py", "b.py"])
        self.assertEqual(result["missing_files"], [])
        self.assertEqual(result["unexpected_files"], [])
        self.assertEqual(result["expected_commits"], ["sha1", "sha2"])
        self.assertEqual(result["actual_commits"], ["sha1", "sha2"])
        self.assertEqual(result["blocking_warnings"], [])

    def test_failed_verification_reports_each_mismatch_and_does_not_pass(self) -> None:
        expected = _expected(
            base="main",
            head="feature/x",
            title="Expected title",
            files=["a.py"],
            commits=["sha1"],
        )
        # Payload diverges from expected on every checked dimension.
        payload = _view_payload(
            base="other-base",
            head="other-head",
            title="Different title",
            is_draft=False,
            state="CLOSED",
            files=["a.py", "stray.py"],
            commits=["unexpected_sha"],
        )

        result = build_verification_result(payload, expected=expected)

        self.assertFalse(result["passed"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["base_match"])
        self.assertFalse(result["head_match"])
        self.assertFalse(result["title_match"])
        self.assertFalse(result["draft_match"])
        self.assertFalse(result["state_match"])
        self.assertFalse(result["files_match"])
        self.assertFalse(result["commits_match"])
        self.assertIn("stray.py", result["unexpected_files"])
        self.assertNotIn("a.py", result["missing_files"])
        self.assertEqual(result["missing_commits"], ["sha1"])
        self.assertEqual(result["unexpected_commits"], ["unexpected_sha"])
        warnings = result["blocking_warnings"]
        self.assertIn("GitHub PR baseRefName does not match handoff base", warnings)
        self.assertIn("GitHub PR headRefName does not match handoff head", warnings)
        self.assertIn("GitHub PR isDraft is not true", warnings)
        self.assertIn("GitHub PR state is not OPEN", warnings)
        self.assertIn("GitHub PR title does not match handoff title", warnings)
        self.assertIn("GitHub PR files do not match handoff changed_files", warnings)
        self.assertIn("GitHub PR commits do not match expected branch diff", warnings)

    def test_empty_verification_result_carries_expected_preview_through(self) -> None:
        preview = _expected(files=["a.py"], commits=["sha1"])
        empty = empty_verification_result(expected=preview)
        self.assertFalse(empty["performed"])
        self.assertFalse(empty["passed"])
        self.assertEqual(empty["expected_files"], ["a.py"])
        self.assertEqual(empty["missing_files"], ["a.py"])
        self.assertEqual(empty["expected_commits"], ["sha1"])
        self.assertEqual(empty["missing_commits"], ["sha1"])


if __name__ == "__main__":
    unittest.main()
