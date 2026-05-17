#!/usr/bin/env python3
"""Run the local issue-to-PR-handoff golden-path smoke.

This smoke is local-only. It reuses the issue-to-prepared-workspace smoke to
prove ingestion, explicit workspace preparation, dispatcher execution,
validation, artifact readback, and review evidence, then creates a local PR
handoff package. It does not call GitHub, create pull requests, push, merge,
run external AI executors, start webhooks, or add background workers.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.pr_handoff import PrHandoffRequest, create_pr_handoff
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key


DEFAULT_REPO = "anderson930420/agent-taskflow"
DEFAULT_ISSUE_NUMBER = 9201
DEFAULT_TASK_KEY = f"AT-PR-HANDOFF-{DEFAULT_ISSUE_NUMBER}"
ISSUE_SMOKE_PATH = REPO_ROOT / "scripts" / "run_issue_to_prepared_workspace_smoke.py"


class SmokeFailure(RuntimeError):
    """Raised when the PR handoff smoke fails an invariant."""


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return resolved


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _load_issue_smoke_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "run_issue_to_prepared_workspace_smoke",
        ISSUE_SMOKE_PATH,
    )
    if spec is None or spec.loader is None:
        raise SmokeFailure(f"Unable to load issue smoke module: {ISSUE_SMOKE_PATH}")
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


def _load_handoff_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SmokeFailure(f"pr_handoff.json missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SmokeFailure("pr_handoff.json must contain a JSON object")
    return data


def _verify_handoff_json(
    data: dict[str, Any],
    *,
    task_key: str,
    base_sha: str,
) -> None:
    required_fields = {
        "schema_version",
        "artifact_type",
        "task_key",
        "task_status",
        "branch",
        "base_branch",
        "base_sha",
        "head_sha",
        "changed_files",
        "validation_summary",
        "executor_summary",
        "artifact_summary",
        "review_evidence_summary",
        "proposed_pr",
        "safety",
    }
    missing = sorted(required_fields - set(data))
    _require(not missing, f"pr_handoff.json missing fields: {missing}")
    _require(data["artifact_type"] == "pr_handoff", "handoff artifact_type mismatch")
    _require(data["task_key"] == task_key, "handoff task_key mismatch")
    _require(data["task_status"] == "waiting_approval", "handoff task_status mismatch")
    _require(bool(data["branch"]), "handoff branch missing")
    _require(data["base_branch"] == "main", "handoff base_branch mismatch")
    _require(data["base_sha"] == base_sha, "handoff base_sha mismatch")
    _require(bool(data["head_sha"]), "handoff head_sha missing")
    _require(bool(data["changed_files"]), "handoff changed_files missing")
    _require(isinstance(data["validation_summary"], dict), "validation_summary missing")
    _require(isinstance(data["executor_summary"], dict), "executor_summary missing")
    _require(isinstance(data["artifact_summary"], dict), "artifact_summary missing")
    _require(
        isinstance(data["review_evidence_summary"], dict),
        "review_evidence_summary missing",
    )

    proposed_pr = data["proposed_pr"]
    _require(isinstance(proposed_pr, dict), "proposed_pr must be an object")
    _require(proposed_pr.get("draft_recommended") is True, "draft PR not recommended")
    command_preview = str(proposed_pr.get("create_command_preview", ""))
    inert_phrase = " ".join(["gh", "pr", "create"])
    _require(inert_phrase in command_preview, "create_command_preview missing inert gh text")
    _require(proposed_pr.get("base_branch") == "main", "proposed PR base mismatch")
    _require(proposed_pr.get("head_branch") == data["branch"], "proposed PR head mismatch")

    safety = data["safety"]
    _require(safety.get("pr_created") is False, "safety.pr_created must be false")
    _require(safety.get("pushed") is False, "safety.pushed must be false")
    _require(safety.get("merged") is False, "safety.merged must be false")
    _require(
        safety.get("cleanup_performed") is False,
        "safety.cleanup_performed must be false",
    )
    _require(safety.get("github_mutated") is False, "safety.github_mutated must be false")
    _require(
        safety.get("human_review_required") is True,
        "safety.human_review_required must be true",
    )


def _verify_handoff_markdown(path: Path) -> str:
    if not path.is_file():
        raise SmokeFailure(f"pr_handoff.md missing: {path}")
    text = path.read_text(encoding="utf-8")
    required_phrases = [
        "Task Summary",
        "Branch / Worktree / Base",
        "Validation Status",
        "Executor Run Summary",
        "Artifact List",
        "Changed Files",
        "Proposed PR",
        "Manual Next Steps",
        "Safety Warning",
        "This package did not create a PR.",
        "This package did not push.",
        "This package did not merge.",
    ]
    for phrase in required_phrases:
        _require(phrase in text, f"pr_handoff.md missing phrase: {phrase}")
    return text


def _verify_store_records(
    store: TaskMirrorStore,
    *,
    task_key: str,
    json_path: Path,
) -> tuple[bool, bool]:
    artifact_seen = any(
        artifact.artifact_type == "pr_handoff" and artifact.path == json_path
        for artifact in store.list_task_artifacts(task_key)
    )
    event_seen = any(
        event.event_type == "pr_handoff_created"
        for event in store.list_task_events(task_key)
    )
    _require(artifact_seen, "pr_handoff artifact record missing")
    _require(event_seen, "pr_handoff_created event missing")
    return artifact_seen, event_seen


def _review_evidence_available(db_path: Path, task_key: str) -> bool:
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        payload = _assert_response(
            client.get(f"/api/tasks/{task_key}/review-evidence"),
            200,
            "review evidence after handoff",
        )
    item = payload.get("item", {})
    return bool(
        item.get("mission_contract")
        and item.get("validator_results")
        and item.get("artifacts")
    )


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    issue_number: int = DEFAULT_ISSUE_NUMBER,
    skip_handoff_for_test: bool = False,
) -> dict[str, Any]:
    """Run the full local issue-to-PR-handoff smoke."""

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    workspace_root.mkdir(parents=True, exist_ok=True)

    issue_smoke = _load_issue_smoke_module()
    issue_summary = issue_smoke.run_smoke(
        workspace_root=workspace_root,
        task_key=normalized_task_key,
        issue_number=issue_number,
    )
    _require(
        issue_summary.get("final_status") == "waiting_approval",
        "issue-to-prepared-workspace flow did not reach waiting_approval",
    )
    _require(
        issue_summary.get("review_evidence_available") is True,
        "review evidence was unavailable before handoff",
    )

    db_path = Path(str(issue_summary["db_path"]))
    store = TaskMirrorStore(db_path)
    task = store.get_task(normalized_task_key)
    _require(task is not None, "task missing after issue smoke")

    if skip_handoff_for_test:
        raise SmokeFailure("PR handoff was skipped after waiting_approval")

    output_dir = workspace_root / "pr-handoff"
    handoff = create_pr_handoff(
        PrHandoffRequest(
            task_key=normalized_task_key,
            db_path=db_path,
            output_dir=output_dir,
            repo=DEFAULT_REPO,
        ),
        store=store,
    )
    _require(handoff.ok, "PR handoff result was not ok")

    handoff_json = _load_handoff_json(handoff.json_path)
    _verify_handoff_json(
        handoff_json,
        task_key=normalized_task_key,
        base_sha=str(issue_summary["base_sha"]),
    )
    _verify_handoff_markdown(handoff.markdown_path)
    artifact_seen, event_seen = _verify_store_records(
        store,
        task_key=normalized_task_key,
        json_path=handoff.json_path,
    )

    review_after_handoff = _review_evidence_available(db_path, normalized_task_key)
    _require(review_after_handoff, "review evidence unavailable after handoff")

    proposed_pr = handoff_json["proposed_pr"]
    return {
        "ok": True,
        "db_path": str(db_path),
        "repo_path": issue_summary["repo_path"],
        "task_key": normalized_task_key,
        "issue_number": issue_number,
        "issue_spec_path": issue_summary["issue_spec_path"],
        "ingestion_event_seen": issue_summary["ingestion_event_seen"],
        "issue_spec_artifact_seen": issue_summary["issue_spec_artifact_seen"],
        "worktree_path": issue_summary["worktree_path"],
        "branch": issue_summary["branch"],
        "base_branch": issue_summary["base_branch"],
        "base_sha": issue_summary["base_sha"],
        "head_sha": handoff_json["head_sha"],
        "changed_files": handoff_json["changed_files"],
        "final_status": issue_summary["final_status"],
        "review_evidence_available": review_after_handoff,
        "handoff_status": handoff.status,
        "pr_handoff_json_path": str(handoff.json_path),
        "pr_handoff_markdown_path": str(handoff.markdown_path),
        "pr_handoff_artifact_seen": artifact_seen,
        "pr_handoff_event_seen": event_seen,
        "proposed_pr_title": proposed_pr.get("title"),
        "proposed_pr_base_branch": proposed_pr.get("base_branch"),
        "proposed_pr_head_branch": proposed_pr.get("head_branch"),
        "proposed_pr_draft_recommended": proposed_pr.get("draft_recommended"),
        "create_command_preview": proposed_pr.get("create_command_preview"),
        "safety": handoff_json["safety"],
        "validation_summary": handoff_json["validation_summary"],
        "executor_summary": handoff_json["executor_summary"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local issue-to-PR-handoff golden-path smoke.",
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
        "--skip-handoff-for-test",
        action="store_true",
        help="Testing-only failure path: skip PR handoff after waiting_approval.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.workspace_root:
        workspace_root = _require_absolute_path(args.workspace_root, "workspace_root")
    else:
        workspace_root = Path(tempfile.mkdtemp(prefix="agent-taskflow-pr-handoff-smoke-"))

    try:
        summary = run_smoke(
            workspace_root=workspace_root,
            task_key=args.task_key,
            issue_number=args.issue_number,
            skip_handoff_for_test=args.skip_handoff_for_test,
        )
        summary["workspace_kept"] = True
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"PR handoff golden-path smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
