#!/usr/bin/env python3
"""Standalone CI selection and evidence; never a Taskflow lifecycle authority."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
BASE_TESTS = (
    "tests/test_workflow_contract.py",
    "tests/test_workflow_schema.py",
    "tests/test_policy_validator.py",
    "tests/test_changed_files_validator.py",
    "tests/test_ci_validation.py",
)
# No current product module has an audited low-risk boundary. Admission requires
# direct tests AND the complete transitive production caller set; see the guide.
RUNTIME_FAST: dict[str, dict[str, tuple[str, ...]]] = {}
RUNTIME_FULL_CALLERS = {
    "agent_taskflow/store.py",
    "agent_taskflow/dispatcher.py",
    "agent_taskflow/approved_task_runner.py",
    "agent_taskflow/_helpers.py",
    "agent_taskflow/integration_controller.py",
    "agent_taskflow/atomic_write.py",
    "agent_taskflow/concurrency_gate.py",
}
SENSITIVE = re.compile(
    r"workflow|policy|security|credential|secret|deploy|production|\bcron\b|"
    r"systemd|nginx|migration|schema|persist|sqlite|artifact|evidence|lifecycle|"
    r"dispatch|approved.?task|integration|scheduler|concurren|symlink|race|"
    r"path.?polic|filesystem|worktree|approval|governance|validator|\bCI\b|"
    r"permissions?|authori[sz]ation|authentication|access[ -]?control|rbac|"
    r"chmod|chown|setfacl|\bsudo\b|/(?:etc|root|usr|var)(?:/|$)",
    re.IGNORECASE,
)
INFRA_FILES = {"pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini", ".gitignore"}
GENERATED_METADATA = "agent_taskflow.egg-info/"


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_changes(data: bytes, layer: str) -> list[dict]:
    """Parse NUL-delimited raw Git output, including both rename endpoints/modes."""
    parts = iter(data.split(b"\0"))
    result = []
    for header in parts:
        if not header:
            continue
        old_mode, new_mode, old_oid, new_oid, status = header.decode("ascii").split()
        paths = [os.fsdecode(next(parts))]
        if status[0] in "RC":
            paths.append(os.fsdecode(next(parts)))
        result.append(dict(layer=layer, status=status, paths=paths,
                           old_mode=old_mode[1:], new_mode=new_mode,
                           old_oid=old_oid, new_oid=new_oid))
    return result


def file_state(root: Path, name: str) -> dict:
    path = root / name
    if not path.parent.resolve().is_relative_to(root):
        return {"path": name, "kind": "special", "reason": "parent symlink escapes repository"}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": name, "kind": "missing"}
    mode = oct(stat.S_IMODE(info.st_mode))
    if stat.S_ISLNK(info.st_mode):
        return {"path": name, "kind": "symlink", "mode": mode,
                "sha256": sha(os.fsencode(os.readlink(path)))}
    if stat.S_ISREG(info.st_mode):
        return {"path": name, "kind": "file", "mode": mode,
                "sha256": sha(path.read_bytes())}
    return {"path": name, "kind": "special", "mode": mode}


def snapshot(root: Path, base: str) -> tuple[dict, dict[str, bytes]]:
    base_sha = git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
    head = git(root, "rev-parse", "HEAD").decode().strip()
    git(root, "merge-base", "--is-ancestor", base_sha, head)
    layers = {"committed": [base_sha, head], "staged": ["--cached", head], "unstaged": []}
    changes, patches = [], {}
    for name, refs in layers.items():
        common = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", *refs]
        changes.extend(raw_changes(git(root, *common, "--raw", "--no-abbrev", "-z", "--"), name))
        patches[name] = git(root, *common, "--binary", "--full-index", "--")
    untracked = [os.fsdecode(p) for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p]
    # Only untracked installer metadata is excluded. Tracked metadata is audited.
    excluded = sorted(p for p in untracked if p.startswith(GENERATED_METADATA))
    for name in sorted(set(untracked) - set(excluded)):
        changes.append(dict(layer="untracked", status="?", paths=[name]))
    paths = sorted({p for c in changes for p in c["paths"]})
    audit = dict(base=base_sha, head=head, changes=changes, paths=paths,
                 files=[file_state(root, p) for p in paths],
                 patch_sha256={k: sha(v) for k, v in patches.items()},
                 excluded_untracked={p: "generated editable-install metadata" for p in excluded})
    audit["source_sha256"] = sha(json.dumps(audit, sort_keys=True).encode())
    return audit, patches


def python_graph(root: Path, extra_paths: tuple[str, ...] = ()) -> tuple[dict[str, set[str]], list[str]]:
    """Conservative static reverse import/literal-reference graph, never execution."""
    files = sorted(p for d in ("agent_taskflow", "scripts", "tests") for p in (root / d).rglob("*.py")
                   if "__pycache__" not in p.parts)
    extra_python = {root / p for p in extra_paths if p.endswith(".py")}
    documents = sorted({p for p in extra_paths if p.startswith("docs/") and p.endswith(".md")})
    modules = {}
    basenames: dict[str, set[str]] = {}
    for p in sorted(set(files) | extra_python):
        name = p.relative_to(root).as_posix()
        module = name[:-3].replace("/", ".").removesuffix(".__init__")
        modules[module] = name
        if name.startswith("tests/"):
            modules[p.stem] = name  # unittest discovers helpers as top-level modules
        basenames.setdefault(p.name, set()).add(name)
    document_basenames: dict[str, set[str]] = {}
    for document in documents:
        document_basenames.setdefault(Path(document).name, set()).add(document)
    reverse: dict[str, set[str]] = {
        name: set() for name in [*(p.relative_to(root).as_posix() for p in files), *documents]
    }
    errors = []
    for path in files:
        name = path.relative_to(root).as_posix()
        try:
            if path.is_symlink():
                raise ValueError("Python symlink")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
            dependencies: set[str] = set()
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        package = name[:-3].replace("/", ".").rsplit(".", 1)[0]
                        module = importlib.util.resolve_name("." * node.level + module, package)
                    imported = [module, *(module + "." + a.name for a in node.names)]
                for module in imported:
                    # Importing a submodule executes package initializers too.
                    for i in range(1, len(module.split(".")) + 1):
                        target = modules.get(".".join(module.split(".")[:i]))
                        if target:
                            dependencies.add(target)
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # Covers subprocess scripts, patch targets and literal dynamic imports.
                    literal = node.value
                    if literal in documents:
                        dependencies.add(literal)
                    elif literal in document_basenames:
                        candidates = document_basenames[literal]
                        if len(candidates) == 1:
                            dependencies.update(candidates)
                        else:
                            errors.append(f"{name}: ambiguous documentation basename reference: {literal}")
                    for token in re.findall(r"[\w./]+", literal):
                        target = modules.get(token)
                        if target:
                            dependencies.add(target)
                        dependencies.update(basenames.get(token.rsplit("/", 1)[-1], ()))
            for dependency in dependencies - {name}:
                reverse.setdefault(dependency, set()).add(name)
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
    return reverse, errors


def impacted(reverse: dict[str, set[str]], paths: list[str]) -> dict[str, list[str]]:
    reasons = {p: [p] for p in paths}
    queue = list(paths)
    while queue:
        source = queue.pop(0)
        for caller in sorted(reverse.get(source, ())):
            if caller not in reasons:
                reasons[caller] = [*reasons[source], caller]
                queue.append(caller)
    return reasons


def runtime_boundary_errors(root: Path, paths: set[str]) -> list[str]:
    """Admission is narrow: no new I/O imports or dynamic execution in the leaf/callers."""
    pure_modules = {"__future__", "typing", "dataclasses", "collections", "enum", "math",
                    "re", "string", "functools", "itertools", "operator", "datetime"}
    admitted_modules = {p[:-3].replace("/", ".") for p in paths}
    errors = []
    for name in sorted(paths):
        if name.startswith("agent_taskflow/cli/") or name in RUNTIME_FULL_CALLERS:
            errors.append(f"{name}: CLI/shared-core caller requires full validation")
            continue
        try:
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        errors.append(f"{name}: relative import requires admission review")
                    imported = [node.module or ""]
                for module in imported:
                    if module.split(".")[0] not in pure_modules and module not in admitted_modules:
                        errors.append(f"{name}: import outside pure boundary: {module}")
                if isinstance(node, ast.Call):
                    call = node.func.id if isinstance(node.func, ast.Name) else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
                    if call in {"open", "eval", "exec", "compile", "__import__", "import_module", "getattr", "setattr"}:
                        errors.append(f"{name}: dynamic/I/O call requires review: {call}")
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(f"{name}: {exc}")
    return errors


def doc_text(root: Path, path: str, base: str) -> str:
    # Inspect both versions: deleting sensitive text must not grant an exemption.
    parts = []
    current = root / path
    if current.is_file() and not current.is_symlink():
        parts.append(current.read_text(encoding="utf-8"))
    for ref in (base, "HEAD", ""):
        try:
            parts.append(git(root, "show", f"{ref}:{path}").decode("utf-8"))
        except subprocess.CalledProcessError:
            pass  # newly added/deleted documents need not exist in every layer
    return "\n".join(parts)


def select(root: Path, audit: dict, *, event: str = "local", force_full: bool = False,
           risks: tuple[str, ...] = ()) -> dict:
    reverse, errors = python_graph(root, tuple(audit["paths"]))
    affected = impacted(reverse, audit["paths"])
    tests = {p for p in affected if p.startswith("tests/test_") and p.endswith(".py")}
    tests.update(p for p in BASE_TESTS if (root / p).is_file())
    reasons = [f"impact analysis uncertain: {e}" for e in errors]
    path_reasons = {}
    docs = []
    for path in audit["paths"]:
        reason = None
        if path.startswith(("tests/", ".github/", "ops/", "migrations/")) or path in INFRA_FILES or "requirements" in path or "constraints" in path:
            reason = "CI/test infrastructure, dependencies, discovery, deployment or migration"
        elif SENSITIVE.search(path) or path in {"AGENTS.md", "WORKFLOW.md", "CLAUDE.md"}:
            reason = "sensitive workflow, shared runtime, persistence, evidence or security boundary"
        elif (root / path).is_symlink() or not (root / path).parent.resolve().is_relative_to(root):
            reason = "filesystem boundary requires full"
        elif path.startswith("docs/") and path.endswith(".md"):
            try:
                if SENSITIVE.search(doc_text(root, path, audit["base"])):
                    reason = "documentation describes sensitive workflow/policy/security/deployment/runtime behavior"
                else:
                    docs.append(path)
            except (OSError, UnicodeError) as exc:
                reason = f"documentation impact uncertain: {exc}"
        elif path in RUNTIME_FAST:
            admission = RUNTIME_FAST[path]
            callers = impacted(reverse, [path])
            runtime = {p for p in callers if not p.startswith("tests/")}
            expected = {path, *admission["callers"]}
            if runtime != expected or not admission["tests"] or any(not (root / t).is_file() for t in admission["tests"]):
                reason = "audited runtime caller/test boundary changed or coverage missing"
            elif boundary_errors := runtime_boundary_errors(root, expected):
                reason = "runtime boundary uncertain: " + "; ".join(boundary_errors)
            else:
                tests.update(admission["tests"])
        else:
            reason = "unmapped impact: no audited low-risk exemption"
        path_reasons[path] = reason or ("ordinary documentation" if path in docs else "audited local runtime and transitive callers")
        if reason:
            reasons.append(f"{path}: {reason}")
    for change in audit["changes"]:
        if change["status"][0] not in {"M", "?"} or change.get("old_mode") != change.get("new_mode"):
            reasons.append(f"structural change requires full: {change['status']} {change['paths']}")
    for state in audit["files"]:
        if state["kind"] in {"symlink", "special"}:
            reasons.append(f"filesystem boundary requires full: {state['path']}")
    if not audit["paths"]:
        reasons.append("empty diff cannot establish reduced validation scope")
    if event not in {"local", "pull_request"}:
        reasons.append(f"{event}: full required for main/release/nightly/manual CI")
    if force_full:
        reasons.append("explicit full gate requested")
    reasons.extend(f"explicit risk escalation: {r}" for r in risks)
    # Documentation tests identify paths through literal filenames; also run all
    # documentation tests for files whose references are constructed dynamically.
    if docs:
        tests.update(p.relative_to(root).as_posix() for p in (root / "tests").glob("test_*doc*.py"))
    level = "full" if reasons else ("docs" if len(docs) == len(audit["paths"]) else "fast")
    return dict(level=level, reasons=sorted(set(reasons)), path_reasons=path_reasons,
                tests=sorted(tests), caller_evidence=affected, documents=docs,
                event=event, source_sha256=audit["source_sha256"],
                runtime_fast_allowlist=RUNTIME_FAST)


def check_dependencies(root: Path) -> None:
    config = tomllib.loads((root / "pyproject.toml").read_text())
    # pytest is unconditional: unittest contains tests that can silently skip it.
    imports = {"pytest", "packaging", *config["tool"]["agent-taskflow"]["ci"]["required-imports"]}
    for module in sorted(imports):
        importlib.import_module(module)
    from packaging.requirements import Requirement
    requirements = [*config["project"]["dependencies"], *config["project"]["optional-dependencies"]["test"]]
    for text in requirements:
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate({"extra": "test"}):
            continue
        installed = importlib.metadata.version(requirement.name)
        if installed not in requirement.specifier:
            raise RuntimeError(f"{requirement.name} {installed} does not satisfy {requirement}")
    print("Required runtime/test imports and declared dependency versions passed.")


def collect_unittest(root: Path) -> None:
    import unittest
    loader = unittest.TestLoader()
    suite = loader.discover(str(root / "tests"))
    if loader.errors or not suite.countTestCases():
        raise RuntimeError("unittest collection failed: " + "\n".join(loader.errors))
    print(f"unittest collected {suite.countTestCases()} tests (no execution)")


def check_doc_links(root: Path, documents: list[str]) -> None:
    """Check local inline/reference Markdown destinations, not remote availability."""
    from urllib.parse import unquote, urlsplit
    for name in documents:
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        links = re.findall(r"!?\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)", text)
        links += re.findall(r"^\s*\[[^\]]+\]:\s*<?([^\s>]+)", text, re.MULTILINE)
        for link in links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (path.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(root) or not target.exists():
                raise ValueError(f"{name}: missing/outside-repository local link {link}")
    print(f"Local document links checked in {len(documents)} files; remote URLs/anchors not checked.")


def command_plan(selection: dict) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
    py = sys.executable
    runner = str(Path(__file__).resolve())
    preflight = [
        ("dependencies", [py, runner, "_dependencies"]),
        ("pip-check", [py, "-m", "pip", "check"]),
        ("pytest-collection", [py, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"]),
        ("unittest-collection", [py, runner, "_unittest-collection"]),
    ]
    fast = [
        ("compile", [py, "-m", "compileall", "agent_taskflow", "scripts", "tests"]),
        ("workflow-contract", [py, "scripts/validate_workflow_contract.py"]),
        ("workflow-policy", [py, "scripts/validate_workflow_policy.py"]),
    ]
    if selection["documents"]:
        fast.append(("document-links", [py, runner, "_links", *selection["documents"]]))
    if selection["tests"]:
        fast.append(("targeted-tests", [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--", *selection["tests"]]))
    return preflight, fast


def run_command(root: Path, output: Path, name: str, argv: list[str]) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    before = time.monotonic()
    env = dict(os.environ, PYTHONPATH=".")
    # Ambient pytest options/plugins may silently filter tests or enable retries.
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    log = output / f"{name}.log"
    print(f"[{name}] {argv}", flush=True)
    with log.open("xb") as stream:
        result = subprocess.run(argv, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT)
    record = dict(name=name, argv=argv, cwd=str(root), started_at=started,
                  duration_seconds=time.monotonic() - before, exit_code=result.returncode,
                  log=log.name, log_sha256=sha(log.read_bytes()),
                  environment={"PYTHONPATH": ".", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None})
    (output / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"[{name}] exit {result.returncode}; {record['duration_seconds']:.2f}s", flush=True)
    return record


def execute(root: Path, output: Path, selection: dict, run=run_command) -> dict:
    records = []
    report = dict(selection=selection, commands=records, passed=False, full_executed=False,
                  runtime_full_validated=False, full_required=selection["level"] == "full")
    def step(name, argv):
        record = run(root, output, name, argv)
        records.append(record)
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        return record["exit_code"] == 0
    preflight, fast = command_plan(selection)
    for name, argv in preflight:
        if not step(name, argv):
            report["full_required"] = True
            report["blocked"] = "preflight failed; repair environment/collection before any test execution"
            break
    else:
        fast_ok = True
        for name, argv in fast:
            fast_ok = step(name, argv) and fast_ok
        report["fast_passed"] = fast_ok
        if not fast_ok:
            report["full_required"] = True
            report["escalation"] = "fast failure or flakiness; full required, original failure retained"
        full_ok = True
        if report["full_required"]:
            report["full_executed"] = True
            # Always run BOTH serial suites even if pytest fails. Unittest uses
            # the exact historical CI argv and PYTHONPATH=. from run_command.
            full_ok = step("full-pytest", [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"])
            full_ok = step("full-unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests"]) and full_ok
            report["runtime_full_validated"] = full_ok and fast_ok
        report["passed"] = fast_ok and full_ok
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in {"_dependencies", "_unittest-collection", "_links"}:
        {"_dependencies": lambda: check_dependencies(ROOT),
         "_unittest-collection": lambda: collect_unittest(ROOT),
         "_links": lambda: check_doc_links(ROOT, args[1:])}[args[0]]()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("select", "run"))
    parser.add_argument("--base", required=True, help="explicit comparison commit/ref; CI uses PR merge-base")
    parser.add_argument("--output", required=True, type=Path, help="new evidence directory outside checkout")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "local"))
    parser.add_argument("--full", action="store_true", help="escalate; no option can downgrade risk")
    parser.add_argument("--risk", action="append", default=[], help="uncertainty/flakiness/semantic risk requiring full")
    options = parser.parse_args(args)
    output = options.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error("evidence directory must be outside checkout to keep source audit stable")
    output.mkdir(parents=True, exist_ok=False)
    try:
        audit, patches = snapshot(ROOT, options.base)
        (output / "audit.json").write_text(json.dumps(audit, indent=2) + "\n")
        for name, patch in patches.items():
            (output / f"{name}.patch").write_bytes(patch)
        selection = select(ROOT, audit, event=options.event, force_full=options.full, risks=tuple(options.risk))
        (output / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
        print(json.dumps(selection, indent=2), flush=True)
        if options.action == "select":
            return 0
        report = execute(ROOT, output, selection)
        final_audit, _ = snapshot(ROOT, options.base)
        (output / "final-audit.json").write_text(json.dumps(final_audit, indent=2) + "\n")
        if audit["source_sha256"] != final_audit["source_sha256"]:
            report.update(passed=False, runtime_full_validated=False,
                          source_changed="source changed during validation; fresh run required")
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        return 0 if report["passed"] else 1
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        (output / "failure.json").write_text(json.dumps({"error": str(exc), "full_required": True}) + "\n")
        print(f"CI validation blocked: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
