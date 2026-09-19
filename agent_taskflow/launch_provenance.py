"""Captured launch inputs, never permission or lifecycle authority.

References describe bytes already selected by a runner. No evidence consumer
opens the referenced paths, and no prompt, environment or credential is stored.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LaunchContentReference:
    reference: str
    sha256: str
    length_bytes: int
    source: str
    encoding: str = "utf-8"

    @classmethod
    def from_text(
        cls, reference: str | Path, text: str, *, source: str,
    ) -> LaunchContentReference | None:
        """Hash selected text as UTF-8, not a later read of a mutable file."""
        if not isinstance(reference, (str, Path)) or not str(reference).strip():
            return None
        if not isinstance(source, str) or not source.strip():
            return None
        try:
            content = text.encode("utf-8")
            return cls(str(reference), hashlib.sha256(content).hexdigest(), len(content), source)
        except (TypeError, AttributeError, UnicodeError):
            return None  # Optional metadata must not change the invocation outcome.


@dataclass(frozen=True)
class ExecutorLaunchProvenance:
    canonical_execution_path: str | None = None
    path_source: str | None = None
    base_commit: str | None = None
    base_source: str | None = None
    policy_version: str | None = None
    permission_profile: str | None = None
    spec_reference: LaunchContentReference | None = None
    config_snapshot_reference: LaunchContentReference | None = None
    prompt_reference: LaunchContentReference | None = None
    requested_model: str | None = None
    model_source: str | None = None
    requested_tools: tuple[str, ...] | None = None

    def for_adapter(
        self, *, model: str | None, model_source: str,
        prompt: str | None = None, prompt_path: Path | None = None,
        prompt_source: str | None = None, tools: tuple[str, ...] | None = None,
    ) -> ExecutorLaunchProvenance:
        return replace(
            self, requested_model=model, model_source=model_source, requested_tools=tools,
            prompt_reference=(LaunchContentReference.from_text(
                prompt_path, prompt, source=prompt_source,
            ) if prompt is not None and prompt_path is not None and prompt_source else None),
        )


def runner_configuration(reference: str, **selected: Any) -> LaunchContentReference | None:
    """Digest an allowlisted projection of resolved runtime configuration.

    Callers explicitly enumerate selected fields; never pass a request, task,
    argv or environment wholesale. This is not a claim about a config file.
    """
    try:
        text = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return LaunchContentReference.from_text(reference, text, source="runner_selected_fields_json")


def launch_provenance_payload(value: ExecutorLaunchProvenance | None) -> dict[str, Any]:
    """Normalize optional metadata without accepting malformed observations."""
    result: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    content_fields = {"prompt_reference", "spec_reference", "config_snapshot_reference"}
    for field in ExecutorLaunchProvenance.__dataclass_fields__:
        item = getattr(value, field, None) if isinstance(value, ExecutorLaunchProvenance) else None
        valid = isinstance(item, str) and bool(item.strip())
        if field in content_fields:
            valid = (
                isinstance(item, LaunchContentReference)
                and isinstance(item.reference, str) and bool(item.reference.strip())
                and isinstance(item.source, str) and bool(item.source.strip())
                and isinstance(item.sha256, str) and len(item.sha256) == 64
                and all(c in "0123456789abcdef" for c in item.sha256)
                and type(item.length_bytes) is int and item.length_bytes >= 0
                and item.encoding == "utf-8"
            )
        elif field == "requested_tools":
            valid = isinstance(item, tuple) and all(isinstance(t, str) and t.strip() for t in item)
        result[field] = (asdict(item) if field in content_fields else
                         list(item) if field == "requested_tools" else item) if valid else None
        if not valid:
            reasons[field] = "not_observed_by_runner" if item is None else "invalid_runner_metadata"
    result["unknown_reasons"] = reasons
    return result
