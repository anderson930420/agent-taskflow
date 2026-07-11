#!/usr/bin/env python3
"""One-shot exact-head contract update for PR-5; removed by its workflow run."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected pattern missing in {path}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# ApprovedTaskRunner artifacts and prompts are authoritative in the latest Attempt root.
for task_key in ("AT-GH-727", "AT-GH-820", "AT-GH-901"):
    replace_once(
        "tests/test_approved_task_runner.py",
        f'        artifact_dir = self.artifact_root / "{task_key}"\n        execution = json.loads(\n',
        f'        artifact_dir = self.store.get_task("{task_key}").artifact_dir\n'
        '        assert artifact_dir is not None\n'
        '        execution = json.loads(\n',
    )

replace_once(
    "tests/test_approved_task_runner.py",
    '''        blocked_execution = json.loads(\n            (self.artifact_root / "AT-GH-902" / "claude-code-execution.json").read_text(\n                encoding="utf-8"\n            )\n        )\n''',
    '''        blocked_artifact_dir = self.store.get_task("AT-GH-902").artifact_dir\n        assert blocked_artifact_dir is not None\n        blocked_execution = json.loads(\n            (blocked_artifact_dir / "claude-code-execution.json").read_text(\n                encoding="utf-8"\n            )\n        )\n''',
)
replace_once(
    "tests/test_approved_task_runner.py",
    '''        prompt_path = artifact_dir / "implementation_prompt.md"\n        self.assertTrue(prompt_path.exists())\n''',
    '''        attempt_artifact_dir = self.store.get_task("AT-GH-67").artifact_dir\n        assert attempt_artifact_dir is not None\n        prompt_path = attempt_artifact_dir / "implementation_prompt.md"\n        self.assertTrue(prompt_path.exists())\n''',
)
replace_once(
    "tests/test_approved_task_runner.py",
    '''        # A pre-supplied prompt is left untouched (not regenerated from the spec).\n        self.assertEqual(prompt_path.read_text(encoding="utf-8"), "# Pre-supplied prompt\\n")\n        self.assertEqual(opencode.calls[0].prompt_path, prompt_path)\n''',
    '''        # A pre-supplied prompt is snapshotted into the Attempt artifact root and\n        # the task-level input remains untouched.\n        attempt_artifact_dir = self.store.get_task("AT-GH-69").artifact_dir\n        assert attempt_artifact_dir is not None\n        attempt_prompt_path = attempt_artifact_dir / "implementation_prompt.md"\n        self.assertEqual(prompt_path.read_text(encoding="utf-8"), "# Pre-supplied prompt\\n")\n        self.assertEqual(attempt_prompt_path.read_text(encoding="utf-8"), "# Pre-supplied prompt\\n")\n        self.assertEqual(opencode.calls[0].prompt_path, attempt_prompt_path)\n''',
)

replace_once(
    "tests/test_run_approved_task_script.py",
    '''        execution = json.loads(\n            (self.artifact_root / "AT-GH-513" / "claude-code-execution.json").read_text(\n                encoding="utf-8"\n            )\n        )\n''',
    '''        artifact_dir = self.store.get_task("AT-GH-513").artifact_dir\n        assert artifact_dir is not None\n        execution = json.loads(\n            (artifact_dir / "claude-code-execution.json").read_text(encoding="utf-8")\n        )\n''',
)
replace_once(
    "tests/test_run_approved_task_script.py",
    '''        artifact_dir = self.artifact_root / "AT-GH-910"\n        execution = json.loads(\n''',
    '''        artifact_dir = self.store.get_task("AT-GH-910").artifact_dir\n        assert artifact_dir is not None\n        execution = json.loads(\n''',
)

replace_once(
    "tests/test_attempt_resources.py",
    '''        self.assertIn(claim.attempt_id, resource.branch_name)\n''',
    '''        self.assertTrue(\n            resource.branch_name.endswith(claim.attempt_id.removeprefix("attempt-")[:12])\n        )\n        self.assertEqual(resource.attempt_id, claim.attempt_id)\n''',
)
replace_once(
    "tests/test_run_issue_to_waiting_approval_smoke.py",
    '''        worktree_root = Path(str(summary["worktree_root"]))\n        task_key = str(summary["task_key"])\n        # worktree path follows worktree_path_from_base(worktree_root, task_key)\n        marker = worktree_root / task_key / FAKE_MARKER_RELATIVE\n''',
    '''        marker = Path(str(summary["worktree_path"])) / FAKE_MARKER_RELATIVE\n''',
)

# Smoke summaries expose the post-dispatch Attempt worktree, not the pre-dispatch legacy workspace.
for script in (
    "scripts/run_issue_to_prepared_workspace_smoke.py",
    "scripts/run_prepared_workspace_golden_path_smoke.py",
):
    replace_once(
        script,
        '''    _require(review_evidence_available, "review evidence was incomplete")\n\n    return {\n''',
        '''    _require(review_evidence_available, "review evidence was incomplete")\n\n    final_worktree = store.get_task_worktree(normalized_task_key)\n    _require(final_worktree is not None, "final Attempt worktree record is missing")\n    assert final_worktree is not None\n\n    return {\n''',
    )
    replace_once(
        script,
        '''        "worktree_path": str(prepared_record.worktree_path),\n        "branch": prepared_record.branch,\n        "base_branch": prepared_record.base_branch,\n        "base_sha": prepared_record.base_sha,\n''',
        '''        "worktree_path": str(final_worktree.worktree_path),\n        "branch": final_worktree.branch,\n        "base_branch": final_worktree.base_branch,\n        "base_sha": final_worktree.base_sha,\n''',
    )

replace_once(
    "scripts/run_pr_handoff_golden_path_smoke.py",
    '''        "worktree_path": issue_summary["worktree_path"],\n        "branch": issue_summary["branch"],\n        "base_branch": issue_summary["base_branch"],\n''',
    '''        "worktree_path": handoff_json["worktree_path"],\n        "branch": handoff_json["branch"],\n        "base_branch": handoff_json["base_branch"],\n''',
)
replace_once(
    "scripts/run_issue_to_waiting_approval_smoke.py",
    '''    runner_result = handoff_dict.get("runner_result") or {}\n\n    return {\n''',
    '''    runner_result = handoff_dict.get("runner_result") or {}\n    final_worktree = store.get_task_worktree(task_key)\n    _require(final_worktree is not None, "final Attempt worktree record is missing")\n    assert final_worktree is not None\n\n    return {\n''',
)
replace_once(
    "scripts/run_issue_to_waiting_approval_smoke.py",
    '''        "worktree_root": str(paths.worktree_root),\n        "issue_number": issue_number,\n''',
    '''        "worktree_root": str(paths.worktree_root),\n        "worktree_path": str(final_worktree.worktree_path),\n        "branch": final_worktree.branch,\n        "issue_number": issue_number,\n''',
)

# Reconcile M0: PR-5 closes the resource/fresh-worktree foundation, while M0 remains open.
replace_once(
    "docs/m0-correctness-baseline-status.md",
    '> Scope: atomic permission, Task/Attempt schema, PR-3 leases, and PR-4 canonical runtime admission',
    '> Scope: atomic permission, Task/Attempt schema, PR-3/PR-4 runtime admission, and PR-5 Attempt resources',
)
replace_once(
    "docs/m0-correctness-baseline-status.md",
    '''The overall Level 2 Milestone 0 exit gate is **not complete** because retry/reset\ndoes not yet create a new Attempt with a fresh worktree, and branch, worktree,\nlock, PID, and artifact resources are not yet Attempt-scoped. This document must\nnot be used as evidence that Level 2 Milestone 0 has passed.''',
    '''PR-5 implements Attempt-scoped branch, worktree, lock, PID, and artifact\nresources plus fresh-worktree retry identity. The overall Level 2 Milestone 0 exit\ngate is still **not complete** because dual-Attempt reset audit binding, concurrent\nreset compare-and-set semantics, and process-group termination/recovery remain\nopen. This document must not be used as evidence that Milestone 0 has passed.''',
)
replace_once(
    "docs/m0-correctness-baseline-status.md",
    '| Retry uses destroy-and-recreate worktree semantics | **Blocked** | Retry must create a fresh worktree; current legacy behavior can retain or reuse the prior task worktree. |',
    '| Retry uses fresh Attempt worktree semantics | **Passed after PR-5 migration** | Each claim allocates a unique Attempt branch/worktree; terminal history is retained and a retry cannot reuse the prior Attempt path. |',
)
replace_once(
    "docs/m0-correctness-baseline-status.md",
    '''1. Attempt-scoped branch, worktree, lock, PID, and artifact resources.\n2. Retry/reset semantics that close the prior Attempt, allocate a new Attempt,\n   and create a fresh worktree without overwriting historical evidence.\n3. Process-group lifecycle and crash recovery tied to lease expiry and PID\n   evidence.\n4. Reset audit events bound to both the closed Attempt and newly created Attempt.\n5. Regression coverage proving two simultaneous reset requests produce exactly\n   one new retry Attempt and one fail-closed rejection.''',
    '''1. Process-group lifecycle and crash recovery tied to lease expiry and PID\n   evidence.\n2. Reset audit events bound to both the closed Attempt and newly created Attempt.\n3. Concurrent reset compare-and-set coverage proving two simultaneous reset\n   requests produce one accepted reset lineage and one fail-closed rejection.''',
)
replace_once(
    "docs/m0-correctness-baseline-status.md",
    '''canonical_explicit_token_wiring = implemented_after_migration\nimplicit_status_pickup = disabled_after_migration\nmilestone_0 = open_blocked''',
    '''canonical_explicit_token_wiring = implemented_after_migration\nimplicit_status_pickup = disabled_after_migration\nattempt_scoped_resources = implemented_after_pr5_migration\nfresh_worktree_retry_identity = implemented\nmilestone_0 = open_blocked''',
)

# Restore the normal exact-head CI authority and remove this one-shot file.
(ROOT / ".github/workflows/ci.yml").write_text(
    '''name: CI\n\non:\n  pull_request:\n  push:\n    branches:\n      - main\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n\n      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n\n      - name: Install package and dependencies\n        run: python -m pip install -e .\n\n      - name: Run unit tests\n        run: PYTHONPATH=. python -m unittest discover -s tests\n\n      - name: Compile sources\n        run: PYTHONPATH=. python -m compileall agent_taskflow scripts tests\n''',
    encoding="utf-8",
)
Path(__file__).unlink()
