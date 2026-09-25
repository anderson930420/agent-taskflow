"""The V1 execution policy: one resolver over ``config/projects.yaml`` (RULINGS 67).

OWNER RULING D1 makes each project's ``execution:`` block in the repository
registry the only repo-level source of V1 execution policy: the executor and
its argv, model, effort, timeout, permission profile, the implementation and
integration validators, and a ``policy_version`` bumped on every change. A V1
Ticket accepts no CLI, API or legacy override of any of it. A project without a
valid policy is not runnable, which also settles H6: the claim path refuses its
Tickets (:mod:`agent_taskflow.ready_queue`, :mod:`agent_taskflow.runtime_admission`).

:func:`resolve_execution_policy` is the only resolver. It reads the registry by
absolute path, anchored to this package (never the working directory), validates
the block strictly, and returns a frozen :class:`ExecutionPolicy` carrying the
sha256 of its canonical form. Every refusal raises :class:`ExecutionPolicyError`
with one of the ``POLICY_*`` reason codes, so a caller can show why a Ticket is
not runnable. Nothing here runs a process or writes a file.

Schema (every key required, no other key accepted)::

    execution:
      executor: claude-code              # EXECUTOR_ALLOWLIST; manual/noop/shell never
      argv: [claude, --print, --model, "{model}", --effort, "{effort}"]
      model: <model id>                  # substituted for {model}
      effort: <effort level>             # substituted for {effort}
      timeout_seconds: 3600              # executor wall clock, 1..86400
      permission_profile: <profile id>
      policy_version: "<version>"        # bump whenever the block changes
      implementation_validators:         # run by the dispatcher, by registry name
        - {name: pytest, timeout_seconds: 1800}
      integration_validators:            # run by the integration tick
        - {name: pytest, command: [python, -m, pytest, -q], timeout_seconds: 1800}

``argv`` must contain ``{model}`` and ``{effort}`` exactly once each, so the
model and effort recorded on the Attempt are the ones the executor receives.
The policy holds no secret: no token, credential or environment value belongs in
it, and none is ever recorded from it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

#: The registry every V1 entry point resolves policy from. Absolute and anchored
#: to the installed package, so a process's working directory never selects it.
PROJECTS_REGISTRY_PATH = PACKAGE_ROOT / "config" / "projects.yaml"

#: Executors a V1 policy may name. ``manual``, ``noop`` and ``shell`` never run a
#: real implementation, and the other adapters are not yet admitted (RULINGS 67).
EXECUTOR_ALLOWLIST = frozenset({"claude-code"})

POLICY_REGISTRY_UNAVAILABLE = "execution_policy_registry_unavailable"
POLICY_PROJECT_NOT_REGISTERED = "execution_policy_project_not_registered"
POLICY_MISSING = "execution_policy_missing"
POLICY_INVALID = "execution_policy_invalid"
POLICY_EXECUTOR_NOT_ALLOWED = "execution_policy_executor_not_allowed"
POLICY_VALIDATORS_EMPTY = "execution_policy_validators_empty"
POLICY_TIMEOUT_MISSING = "execution_policy_timeout_missing"
POLICY_OVERRIDE_REFUSED = "execution_policy_override_refused"
POLICY_CHANGED = "execution_policy_changed"
POLICY_REPO_PATH_MISMATCH = "execution_policy_repo_path_mismatch"

POLICY_REASON_CODES = frozenset(
    {
        POLICY_REGISTRY_UNAVAILABLE,
        POLICY_PROJECT_NOT_REGISTERED,
        POLICY_MISSING,
        POLICY_INVALID,
        POLICY_EXECUTOR_NOT_ALLOWED,
        POLICY_VALIDATORS_EMPTY,
        POLICY_TIMEOUT_MISSING,
        POLICY_OVERRIDE_REFUSED,
        POLICY_CHANGED,
        POLICY_REPO_PATH_MISMATCH,
    }
)

POLICY_SCHEMA_VERSION = "execution_policy.v1"
POLICY_SNAPSHOT_FILENAME = "execution_policy.json"
MAX_TIMEOUT_SECONDS = 86400

_POLICY_KEYS = frozenset(
    {
        "executor",
        "argv",
        "model",
        "effort",
        "timeout_seconds",
        "permission_profile",
        "policy_version",
        "implementation_validators",
        "integration_validators",
    }
)
_PLACEHOLDERS = ("{model}", "{effort}")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")


class ExecutionPolicyError(ValueError):
    """A project has no usable V1 execution policy. ``reason_code`` says why."""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in POLICY_REASON_CODES:
            raise ValueError(f"Unknown execution policy reason code: {reason_code!r}")
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}")


@dataclass(frozen=True)
class ImplementationValidatorPolicy:
    name: str
    timeout_seconds: int


@dataclass(frozen=True)
class IntegrationValidatorPolicy:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class ExecutionPolicy:
    """One project's validated V1 execution policy. Immutable."""

    project: str
    executor: str
    argv: tuple[str, ...]
    model: str
    effort: str
    timeout_seconds: int
    permission_profile: str
    policy_version: str
    implementation_validators: tuple[ImplementationValidatorPolicy, ...]
    integration_validators: tuple[IntegrationValidatorPolicy, ...]
    registry_path: Path
    sha256: str
    # The registry entry's absolute repo_path (not part of the policy hash). A
    # Ticket runs only when its stored repo_path is this one.
    repo_path: Path | None = None

    def canonical(self) -> dict[str, Any]:
        """The hashed form: the policy block only, not where it was read from."""
        return _canonical_block(
            executor=self.executor,
            argv=self.argv,
            model=self.model,
            effort=self.effort,
            timeout_seconds=self.timeout_seconds,
            permission_profile=self.permission_profile,
            policy_version=self.policy_version,
            implementation_validators=self.implementation_validators,
            integration_validators=self.integration_validators,
        )

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical())

    def resolved_argv(self) -> tuple[str, ...]:
        """The executor argv with ``{model}`` and ``{effort}`` substituted."""
        return tuple(
            part.replace("{model}", self.model).replace("{effort}", self.effort)
            for part in self.argv
        )

    @property
    def implementation_validator_names(self) -> tuple[str, ...]:
        return tuple(v.name for v in self.implementation_validators)

    def implementation_validator_timeout(self, name: str) -> int:
        for validator in self.implementation_validators:
            if validator.name == name:
                return validator.timeout_seconds
        raise KeyError(name)

    def integration_validator_specs(self) -> tuple[Any, ...]:
        """The integration tick's validator specs, built from this policy only."""
        from agent_taskflow.integration_validators import IntegrationValidatorSpec

        return tuple(
            IntegrationValidatorSpec(v.name, v.command, v.timeout_seconds)
            for v in self.integration_validators
        )

    def snapshot(self) -> dict[str, Any]:
        """The Attempt's evidence copy: effective values, sha256, no secrets."""
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "project": self.project,
            "policy_sha256": self.sha256,
            "registry_path": str(self.registry_path),
            "resolved_argv": list(self.resolved_argv()),
            "policy": self.canonical(),
        }


