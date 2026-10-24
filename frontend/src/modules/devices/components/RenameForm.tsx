import { useEffect, useState } from "react";

import { apiSend, type Device } from "../../shared";

/** Admin-only: give a device a name everyone at home recognises. Empty name = use what the device reports. */
export function RenameForm({ device, onSaved }: { device: Device; onSaved: () => void }) {
  const [name, setName] = useState(device.custom_name ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setName(device.custom_name ?? ""), [device.id, device.custom_name]);

  const changed = name.trim() !== (device.custom_name ?? "");

  return (
    <form
      className="flex flex-wrap items-start gap-2"
      onSubmit={async (event) => {
        event.preventDefault();
        setSaving(true);
        setError(null);
        try {
          await apiSend(`/api/devices/${device.id}`, "PATCH", { name: name.trim() || null });
          onSaved();
        } catch (err) {
          setError(err instanceof Error ? err.message : "Could not save the name");
        } finally {
          setSaving(false);
        }
      }}
    >
      <label htmlFor={`rename-${device.id}`} className="sr-only">
        Device name
      </label>
      <input
        id={`rename-${device.id}`}
        className="field max-w-xs"
        value={name}
        maxLength={60}
        placeholder={device.hostname ?? "e.g. Mum's phone"}
        onChange={(e) => setName(e.target.value)}
      />
      <button type="submit" className="btn-primary" disabled={!changed || saving}>
        {saving ? "Saving…" : "Save name"}
      </button>
      {error && (
        <p className="w-full text-sm text-critical" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
