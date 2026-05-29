"""Deterministic implementation prompt generation for prompt-driven executors.

Some executors (currently ``opencode``) cannot start without an
``implementation_prompt.md`` describing the work. That prompt is normally
produced ahead of time by the explicit task execution package step
(:mod:`agent_taskflow.task_execution_package`). When it has not been produced,
the approved task runner generates one deterministically from the mirrored
``issue_spec.md`` so a confirmed execution can proceed instead of blocking only
because the prompt file is absent.

This module owns the deterministic prompt template and the set of executors
that require a prompt. It performs no I/O, runs no executor, and makes no
GitHub, approval, merge, push, or cleanup decision. The runner remains
responsible for the workspace, artifact recording, validation, and the human
review gate.
"""

from __future__ import annotations


# Executors that cannot start without an implementation_prompt.md.
EXECUTORS_REQUIRING_PROMPT = frozenset({"opencode"})

IMPLEMENTATION_PROMPT_FILENAME = "implementation_prompt.md"


def render_implementation_prompt(
    *,
    task_key: str,
    title: str | None,
    issue_spec: str,
) -> str:
    """Render a deterministic implementation prompt from issue spec evidence.

    The output is input/spec evidence for a bounded implementation worker. It
    inlines the mirrored issue spec verbatim and is fully determined by its
    inputs so repeated generation is reproducible.
    """

    resolved_title = (title or "").strip() or f"Task {task_key}"
    spec_body = issue_spec.strip() or "(issue spec was empty)"
    return "\n".join(
        [
            f"# Implementation Prompt — {task_key}",
            "",
            "This prompt is deterministic input/spec evidence for a bounded",
            "implementation worker. It was generated from the mirrored issue spec.",
            "It is not implementation evidence, validation evidence, approval, PR",
            "creation, push, merge, or cleanup evidence.",
            "",
            f"- Task key: {task_key}",
            f"- Task title: {resolved_title}",
            "",
            "## Source issue spec",
            "",
            spec_body,
            "",
            "## Constraints",
            "",
            "- Make the minimal safe change required to satisfy the issue spec.",
            "- Do not approve the task.",
            "- Do not merge.",
            "- Do not push.",
            "- Do not delete branches or worktrees.",
            "- Do not run cleanup.",
            "- Keep all work inside the prepared worktree.",
            "- Validators and human review follow after implementation.",
            "",
            "## Expected output",
            "",
            "- Code, documentation, and test changes only, inside the prepared worktree.",
            "- No GitHub mutation (no issue, PR, branch, or label changes).",
            "",
        ]
    )


__all__ = [
    "EXECUTORS_REQUIRING_PROMPT",
    "IMPLEMENTATION_PROMPT_FILENAME",
    "render_implementation_prompt",
]
