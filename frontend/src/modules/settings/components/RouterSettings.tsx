import { useEffect, useState } from "react";

import {
  FieldInput,
  ProviderBadge,
  apiGet,
  apiSend,
  formatAgo,
  useAdmin,
  useStatus,
  type RouterCatalogItem,
  type RouterSettings as Saved,
} from "../../shared";

const REPO = "https://github.com/khaledsharara/tattle";
const CAN: Record<string, string> = {
  device_counters: "counts each device's traffic (no gateway needed)",
  wan_counters: "reads the router's own internet counters",
  wan_login: "tells which line is in use",
  link_status: "reads the line speed",
  dhcp_enabled: "checks the router's DHCP is off",
};

/** Admin: which router Tattle reads, if any. Saving tests it first. */
export function RouterSettings() {
  const { isAdmin } = useAdmin();
  const { refresh } = useStatus();
  const [catalog, setCatalog] = useState<RouterCatalogItem[]>([]);
  const [saved, setSaved] = useState<Saved | null>(null);
  const [adapterId, setAdapterId] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    Promise.all([apiGet<{ routers: RouterCatalogItem[] }>("/api/routers"), apiGet<Saved>("/api/router")])
      .then(([list, current]) => {
        setCatalog(list.routers);
        setSaved(current);
        setAdapterId(current.adapter ?? "");
        setValues(current.values);
      })
      .catch(() => undefined);
  }, [isAdmin]);

  const adapter = catalog.find((item) => item.id === adapterId) ?? null;

  const save = async (id: string | null) => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await apiSend<{ adapter: string | null; found: string[] }>("/api/router", "PUT", { adapter: id, values: id ? values : {} });
      setSaved({ adapter: result.adapter, values, error: null, read_at: null });
      setMessage({
        tone: "ok",
        text: id ? `Saved. Tattle read: ${result.found.join("; ") || "the router answered"}.` : "Tattle no longer reads a router.",
      });
      refresh();
    } catch (err) {
      setMessage({ tone: "error", text: err instanceof Error ? err.message : "Could not save" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-2">
        Optional. Some routers keep a count per device, so Tattle can count without becoming the network's gateway; others
        tell which line is in use or how fast the line synced.{" "}
        <a className="underline underline-offset-4" href={`${REPO}/blob/main/docs/routers/README.md`} target="_blank" rel="noreferrer">
          Supported routers
        </a>
      </p>
      {saved?.adapter && saved.error && (
        <p className="text-sm text-critical" role="alert">
          Last read failed: {saved.error}
        </p>
      )}
      {saved?.adapter && !saved.error && saved.read_at && <p className="text-sm text-ink-2">Read {formatAgo(saved.read_at)}.</p>}

      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void save(adapterId || null);
        }}
      >
        <fieldset disabled={!isAdmin || busy} className="space-y-4">
          <label className="block text-sm font-medium">
            Router
            <select
              className="field mt-1 block max-w-sm"
              value={adapterId}
              onChange={(e) => {
                setAdapterId(e.target.value);
                setValues(e.target.value === saved?.adapter ? saved.values : {});
                setMessage(null);
              }}
            >
              <option value="">None</option>
              {catalog.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                  {item.status === "beta" ? " (beta)" : ""}
                </option>
              ))}
            </select>
          </label>

          {adapter && (
            <>
              <p className="flex flex-wrap items-center gap-2 text-sm text-ink-2">
                <ProviderBadge provider={adapter} />
                {adapter.capabilities.map((c) => CAN[c] ?? c).join(", ")}.
              </p>
              {adapter.notes && <p className="text-sm text-ink-2">{adapter.notes}</p>}
              <div className="grid gap-3 sm:grid-cols-2">
                {adapter.fields.map((field) => (
                  <FieldInput
                    key={field.name}
                    field={field}
                    value={values[field.name] ?? ""}
                    onChange={(value) => setValues((v) => ({ ...v, [field.name]: value }))}
                  />
                ))}
              </div>
            </>
          )}
        </fieldset>
        {isAdmin ? (
          (adapterId || saved?.adapter) && (
            <div className="flex gap-2">
              <button type="submit" className="btn-primary" disabled={busy}>
                {busy ? "Testing…" : adapterId ? "Save and test" : "Stop reading the router"}
              </button>
            </div>
          )
        ) : (
          <p className="text-sm text-ink-2">Sign in as admin to change the router.</p>
        )}
      </form>

      {message && (
        <p className={`text-sm ${message.tone === "error" ? "text-critical" : "text-ink-2"}`} role={message.tone === "error" ? "alert" : "status"}>
          {message.text}
        </p>
      )}
    </div>
  );
}
