#!/usr/bin/env python3
"""Run a Mission Control golden-path smoke through the existing PiExecutor.

By default this script uses an isolated fake ``pi`` binary so it is safe for
automation. Running the real Pi CLI is explicit opt-in and requires
``--real-pi --confirm-real-pi``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient

from agent_taskflow.api.main import create_app
from agent_taskflow.dispatcher import Dispatcher
from agent_taskflow.store import TaskMirrorStore
from agent_taskflow.tasks import normalize_task_key
from agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult


DEFAULT_TASK_KEY = "AT-PI-GOLDEN-PATH"
DEFAULT_PROJECT = "agent-taskflow"
PI_VALIDATOR = "pi-golden-path"
EXPECTED_ARTIFACT_NAME = "pi_golden_path_result.txt"
EXPECTED_ARTIFACT_CONTENT = "pi-golden-path-ok"
VALIDATOR_LOG_NAME = "pi-golden-path-validator.log"

PROMPT_TEMPLATE = """Mission Control Pi executor golden-path smoke.

Write exactly one task result artifact:
{artifact_path}

The file content must be exactly:
{artifact_content}
Do not add extra whitespace.

Hard boundaries:
- Do not modify files outside artifact_dir: {artifact_dir}
- Do not modify the repository source.
- Do not push.
- Do not merge.
- Do not approve.
- Do not cleanup.
- Do not delete branches.
- Do not delete worktrees.
- Stop after writing the artifact file.
"""


class SmokeFailure(RuntimeError):
    """Raised when the Pi golden-path smoke does not meet expectations."""


class PiGoldenPathValidator(Validator):
    """Script-local validator for the Pi executor golden-path smoke."""

    name = PI_VALIDATOR

    def __init__(
        self,
        *,
        expected_artifact_name: str = EXPECTED_ARTIFACT_NAME,
        expected_content: str = EXPECTED_ARTIFACT_CONTENT,
    ) -> None:
        self.expected_artifact_name = expected_artifact_name
        self.expected_content = expected_content

    def run(self, context: ValidatorContext) -> ValidatorResult:
        artifact_path = context.artifact_dir / self.expected_artifact_name
        contract_path = context.artifact_dir / "mission_contract.json"
        pi_log_path = context.artifact_dir / "pi-executor.log"
        pi_prompt_path = context.artifact_dir / "pi_mission_prompt.md"
        log_path = context.artifact_dir / VALIDATOR_LOG_NAME

        failures: list[str] = []
        if not artifact_path.is_file():
            failures.append(f"expected artifact missing: {artifact_path}")
        else:
            actual = artifact_path.read_text(encoding="utf-8")
            if actual != self.expected_content:
                failures.append(
                    "expected artifact content mismatch: "
                    f"expected={self.expected_content!r} actual={actual!r}"
                )

        if not contract_path.is_file():
            failures.append(f"mission_contract.json missing: {contract_path}")

        if not pi_log_path.is_file() and not pi_prompt_path.is_file():
            failures.append(
                "expected Pi executor log or protocol prompt artifact was not produced"
            )

        if failures:
            summary = "; ".join(failures)
            log_path.write_text(summary + "\n", encoding="utf-8")
            return ValidatorResult(
                validator=self.name,
                status="failed",
                exit_code=1,
                log_path=log_path,
                summary=summary,
                artifacts={"log": log_path},
            )

        summary = "Pi golden-path validator verified artifact, contract, and Pi evidence."
        log_path.write_text(summary + "\n", encoding="utf-8")
        return ValidatorResult(
            validator=self.name,
            status="passed",
            exit_code=0,
            log_path=log_path,
            summary=summary,
            artifacts={"log": log_path},
        )


def _require_absolute_path(path: str | Path, field_name: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field_name} must be absolute: {path}")
    return resolved


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _assert_response(response: Any, expected_status: int, action: str) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise SmokeFailure(
            f"{action} returned HTTP {response.status_code}: {response.text}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{action} returned non-object JSON: {payload!r}")
    return payload


def _artifact_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in payload.get("items", []):
        if "name" in item:
            names.add(str(item["name"]))
        if "path" in item:
            names.add(Path(str(item["path"])).name)
    return names


def _fake_pi_script(*, write_expected_artifact: bool = True) -> str:
    write_block = (
        "result_path.write_text('pi-golden-path-ok', encoding='utf-8')\n"
        if write_expected_artifact
        else "result_path.write_text('wrong-content\\n', encoding='utf-8')\n"
    )
    return (
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        "import re\n"
        "import sys\n"
        "from pathlib import Path\n"
        "prompt = ''\n"
        "args = sys.argv[1:]\n"
        "for index, arg in enumerate(args):\n"
        "    if arg == '-p' and index + 1 < len(args):\n"
        "        prompt = args[index + 1]\n"
        "        break\n"
        "match = re.search(r'Artifact directory:\\*\\* `([^`]+)`', prompt)\n"
        "if not match:\n"
        "    match = re.search(r'artifact_dir:\\s*([^\\n]+)', prompt)\n"
        "if not match:\n"
        "    print('fake pi could not find artifact directory in prompt')\n"
        "    sys.exit(2)\n"
        "artifact_dir = Path(match.group(1).strip().strip('`'))\n"
        "artifact_dir.mkdir(parents=True, exist_ok=True)\n"
        "result_path = artifact_dir / 'pi_golden_path_result.txt'\n"
        f"{write_block}"
        "print(f'fake pi wrote {result_path}')\n"
        "sys.exit(0)\n"
    )


def _write_fake_pi(
    bin_dir: Path,
    *,
    write_expected_artifact: bool = True,
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_pi = bin_dir / "pi"
    fake_pi.write_text(
        _fake_pi_script(write_expected_artifact=write_expected_artifact),
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    return fake_pi


def _make_dispatcher_factory() -> Any:
    def dispatcher_factory(
        store: TaskMirrorStore,
        validators: Sequence[str],
    ) -> Dispatcher:
        return Dispatcher(
            store,
            validator_registry={
                PI_VALIDATOR: PiGoldenPathValidator(),
            },
            validators=validators,
            default_executor="pi",
        )

    return dispatcher_factory


def _prepend_path(path: Path) -> str:
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{path}{os.pathsep}{original}" if original else str(path)
    return original


def run_smoke(
    *,
    workspace_root: Path,
    task_key: str = DEFAULT_TASK_KEY,
    project: str = DEFAULT_PROJECT,
    real_pi: bool = False,
    confirm_real_pi: bool = False,
    fake_pi_bin: Path | None = None,
    fake_pi_writes_expected_artifact: bool = True,
) -> dict[str, Any]:
    """Run the Pi golden-path smoke and return a compact summary."""

    if real_pi and not confirm_real_pi:
        raise SmokeFailure("real Pi smoke requires --confirm-real-pi")

    normalized_task_key = normalize_task_key(task_key)
    workspace_root = _require_absolute_path(workspace_root, "workspace_root")
    db_path = workspace_root / "pi-golden-path-smoke.db"
    repo_path = workspace_root / "repo"
    worktree_path = repo_path / ".worktrees" / normalized_task_key
    artifact_dir = workspace_root / "artifacts" / normalized_task_key
    prompt_path = artifact_dir / "implementation_prompt.md"
    expected_artifact_path = artifact_dir / EXPECTED_ARTIFACT_NAME

    worktree_path.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        PROMPT_TEMPLATE.format(
            artifact_path=expected_artifact_path,
            artifact_content=EXPECTED_ARTIFACT_CONTENT,
            artifact_dir=artifact_dir,
        ),
        encoding="utf-8",
    )

    fake_pi_path: Path | None = None
    original_path: str | None = None
    if not real_pi:
        if fake_pi_bin is not None:
            fake_pi_path = _require_absolute_path(fake_pi_bin, "fake_pi_bin")
            _require(fake_pi_path.is_file(), f"fake pi binary not found: {fake_pi_path}")
            _require(os.access(fake_pi_path, os.X_OK), f"fake pi binary is not executable: {fake_pi_path}")
            original_path = _prepend_path(fake_pi_path.parent)
        else:
            fake_pi_path = _write_fake_pi(
                workspace_root / "bin",
                write_expected_artifact=fake_pi_writes_expected_artifact,
            )
            original_path = _prepend_path(fake_pi_path.parent)

    app = create_app(
        db_path=db_path,
        dispatcher_factory=_make_dispatcher_factory(),
    )

    try:
        with TestClient(app) as client:
            health = _assert_response(client.get("/health"), 200, "health")
            _require(health.get("status") == "ok", "health endpoint did not return ok")

            create_payload = _assert_response(
                client.post(
                    "/api/tasks",
                    json={
                        "task_key": normalized_task_key,
                        "project": project,
                        "repo_path": str(repo_path),
                        "worktree_path": str(worktree_path),
                        "artifact_dir": str(artifact_dir),
                        "title": "Pi executor golden-path smoke",
                        "board": project,
                        "branch": f"smoke/{normalized_task_key}",
                        "base_branch": "main",
                    },
                ),
                200,
                "create task",
            )
            _require(create_payload.get("ok") is True, "task create response was not ok")

            start_payload = _assert_response(
                client.post(
                    f"/api/tasks/{normalized_task_key}/start",
                    json={
                        "executor": "pi",
                        "validators": [PI_VALIDATOR],
                    },
                ),
                200,
                "start task",
            )

            task_payload = _assert_response(
                client.get(f"/api/tasks/{normalized_task_key}"),
                200,
                "task readback",
            )
            runs_payload = _assert_response(
                client.get(f"/api/tasks/{normalized_task_key}/runs"),
                200,
                "runs readback",
            )
            validations_payload = _assert_response(
                client.get(f"/api/tasks/{normalized_task_key}/validations"),
                200,
                "validations readback",
            )
            artifacts_payload = _assert_response(
                client.get(f"/api/tasks/{normalized_task_key}/artifacts"),
                200,
                "artifacts readback",
            )
            preview_payload = _assert_response(
                client.get(
                    f"/api/tasks/{normalized_task_key}/artifacts/{EXPECTED_ARTIFACT_NAME}"
                ),
                200,
                "artifact preview readback",
            )
            evidence_payload = _assert_response(
                client.get(f"/api/tasks/{normalized_task_key}/review-evidence"),
                200,
                "review evidence readback",
            )
    finally:
        if original_path is not None:
            os.environ["PATH"] = original_path

    task_item = task_payload.get("item", {})
    runs = runs_payload.get("items", [])
    validations = validations_payload.get("items", [])
    artifact_names = _artifact_names(artifacts_payload)
    evidence_item = evidence_payload.get("item", {})

    expected_final_status = (
        "waiting_approval" if fake_pi_writes_expected_artifact or real_pi else "blocked"
    )
    _require(
        start_payload.get("status") == expected_final_status,
        f"start status mismatch: expected {expected_final_status}, got {start_payload.get('status')}",
    )
    _require(
        task_item.get("status") == expected_final_status,
        f"task status mismatch: expected {expected_final_status}, got {task_item.get('status')}",
    )
    _require(len(runs) == 1, f"expected one executor run, got {len(runs)}")
    _require(runs[0].get("executor") == "pi", "executor run did not use PiExecutor")
    _require(runs[0].get("status") == "completed", "Pi executor did not complete")
    _require(len(validations) == 1, f"expected one validator result, got {len(validations)}")
    expected_validator_status = (
        "passed" if expected_final_status == "waiting_approval" else "failed"
    )
    _require(
        validations[0].get("status") == expected_validator_status,
        f"validator status mismatch: expected {expected_validator_status}, got {validations[0].get('status')}",
    )
    _require("mission_contract.json" in artifact_names, "mission contract not listed")
    _require("pi-executor.log" in artifact_names, "Pi executor log not listed")
    _require(
        "pi_mission_prompt.md" in artifact_names,
        "Pi protocol prompt artifact not listed",
    )
    _require(
        EXPECTED_ARTIFACT_NAME in artifact_names,
        "expected Pi result artifact not listed",
    )
    if expected_final_status == "waiting_approval":
        _require(
            preview_payload.get("content") == EXPECTED_ARTIFACT_CONTENT,
            "Pi result preview content mismatch",
        )
        _require(
            evidence_item.get("mission_contract", {}).get("executor") == "pi",
            "review evidence did not read Pi mission contract",
        )

    return {
        "ok": expected_final_status == "waiting_approval",
        "mode": "real-pi" if real_pi else "fake-pi",
        "task_key": normalized_task_key,
        "final_status": task_item.get("status"),
        "db_path": str(db_path),
        "workspace_root": str(workspace_root),
        "repo_path": str(repo_path),
        "worktree_path": str(worktree_path),
        "artifact_dir": str(artifact_dir),
        "prompt_path": str(prompt_path),
        "fake_pi_bin": str(fake_pi_path) if fake_pi_path else None,
        "executor": {
            "name": runs[0].get("executor"),
            "status": runs[0].get("status"),
            "log": runs[0].get("log_path"),
            "artifacts": runs[0].get("artifacts", {}),
        },
        "validator": {
            "name": validations[0].get("validator"),
            "status": validations[0].get("status"),
            "log": validations[0].get("log_path"),
        },
        "readbacks": {
            "task": task_item.get("status"),
            "runs": runs_payload.get("count"),
            "validations": validations_payload.get("count"),
            "artifacts": sorted(artifact_names),
            "review_evidence_contract": evidence_item.get("mission_contract", {}).get("status"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an existing-PiExecutor Mission Control golden-path smoke.",
    )
    parser.add_argument(
        "--task-key",
        default=DEFAULT_TASK_KEY,
        help=f"Task key to use. Default: {DEFAULT_TASK_KEY}",
    )
    parser.add_argument(
        "--workspace-root",
        help=(
            "Absolute workspace root to use. By default a temporary directory "
            "under /tmp is created and removed after the run."
        ),
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep the auto-created temporary workspace after the run.",
    )
    parser.add_argument(
        "--fake-pi-bin",
        help="Absolute path to an executable fake pi binary. Fake mode is the default.",
    )
    parser.add_argument(
        "--real-pi",
        action="store_true",
        help="Use the real pi command on PATH instead of a fake pi binary.",
    )
    parser.add_argument(
        "--confirm-real-pi",
        action="store_true",
        help="Required with --real-pi to confirm intentional real Pi execution.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cleanup_workspace = False
    if args.workspace_root:
        workspace_root = _require_absolute_path(args.workspace_root, "workspace_root")
    else:
        workspace_root = Path(tempfile.mkdtemp(prefix="agent-taskflow-pi-golden-"))
        cleanup_workspace = not args.keep_workspace

    try:
        summary = run_smoke(
            workspace_root=workspace_root,
            task_key=args.task_key,
            real_pi=args.real_pi,
            confirm_real_pi=args.confirm_real_pi,
            fake_pi_bin=Path(args.fake_pi_bin) if args.fake_pi_bin else None,
        )
        summary["workspace_kept"] = not cleanup_workspace
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary["ok"] else 1
    except Exception as exc:
        print(f"Pi executor golden-path smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup_workspace:
            shutil.rmtree(workspace_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
