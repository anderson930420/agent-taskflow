"use client";

import type { ActionResponse, ApiFailure } from "../lib/types";

export type ActionResultState =
  | {
      kind: "success";
      response: ActionResponse;
    }
  | {
      kind: "failure";
      error: ApiFailure;
      action?: string;
    };

export function ActionResultBanner({
  result
}: {
  result: ActionResultState | null;
}) {
  if (!result) {
    return null;
  }

  if (result.kind === "failure") {
    return (
      <div className="error" role="alert">
        <strong>Action failed</strong>
        {result.action ? <>: {result.action}</> : null}
        <br />
        {result.error.message}
      </div>
    );
  }

  return (
    <div className="success" role="status">
      <strong>Action succeeded</strong>: {result.response.message}
      {result.response.status ? (
        <>
          <br />
          Resulting status:{" "}
          <span className="mono">{result.response.status}</span>
        </>
      ) : null}
    </div>
  );
}
