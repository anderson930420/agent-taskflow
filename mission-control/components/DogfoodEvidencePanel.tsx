"use client";

import { useEffect, useState } from "react";
import { requestJson } from "../lib/api";
import type {
  ApiFailure,
  DogfoodEvidenceItem,
  TaskDogfoodEvidenceBundle,
} from "../lib/types";
import { ApiErrorPanel } from "./ApiErrorPanel";
import { StatusBadge } from "./StatusBadge";

interface DogfoodEvidencePanelProps {
  taskKey: string;
}

const GROUPS: Array<{ key: string; label: string }> = [
  { key: "issue", label: "Issue/spec" },
  { key: "execution", label: "Execution" },
  { key: "validation", label: "Validation" },
  { key: "review", label: "Review" },
  { key: "handoff", label: "Handoff" },
  { key: "publication", label: "Publication" },
  { key: "draft_pr", label: "Draft PR" },
  { key: "preflight", label: "Preflight" },
  { key: "governance", label: "Governance" },
  { key: "other", label: "Other" },
];

function itemSubtitle(item: DogfoodEvidenceItem): string {
  if (item.status) {
    return `${item.kind} · ${item.source} · ${item.status}`;
  }
  return `${item.kind} · ${item.source}`;
}

function EvidenceGroup({
  label,
  items,
}: {
  label: string;
  items: DogfoodEvidenceItem[];
}) {
  return (
    <div
      style={{
        border: "1px solid var(--border)",
        borderRadius: "8px",
        background: "var(--panel)",
        minHeight: "96px",
        padding: "10px 12px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "8px",
          marginBottom: "8px",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "0.88rem" }}>{label}</h3>
        <span className="muted" style={{ fontSize: "0.72rem" }}>
          {items.length}
        </span>
      </div>

      {items.length === 0 ? (
        <div className="empty" style={{ padding: "8px 0", textAlign: "left" }}>
          No evidence recorded.
        </div>
      ) : (
        <div style={{ display: "grid", gap: "8px" }}>
          {items.map((item, index) => (
            <div key={`${item.source}-${item.name}-${index}`}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "8px",
                  minWidth: 0,
                }}
              >
                <code
                  style={{
                    fontSize: "0.74rem",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {item.name}
                </code>
                {item.status ? <StatusBadge status={item.status} /> : null}
              </div>
              <div className="muted" style={{ fontSize: "0.68rem" }}>
                {itemSubtitle(item)}
              </div>
              {item.path ? (
                <div
                  className="mono"
                  style={{
                    color: "var(--muted-2)",
                    fontSize: "0.66rem",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={item.path}
                >
                  {item.path}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function DogfoodEvidencePanel({ taskKey }: DogfoodEvidencePanelProps) {
  const [bundle, setBundle] = useState<TaskDogfoodEvidenceBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiFailure | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);
      const result = await requestJson<TaskDogfoodEvidenceBundle>(
        `/api/tasks/${encodeURIComponent(taskKey)}/evidence`
      );
      if (cancelled) return;
      if (result.ok) {
        setBundle(result.data as TaskDogfoodEvidenceBundle);
      } else {
        setError(result.error as ApiFailure);
      }
      setLoading(false);
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [taskKey]);

  if (loading) {
    return <div className="empty">Loading dogfood evidence summary...</div>;
  }

  if (error) {
    return (
      <ApiErrorPanel
        error={error}
        title="Dogfood Evidence Unavailable"
        retryLabel="Retry"
        onRetry={() => {
          setLoading(true);
          requestJson<TaskDogfoodEvidenceBundle>(
            `/api/tasks/${encodeURIComponent(taskKey)}/evidence`
          ).then((result) => {
            if (result.ok) {
              setBundle(result.data as TaskDogfoodEvidenceBundle);
              setError(null);
            } else {
              setError(result.error as ApiFailure);
            }
            setLoading(false);
          });
        }}
      />
    );
  }

  if (!bundle) {
    return <div className="empty">No dogfood evidence summary available.</div>;
  }

  const evidence = bundle.item;

  return (
    <div>
      <div
        style={{
          marginBottom: "14px",
          padding: "10px 12px",
          border: "1px solid var(--border)",
          borderRadius: "8px",
          background: "var(--panel-2)",
          color: "var(--muted)",
          fontSize: "0.82rem",
        }}
      >
        Read-only evidence view. No push, PR creation, merge, approval, or
        cleanup actions are available from Mission Control.
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: "10px",
        }}
      >
        {GROUPS.map((group) => (
          <EvidenceGroup
            key={group.key}
            label={group.label}
            items={evidence.categories[group.key] ?? []}
          />
        ))}
      </div>
    </div>
  );
}
