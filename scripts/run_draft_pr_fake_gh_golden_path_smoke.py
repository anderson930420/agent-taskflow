#!/usr/bin/env python3
"""Run the local issue-to-draft-PR-evidence golden-path smoke with fake gh.

This smoke is local-only. It reuses the PR handoff golden-path smoke, then
injects a fake gh runner into draft PR creation to prove the explicit
create-and-verify command contract without touching GitHub.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.draft_pr import DraftPrCreationRequest, create_draft_pr
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


DEFAULT_REPO = "anderson930420/agent-taskflow"
DEFAULT_ISSUE_NUMBER = 9401
DEFAULT_TASK_KEY = f"AT-DRAFT-PR-FAKE-GH-{DEFAULT_ISSUE_NUMBER}"
FAKE_PR_URL = "https://github.com/anderson930420/agent-taskflow/pull/9999"
FAKE_PR_NUMBER = 9999
HANDOFF_SMOKE_PATH = REPO_ROOT / "scripts" / "run_pr_handoff_golden_path_smoke.py"


class SmokeFailure(RuntimeError):
    """Raised when the fake-gh draft PR smoke fails an invariant."""


class FakeCompletedProcess:
    def __init__(self, *, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeGhRunner:
    """Strict fake runner for the expected draft PR create/view command pair."""

    def __init__(
        self,
        *,
        expected_repo: str,
        expected_base: str,
        expected_head: str,
        fake_create_missing_url: bool = False,
        fake_view_non_draft: bool = False,
    ) -> None:
        self.expected_repo = expected_repo
        self.expected_base = expected_base
        self.expected_head = expected_head
        self.fake_create_missing_url = fake_create_missing_url
        self.fake_view_non_draft = fake_view_non_draft
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeCompletedProcess:
        if kwargs.get("shell") is not False:
            raise SmokeFailure("fake gh runner requires shell=False")
        if not isinstance(args, list):
            raise SmokeFailure("fake gh runner expected argv list")
        self._reject_forbidden(args)
        self.calls.append(args)

        if len(self.calls) == 1:
            self._verify_create(args)
            stdout = "created draft pull request\n" if self.fake_create_missing_url else f"{FAKE_PR_URL}\n"
            return FakeCompletedProcess(returncode=0, stdout=stdout)
        if len(self.calls) == 2:
            self._verify_view(args)
            payload = {
                "url": FAKE_PR_URL,
                "number": FAKE_PR_NUMBER,
                "headRefName": self.expected_head,
                "baseRefName": self.expected_base,
                "isDraft": not self.fake_view_non_draft,
            }
            return FakeCompletedProcess(returncode=0, stdout=json.dumps(payload))
        raise SmokeFailure(f"unexpected extra fake gh command: {args!r}")

    @staticmethod
    def _reject_forbidden(args: list[str]) -> None:
        command_text = " ".join(args)
        forbidden_phrases = [
            " ".join(["git", "push"]),
            " ".join(["gh", "pr", "merge"]),
            " ".join(["gh", "pr", "review", "--approve"]),
            " ".join(["gh", "issue", "edit"]),
            " ".join(["git", "merge"]),
            " ".join(["git", "rebase"]),
            " ".join(["git", "branch", "-d"]),
            " ".join(["git", "branch", "-D"]),
            " ".join(["git", "worktree", "remove"]),
            " ".join(["git", "reset", "--hard"]),
            " ".join(["force", "push"]),
            "_".join(["delete", "branch"]),
            "_".join(["delete", "worktree"]),
        ]
        for phrase in forbidden_phrases:
            if phrase in command_text:
                raise SmokeFailure(f"forbidden command observed: {phrase}")

    def _verify_create(self, args: list[str]) -> None:
        expected_prefix = ["gh", "pr", "create"]
        if args[:3] != expected_prefix:
            raise SmokeFailure(f"unexpected fake gh create command: {args!r}")
        if "--json" in args:
            raise SmokeFailure("gh pr create must not include --json")
        self._require_flag(args, "--draft")
        self._require_flag_value(args, "--repo", self.expected_repo)
        self._require_flag_value(args, "--base", self.expected_base)
        self._require_flag_value(args, "--head", self.expected_head)
        self._require_non_empty_flag(args, "--title")
        self._require_non_empty_flag(args, "--body")

    def _verify_view(self, args: list[str]) -> None:
        if args[:4] != ["gh", "pr", "view", FAKE_PR_URL]:
            raise SmokeFailure(f"unexpected fake gh view command: {args!r}")
        self._require_flag_value(args, "--repo", self.expected_repo)
        self._require_flag_value(args, "--json", "url,number,headRefName,baseRefName,isDraft")

    @staticmethod
    def _require_flag(args: list[str], flag: str) -> None:
        if flag not in args:
            raise SmokeFailure(f"expected fake gh command flag missing: {flag}")

    @staticmethod
    def _require_flag_value(args: list[str], flag: str, expected: str) -> None:
        if flag not in args:
            raise SmokeFailure(f"expected fake gh command flag missing: {flag}")
        index = args.index(flag)
        if index + 1 >= len(args) or args[index + 1] != expected:
            raise SmokeFailure(f"expected {flag} {expected!r}, got {args!r}")

    @staticmethod
    def _require_non_empty_flag(args: list[str], flag: str) -> None:
        if flag not in args:
            raise SmokeFailure(f"expected fake gh command flag missing: {flag}")
        index = args.index(flag)
        if index + 1 >= len(args) or not args[index + 1]:
            raise SmokeFailure(f"expected non-empty {flag} value")

    @property
    def create_command(self) -> list[str]:
        return self.calls[0] if len(self.calls) >= 1 else []

    @property
    def view_command(self) -> list[str]:
        return self.calls[1] if len(self.calls) >= 2 else []


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return resolved


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _load_handoff_smoke_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "run_pr_handoff_golden_path_smoke",
        HANDOFF_SMOKE_PATH,
    )
    if spec is None or spec.loader is None:
        raise SmokeFailure(f"Unable to load handoff smoke module: {HANDOFF_SMOKE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_response(response: Any, expected_status: int, action: str) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise SmokeFailure(
            f"{action} returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{action} returned non-object JSON: {payload!r}")
    return payload


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise SmokeFailure(f"{label} missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SmokeFailure(f"{label} must contain a JSON object")
    return data


def _review_evidence_available(db_path: Path, task_key: str) -> bool:
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        payload = _assert_response(
            client.get(f"/api/tasks/{task_key}/review-evidence"),
            200,
            "review evidence after draft PR evidence",
        )
    item = payload.get("item", {})
    return bool(
        item.get("mission_contract")
        and item.get("validator_results")
        and item.get("artifacts")
    )


def _verify_draft_pr_json(
    data: dict[str, Any],
    *,
    task_key: str,
    repo: str,
    base_branch: str,
    head_branch: str,
    handoff_json_path: Path,
) -> None:
    _require(data.get("kind") == "draft_pr_created", "draft_pr kind mismatch")
    _require(data.get("artifact_type") == "draft_pr", "draft_pr artifact_type mismatch")
    _require(data.get("task_key") == task_key, "draft_pr task_key mismatch")
    _require(data.get("repo") == repo, "draft_pr repo mismatch")
    _require(data.get("pr_url") == FAKE_PR_URL, "draft_pr pr_url mismatch")
    _require(data.get("pr_number") == FAKE_PR_NUMBER, "draft_pr pr_number mismatch")
    _require(data.get("is_draft") is True, "draft_pr is_draft must be true")
    _require(data.get("base_branch") == base_branch, "draft_pr base_branch mismatch")
    _require(data.get("head_branch") == head_branch, "draft_pr head_branch mismatch")
    _require(bool(data.get("title")), "draft_pr title missing")
    _require(bool(data.get("command_preview")), "draft_pr command_preview missing")
    _require(
        data.get("handoff_json_path") == str(handoff_json_path),
        "draft_pr handoff_json_path mismatch",
    )
    safety = data.get("safety", {})
    _require(safety.get("pr_created") is True, "draft_pr safety.pr_created must be true")
    _require(safety.get("pushed") is False, "draft_pr safety.pushed must be false")
    _require(safety.get("merged") is False, "draft_pr safety.merged must be false")
    _require(
        safety.get("cleanup_performed") is False,
        "draft_pr safety.cleanup_performed must be false",
    )
    _require(
        safety.get("human_review_required") is True,
        "draft_pr safety.human_review_required must be true",
    )


def _verify_handoff_conservative(data: dict[str, Any]) -> None:
    safety = data.get("safety", {})
    _require(safety.get("pr_created") is False, "handoff safety.pr_created changed")
    _require(safety.get("github_mutated") is False, "handoff safety.github_mutated changed")


def _verify_store_records(
    store: TaskMirrorStore,
    *,
    task_key: str,
    draft_pr_json_path: Path,
) -> tuple[bool, bool]:
    artifact_seen = any(
        artifact.artifact_type == "draft_pr" and artifact.path == draft_pr_json_path
        for artifact in store.list_task_artifacts(task_key)
    )
    event_seen = any(
        event.event_type == "draft_pr_created"
        for event in store.list_task_events(task_key)
    )
    _require(artifact_seen, "draft_pr artifact record missing")
    _require(event_seen, "draft_pr_created event missing")
    return artifact_seen, event_seen


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    issue_number: int = DEFAULT_ISSUE_NUMBER,
    skip_draft_pr_for_test: bool = False,
    fake_view_non_draft_for_test: bool = False,
    fake_create_missing_url_for_test: bool = False,
) -> dict[str, Any]:
    """Run the local issue-to-draft-PR-evidence smoke with fake gh."""

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    workspace_root.mkdir(parents=True, exist_ok=True)

    handoff_smoke = _load_handoff_smoke_module()
    handoff_summary = handoff_smoke.run_smoke(
        workspace_root=workspace_root,
        task_key=normalized_task_key,
        issue_number=issue_number,
    )
    _require(
        handoff_summary.get("final_status") == "waiting_approval",
        "handoff flow did not reach waiting_approval",
    )
    _require(
        handoff_summary.get("handoff_status") == "created",
        "handoff package was not created",
    )

    db_path = Path(str(handoff_summary["db_path"]))
    store = TaskMirrorStore(db_path)
    handoff_json_path = Path(str(handoff_summary["pr_handoff_json_path"]))
    handoff_before = _read_json_object(handoff_json_path, "pr_handoff.json before draft PR")
    _verify_handoff_conservative(handoff_before)

    if skip_draft_pr_for_test:
        raise SmokeFailure("Draft PR creation was skipped after PR handoff")

    base_branch = str(handoff_summary["proposed_pr_base_branch"])
    head_branch = str(handoff_summary["proposed_pr_head_branch"])
    fake_runner = FakeGhRunner(
        expected_repo=DEFAULT_REPO,
        expected_base=base_branch,
        expected_head=head_branch,
        fake_create_missing_url=fake_create_missing_url_for_test,
        fake_view_non_draft=fake_view_non_draft_for_test,
    )

    result = create_draft_pr(
        DraftPrCreationRequest(
            task_key=normalized_task_key,
            db_path=db_path,
            dry_run=False,
            confirm_create_pr=True,
        ),
        store=store,
        runner=fake_runner,
    )
    _require(result.ok, "draft PR creation result was not ok")
    _require(result.status == "created", "draft PR creation status was not created")
    _require(len(fake_runner.calls) == 2, "fake gh did not observe exactly two commands")

    draft_pr_json_path = result.draft_pr_json_path
    _require(draft_pr_json_path is not None, "draft_pr_json_path missing")
    draft_pr_json = _read_json_object(draft_pr_json_path, "draft_pr.json")
    _verify_draft_pr_json(
        draft_pr_json,
        task_key=normalized_task_key,
        repo=DEFAULT_REPO,
        base_branch=base_branch,
        head_branch=head_branch,
        handoff_json_path=handoff_json_path,
    )

    artifact_seen, event_seen = _verify_store_records(
        store,
        task_key=normalized_task_key,
        draft_pr_json_path=draft_pr_json_path,
    )

    handoff_after = _read_json_object(handoff_json_path, "pr_handoff.json after draft PR")
    handoff_unchanged = handoff_before == handoff_after
    _require(handoff_unchanged, "pr_handoff.json changed after draft PR evidence")
    _verify_handoff_conservative(handoff_after)

    review_after_draft = _review_evidence_available(db_path, normalized_task_key)
    _require(review_after_draft, "review evidence unavailable after draft PR evidence")

    return {
        "ok": True,
        "db_path": str(db_path),
        "repo_path": handoff_summary["repo_path"],
        "task_key": normalized_task_key,
        "issue_number": issue_number,
        "final_status": handoff_summary["final_status"],
        "worktree_path": handoff_summary["worktree_path"],
        "branch": handoff_summary["branch"],
        "base_branch": base_branch,
        "base_sha": handoff_summary["base_sha"],
        "pr_handoff_json_path": str(handoff_json_path),
        "draft_pr_json_path": str(draft_pr_json_path),
        "draft_pr_event_seen": event_seen,
        "draft_pr_artifact_seen": artifact_seen,
        "fake_pr_url": FAKE_PR_URL,
        "fake_pr_number": FAKE_PR_NUMBER,
        "fake_create_command_seen": bool(fake_runner.create_command),
        "fake_view_command_seen": bool(fake_runner.view_command),
        "gh_create_command": fake_runner.create_command,
        "gh_view_command": fake_runner.view_command,
        "review_evidence_available": review_after_draft,
        "handoff_unchanged": handoff_unchanged,
        "safety": draft_pr_json["safety"],
        "handoff_safety": handoff_after["safety"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local fake-gh draft PR golden-path smoke.",
    )
    parser.add_argument(
        "--task-key",
        default=DEFAULT_TASK_KEY,
        help=f"Task key to use. Default: {DEFAULT_TASK_KEY}",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=DEFAULT_ISSUE_NUMBER,
        help=f"Offline issue number to use. Default: {DEFAULT_ISSUE_NUMBER}",
    )
    parser.add_argument(
        "--workspace-root",
        help=(
            "Absolute workspace root to use. By default a temporary directory "
            "under /tmp is created and preserved for proof-of-work inspection."
        ),
    )
    parser.add_argument(
        "--skip-draft-pr-for-test",
        action="store_true",
        help="Testing-only failure path: skip draft PR creation after handoff.",
    )
    parser.add_argument(
        "--fake-view-non-draft-for-test",
        action="store_true",
        help="Testing-only failure path: fake gh view reports a non-draft PR.",
    )
    parser.add_argument(
        "--fake-create-missing-url-for-test",
        action="store_true",
        help="Testing-only failure path: fake gh create omits the PR URL.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.workspace_root:
        workspace_root = _require_absolute_path(args.workspace_root, "workspace_root")
    else:
        workspace_root = Path(tempfile.mkdtemp(prefix="agent-taskflow-draft-pr-fake-gh-smoke-"))

    try:
        summary = run_smoke(
            workspace_root=workspace_root,
            task_key=args.task_key,
            issue_number=args.issue_number,
            skip_draft_pr_for_test=args.skip_draft_pr_for_test,
            fake_view_non_draft_for_test=args.fake_view_non_draft_for_test,
            fake_create_missing_url_for_test=args.fake_create_missing_url_for_test,
        )
        summary["workspace_kept"] = True
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Draft PR fake-gh golden-path smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