def _canonical_block(
    *,
    executor: str,
    argv: tuple[str, ...],
    model: str,
    effort: str,
    timeout_seconds: int,
    permission_profile: str,
    policy_version: str,
    implementation_validators: tuple[ImplementationValidatorPolicy, ...],
    integration_validators: tuple[IntegrationValidatorPolicy, ...],
) -> dict[str, Any]:
    return {
        "executor": executor,
        "argv": list(argv),
        "model": model,
        "effort": effort,
        "timeout_seconds": timeout_seconds,
        "permission_profile": permission_profile,
        "policy_version": policy_version,
        "implementation_validators": [
            {"name": v.name, "timeout_seconds": v.timeout_seconds}
            for v in implementation_validators
        ],
        "integration_validators": [
            {"name": v.name, "command": list(v.command), "timeout_seconds": v.timeout_seconds}
            for v in integration_validators
        ],
    }


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _invalid(project: str, message: str) -> ExecutionPolicyError:
    return ExecutionPolicyError(POLICY_INVALID, f"project {project!r}: {message}")


def _token(project: str, block: Mapping[str, Any], key: str) -> str:
    value = block.get(key)
    if not isinstance(value, str) or not _TOKEN.match(value):
        raise _invalid(project, f"{key} must be a non-empty token without spaces or braces")
    return value


