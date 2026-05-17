# Changed-Files Policy Validator

`changed-files` is an opt-in deterministic validator that audits a task
worktree before human approval. It exists to catch executor output that changes
files outside the intended task scope.

Path policy is recorded in `mission_contract.json`:

```json
{
  "allowed_paths": ["src", "tests"],
  "forbidden_paths": ["secrets", ".env"]
}
```

The validator currently enforces only the `allowed_paths` and
`forbidden_paths` values present in `mission_contract.json`. The draft
machine-readable workflow policy's `path_policy` is not automatically injected
into mission contracts by the dispatcher in this branch. Full
policy-to-contract runtime enforcement is deferred to a later branch.

Semantics:

- `forbidden_paths` wins over `allowed_paths`.
- If `allowed_paths` is empty, any repo-relative path is allowed unless it
  matches `forbidden_paths`.
- If `allowed_paths` is non-empty, every changed file must be equal to or under
  one of those paths.
- Modified, added, deleted, and untracked files are collected with
  `git status --porcelain=v1 -z --untracked-files=all`.
- Task artifacts are separate from repo changes when the artifact directory is
  outside the worktree, matching the current Mission Control architecture.
- Malformed path policy blocks validation instead of widening scope.

The validator writes `changed-files-audit.json` and
`changed-files-validate.log` into the task artifact directory. Dispatcher/store
validation metadata records both artifacts through the existing validator result
path.

This validator does not add GitHub integration, create PRs, merge, push, or
change executor behavior. It is a bridge hardening check for Pi, OpenCode,
Shell, and future executors.
