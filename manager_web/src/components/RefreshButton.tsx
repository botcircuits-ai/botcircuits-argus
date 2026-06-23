"use client";

import { useState } from "react";

/**
 * Refresh button with clear click feedback: while the async `onRefresh` runs it
 * shows a spinner + "Refreshing…" and is disabled. A short minimum spin keeps
 * the feedback visible even when the backend responds instantly (otherwise a
 * click looks like nothing happened). The lime ping dot draws attention when
 * idle and hides while refreshing.
 */
export function RefreshButton({
  onRefresh,
  minSpinMs = 500,
  indicator = true,
}: {
  onRefresh: () => Promise<void> | void;
  minSpinMs?: number;
  /** Show the idle lime ping dot. Default true. */
  indicator?: boolean;
}) {
  const [busy, setBusy] = useState(false);

  async function handle() {
    if (busy) return;
    setBusy(true);
    const started = Date.now();
    try {
      await onRefresh();
    } finally {
      const remaining = minSpinMs - (Date.now() - started);
      if (remaining > 0) await new Promise((r) => setTimeout(r, remaining));
      setBusy(false);
    }
  }

  return (
    <button
      onClick={handle}
      disabled={busy}
      aria-busy={busy}
      className="relative h-9 px-3 inline-flex items-center gap-2 rounded-lg border border-border bg-elevated text-sm font-medium text-fg shadow-sm hover:bg-brand/10 hover:border-brand/40 active:scale-[0.97] disabled:opacity-80 disabled:cursor-not-allowed transition"
    >
      {!busy && indicator && (
        <span className="absolute -top-1 -right-1 flex h-2.5 w-2.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-400 opacity-75" />
          <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-brand-500" />
        </span>
      )}
      {busy && (
        <svg
          viewBox="0 0 24 24"
          className="w-4 h-4 animate-spin text-brand-600 dark:text-brand-400"
          fill="none"
          aria-hidden
        >
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" opacity="0.25" />
          <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      )}
      {busy ? "Refreshing…" : "Refresh"}
    </button>
  );
}
