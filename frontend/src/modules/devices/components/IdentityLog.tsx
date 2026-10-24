import { Link } from "react-router-dom";

import { formatAgo, usePolling, type IdentityEvent } from "../../shared";

/** Read-only record of what Tattle grouped or split on its own. */
export function IdentityLog() {
  const { data } = usePolling<{ events: IdentityEvent[] }>("/api/identity-events?limit=12", 60_000);
  const events = data?.events ?? [];
  if (events.length === 0) return null;

  return (
    <details className="panel p-5">
      <summary className="cursor-pointer text-sm font-semibold">
        What Tattle grouped automatically
        <span className="ml-2 font-normal text-muted">
          {events.length} recent {events.length === 1 ? "change" : "changes"}
        </span>
      </summary>
      <ol className="mt-4 space-y-3 text-sm">
        {events.map((event) => (
          <li key={event.id}>
            <p>
              {event.kind === "merged" ? "Joined " : "Split "}
              <span className="font-medium tabular">{event.mac ?? "an address"}</span>
              {event.kind === "merged" ? " into " : " off from "}
              <Link to={`/devices/${event.other.id}`} className="font-medium underline underline-offset-4">
                {event.other.name}
              </Link>
              <span className="text-muted">, {formatAgo(event.ts)}</span>
            </p>
            <p className="text-xs text-ink-2">{event.reason}</p>
          </li>
        ))}
      </ol>
    </details>
  );
}
