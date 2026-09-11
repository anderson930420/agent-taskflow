import type { RuntimeObservedStep } from "../lib/types";
import { stepColor, stepGlyph, stepLabel } from "../lib/realtime";

/**
 * SPEC §14.2 execution step list.
 *
 * Renders the real lifecycle — ✓ done, ● in flight, ○ not started — for the
 * seven SPEC §14.1 first-level steps. It deliberately shows no share of work
 * and no countdown: the glyph and the step status are the whole signal.
 */
export function ExecutionStepList({
  steps,
  compact = false
}: {
  steps: RuntimeObservedStep[];
  compact?: boolean;
}) {
  if (steps.length === 0) {
    return <p className="muted">No execution steps recorded yet.</p>;
  }

  return (
    <ul
      style={{
        listStyle: "none",
        margin: 0,
        padding: 0,
        display: "flex",
        flexDirection: compact ? "row" : "column",
        flexWrap: "wrap",
        gap: compact ? "10px" : "4px"
      }}
    >
      {steps.map((step) => (
        <li
          key={step.name}
          title={step.summary ?? `${stepLabel(step)}: ${step.status}`}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            fontSize: compact ? "0.68rem" : "0.82rem",
            color: stepColor(String(step.status))
          }}
        >
          <span aria-hidden="true">{stepGlyph(String(step.status))}</span>
          <span>{stepLabel(step)}</span>
          <span className="muted" style={{ fontSize: "0.66rem" }}>
            {String(step.status)}
          </span>
        </li>
      ))}
    </ul>
  );
}
