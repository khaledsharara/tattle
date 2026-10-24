import { useState } from "react";

import { apiSend, formatAgo, useAdmin, type Device } from "../../shared";

/** The network identities shown as one device, with a way to undo a merge. */
export function IdentityList({ device, onChanged }: { device: Device; onChanged: () => void }) {
  const { isAdmin } = useAdmin();
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (device.identities.length < 2) return null;

  return (
    <section className="panel space-y-3 p-5" aria-labelledby="identities-title">
      <div>
        <h2 id="identities-title" className="text-sm font-semibold">
          Seen under {device.identities.length} addresses
        </h2>
        <p className="mt-1 text-sm text-ink-2">
          Usage from all of them is counted here. Tattle undoes its own merges if the addresses are ever online at the
          same time.
        </p>
      </div>
      <ul className="divide-y divide-hairline text-sm">
        {device.identities.map((item) => (
          <li key={item.id} className="flex flex-wrap items-center justify-between gap-3 py-2.5">
            <div className="min-w-0">
              <p className="font-medium tabular">
                {item.mac ?? "No hardware address"}
                {item.primary && <span className="ml-2 text-xs font-normal text-muted">first seen</span>}
              </p>
              <p className="text-xs text-muted">
                {[item.ip, item.private_mac ? "private address" : null, `seen ${formatAgo(item.last_seen)}`]
                  .filter(Boolean)
                  .join(", ")}
              </p>
              {item.reason && (
                <p className="mt-0.5 text-xs text-ink-2">
                  {item.automatic ? "Merged automatically" : "Merged by you"}: {item.reason.replace(/^Merged by admin:?\s*/, "") || "same device"}
                </p>
              )}
            </div>
            {isAdmin && !item.primary && (
              <button
                type="button"
                className="btn-quiet"
                disabled={busy === item.id}
                onClick={async () => {
                  setBusy(item.id);
                  setError(null);
                  try {
                    await apiSend(`/api/devices/${item.id}/split`, "POST");
                    onChanged();
                  } catch (err) {
                    setError(err instanceof Error ? err.message : "Could not split");
                  } finally {
                    setBusy(null);
                  }
                }}
              >
                Split off
              </button>
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
