import React from "react";
import { StatusBadge } from "./StatusBadge";
import type {
  ArtifactFileSummary,
  MissionContractSummary,
  TaskReviewEvidence,
  ValidatorResultSummary,
} from "../lib/types";

interface ReviewEvidencePanelProps {
  evidence: TaskReviewEvidence;
  onPreviewArtifact: (name: string) => void;
}

function ValueOrDash({ value }: { value: unknown }): React.ReactElement {
  if (value === undefined || value === null || value === "") {
    return <span>—</span>;
  }
  return <span>{String(value)}</span>;
}

function ContractCard({
  contract,
}: {
  contract: MissionContractSummary;
}): React.ReactElement {
  const statusLabel =
    contract.status === "present"
      ? "Present"
      : contract.status === "missing"
      ? "Missing"
      : "Invalid";

  return (
    <div className="panel">
      <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "12px" }}>
        <h3 style={{ margin: 0 }}>Mission Contract</h3>
        <span
          style={{
            padding: "2px 8px",
            borderRadius: "4px",
            fontSize: "12px",
            fontWeight: 500,
            backgroundColor: contract.status === "present" ? "#10b981" : "#ef4444",
            color: "#fff",
          }}
        >
          {statusLabel}
        </span>
      </div>

      {contract.status !== "present" && (
        <div
          style={{
            padding: "8px 12px",
            backgroundColor: contract.status === "missing" ? "#f59e0b" : "#ef4444",
            color: "#fff",
            borderRadius: "4px",
            marginBottom: "12px",
            fontSize: "13px",
          }}
        >
          {contract.status === "missing"
            ? "mission_contract.json was not found in the artifact directory."
            : `mission_contract.json is invalid: ${contract.error ?? "unknown error"}`}
        </div>
      )}

      {contract.status === "present" && (
        <div className="table-wrap">
          <table>
            <tbody>
              {[
                ["Task key", contract.task_key],
                ["Goal", contract.goal],
                ["Executor", contract.executor],
                [
                  "Human approval required",
                  contract.human_approval_required ? "Yes" : "No",
                ],
                [
                  "Required validators",
                  (contract.required_validators ?? []).join(", ") || "—",
                ],
              ].map(([label, value]) => (
                <tr key={String(label)}>
                  <th>{label}</th>
                  <td><ValueOrDash value={value} /></td>
                </tr>
              ))}
            </tbody>
          </table>

          {(contract.forbidden_actions ?? []).length > 0 && (
            <>
              <p style={{ margin: "12px 0 4px 0", fontWeight: 500 }}>Forbidden actions</p>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
                {(contract.forbidden_actions ?? []).map((action) => (
                  <span
                    key={action}
                    style={{
                      padding: "2px 6px",
                      backgroundColor: "#1e293b",
                      color: "#94a3b8",
                      borderRadius: "3px",
                      fontSize: "11px",
                      fontFamily: "monospace",
                    }}
                  >
                    {action}
                  </span>
                ))}
              </div>
            </>
          )}

          {(contract.expected_artifacts ?? []).length > 0 && (
            <>
              <p style={{ margin: "12px 0 4px 0", fontWeight: 500 }}>Expected artifacts</p>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
                {(contract.expected_artifacts ?? []).map((artifact) => (
                  <span
                    key={artifact}
                    style={{
                      padding: "2px 6px",
                      backgroundColor: "#1e293b",
                      color: "#94a3b8",
                      borderRadius: "3px",
                      fontSize: "11px",
                      fontFamily: "monospace",
                    }}
                  >
                    {artifact}
                  </span>
                ))}
              </div>
            </>
          )}

          {(contract.governance_rules ?? []).length > 0 && (
            <>
              <p style={{ margin: "12px 0 4px 0", fontWeight: 500 }}>Governance rules</p>
              <ul style={{ margin: 0, paddingLeft: "20px", fontSize: "12px" }}>
                {(contract.governance_rules ?? []).map((rule, i) => (
                  <li key={i} style={{ marginBottom: "2px" }}>
                    {rule}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ValidatorResultsPanel({
  results,
}: {
  results: ValidatorResultSummary[];
}): React.ReactElement {
  if (results.length === 0) {
    return (
      <div className="panel">
        <h3 style={{ margin: "0 0 12px 0" }}>Validator Results</h3>
        <div className="empty">No validator results recorded.</div>
      </div>
    );
  }

  return (
    <div className="panel">
      <h3 style={{ margin: "0 0 12px 0" }}>Validator Results</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Validator</th>
              <th>Status</th>
              <th>Exit code</th>
              <th>Summary</th>
            </tr>
          </thead>
          <tbody>
            {results.map((result, i) => (
              <tr key={i}>
                <td><ValueOrDash value={result.validator} /></td>
                <td>
                  <StatusBadge status={result.status} />
                </td>
                <td><ValueOrDash value={result.exit_code} /></td>
                <td><ValueOrDash value={result.summary} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ArtifactListPanel({
  artifacts,
  onPreview,
}: {
  artifacts: ArtifactFileSummary[];
  onPreview: (name: string) => void;
}): React.ReactElement {
  if (artifacts.length === 0) {
    return (
      <div className="panel">
        <h3 style={{ margin: "0 0 12px 0" }}>Artifacts</h3>
        <div className="empty">No artifacts found in artifact directory.</div>
      </div>
    );
  }

  return (
    <div className="panel">
      <h3 style={{ margin: "0 0 12px 0" }}>Artifacts</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind</th>
              <th>Size</th>
              <th>Preview</th>
              <th>Warnings</th>
            </tr>
          </thead>
          <tbody>
            {artifacts.map((artifact) => (
              <tr key={artifact.name}>
                <td className="mono" style={{ fontSize: "13px" }}>
                  {artifact.name}
                </td>
                <td>
                  <span
                    style={{
                      padding: "2px 6px",
                      backgroundColor: "#1e293b",
                      color: "#94a3b8",
                      borderRadius: "3px",
                      fontSize: "11px",
                    }}
                  >
                    {artifact.kind}
                  </span>
                </td>
                <td className="mono" style={{ fontSize: "13px" }}>
                  {artifact.size_bytes === 0
                    ? "—"
                    : artifact.size_bytes < 1024
                    ? `${artifact.size_bytes} B`
                    : artifact.size_bytes < 1024 * 1024
                    ? `${(artifact.size_bytes / 1024).toFixed(1)} KB`
                    : `${(artifact.size_bytes / 1024 / 1024).toFixed(1)} MB`}
                </td>
                <td>
                  {artifact.is_binary ? (
                    <span className="muted">binary</span>
                  ) : artifact.preview_available ? (
                    <button
                      onClick={() => onPreview(artifact.name)}
                      style={{
                        padding: "2px 8px",
                        backgroundColor: "#3b82f6",
                        color: "#fff",
                        border: "none",
                        borderRadius: "3px",
                        fontSize: "11px",
                        cursor: "pointer",
                      }}
                    >
                      preview
                    </button>
                  ) : (
                    <span className="muted">n/a</span>
                  )}
                </td>
                <td>
                  {artifact.has_secret_warning && (
                    <span
                      style={{
                        padding: "2px 6px",
                        backgroundColor: "#ef4444",
                        color: "#fff",
                        borderRadius: "3px",
                        fontSize: "11px",
                      }}
                    >
                      secret detected
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ReviewEvidencePanel({
  evidence,
  onPreviewArtifact,
}: ReviewEvidencePanelProps): React.ReactElement {
  return (
    <div>
      {(evidence.policy_warnings ?? []).length > 0 && (
        <div
          style={{
            marginBottom: "16px",
            padding: "10px 14px",
            backgroundColor: "#fef3c7",
            border: "1px solid #f59e0b",
            borderRadius: "6px",
          }}
        >
          <p style={{ margin: "0 0 6px 0", fontWeight: 600, color: "#92400e", fontSize: "13px" }}>
            Policy Warnings
          </p>
          <ul style={{ margin: 0, paddingLeft: "18px", color: "#92400e", fontSize: "12px" }}>
            {(evidence.policy_warnings ?? []).map((warning, i) => (
              <li key={i}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      <ContractCard contract={evidence.mission_contract} />
      <ValidatorResultsPanel results={evidence.validator_results ?? []} />
      <ArtifactListPanel
        artifacts={evidence.artifacts ?? []}
        onPreview={onPreviewArtifact}
      />
    </div>
  );
}
