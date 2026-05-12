"use client";

import React from "react";
import { useState } from "react";
import type { ArtifactPreview, TaskReviewEvidence } from "../lib/types";
import {
  getTaskReviewEvidence,
  getArtifactPreview as fetchArtifactPreview,
} from "../lib/api";
import { ReviewEvidencePanel } from "./ReviewEvidencePanel";

function PreviewModal({
  name,
  preview,
  onClose,
}: {
  name: string;
  preview: ArtifactPreview;
  onClose: () => void;
}): React.ReactElement {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        backgroundColor: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
      }}
      onClick={onClose}
    >
      <div
        style={{
          backgroundColor: "#1e293b",
          color: "#f1f5f9",
          borderRadius: "8px",
          padding: "20px",
          maxWidth: "800px",
          width: "90%",
          maxHeight: "80vh",
          overflow: "auto",
          display: "flex",
          flexDirection: "column",
          gap: "12px",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0, fontFamily: "monospace", fontSize: "14px" }}>{name}</h3>
          <button
            onClick={onClose}
            style={{
              padding: "4px 10px",
              backgroundColor: "#ef4444",
              color: "#fff",
              border: "none",
              borderRadius: "4px",
              cursor: "pointer",
              fontSize: "12px",
            }}
          >
            close
          </button>
        </div>
        {preview.preview_reason ? (
          <div
            style={{
              padding: "10px",
              backgroundColor: "#374151",
              borderRadius: "4px",
              fontSize: "12px",
              color: "#f59e0b",
            }}
          >
            Preview not available: {preview.preview_reason}
          </div>
        ) : preview.content ? (
          <pre
            style={{
              margin: 0,
              padding: "12px",
              backgroundColor: "#0f172a",
              borderRadius: "4px",
              fontSize: "12px",
              fontFamily: "monospace",
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              maxHeight: "60vh",
              overflow: "auto",
            }}
          >
            {preview.truncated && (
              <div style={{ color: "#f59e0b", marginBottom: "8px", fontSize: "11px" }}>
                [truncated — file exceeds preview limit]
              </div>
            )}
            {preview.content}
          </pre>
        ) : null}
      </div>
    </div>
  );
}

function ReviewEvidenceSection({
  taskKey,
}: {
  taskKey: string;
}): React.ReactElement {
  const [evidence, setEvidence] = useState<TaskReviewEvidence | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [previewArtifact, setPreviewArtifact] = useState<ArtifactPreview | null>(null);

  async function loadEvidence(): Promise<void> {
    setLoading(true);
    setLoadError(null);
    const result = await getTaskReviewEvidence(taskKey);
    if (result.ok) {
      setEvidence(result.data.item);
    } else {
      setLoadError(result.error.message);
    }
    setLoading(false);
  }

  async function openPreview(name: string): Promise<void> {
    const result = await fetchArtifactPreview(taskKey, name);
    if (result.ok) {
      setPreviewArtifact(result.data);
    }
  }

  if (evidence === null && !loading) {
    return (
      <div style={{ textAlign: "center", padding: "20px" }}>
        <button
          onClick={loadEvidence}
          style={{
            padding: "8px 20px",
            backgroundColor: "#3b82f6",
            color: "#fff",
            border: "none",
            borderRadius: "6px",
            cursor: "pointer",
            fontSize: "14px",
          }}
        >
          Load Review Evidence
        </button>
      </div>
    );
  }

  if (loading) {
    return <div className="empty">Loading review evidence&hellip;</div>;
  }

  if (loadError) {
    return <div className="error">{loadError}</div>;
  }

  if (evidence === null) {
    return <div className="empty">No review evidence available.</div>;
  }

  return (
    <div>
      <ReviewEvidencePanel evidence={evidence} onPreviewArtifact={openPreview} />
      {previewArtifact !== null && (
        <PreviewModal
          name={previewArtifact.name}
          preview={previewArtifact}
          onClose={() => setPreviewArtifact(null)}
        />
      )}
    </div>
  );
}

export { ReviewEvidenceSection };