def _timeout(project: str, value: Any, where: str) -> int:
    if value is None:
        raise ExecutionPolicyError(
            POLICY_TIMEOUT_MISSING, f"project {project!r}: {where} timeout_seconds is required"
        )
    if type(value) is not int or not 1 <= value <= MAX_TIMEOUT_SECONDS:
        raise _invalid(
            project, f"{where} timeout_seconds must be an integer in 1..{MAX_TIMEOUT_SECONDS}"
        )
    return value


def _validator_list(project: str, block: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    values = block.get(key)
    if values is None or values == []:
        raise ExecutionPolicyError(
            POLICY_VALIDATORS_EMPTY, f"project {project!r}: {key} must list at least one validator"
        )
    if not isinstance(values, list):
        raise _invalid(project, f"{key} must be a list")
    for value in values:
        if not isinstance(value, dict):
            raise _invalid(project, f"each {key} entry must be a mapping")
    names = [value.get("name") for value in values]
    if len(set(map(str, names))) != len(names):
        raise _invalid(project, f"{key} names must be unique")
    return values


def parse_execution_policy(
    project: str,
    block: Any,
    *,
    registry_path: Path,
) -> ExecutionPolicy:
    """Validate one raw ``execution:`` block. Raises :class:`ExecutionPolicyError`."""
    from agent_taskflow.validators.registry import list_validator_names

    if block is None:
        raise ExecutionPolicyError(
            POLICY_MISSING, f"project {project!r} has no execution: policy"
        )
    if not isinstance(block, dict):
        raise _invalid(project, "execution: must be a mapping")
    unknown = sorted(set(map(str, block)) - _POLICY_KEYS)
    if unknown:
        raise _invalid(project, f"unknown execution keys: {', '.join(unknown)}")

    executor = block.get("executor")
    if not isinstance(executor, str) or not executor.strip():
        raise _invalid(project, "executor is required")
    if executor not in EXECUTOR_ALLOWLIST:
        raise ExecutionPolicyError(
            POLICY_EXECUTOR_NOT_ALLOWED,
            f"project {project!r}: executor {executor!r} is not one of "
            f"{', '.join(sorted(EXECUTOR_ALLOWLIST))}",
        )

    timeout_seconds = _timeout(project, block.get("timeout_seconds"), "executor")

    argv = block.get("argv")
    if (
        not isinstance(argv, list) or not argv
        or any(not isinstance(part, str) or not part.strip() for part in argv)
    ):
        raise _invalid(project, "argv must be a non-empty list of non-empty strings")
    joined = "\0".join(argv)
    for placeholder in _PLACEHOLDERS:
        if joined.count(placeholder) != 1:
            raise _invalid(project, f"argv must contain {placeholder} exactly once")
    stripped = joined
    for placeholder in _PLACEHOLDERS:
        stripped = stripped.replace(placeholder, "")
    if "{" in stripped or "}" in stripped:
        raise _invalid(project, "argv may use only the {model} and {effort} placeholders")
    if "{" in argv[0]:
        raise _invalid(project, "argv[0] must name the executable, not a placeholder")

    model = _token(project, block, "model")
    effort = _token(project, block, "effort")
    permission_profile = _token(project, block, "permission_profile")
    raw_version = block.get("policy_version")
    if isinstance(raw_version, int) and not isinstance(raw_version, bool):
        raise _invalid(project, "policy_version must be a quoted string")
    policy_version = _token(project, block, "policy_version")

    known_validators = set(list_validator_names())
    implementation: list[ImplementationValidatorPolicy] = []
    for value in _validator_list(project, block, "implementation_validators"):
        if set(map(str, value)) - {"name", "timeout_seconds"}:
            raise _invalid(project, "implementation_validators entries take name and timeout_seconds")
        name = value.get("name")
        if not isinstance(name, str) or name not in known_validators:
            raise _invalid(
                project,
                f"implementation validator {name!r} is not one of {', '.join(sorted(known_validators))}",
            )
        implementation.append(
            ImplementationValidatorPolicy(
                name, _timeout(project, value.get("timeout_seconds"), f"validator {name!r}")
            )
        )

    integration: list[IntegrationValidatorPolicy] = []
    for value in _validator_list(project, block, "integration_validators"):
        if set(map(str, value)) - {"name", "command", "timeout_seconds"}:
            raise _invalid(
                project, "integration_validators entries take name, command and timeout_seconds"
            )
        name = value.get("name")
        if not isinstance(name, str) or not _TOKEN.match(name):
            raise _invalid(project, "integration validator name must be a non-empty token")
        command = value.get("command")
        if (
            not isinstance(command, list) or not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            raise _invalid(
                project, f"integration validator {name!r} command must be a non-empty list of strings"
            )
        integration.append(
            IntegrationValidatorPolicy(
                name,
                tuple(command),
                _timeout(project, value.get("timeout_seconds"), f"validator {name!r}"),
            )
        )

    canonical = _canonical_block(
        executor=executor,
        argv=tuple(argv),
        model=model,
        effort=effort,
        timeout_seconds=timeout_seconds,
        permission_profile=permission_profile,
        policy_version=policy_version,
        implementation_validators=tuple(implementation),
        integration_validators=tuple(integration),
    )
    return ExecutionPolicy(
        project=project,
        executor=executor,
        argv=tuple(argv),
        model=model,
        effort=effort,
        timeout_seconds=timeout_seconds,
        permission_profile=permission_profile,
        policy_version=policy_version,
        implementation_validators=tuple(implementation),
        integration_validators=tuple(integration),
        registry_path=registry_path,
        sha256=hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest(),
    )


def _registry_path(config_path: str | Path | None) -> Path:
    path = Path(PROJECTS_REGISTRY_PATH if config_path is None else config_path)
    if not path.is_absolute():
        raise ExecutionPolicyError(
            POLICY_REGISTRY_UNAVAILABLE,
            f"the project registry path must be absolute, not {str(path)!r}",
        )
    return path


def _strict_yaml_load(text: str) -> Any:
    """``yaml.safe_load`` that refuses a duplicated mapping key anywhere.

    PyYAML keeps the last of two equal keys, so a second ``execution:`` block
    or field would silently win. Here it is an error instead. YAML merge keys
    (``<<``) are not counted as duplicates.
    """
    # Imported here: admission CLIs that run without site-packages import
    # this module and must not need PyYAML until a policy is actually read.
    import yaml

    class DuplicateKeyError(yaml.constructor.ConstructorError):
        pass

    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: StrictLoader, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = loader.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
            except TypeError:
                continue  # SafeLoader reports an unhashable key itself
            if duplicate:
                raise DuplicateKeyError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    try:
        return yaml.load(text, Loader=StrictLoader)  # noqa: S506 - SafeLoader subclass
    except DuplicateKeyError as exc:
        raise ExecutionPolicyError(
            POLICY_INVALID, f"duplicate mapping key in the project registry: {exc}"
        ) from exc


def load_registry_projects(config_path: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    """Read the registry's ``projects`` mapping. Raises :class:`ExecutionPolicyError`.

    This is also the registry reader of Ticket creation
    (:mod:`agent_taskflow.ticket_repositories`), so both read the same file the
    same strict way.
    """
    import yaml

    path = _registry_path(config_path)
    try:
        data = _strict_yaml_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ExecutionPolicyError(
            POLICY_REGISTRY_UNAVAILABLE, f"cannot read {path}: {exc.__class__.__name__}: {exc}"
        ) from exc
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise ExecutionPolicyError(
            POLICY_REGISTRY_UNAVAILABLE, f"{path} has no projects: mapping"
        )
    return path, projects


def resolve_execution_policy(
    project: str,
    *,
    config_path: str | Path | None = None,
) -> ExecutionPolicy:
    """Return ``project``'s validated policy, or raise :class:`ExecutionPolicyError`."""
    path, projects = load_registry_projects(config_path)
    name = str(project or "").strip()
    entry = projects.get(name) if name else None
    if not isinstance(entry, dict):
        raise ExecutionPolicyError(
            POLICY_PROJECT_NOT_REGISTERED, f"project {name!r} is not in {path}"
        )
    policy = parse_execution_policy(name, entry.get("execution"), registry_path=path)
    raw_repo_path = entry.get("repo_path")
    repo_path = (
        Path(raw_repo_path)
        if isinstance(raw_repo_path, str) and Path(raw_repo_path).is_absolute()
        else None
    )
    return replace(policy, repo_path=repo_path)


def ticket_repo_path_refusal(
    policy: ExecutionPolicy,
    ticket_repo_path: str | Path | None,
) -> ExecutionPolicyError | None:
    """Refuse a Ticket whose stored repo_path is not its project's registry entry's.

    Ticket creation copied repo_path from the registry when the Ticket was
    made; if the entry has since moved, or the Ticket was made from another
    registry, the policy would run against a repository it was not written for.
    """
    def resolved(value: str | Path | None) -> Path | None:
        if value is None or not str(value).strip() or not Path(value).is_absolute():
            return None
        return Path(value).resolve()

    registry_repo, ticket_repo = resolved(policy.repo_path), resolved(ticket_repo_path)
    if registry_repo is None or ticket_repo is None or registry_repo != ticket_repo:
        return ExecutionPolicyError(
            POLICY_REPO_PATH_MISMATCH,
            f"project {policy.project!r}: the Ticket's repo_path {str(ticket_repo_path)!r} is not "
            f"the registry's repo_path {str(policy.repo_path)!r} in {policy.registry_path}",
        )
    return None


def execution_policy_refusal(
    project: str,
    *,
    config_path: str | Path | None = None,
) -> ExecutionPolicyError | None:
    """Return why ``project`` is not runnable, or None when its policy is valid."""
    try:
        resolve_execution_policy(project, config_path=config_path)
    except ExecutionPolicyError as exc:
        return exc
    return None


def resolve_repository_execution_policy(
    *,
    github_repo: str,
    repo_path: str | Path,
    config_path: str | Path | None = None,
) -> ExecutionPolicy:
    """The policy of the one project registered for ``github_repo`` at ``repo_path``."""
    path, projects = load_registry_projects(config_path)
    target = Path(repo_path).expanduser().resolve()
    matches = [
        name
        for name, entry in projects.items()
        if isinstance(entry, dict)
        and str(entry.get("github_repo") or "").strip() == github_repo
        and isinstance(entry.get("repo_path"), str)
        and Path(entry["repo_path"]).expanduser().resolve() == target
    ]
    if len(matches) != 1:
        raise ExecutionPolicyError(
            POLICY_PROJECT_NOT_REGISTERED,
            f"{len(matches)} projects in {path} register {github_repo!r} at {target}; exactly one is required",
        )
    return resolve_execution_policy(str(matches[0]), config_path=path)


__all__ = [
    "EXECUTOR_ALLOWLIST",
    "ExecutionPolicy",
    "ExecutionPolicyError",
    "ImplementationValidatorPolicy",
    "IntegrationValidatorPolicy",
    "PACKAGE_ROOT",
    "POLICY_CHANGED",
    "POLICY_EXECUTOR_NOT_ALLOWED",
    "POLICY_INVALID",
    "POLICY_MISSING",
    "POLICY_OVERRIDE_REFUSED",
    "POLICY_PROJECT_NOT_REGISTERED",
    "POLICY_REASON_CODES",
    "POLICY_REGISTRY_UNAVAILABLE",
    "POLICY_REPO_PATH_MISMATCH",
    "POLICY_SCHEMA_VERSION",
    "POLICY_SNAPSHOT_FILENAME",
    "POLICY_TIMEOUT_MISSING",
    "POLICY_VALIDATORS_EMPTY",
    "PROJECTS_REGISTRY_PATH",
    "execution_policy_refusal",
    "load_registry_projects",
    "parse_execution_policy",
    "resolve_execution_policy",
    "resolve_repository_execution_policy",
    "ticket_repo_path_refusal",
]
