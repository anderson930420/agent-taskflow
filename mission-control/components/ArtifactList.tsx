import type { Artifact } from "../lib/types";

export function ArtifactList({ artifacts }: { artifacts: Artifact[] }) {
  if (artifacts.length === 0) {
    return <div className="empty">No artifacts recorded.</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Type</th>
            <th>Path</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          {artifacts.map((artifact, index) => (
            <tr key={`${artifact.task_key}-${artifact.artifact_type}-${index}`}>
              <td>{artifact.artifact_type}</td>
              <td className="mono">{artifact.path}</td>
              <td className="mono">{artifact.created_at ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
