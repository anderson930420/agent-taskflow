"""Deterministic, static affected-test selection and Git change discovery.

The dependency scan is deliberately direct: it does not follow transitive imports.
A change to a file under agent_taskflow/api/ also selects every test that calls the API over HTTP.
Packaging changes run the small policy and surface smoke set below.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

SMOKE_TESTS = ("tests/test_v0_executor_local_path.py", "tests/test_v0_supported_surface.py")


def changed_paths(root: Path, *, base_commit: str | None = None,
                  base_branch: str = "main", target_ref: str | None = None) -> set[str]:
    """Collect committed, index, working-tree and untracked paths, including deletions."""
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)

    if target_ref is not None:
        base = git("merge-base", "HEAD", target_ref).decode().strip()
    elif base_commit:
        base = base_commit
    else:
        try:
            base = git("merge-base", "HEAD", f"origin/{base_branch}").decode().strip()
        except subprocess.CalledProcessError:
            base = "HEAD"
    # --no-renames preserves both sides of a rename, including deleted paths.
    paths = git("diff", "--name-only", "--no-renames", "-z", base, "--")
    paths += git("diff", "--name-only", "--no-renames", "--cached", "-z", "--")
    paths += git("diff", "--name-only", "--no-renames", "-z", "--")
    paths += git("ls-files", "--others", "--exclude-standard", "-z")
    return {p.decode("utf-8", errors="surrogateescape") for p in paths.split(b"\0") if p}


def _imports(source: str, module: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module or alias.name.startswith(module + ".") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            parent = node.module or ""
            if parent == module or parent.startswith(module + "."):
                return True
            if any(f"{parent}.{alias.name}".strip(".") == module for alias in node.names):
                return True
            # Tests sometimes import sibling helpers as `from . import helper`.
            if node.level and any(alias.name == module.rsplit(".", 1)[-1] for alias in node.names):
                return True
    return False


def select_tests(root: Path, paths: set[str], tests_dir: Path | None = None) -> dict[str, list[str]]:
    """Return sorted repository-relative test paths mapped to sorted reasons."""
    tests_dir = tests_dir or root / "tests"
    tests = sorted(p for p in tests_dir.rglob("test_*.py") if p.is_file())
    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in tests}
    selected: dict[str, set[str]] = {}
    def add(test: Path, reason: str) -> None:
        selected.setdefault(test.relative_to(root).as_posix(), set()).add(reason)
    for change in sorted(paths):
        path = Path(change)
        if change in {"pyproject.toml", "constraints.txt"}:
            for test in tests:
                if test.relative_to(root).as_posix() in SMOKE_TESTS:
                    add(test, f"packaging smoke: {change}")
        if change == "config/projects.yaml":
            for test, text in texts.items():
                if change in text or ('"projects.yaml"' in text or "'projects.yaml'" in text):
                    add(test, f"references {change}")
        if path.suffix != ".py":
            continue
        is_test_path = path.parts and path.parts[0] == "tests"
        if is_test_path and path.name.startswith("test_"):
            for test in tests:
                if test.relative_to(root).as_posix() == change:
                    add(test, f"changed test: {change}")
            if (root / path).exists():
                continue
        module = path.with_suffix("").as_posix().replace("/", ".")
        # HTTP-only tests import the app, not the route module, so any API change selects them.
        is_api_path = path.parts[:2] == ("agent_taskflow", "api")
        for test, text in texts.items():
            if is_api_path and ("/api/" in text or _imports(text, "agent_taskflow.api")):
                add(test, f"api route changed: {change}")
            if not is_test_path and (test.name == f"test_{path.stem}.py" or test.name.startswith(f"test_{path.stem}_")):
                add(test, f"name matches {change}")
            if _imports(text, module) or (is_test_path and _imports(text, path.stem)):
                add(test, f"imports {module}")
            if path.parts[0] == "scripts" and (change in text or ("scripts" in text and path.name in text)):
                add(test, f"references {change}")
    return {key: sorted(selected[key]) for key in sorted(selected)}


def format_selection(selection: dict[str, list[str]]) -> str:
    if not selection:
        return "no affected tests\n"
    return "".join(f"{path}: {', '.join(reasons)}\n" for path, reasons in selection.items())
