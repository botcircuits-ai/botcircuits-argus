import type { GateSummary } from "@/lib/api";
import { cx } from "@/lib/format";

/** Compact verification-gate verdict badge: "gate passed" / "gate failed",
 * plus a repair-attempt count when the run needed more than one try.
 * Renders nothing when `gate` is `null` (no gate configured / never run). */
export function GateBadge({
  gate,
  className,
}: {
  gate: GateSummary | null | undefined;
  className?: string;
}) {
  if (!gate) return null;
  return (
    <span
      title={
        gate.retries > 0
          ? `Verification gate ${gate.passed ? "passed" : "failed"} after ` +
            `${gate.attempts} attempt${gate.attempts === 1 ? "" : "s"} ` +
            `(${gate.retries} auto-repair retr${gate.retries === 1 ? "y" : "ies"})`
          : `Verification gate ${gate.passed ? "passed" : "failed"}`
      }
      className={cx(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium",
        gate.passed
          ? "bg-ok/15 text-ok border-ok/30"
          : "bg-danger/15 text-danger border-danger/30",
        className,
      )}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      gate {gate.passed ? "passed" : "failed"}
      {gate.retries > 0 && (
        <span className="opacity-70 tabular-nums">
          · {gate.retries} retr{gate.retries === 1 ? "y" : "ies"}
        </span>
      )}
    </span>
  );
}
