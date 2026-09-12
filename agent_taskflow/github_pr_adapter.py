"""GitHub PR adapter for the Step 2 integration controller (§31, §32, §34).

Read/write on pull requests, never merge. The adapter exposes exactly three
operations — create a PR, update the same PR, poll a PR's state — and every
argv it is asked to run first passes :func:`assert_not_a_merge_command`, so
``gh pr merge`` and its API equivalents are unreachable rather than merely
unused (§34, §44). Every ``gh api`` argv must also pass
:func:`assert_gh_api_allowed`, which admits only the one REST form the
adapter issues (review Ruling 35).

Polling is idempotent and read-only: it issues a single ``gh pr view`` and
normalizes the response into the §32.1 enum vocabulary. GitHub CI state is
reported here but is not a Taskflow lifecycle authority (§30) — this module
records it and nothing in Step 2 branches on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol, Sequence


__all__ = [
    "GitHubPrAdapter",
    "GitHubPrError",
    "PrSnapshot",
    "assert_gh_api_allowed",
    "assert_not_a_merge_command",
]


# Every name here must be a field real `gh pr view --json` accepts. gh 2.45.0
# has no `merged` field — asking for one fails every poll with
# `Unknown JSON field: "merged"` (review round 4, Ruling 29) — so the merge
# outcome is derived from `state`, `mergedAt` and `mergeCommit` instead.
PR_VIEW_FIELDS = (
    "number",
    "url",
    "state",
    "isDraft",
    "mergedAt",
    "mergeCommit",
    "headRefName",
    "baseRefName",
    "headRefOid",
    "reviewDecision",
    "statusCheckRollup",
    "reviews",
    "title",
    "body",
)

# The merge guard parses argv rather than pattern-matching a rendered string
# (review Ruling 3): the executable is found by basename, whatever its path,
# and global flags before the subcommand are skipped with their values.
_GH_GLOBAL_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
     "--exec-path", "--super-prefix"}
)
_API_PR_MERGE_RE = re.compile(r"(^|/)pulls/\d+/merge/?$")
# `repos/<o>/<r>/merges` merges one branch into another; `graphql` can run a
# `mergePullRequest` mutation (review Ruling 35b).
_API_BRANCH_MERGE_RE = re.compile(r"(^|/)merges/?$")
_API_GRAPHQL_RE = re.compile(r"(^|/)graphql/?$")

# Review Ruling 35: `gh pr edit` fails on gh 2.45.0 (Projects-classic GraphQL
# deprecation), so the adapter updates a PR through REST instead. That is the
# only `gh api` call it makes, and assert_gh_api_allowed admits nothing else.
_GH_API_UPDATE_FIELDS = ("title", "body")

_CI_FAILURE_STATES = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
_CI_PENDING_STATES = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", ""}
_CI_SUCCESS_STATES = {"SUCCESS", "NEUTRAL", "SKIPPED"}


class GitHubPrError(RuntimeError):
    """Raised when a GitHub PR operation is unsafe or fails."""


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CompletedProcessLike]


def _executable_index(tokens: Sequence[str], name: str) -> int | None:
    """Index of the first token whose basename is ``name`` (``/usr/bin/gh``, ``./gh``)."""
    for index, token in enumerate(tokens):
        if PurePosixPath(token).name == name:
            return index
    return None


def _positionals(tokens: Sequence[str], value_flags: frozenset[str]) -> list[str]:
    """Non-flag tokens, skipping each separate-valued flag together with its value."""
    positionals: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in value_flags:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        positionals.append(token)
    return positionals


_GH_API_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
_GH_API_METHOD_FLAGS = frozenset({"-X", "--method"})


def _gh_api_method_and_endpoints(tokens: Sequence[str]) -> tuple[str | None, list[str]]:
    """The method value and endpoint positionals of a ``gh api`` argv.

    Field values are opaque (ruling 42). ``-f body=...`` carries user text such
    as a PR body or a reviewer hint listing changed files; matching the merge
    patterns against it refused legitimate updates and stopped re-integration
    in ``needs_decision``.
    """
    method: str | None = None
    endpoints: list[str] = []
    index = 1 if tokens[:1] == ["api"] else 0
    while index < len(tokens):
        token = tokens[index]
        flag, separator, inline = token.partition("=")
        if flag in _GH_API_METHOD_FLAGS:
            if separator:
                method = inline
                index += 1
            else:
                if index + 1 < len(tokens):
                    method = tokens[index + 1]
                index += 2
            continue
        if flag in _GH_API_FIELD_FLAGS:
            index += 1 if separator else 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        endpoints.append(token)
        index += 1
    return method, endpoints


def _refuse_merge(rendered: str) -> None:
    raise GitHubPrError(
        "Taskflow must never merge: refusing to run "
        f"{rendered!r}. Every merge requires a human on GitHub (§34)."
    )


def assert_not_a_merge_command(argv: Sequence[str]) -> None:
    """Reject any argv that would merge a PR or advance the target branch.

    Matches *parsed* argv (review Ruling 3): ``gh ... pr merge`` is rejected
    whatever the executable path (``/usr/bin/gh``, ``./gh``, ``env gh``) and
    whatever global flags precede the subcommand (``gh --repo o/r pr merge``).
    Also rejected: the REST merge endpoint through ``gh api``, and ``git
    merge`` or a ``git push`` with a ``:`` refspec — this adapter never needs
    either.
    """
    tokens = [str(part) for part in argv]
    rendered = " ".join(tokens)

    gh = _executable_index(tokens, "gh")
    if gh is not None:
        positionals = _positionals(tokens[gh + 1 :], _GH_GLOBAL_VALUE_FLAGS)
        if any(positionals[i : i + 2] == ["pr", "merge"] for i in range(len(positionals) - 1)):
            _refuse_merge(rendered)
        if positionals[:1] == ["api"]:
            # Ruling 42: scan only the endpoint and the method, never a field
            # value. A PR body may legitimately end in a path like `web/graphql`
            # or `repos/o/r/merges`; scanning it made update_pr refuse and
            # re-integration stop in needs_decision. assert_gh_api_allowed
            # already refuses every merge vector on its own.
            method, endpoints = _gh_api_method_and_endpoints(tokens[gh + 1 :])
            scanned = [*endpoints, method] if method is not None else list(endpoints)
            if any(
                pattern.search(token)
                for token in scanned
                for pattern in (_API_PR_MERGE_RE, _API_BRANCH_MERGE_RE, _API_GRAPHQL_RE)
            ):
                _refuse_merge(rendered)

    git = _executable_index(tokens, "git")
    if git is not None:
        positionals = _positionals(tokens[git + 1 :], _GIT_GLOBAL_VALUE_FLAGS)
        if positionals[:1] == ["merge"]:
            _refuse_merge(rendered)
        if positionals[:1] == ["push"] and any(":" in token for token in positionals[1:]):
            _refuse_merge(rendered)


def assert_gh_api_allowed(argv: Sequence[str], *, repo: str) -> None:
    """Admit a ``gh api`` argv only in the exact form this adapter issues.

    Review Ruling 35b: once the adapter talks REST, ``gh api`` must not become
    a merge vector — ``-X PUT repos/o/r/pulls/N/merge`` merges a PR,
    ``repos/o/r/merges`` merges branches and ``graphql`` can run a
    ``mergePullRequest`` mutation. So this is an allowlist, in the spirit of
    the push allowlist, not a denylist. The one admitted form is::

        gh api -X PATCH repos/<repo>/pulls/<number> -f title=<t> -f body=<b>

    with ``repo`` exactly the adapter's own repository, a positive PR number,
    at least one of the ``title``/``body`` fields (each at most once) and no
    other flag. No GET is admitted: the adapter reads through ``gh pr view``.
    Everything else is refused — every other method or method spelling
    (``--method``, ``-XPATCH``), every other endpoint (``/merge``,
    ``/merges``, ``graphql``, ``issues``, another repository, a full
    ``https://`` URL, a leading ``/``, a query string), ``-F`` (which reads
    ``@file`` and converts types), and any global flag before ``api``.

    argv is parsed the way :func:`assert_not_a_merge_command` parses it: the
    executable is found by basename and global flags are skipped with their
    values, so ``/usr/bin/gh -R o/r api ...`` is recognized as ``gh api``.
    A non-``api`` gh command is left to the merge guard.
    """
    tokens = [str(part) for part in argv]
    gh = _executable_index(tokens, "gh")
    if gh is None:
        return
    if _positionals(tokens[gh + 1 :], _GH_GLOBAL_VALUE_FLAGS)[:1] != ["api"]:
        return

    rendered = " ".join(tokens)
    owner, _, name = repo.strip().partition("/")

    def refuse(reason: str) -> None:
        raise GitHubPrError(
            f"Refusing gh api call {rendered!r}: {reason}. The only gh api "
            f"form Taskflow issues is `gh api -X PATCH repos/{repo}/pulls/<n> "
            "-f title=... -f body=...` (review Ruling 35)."
        )

    if not owner or not name or "/" in name:
        refuse(f"the adapter's repository {repo!r} is not owner/name")
    if tokens[gh + 1] != "api":
        refuse("no global flag may precede `api`")

    method: str | None = None
    endpoint: str | None = None
    fields: list[str] = []
    rest = tokens[gh + 2 :]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in ("-X", "-f"):
            if index + 1 >= len(rest):
                refuse(f"{token} has no value")
            value = rest[index + 1]
            index += 2
            if token == "-X":
                if method is not None:
                    refuse("the method is given more than once")
                method = value
            else:
                key, separator, _ = value.partition("=")
                if not separator or key not in _GH_API_UPDATE_FIELDS:
                    refuse(f"field {key!r} is not one of {', '.join(_GH_API_UPDATE_FIELDS)}")
                if key in fields:
                    refuse(f"field {key!r} is given more than once")
                fields.append(key)
            continue
        if token.startswith("-"):
            refuse(f"flag {token!r} is not allowed")
        if endpoint is not None:
            refuse("more than one endpoint is given")
        endpoint = token
        index += 1

    if method != "PATCH":
        refuse(f"method {method or 'GET (the default)'!r} is not PATCH")
    own_pull = re.compile(
        rf"repos/{re.escape(owner)}/{re.escape(name)}/pulls/[1-9][0-9]*"
    )
    if endpoint is None or own_pull.fullmatch(endpoint) is None:
        refuse(f"endpoint {endpoint!r} is not repos/{repo}/pulls/<number>")
    if not fields:
        refuse("a PATCH must set title or body")


@dataclass(frozen=True)
class PrSnapshot:
    """One poll of a pull request, normalized to the §32.1 vocabulary."""

    number: int
    url: str | None = None
    state: str | None = None
    merged: bool = False
    merged_at: str | None = None
    merge_commit_sha: str | None = None
    head_sha: str | None = None
    head_ref: str | None = None
    base_ref: str | None = None
    review_decision: str | None = None
    ci_status: str | None = None
    is_draft: bool | None = None
    title: str | None = None
    body: str | None = None
    reviews: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_pr_state_fields(self) -> dict[str, Any]:
        """Return the §32.1 fields this snapshot is authoritative for."""
        return {
            "pr_number": self.number,
            "pr_url": self.url,
            "pr_state": self.state,
            "pr_merged": self.merged,
            "pr_head_sha": self.head_sha,
            "merge_commit_sha": self.merge_commit_sha,
            "review_decision": self.review_decision,
            "ci_status": self.ci_status,
        }

    @property
    def latest_review(self) -> dict[str, Any] | None:
        return self.reviews[-1] if self.reviews else None


class GitHubPrAdapter:
    """A narrow ``gh`` CLI wrapper: create, update, and poll pull requests."""

    def __init__(
        self,
        repo: str,
        *,
        runner: Runner | None = None,
        gh_bin: str = "gh",
    ) -> None:
        normalized = repo.strip()
        if not normalized:
            raise GitHubPrError("repo must not be empty")
        self.repo = normalized
        self._runner = runner or subprocess.run
        self._gh_bin = gh_bin

    # -- command execution -------------------------------------------------
    def run(self, argv: Sequence[str], *, cwd: Path) -> CompletedProcessLike:
        """Run one gh command after the merge guard and gh api allowlist clear it."""
        assert_not_a_merge_command(argv)
        assert_gh_api_allowed(argv, repo=self.repo)
        return self._runner(
            list(argv),
            cwd=Path(cwd),
            shell=False,
            check=False,
            text=True,
            # Non-UTF-8 output must never raise (Ruling 31b).
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _run_checked(self, argv: Sequence[str], *, cwd: Path, action: str) -> str:
        completed = self.run(argv, cwd=cwd)
        if completed.returncode != 0:
            raise GitHubPrError(
                f"{action} failed with {completed.returncode}: "
                f"{(completed.stderr or '').strip()}"
            )
        return completed.stdout or ""

    # -- operations --------------------------------------------------------
    def create_pr(
        self,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        cwd: Path,
        draft: bool = True,
    ) -> PrSnapshot:
        """Create a pull request and return its identity (§24, §31).

        The identity — number and URL — comes from `gh pr create`'s own output.
        There is deliberately no follow-up poll: if one raised, a PR that
        really exists on GitHub would be left unrecorded and a retry would open
        a second one (review round 4, Ruling 29d). The caller records this
        identity before doing anything else.
        """
        argv = [self._gh_bin, "pr", "create", "--repo", self.repo]
        if draft:
            argv.append("--draft")
        argv.extend(
            ["--base", base, "--head", head, "--title", title, "--body", body]
        )
        stdout = self._run_checked(argv, cwd=cwd, action="gh pr create")
        return PrSnapshot(
            number=self._extract_pr_number(stdout),
            url=self._extract_pr_url(stdout),
            state="open",
            merged=False,
            head_ref=head,
            base_ref=base,
            is_draft=draft,
        )

    def update_pr(
        self,
        *,
        pr_number: int,
        cwd: Path,
        title: str | None = None,
        body: str | None = None,
    ) -> PrSnapshot:
        """Update the *same* PR in place — the number never changes (§26).

        Uses the REST endpoint, ``gh api -X PATCH repos/<repo>/pulls/<n>``:
        ``gh pr edit`` fails on gh 2.45.0 with the Projects (classic) GraphQL
        deprecation error, so re-integration could not update a PR (review
        Ruling 35). Fields go as ``-f`` raw strings, so a body that starts with
        ``@`` or reads ``true`` is sent literally, never read from a file or
        converted. The PATCH leaves the draft state alone.
        """
        fields: list[str] = []
        if title is not None:
            fields.extend(["-f", f"title={title}"])
        if body is not None:
            fields.extend(["-f", f"body={body}"])
        if not fields:
            return self.poll_pr(pr_number=pr_number, cwd=cwd)
        argv = [
            self._gh_bin,
            "api",
            "-X",
            "PATCH",
            f"repos/{self.repo}/pulls/{int(pr_number)}",
            *fields,
        ]
        self._run_checked(argv, cwd=cwd, action="gh api PATCH pulls")
        return self.poll_pr(pr_number=pr_number, cwd=cwd)

    def poll_pr(self, *, pr_number: int, cwd: Path) -> PrSnapshot:
        """Read a PR's current state. Read-only and safe to repeat (§32)."""
        argv = [
            self._gh_bin,
            "pr",
            "view",
            str(pr_number),
            "--repo",
            self.repo,
            "--json",
            ",".join(PR_VIEW_FIELDS),
        ]
        stdout = self._run_checked(argv, cwd=cwd, action="gh pr view")
        return self._snapshot(self._parse_json(stdout), fallback_number=pr_number)

    # -- normalization -----------------------------------------------------
    def _snapshot(self, payload: dict[str, Any], *, fallback_number: int) -> PrSnapshot:
        number = payload.get("number")
        merge_commit = payload.get("mergeCommit") or {}
        merge_commit_sha = (
            merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        ) or None
        merged_at = payload.get("mergedAt") or None
        merged = self._is_merged(payload.get("state"), merged_at, merge_commit_sha)
        return PrSnapshot(
            number=number if isinstance(number, int) else fallback_number,
            url=payload.get("url"),
            state=self._normalize_state(payload.get("state"), merged=merged),
            merged=merged,
            merged_at=merged_at,
            merge_commit_sha=merge_commit_sha,
            head_sha=payload.get("headRefOid") or None,
            head_ref=payload.get("headRefName") or None,
            base_ref=payload.get("baseRefName") or None,
            review_decision=self._normalize_review_decision(payload.get("reviewDecision")),
            ci_status=self._normalize_ci_status(payload.get("statusCheckRollup")),
            is_draft=payload.get("isDraft"),
            title=payload.get("title"),
            body=payload.get("body"),
            reviews=self._normalize_reviews(payload.get("reviews")),
        )

    @staticmethod
    def _is_merged(state: Any, merged_at: Any, merge_commit_sha: Any) -> bool:
        """Derive the merge outcome from fields real gh supports (Ruling 29a).

        A PR is merged when gh reports ``state == "MERGED"``, or carries a
        ``mergedAt`` time or a ``mergeCommit``. Deriving it matters: dropping
        the non-existent ``merged`` field without this would make a merged PR
        read as closed-unmerged, and the watcher would cancel a merged Ticket.
        §35/§36 verification is still driven by the merge commit itself.
        """
        if isinstance(state, str) and state.strip().upper() == "MERGED":
            return True
        return bool(merged_at) or bool(merge_commit_sha)

    @staticmethod
    def _normalize_state(raw: Any, *, merged: bool) -> str | None:
        """Map gh's OPEN/CLOSED/MERGED onto the §32.1 open|closed enum.

        MERGED is a closed PR; §32.1 carries the merge fact in ``pr_merged``
        rather than in ``pr_state``.
        """
        if merged:
            return "closed"
        if not isinstance(raw, str) or not raw.strip():
            return None
        normalized = raw.strip().upper()
        if normalized == "OPEN":
            return "open"
        if normalized in {"CLOSED", "MERGED"}:
            return "closed"
        return None

    @staticmethod
    def _normalize_review_decision(raw: Any) -> str | None:
        if not isinstance(raw, str):
            return None
        normalized = raw.strip().upper()
        if normalized == "APPROVED":
            return "approved"
        if normalized == "CHANGES_REQUESTED":
            return "changes_requested"
        # REVIEW_REQUIRED and an empty decision both mean "nobody has decided".
        return "none"

    @staticmethod
    def _normalize_ci_status(rollup: Any) -> str | None:
        """Collapse the status check rollup into the §32.1 enum.

        Failure dominates pending, which dominates success, so a green-looking
        summary can never hide a red or still-running check.
        """
        if rollup is None:
            return None
        if not isinstance(rollup, list):
            return "none"
        if not rollup:
            return "none"

        states: list[str] = []
        for check in rollup:
            if not isinstance(check, dict):
                continue
            raw = check.get("conclusion") or check.get("state") or check.get("status") or ""
            states.append(str(raw).strip().upper())

        if any(state in _CI_FAILURE_STATES for state in states):
            return "failure"
        if any(state in _CI_PENDING_STATES for state in states):
            return "pending"
        if any(state in _CI_SUCCESS_STATES for state in states):
            return "success"
        return "none"

    @staticmethod
    def _normalize_reviews(raw: Any) -> tuple[dict[str, Any], ...]:
        if not isinstance(raw, list):
            return ()
        reviews: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            author = entry.get("author")
            login = author.get("login") if isinstance(author, dict) else author
            reviews.append(
                {
                    "author": login,
                    "state": entry.get("state"),
                    "body": entry.get("body"),
                    "submitted_at": entry.get("submittedAt"),
                }
            )
        return tuple(reviews)

    @staticmethod
    def _extract_pr_url(stdout: str) -> str | None:
        match = re.search(r"https?://\S+/pull/\d+", stdout or "")
        return match.group(0) if match else None

    @staticmethod
    def _extract_pr_number(stdout: str) -> int:
        match = re.search(r"/pull/(\d+)", stdout or "")
        if match is None:
            raise GitHubPrError("gh pr create did not print a created PR URL")
        return int(match.group(1))

    @staticmethod
    def _parse_json(stdout: str) -> dict[str, Any]:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GitHubPrError(f"gh returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise GitHubPrError("gh returned non-object JSON")
        return payload
