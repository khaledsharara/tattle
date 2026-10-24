import { useState } from "react";
import { Link } from "react-router-dom";

import { apiSend, formatAgo, useAdmin, usePolling, type DeviceBrief, type MergeSuggestion } from "../../shared";

function Side({ device }: { device: DeviceBrief }) {
  return (
    <Link to={`/devices/${device.id}`} className="min-w-0 rounded-lg bg-page px-3 py-2 ring-1 ring-hairline hover:ring-ink/30">
      <span className="block truncate font-medium">{device.name}</span>
      <span className="block truncate text-xs text-muted">
        {[device.kind, device.mac, `seen ${formatAgo(device.last_seen)}`].filter(Boolean).join(", ")}
      </span>
    </Link>
  );
}

/** Pairs of entries that are probably one phone seen under different Wi-Fi addresses. */
export function MergeSuggestions({ onChanged }: { onChanged: () => void }) {
  const { isAdmin } = useAdmin();
  const { data, reload } = usePolling<{ suggestions: MergeSuggestion[] }>("/api/suggestions", 60_000);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const suggestions = data?.suggestions ?? [];
  if (suggestions.length === 0) return null;

  async function act(suggestion: MergeSuggestion, action: "accept" | "dismiss") {
    setBusy(suggestion.id);
    setError(null);
    try {
      await apiSend(`/api/suggestions/${suggestion.id}/${action}`, "POST");
      reload();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="panel space-y-4 p-5" aria-labelledby="suggestions-title">
      <div>
        <h2 id="suggestions-title" className="text-sm font-semibold">
          Possibly the same device
        </h2>
        <p className="mt-1 text-sm text-ink-2">
          Phones use a different private address on each Wi-Fi name, so one phone can appear more than once.
          {!isAdmin && " Sign in as admin under Settings to merge or dismiss these."}
        </p>
      </div>
      <ul className="space-y-3">
        {suggestions.map((suggestion) => (
          <li key={suggestion.id} className="rounded-xl p-3 ring-1 ring-hairline">
            <div className="grid items-center gap-2 sm:grid-cols-[1fr_auto_1fr]">
              <Side device={suggestion.a} />
              <span className="text-center text-sm text-muted" aria-hidden>
                and
              </span>
              <Side device={suggestion.b} />
            </div>
            <p className="mt-2 text-sm text-ink-2">{suggestion.reason}</p>
            {isAdmin && (
              <div className="mt-3 flex flex-wrap gap-2">
                <button type="button" className="btn-primary" disabled={busy === suggestion.id} onClick={() => act(suggestion, "accept")}>
                  Merge them
                </button>
                <button type="button" className="btn-quiet" disabled={busy === suggestion.id} onClick={() => act(suggestion, "dismiss")}>
                  Not the same
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {error && (
        <p className="text-sm text-critical" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
