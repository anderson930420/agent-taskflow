#!/usr/bin/env python3
"""One-shot PR-9 compatibility cleanup; removed after applying."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected pattern missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "agent_taskflow/executor_launch.py",
    """        process_role: str,\n        state: str,\n""",
    """        process_role: str = "executor",\n        state: str,\n""",
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''            updates={"termination_reason": "executor_process_start_failed"},\n''',
    '''            updates={\n                "termination_reason": _role_reason(\n                    record.process_role, "process_start_failed"\n                )\n            },\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '    """Persistence and append-only audit operations for one executor process group."""',
    '    """Persistence and append-only audit for executor or validator process groups."""',
)
replace_once(
    "agent_taskflow/validators/changed_files.py",
    '''                artifacts={\n                "log": log_path,\n                "audit": audit_path,\n                **process_artifacts,\n            },\n            )\n''',
    '''                artifacts={\n                    "log": log_path,\n                    "audit": audit_path,\n                    **process_artifacts,\n                },\n            )\n''',
)
Path(__file__).unlink()
