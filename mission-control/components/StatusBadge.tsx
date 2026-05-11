import type { TaskStatus } from "../lib/types";

export function StatusBadge({ status }: { status?: TaskStatus | null }) {
  const normalized = status || "unknown";
  return <span className={`badge badge-${normalized}`}>{normalized}</span>;
}
