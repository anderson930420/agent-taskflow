#!/usr/bin/env python3
"""Correct the validator timeout regression command and remove this script."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "tests" / "test_validator_process_lifecycle.py"
text = path.read_text(encoding="utf-8")
old_parent = '"signal.signal(signal.SIGTERM, signal.SIG_IGN);"'
new_parent = '"signal.signal(15, signal.SIG_IGN);"'
old_child = '"\'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)\']);"'
new_child = '"\'import signal,time; signal.signal(15, signal.SIG_IGN); time.sleep(60)\']);"'
if text.count(old_parent) != 1 or text.count(old_child) != 1:
    raise RuntimeError("validator timeout signal anchors changed")
text = text.replace(old_parent, new_parent, 1).replace(old_child, new_child, 1)
path.write_text(text, encoding="utf-8")
Path(__file__).unlink()
