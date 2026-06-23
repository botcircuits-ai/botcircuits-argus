/** BotCircuits wordmark — a small lime "circuit" glyph + name. */
export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2 font-semibold tracking-tight">
      <svg
        width="22"
        height="22"
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden
        className="text-brand-500"
      >
        <circle cx="12" cy="12" r="3" fill="currentColor" />
        <circle cx="5" cy="5" r="2" fill="currentColor" />
        <circle cx="19" cy="5" r="2" fill="currentColor" />
        <circle cx="5" cy="19" r="2" fill="currentColor" />
        <circle cx="19" cy="19" r="2" fill="currentColor" />
        <path
          d="M7 6.5 10.5 10M17 6.5 13.5 10M7 17.5 10.5 14M17 17.5 13.5 14"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
        />
      </svg>
      {!compact && (
        <span className="text-fg">
          BotCircuits <span className="text-muted font-normal">Argus</span>
        </span>
      )}
    </span>
  );
}
