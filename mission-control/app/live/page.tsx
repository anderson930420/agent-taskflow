import Link from "next/link";
import { LiveBoard } from "../../components/LiveBoard";
import { API_BASE_URL, getRealtimeBoard } from "../../lib/api";

export const dynamic = "force-dynamic";

/**
 * SPEC §16 live board.
 *
 * The first paint is server-rendered from a read-only snapshot; the client
 * then subscribes to the SPEC §15 SSE stream, which opens every connection
 * with a fresh full snapshot.
 */
export default async function LiveBoardPage() {
  const result = await getRealtimeBoard();

  if (!result.ok) {
    return (
      <main className="error-page">
        <section className="error-panel">
          <div className="error-eyebrow">Mission Control</div>
          <h1>Agent Taskflow API unavailable</h1>
          <p>{result.error.message}</p>
          <p>
            API base URL: <span className="mono">{API_BASE_URL}</span>
          </p>
          <p>
            <Link href="/">← Back to dashboard</Link>
          </p>
        </section>
      </main>
    );
  }

  return <LiveBoard initial={result.data} />;
}
