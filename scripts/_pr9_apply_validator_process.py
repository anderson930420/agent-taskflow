#!/usr/bin/env python3
"""Fix and execute the PR-9 integration patch, then remove patch infrastructure."""

from __future__ import annotations

from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
impl = ROOT / "scripts" / "_pr9_apply_validator_process_impl.py"
temp = ROOT / "scripts" / "_pr9_apply_validator_process_fixed.py"
source = impl.read_text(encoding="utf-8")
old = "for _ in range(4):"
if old not in source:
    raise RuntimeError("changed-files replacement loop anchor missing")
source = source.replace(old, "for _ in range(3):", 1)
temp.write_text(source, encoding="utf-8")
runpy.run_path(str(temp), run_name="__main__")

changed_files = ROOT / "agent_taskflow" / "validators" / "changed_files.py"
text = changed_files.read_text(encoding="utf-8")
old_artifact = '            artifacts={"log": log_path, "audit": audit_path},\n'
new_artifact = '''            artifacts={
                "log": log_path,
                "audit": audit_path,
                **process_artifacts,
            },
'''
if old_artifact not in text:
    raise RuntimeError("final changed-files artifact anchor missing")
changed_files.write_text(text.replace(old_artifact, new_artifact, 1), encoding="utf-8")

impl.unlink()
Path(__file__).unlink()
