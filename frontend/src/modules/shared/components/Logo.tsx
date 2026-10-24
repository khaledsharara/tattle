/** Tattle's mark: a speech bubble telling usage bars. */
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <path
        d="M7 3h18a5 5 0 0 1 5 5v11a5 5 0 0 1-5 5H13.5L7 29.5V24a5 5 0 0 1-5-5V8a5 5 0 0 1 5-5z"
        className="fill-ink"
      />
      <rect x="8" y="14" width="4" height="5" rx="1.5" className="fill-page" />
      <rect x="14" y="11" width="4" height="8" rx="1.5" className="fill-page" />
      <rect x="20" y="7.5" width="4" height="11.5" rx="1.5" fill="rgb(var(--s1))" />
    </svg>
  );
}
