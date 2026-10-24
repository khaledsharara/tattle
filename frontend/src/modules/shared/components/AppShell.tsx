import type { ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { useAdmin } from "../hooks/useAdmin";
import { useStatus } from "../hooks/useStatus";
import { getUnitBase } from "../utils/format";
import { Logo } from "./Logo";

const NAV = [
  { to: "/", label: "Overview", end: true, needsCounting: false },
  { to: "/devices", label: "Devices", end: false, needsCounting: true },
  { to: "/settings", label: "Settings", end: false, needsCounting: false },
];

const SOURCE: Record<string, string> = {
  gateway: "Counted on this server as traffic passes through it.",
  router: "Counted from the router's own per-device counters.",
  quota: "Figures from your internet provider.",
  demo: "A demo with made-up data.",
};

export function AppShell({ children }: { children: ReactNode }) {
  const { isAdmin } = useAdmin();
  const { status, counting } = useStatus();
  const { search } = useLocation();
  // Keep the selected period when moving between pages.
  const range = new URLSearchParams(search);
  const carry = range.has("range") ? `?${range.toString()}` : "";

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-4 sm:px-6">
      {status?.demo && (
        <p data-demo-banner className="mt-3 rounded-lg bg-ink px-3 py-2 text-center text-xs text-page">
          Demo: a made-up household. Nothing on your network is being counted.
        </p>
      )}
      <header className="flex flex-wrap items-center justify-between gap-3 py-5">
        <NavLink to={`/${carry}`} className="flex items-center gap-2.5" aria-label="Tattle, overview">
          <Logo />
          <span className="readout text-lg leading-none">Tattle</span>
        </NavLink>
        <nav aria-label="Main">
          <ul className="flex gap-1 text-sm">
            {NAV.filter((item) => counting || !item.needsCounting).map((item) => (
              <li key={item.to}>
                <NavLink
                  to={`${item.to}${carry}`}
                  end={item.end}
                  className={({ isActive }) =>
                    `rounded-lg px-3 py-1.5 transition-colors ${
                      isActive ? "bg-ink text-page" : "text-ink-2 hover:bg-ink/5 hover:text-ink"
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main className="flex-1 pb-12">{children}</main>
      <footer className="flex flex-wrap justify-between gap-2 border-t border-hairline py-4 text-xs text-muted">
        <span>
          {status ? SOURCE[status.mode] : ""} 1 GB = {getUnitBase()} MB.
        </span>
        <span>
          {isAdmin ? "Signed in as admin" : "View only"}
          {status && (
            <>
              {" · "}
              <a href="https://github.com/khaledsharara/tattle" className="underline-offset-4 hover:underline">
                Tattle {status.version}
              </a>
            </>
          )}
        </span>
      </footer>
    </div>
  );
}
