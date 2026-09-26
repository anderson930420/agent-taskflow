"""Regression fixtures for standalone CI selection and fail-closed execution."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import ci_validation as ci


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "CI fixture")
        self.git("config", "user.email", "ci@example.invalid")
        self.write("docs/notes.md", "# Presentation\nPlain text.\n")
        self.write("agent_taskflow/display_label.py", "def label(value):\n    return str(value)\n")
        self.write("agent_taskflow/display_card.py", "from agent_taskflow.display_label import label\n")
        self.write("scripts/show_card.py", "from agent_taskflow.display_card import label\n")
        self.write("tests/test_label.py", "from agent_taskflow.display_label import label\n")
        self.write("tests/test_card.py", "from agent_taskflow.display_card import label\n")
        self.write("tests/test_command.py", "SCRIPT = 'show_card.py'\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return ci.git(self.root, *args).decode()

    def write(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def selection(self, **kwargs):
        audit, _ = ci.snapshot(self.root, self.base)
        return ci.select(self.root, audit, **kwargs)

    def test_plain_documentation_checks_docs_without_runtime_full_claim(self):
        self.write("docs/notes.md", "# Presentation\n[Label](../agent_taskflow/display_label.py)\n")
        selection = self.selection()
        self.assertEqual(selection["level"], "docs")
        ci.check_doc_links(self.root, selection["documents"])
        output = self.root / "logs"
        output.mkdir()
        result = ci.execute(self.root, output, selection, run=fake_runner())
        self.assertTrue(result["passed"])
        self.assertFalse(result["runtime_full_validated"])
        self.assertFalse(result["full_executed"])

    def test_document_literal_selects_direct_guidance_test_with_evidence(self):
        self.write("tests/test_guidance.py", "from pathlib import Path\nNOTE = Path('docs/notes.md')\n")
        self.git("add", "tests/test_guidance.py")
        self.git("commit", "-qm", "guidance fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("docs/notes.md", "# Presentation\nChanged wording.\n")
        selection = self.selection()
        self.assertEqual(selection["level"], "docs")
        self.assertIn("tests/test_guidance.py", selection["tests"])
        self.assertEqual(selection["caller_evidence"]["tests/test_guidance.py"], [
            "docs/notes.md", "tests/test_guidance.py",
        ])
        self.assertEqual(self.selection()["caller_evidence"]["tests/test_guidance.py"],
                         selection["caller_evidence"]["tests/test_guidance.py"])

    def test_document_literal_selects_helper_mediated_guidance_test_with_evidence(self):
        self.write("tests/docs_helper.py", "from pathlib import Path\nNOTE = Path('docs') / 'notes.md'\n")
        self.write("tests/test_guidance.py", "import docs_helper\n")
        self.git("add", "tests/docs_helper.py", "tests/test_guidance.py")
        self.git("commit", "-qm", "guidance helper fixture")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("docs/notes.md", "# Presentation\nChanged wording.\n")
        selection = self.selection()
        self.assertEqual(selection["level"], "docs")
        self.assertIn("tests/test_guidance.py", selection["tests"])
        self.assertEqual(selection["caller_evidence"]["tests/test_guidance.py"], [
            "docs/notes.md", "tests/docs_helper.py", "tests/test_guidance.py",
        ])
        self.assertEqual(self.selection()["caller_evidence"]["tests/test_guidance.py"],
                         selection["caller_evidence"]["tests/test_guidance.py"])

    def test_local_runtime_admission_selects_direct_and_transitive_callers(self):
        self.write("agent_taskflow/display_label.py", "def label(value):\n    return str(value).upper()\n")
        admission = {"agent_taskflow/display_label.py": {
            "tests": ("tests/test_label.py",),
            "callers": ("agent_taskflow/display_card.py", "scripts/show_card.py"),
        }}
        with patch.object(ci, "RUNTIME_FAST", admission):
            result = self.selection()
        self.assertEqual(result["level"], "fast", result["reasons"])
        self.assertEqual(set(result["tests"]), {"tests/test_label.py", "tests/test_card.py", "tests/test_command.py"})
        self.assertEqual(result["caller_evidence"]["tests/test_command.py"], [
            "agent_taskflow/display_label.py", "agent_taskflow/display_card.py",
            "scripts/show_card.py", "tests/test_command.py",
        ])
        preflight, fast = ci.command_plan(result)
        self.assertEqual([x[0] for x in preflight], ["dependencies", "pip-check", "pytest-collection", "unittest-collection"])
        self.assertEqual([x[0] for x in fast], ["compile", "workflow-contract", "workflow-policy", "targeted-tests"])

    def test_admitted_leaf_with_new_io_import_or_caller_requires_full(self):
        admission = {"agent_taskflow/display_label.py": {
            "tests": ("tests/test_label.py",),
            "callers": ("agent_taskflow/display_card.py", "scripts/show_card.py"),
        }}
        self.write("agent_taskflow/display_label.py", "import subprocess\n")
        with patch.object(ci, "RUNTIME_FAST", admission):
            result = self.selection()
        self.assertEqual(result["level"], "full")
        self.assertTrue(any("outside pure boundary" in r for r in result["reasons"]))
        self.write("agent_taskflow/display_label.py", "def label(value): return str(value)\n")
        self.write("agent_taskflow/new_caller.py", "from agent_taskflow.display_label import label\n")
        with patch.object(ci, "RUNTIME_FAST", admission):
            result = self.selection()
        self.assertEqual(result["level"], "full")
        self.assertTrue(any("caller/test boundary" in r for r in result["reasons"]))

    def test_admitted_leaf_with_cli_or_shared_core_caller_requires_full(self):
        for caller in ("agent_taskflow/cli/tool.py", "agent_taskflow/store.py"):
            with self.subTest(caller=caller):
                self.write("agent_taskflow/display_label.py", "def label(value): return str(value)\n")
                self.write(caller, "from agent_taskflow.display_label import label\n")
                admission = {"agent_taskflow/display_label.py": {
                    "tests": ("tests/test_label.py",),
                    "callers": ("agent_taskflow/display_card.py", "scripts/show_card.py", caller),
                }}
                with patch.object(ci, "RUNTIME_FAST", admission):
                    result = self.selection()
                self.assertEqual(result["level"], "full")
                self.assertTrue(any("CLI/shared-core caller requires full validation" in reason
                                    for reason in result["reasons"]))
                (self.root / caller).unlink()

    def test_real_runtime_defaults_full_and_shared_core_has_all_callers(self):
        self.write("agent_taskflow/display_label.py", "def label(value): return value\n")
        result = self.selection()
        self.assertEqual(result["level"], "full")
        self.assertIn("tests/test_command.py", result["tests"])
        self.assertTrue(any("unmapped impact" in r for r in result["reasons"]))

    def test_high_risk_matrix_and_explicit_risk_cannot_be_downgraded(self):
        for name in ["agent_taskflow/store.py", "agent_taskflow/dispatcher.py",
                     "agent_taskflow/approved_task_runner.py", "agent_taskflow/_helpers.py",
                     "agent_taskflow/integration_controller.py", "agent_taskflow/cli/tool.py",
                     "agent_taskflow/atomic_write.py", "agent_taskflow/concurrency_gate.py",
                     "migrations/001.sql", "ops/start.sh", "WORKFLOW.md", "AGENTS.md",
                     ".github/workflows/ci.yml", "pyproject.toml", "constraints-test.txt",
                     "tests/conftest.py", "tests/step5_support.py", "pytest.ini"]:
            with self.subTest(name=name):
                self.write(name, "# changed\n")
                self.assertEqual(self.selection()["level"], "full")
                (self.root / name).unlink()
        self.write("docs/notes.md", "# Presentation\nEdited.\n")
        self.assertEqual(self.selection(risks=("possible flakiness",))["level"], "full")
        self.assertEqual(self.selection(force_full=True)["level"], "full")
        for event in ["push", "schedule", "release", "workflow_dispatch", "unknown"]:
            self.assertEqual(self.selection(event=event)["level"], "full")

    def test_sensitive_document_content_and_deleted_sensitive_content_require_full(self):
        for content in ["Workflow policy", "Security credentials", "Production deployment",
                        "SQLite migration", "Artifact publication", "filesystem races"]:
            self.write("docs/notes.md", content)
            self.assertEqual(self.selection()["level"], "full")
        self.git("add", ".")
        self.git("commit", "-qm", "sensitive doc")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("docs/notes.md", "Plain text")
        self.assertEqual(self.selection()["level"], "full")

    def test_permission_changing_documentation_requires_full(self):
        sensitive = "# Permissions\nRun `chmod 777 /etc/shadow` to allow access.\n"
        plain = "# Presentation\nPlain text.\n"
        self.write("docs/notes.md", sensitive)
        for label in ("worktree",):
            with self.subTest(label=label):
                result = self.selection()
                self.assertEqual(result["level"], "full")
                self.assertTrue(any("documentation describes sensitive" in reason for reason in result["reasons"]))
        self.git("add", "docs/notes.md")
        self.write("docs/notes.md", plain)
        with self.subTest(label="staged"):
            self.assertEqual(self.selection()["level"], "full")
        self.write("docs/notes.md", sensitive)
        self.git("add", "docs/notes.md")
        self.git("commit", "-qm", "sensitive doc")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("docs/notes.md", plain)
        with self.subTest(label="base and HEAD"):
            self.assertEqual(self.selection()["level"], "full")

    def test_audit_preserves_all_layers_rename_deletion_mode_and_untracked_bytes(self):
        self.write("docs/notes.md", "Committed\n")
        self.git("add", ".")
        self.git("commit", "-qm", "committed")
        self.git("mv", "agent_taskflow/display_label.py", "agent_taskflow/renamed.py")
        self.write("docs/notes.md", "Staged\n")
        self.git("add", "docs/notes.md")
        self.write("docs/notes.md", "Unstaged\n")
        (self.root / "tests/test_card.py").unlink()
        (self.root / "scripts/show_card.py").chmod(0o755)
        unusual = "docs/a newline\n$(touch NEVER).md"
        self.write(unusual, "Untracked\n")
        self.write("agent_taskflow.egg-info/PKG-INFO", "generated")
        audit, patches = ci.snapshot(self.root, self.base)
        self.assertEqual({c["layer"] for c in audit["changes"]}, {"committed", "staged", "unstaged", "untracked"})
        renamed = next(c for c in audit["changes"] if c["status"].startswith("R"))
        self.assertEqual(renamed["paths"], ["agent_taskflow/display_label.py", "agent_taskflow/renamed.py"])
        self.assertTrue(any(c["status"] == "D" for c in audit["changes"]))
        self.assertTrue(any(c.get("old_mode") == "100644" and c.get("new_mode") == "100755" for c in audit["changes"]))
        self.assertIn(unusual, audit["paths"])
        self.assertNotIn("agent_taskflow.egg-info/PKG-INFO", audit["paths"])
        self.assertIn("agent_taskflow.egg-info/PKG-INFO", audit["excluded_untracked"])
        self.assertEqual(set(patches), {"committed", "staged", "unstaged"})
        self.assertEqual(ci.snapshot(self.root, self.base)[0]["source_sha256"], audit["source_sha256"])
        self.write(unusual, "Different\n")
        self.assertNotEqual(ci.snapshot(self.root, self.base)[0]["source_sha256"], audit["source_sha256"])
        self.assertFalse((self.root / "NEVER").exists())
        self.assertEqual(ci.select(self.root, audit)["level"], "full")

    def test_staged_sensitive_document_canceled_in_worktree_requires_full(self):
        self.write("docs/notes.md", "Production credential deployment\n")
        self.git("add", ".")
        self.write("docs/notes.md", "# Presentation\nPlain text.\n")
        self.assertEqual(self.selection()["level"], "full")

    def test_deleted_source_keeps_caller_evidence(self):
        (self.root / "agent_taskflow/display_label.py").unlink()
        result = self.selection()
        self.assertEqual(result["level"], "full")
        self.assertIn("tests/test_command.py", result["caller_evidence"])

    def test_staged_change_canceled_in_worktree_is_still_selected(self):
        self.write("agent_taskflow/display_label.py", "raise RuntimeError('staged')\n")
        self.git("add", ".")
        self.write("agent_taskflow/display_label.py", "def label(value):\n    return str(value)\n")
        audit, _ = ci.snapshot(self.root, self.base)
        self.assertEqual({c["layer"] for c in audit["changes"]}, {"staged", "unstaged"})
        self.assertIn("tests/test_card.py", ci.select(self.root, audit)["tests"])

    def test_unparseable_unknown_missing_base_symlink_fail_closed(self):
        self.write("agent_taskflow/display_card.py", "invalid syntax !")
        self.assertEqual(self.selection()["level"], "full")
        with self.assertRaises(subprocess.CalledProcessError):
            ci.snapshot(self.root, "missing-base")
        os.symlink("/outside/not-read", self.root / "docs/linked.md")
        audit, _ = ci.snapshot(self.root, self.base)
        self.assertEqual(next(s["kind"] for s in audit["files"] if s["path"] == "docs/linked.md"), "symlink")
        self.assertEqual(ci.select(self.root, audit)["level"], "full")

    def test_link_failure_is_real(self):
        self.write("docs/notes.md", "[Broken](missing.md)")
        with self.assertRaisesRegex(ValueError, "missing"):
            ci.check_doc_links(self.root, ["docs/notes.md"])


def fake_runner(failures=()):
    def run(root, output, name, argv):
        return {"name": name, "argv": argv, "exit_code": int(name in failures)}
    return run


class GateTests(unittest.TestCase):
    def run_gates(self, level="fast", failures=()):
        with tempfile.TemporaryDirectory() as directory:
            return ci.execute(Path(directory), Path(directory),
                              {"level": level, "tests": ["tests/test_local.py"], "documents": []},
                              run=fake_runner(failures))

    def test_failed_fast_runs_both_full_suites_and_never_turns_green(self):
        result = self.run_gates(failures=("targeted-tests",))
        self.assertFalse(result["passed"])
        self.assertTrue(result["full_required"])
        self.assertTrue(result["full_executed"])
        self.assertEqual([c["name"] for c in result["commands"]][-2:], ["full-pytest", "full-unittest"])

    def test_full_pytest_failure_still_runs_exact_ci_unittest(self):
        result = self.run_gates("full", ("full-pytest",))
        self.assertFalse(result["passed"])
        self.assertEqual(result["commands"][-1]["argv"][1:], ["-m", "unittest", "discover", "-s", "tests"])
        self.assertFalse(result["runtime_full_validated"])

    def test_preflight_failure_prevents_all_execution_and_requires_full_after_repair(self):
        for failed in ["dependencies", "pip-check", "pytest-collection", "unittest-collection"]:
            with self.subTest(failed=failed):
                result = self.run_gates("full", (failed,))
                self.assertFalse(result["passed"])
                self.assertTrue(result["full_required"])
                self.assertFalse(result["full_executed"])
                self.assertEqual(result["commands"][-1]["name"], failed)

    def test_successful_full_requires_both_suites(self):
        result = self.run_gates("full")
        self.assertTrue(result["passed"])
        self.assertTrue(result["runtime_full_validated"])

    def test_missing_pytest_import_fails_even_when_unittest_would_skip(self):
        original = ci.importlib.import_module
        def missing(name, *args, **kwargs):
            if name == "pytest":
                raise ModuleNotFoundError("pytest deliberately missing")
            return original(name, *args, **kwargs)
        with patch.object(ci.importlib, "import_module", side_effect=missing):
            with self.assertRaisesRegex(ModuleNotFoundError, "pytest"):
                ci.check_dependencies(ci.ROOT)

    def test_other_declared_test_dependency_missing_fails(self):
        config = {"project": {"dependencies": [], "optional-dependencies": {"test": ["extra-test-lib>=1"]}},
                  "tool": {"agent-taskflow": {"ci": {"required-imports": []}}}}
        with patch.object(ci.tomllib, "loads", return_value=config), patch.object(
                ci.importlib.metadata, "version", side_effect=importlib.metadata.PackageNotFoundError("extra-test-lib")):
            with self.assertRaises(importlib.metadata.PackageNotFoundError):
                ci.check_dependencies(ci.ROOT)

    def test_real_command_captures_exit_log_and_clears_pytest_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.dict(os.environ, {"PYTEST_ADDOPTS": "-k nothing"}):
                result = ci.run_command(output, output, "probe", [ci.sys.executable, "-c",
                    "import os,sys; print(os.environ.get('PYTEST_ADDOPTS')); sys.exit(7)"])
            self.assertEqual(result["exit_code"], 7)
            self.assertEqual((output / "probe.log").read_text().strip(), "None")
            self.assertEqual(json.loads((output / "probe.json").read_text())["log_sha256"], ci.sha((output / "probe.log").read_bytes()))


if __name__ == "__main__":
    unittest.main()
