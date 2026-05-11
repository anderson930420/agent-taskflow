"use client";

import { useState } from "react";
import type { ActionResponse, ApiResult } from "../lib/types";
import type { ActionResultState } from "./ActionResultBanner";

export function ConfirmActionButton({
  label,
  confirmMessage,
  disabled = false,
  danger = false,
  onConfirm,
  onResult
}: {
  label: string;
  confirmMessage: string;
  disabled?: boolean;
  danger?: boolean;
  onConfirm: () => Promise<ApiResult<ActionResponse>>;
  onResult: (result: ActionResultState) => void;
}) {
  const [running, setRunning] = useState(false);

  async function handleClick() {
    if (disabled || running) {
      return;
    }

    const confirmed = window.confirm(confirmMessage);
    if (!confirmed) {
      return;
    }

    setRunning(true);

    try {
      const result = await onConfirm();

      if (result.ok) {
        onResult({ kind: "success", response: result.data });
      } else {
        onResult({ kind: "failure", error: result.error, action: label });
      }
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Unknown error while running action.";

      onResult({
        kind: "failure",
        action: label,
        error: { message }
      });
    } finally {
      setRunning(false);
    }
  }

  return (
    <button
      className={danger ? "button button-danger" : "button"}
      disabled={disabled || running}
      onClick={handleClick}
      type="button"
    >
      {running ? "Running..." : label}
    </button>
  );
}
