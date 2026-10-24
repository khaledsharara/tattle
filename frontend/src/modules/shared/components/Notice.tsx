import type { ReactNode } from "react";

/** Explains what went wrong or why something is empty, and what to do about it. */
export function Notice({ title, children, tone = "neutral" }: { title: string; children?: ReactNode; tone?: "neutral" | "error" }) {
  return (
    <div
      role={tone === "error" ? "alert" : undefined}
      className={`rounded-xl px-4 py-3 text-sm ring-1 ${
        tone === "error" ? "bg-critical/10 ring-critical/30" : "bg-surface ring-hairline"
      }`}
    >
      <p className="font-medium">{title}</p>
      {children && <div className="mt-1 text-ink-2">{children}</div>}
    </div>
  );
}
