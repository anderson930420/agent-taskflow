#!/usr/bin/env python3
"""One-shot PR-8 final regression patch; removes itself after applying."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected pattern missing in {path}: {old[:140]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "agent_taskflow/task_status_reset.py",
    '''    preview = lineage_store.preview(request.task_key)\n''',
    '''    try:\n        preview = lineage_store.preview(request.task_key)\n    except (ResetLineageError, KeyError, ValueError) as exc:\n        raise TaskStatusResetError(str(exc)) from exc\n''',
)

replace_once(
    "tests/test_attempt_resources.py",
    '''from agent_taskflow.store import TaskMirrorStore, connect\n''',
    '''from agent_taskflow.store import TaskMirrorStore, connect\nfrom agent_taskflow.task_status_reset import (\n    TaskStatusResetRequest,\n    reset_task_status,\n)\n''',
)
replace_once(
    "tests/test_attempt_resources.py",
    '''        self.base_store.update_task_status(\n            "AT-PR5-1",\n            "queued",\n            source="test-reset",\n            expected_current_status="blocked",\n        )\n        second_store, second, _ = self._claim_and_prepare()\n        self.assertNotEqual(first.attempt_id, second.attempt_id)\n''',
    '''        reset = reset_task_status(\n            TaskStatusResetRequest(\n                task_key="AT-PR5-1",\n                db_path=self.db_path,\n                from_status="blocked",\n                reason="retry resource isolation test",\n                actor="test-reset",\n                request_id="pr5-fresh-retry",\n                expected_reset_generation=0,\n                expected_old_attempt_id=first.attempt_id,\n                confirm_reset=True,\n            )\n        )\n        second_store, second, _ = self._claim_and_prepare()\n        self.assertEqual(second.attempt_id, reset.new_attempt_id)\n        self.assertNotEqual(first.attempt_id, second.attempt_id)\n''',
)

(ROOT / ".github/workflows/ci.yml").write_text(
    '''name: CI\n\non:\n  pull_request:\n  push:\n    branches:\n      - main\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n\n      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n\n      - name: Install package and dependencies\n        run: python -m pip install -e .\n\n      - name: Run unit tests\n        run: PYTHONPATH=. python -m unittest discover -s tests\n\n      - name: Compile sources\n        run: PYTHONPATH=. python -m compileall agent_taskflow scripts tests\n''',
    encoding="utf-8",
)
Path(__file__).unlink()
