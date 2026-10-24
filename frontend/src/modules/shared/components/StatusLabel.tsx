import type { ReactNode } from "react";

type Status = "good" | "warn" | "critical";

const ICONS: Record<Status, JSX.Element> = {
  good: <path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />,
  warn: (
    <>
      <path d="M8 4.5v4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      <circle cx="8" cy="12" r="1.2" fill="currentColor" />
    </>
  ),
  critical: <path d="M4.5 4.5l7 7m0-7l-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />,
};

const TONE: Record<Status, string> = {
  good: "bg-good/15 text-good",
  warn: "bg-warn/20 text-ink",
  critical: "bg-critical/15 text-critical",
};

/** A state is never colour alone: icon + words, the colour only reinforces it. */
export function StatusLabel({ status, children }: { status: Status; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${TONE[status]}`}>
        <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>
          {ICONS[status]}
        </svg>
      </span>
      <span>{children}</span>
    </span>
  );
}
