import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { apiSend, usePolling, type DeviceList } from "../../shared";

/**
 * Admin-only: when one physical device shows up twice (a phone that changed its
 * private Wi-Fi address), fold this entry's history into the other one.
 */
export function MergePanel({ deviceId, deviceName }: { deviceId: number; deviceName: string }) {
  const navigate = useNavigate();
  const { data } = usePolling<DeviceList>("/api/devices?range=30d", 0);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const options = (data?.devices ?? []).filter((d) => d.id !== deviceId && !d.is_server);

  return (
    <form
      className="space-y-3"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!target) return;
        setBusy(true);
        setError(null);
        try {
          await apiSend(`/api/devices/${deviceId}/merge`, "POST", { into: Number(target) });
          navigate(`/devices/${target}`, { replace: true });
        } catch (err) {
          setError(err instanceof Error ? err.message : "Could not merge");
          setBusy(false);
        }
      }}
    >
      <p className="text-sm text-ink-2">
        If “{deviceName}” is really the same device as another entry, move all of its usage there. This cannot be undone.
      </p>
      <div className="flex flex-wrap gap-2">
        <label htmlFor="merge-target" className="sr-only">
          Merge into
        </label>
        <select id="merge-target" className="field max-w-xs" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">Choose the device to keep…</option>
          {options.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
              {d.mac ? ` (${d.mac})` : ""}
            </option>
          ))}
        </select>
        <button type="submit" className="btn-quiet" disabled={!target || busy}>
          {busy ? "Merging…" : "Merge into this device"}
        </button>
      </div>
      {error && (
        <p className="text-sm text-critical" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
