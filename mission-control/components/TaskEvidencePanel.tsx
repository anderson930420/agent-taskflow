"use client";

import { useEffect, useState } from "react";
import { getTaskReviewEvidence } from "../lib/api";
import type { ApiFailure, TaskReviewBundle } from "../lib/types";
import { ApiErrorPanel } from "./ApiErrorPanel";
import { ArtifactReviewPanel } from "./ArtifactReviewPanel";
import { ExecutorLogPanel } from "./ExecutorLogPanel";
import { ValidatorSummaryCard } from "./ValidatorSummaryCard";

interface TaskEvidencePanelProps {
  taskKey: string;
}

export function TaskEvidencePanel({ taskKey }: TaskEvidencePanelProps) {
  const [evidence, setEvidence] = useState<TaskReviewBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiFailure | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);

      const result = await getTaskReviewEvidence(taskKey);

      if (cancelled) return;

      if (result.ok) {
        setEvidence(result.data as TaskReviewBundle);
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
    return (
      <>
        <section className="section panel">
          <h2>Validator Summary</h2>
          <ValidatorSummaryCard evidence={null} loading />
        </section>

        <section className="section panel">
          <h2>Executor Logs</h2>
          <div
            style={{
              padding: "14px 16px",
              background: "var(--panel)",
              border: "1px solid var(--border)",
              borderRadius: "14px",
              fontSize: "0.82rem",
              color: "var(--muted-2)",
              textAlign: "center",
            }}
          >
            Loading…
          </div>
        </section>
      </>
    );
  }

  if (error) {
    return (
      <>
        <section className="section panel">
          <h2>Validator Summary</h2>
          <ApiErrorPanel
            error={error}
            title="Review Evidence Unavailable"
            retryLabel="Retry"
            onRetry={() => {
              const result = getTaskReviewEvidence(taskKey);
              result.then((r) => {
                if (r.ok) setEvidence(r.data as TaskReviewBundle);
                else setError(r.error as ApiFailure);
              });
            }}
          />
        </section>

        <section className="section panel">
          <h2>Executor Logs</h2>
          <div
            style={{
              padding: "14px 16px",
              background: "var(--panel)",
              border: "1px solid var(--border)",
              borderRadius: "14px",
              fontSize: "0.82rem",
              color: "var(--muted-2)",
              textAlign: "center",
            }}
          >
            No executor log artifacts found for this task.
          </div>
        </section>
      </>
    );
  }

  if (!evidence) {
    return null;
  }

  const item = evidence.item;
  const executorLogs = (item.artifacts ?? []).filter(
    (a) => a.is_executor_log
  );

  return (
    <>
      <section className="section panel">
        <h2>Validator Summary</h2>
        <ValidatorSummaryCard evidence={item} />
      </section>

      <section className="section panel">
        <h2>Executor Logs</h2>
        <ExecutorLogPanel taskKey={taskKey} executorLogs={executorLogs} />
      </section>

      <section className="section panel">
        <h2>Artifact Review</h2>
        <ArtifactReviewPanel evidence={item} taskKey={taskKey} />
      </section>
    </>
  );
}