"""A fake ``claude`` executable for tests. It never calls a model.

The tests' execution policies name it as argv[1] (after the Python
interpreter). It reads the implementer prompt from stdin, prints its argv, and
by default writes one file into its working directory (the Ticket worktree),
so the run leaves a diff. Environment knobs:

* ``FAKE_CLAUDE_WRITE=0``: write nothing (no diff);
* ``FAKE_CLAUDE_EXIT_CODE=<n>``: exit with ``n``;
* ``FAKE_CLAUDE_COMMIT=1``: commit the file instead of leaving it uncommitted.

Not a test module and not production code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    prompt = sys.stdin.read()
    print(json.dumps({"argv": sys.argv[1:], "prompt_bytes": len(prompt.encode("utf-8"))}))
    if os.environ.get("FAKE_CLAUDE_WRITE", "1") != "0":
        target = Path.cwd() / "fake-claude-change.txt"
        target.write_text("written by the fake claude executable\n", encoding="utf-8")
        if os.environ.get("FAKE_CLAUDE_COMMIT") == "1":
            subprocess.run(["git", "add", target.name], check=True)
            subprocess.run(
                ["git", "-c", "user.name=fake", "-c", "user.email=fake@example.invalid",
                 "commit", "-q", "-m", "fake change"],
                check=True,
            )
    return int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
