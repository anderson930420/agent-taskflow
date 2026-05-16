"""Shared workflow policy artifact metadata constants.

These constants encode the canonical artifact metadata contract frozen in
docs/workflow-policy-artifact-metadata-contract.md. All code that references
canonical workflow policy artifact filenames, types, kinds, or package types
should import from this module to avoid duplication and drift.

This module does not add runtime behavior, dispatcher logic, API endpoints,
or Mission Control UI changes. It is a pure constants definition.
"""

from __future__ import annotations


# ------------------------------------------------------------------
# Canonical artifact filenames
# ------------------------------------------------------------------

WORKFLOW_POLICY_SUMMARY_FILENAME = "workflow_policy_summary.json"
"""Filename of the machine-readable workflow policy summary artifact."""

WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME = "artifact_index.json"
"""Filename of the proof-of-work package artifact index file."""


# ------------------------------------------------------------------
# Canonical store artifact types (TASK_ARTIFACT_TYPES values)
# ------------------------------------------------------------------

WORKFLOW_POLICY_SUMMARY_ARTIFACT_TYPE = "workflow_policy_summary"
"""Store artifact type for the workflow policy summary artifact."""

WORKFLOW_POLICY_ARTIFACT_INDEX_TYPE = "artifact_index"
"""Store artifact type for the proof-of-work package artifact index."""


# ------------------------------------------------------------------
# Canonical review evidence file kind
# ------------------------------------------------------------------

WORKFLOW_POLICY_REVIEW_KIND = "workflow_policy"
"""File kind assigned to canonical workflow policy artifact files in review evidence."""


# ------------------------------------------------------------------
# Canonical proof-of-work package type
# ------------------------------------------------------------------

WORKFLOW_POLICY_PACKAGE_TYPE = "workflow_policy_proof_of_work"
"""Value of package_type in artifact_index.json for workflow policy packages."""


# ------------------------------------------------------------------
# Artifact index version
# ------------------------------------------------------------------

WORKFLOW_POLICY_ARTIFACT_INDEX_VERSION = "0.1"
"""Version string for the artifact_index.json format."""


# ------------------------------------------------------------------
# Named sets for convenience
# ------------------------------------------------------------------

WORKFLOW_POLICY_ARTIFACT_FILENAMES = frozenset({
    WORKFLOW_POLICY_SUMMARY_FILENAME,
    WORKFLOW_POLICY_ARTIFACT_INDEX_FILENAME,
})
"""Frozenset of canonical workflow policy artifact filenames."""


# ------------------------------------------------------------------
# Required fields (for documentation and verification)
# ------------------------------------------------------------------

WORKFLOW_POLICY_REQUIRED_SUMMARY_FIELDS = (
    "artifact_type",
    "schema_version",
    "source_path",
    "validation_status",
    "validation_errors",
    "validation_warnings",
    "allowed_executors",
    "required_validators",
    "path_policy",
    "workspace_policy",
    "proof_of_work",
    "human_review",
    "forbidden_actions",
    "deferred_integrations",
    "governance_invariants",
    "generated_at",
)
"""Tuple of required top-level fields in workflow_policy_summary.json.

Note: validation_errors and validation_warnings are required fields (they may
be empty lists when validation passes, but the keys must be present).
"""

WORKFLOW_POLICY_REQUIRED_INDEX_TOP_LEVEL_FIELDS = (
    "artifact_index_version",
    "package_type",
    "generated_at",
    "artifacts",
)
"""Tuple of required top-level fields in artifact_index.json."""


# ------------------------------------------------------------------
# Backward compatibility note
# ------------------------------------------------------------------

# The existing "other" artifact type remains valid for non-canonical artifacts.
# New workflow policy artifacts should use the explicit types above.
# DB schema is unchanged; artifact type is stored as TEXT.