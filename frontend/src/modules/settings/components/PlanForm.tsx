import { useEffect, useState } from "react";

import { apiSend, formatBytes, formatDay, useAdmin, type AppSettings, type CycleSettings } from "../../shared";
import type { PlanFormValues } from "../types";
import { parsePlanForm } from "../utils/validation";

/** "2026-10-15" -> "Oct 15", read as a local day. */
function isoDay(iso: string): string {
  return formatDay(new Date(`${iso}T12:00:00`).getTime() / 1000);
}

/** The cycle as the provider counts it, for the line in use: nothing to type. */
function CycleFromProvider({ cycle }: { cycle: CycleSettings }) {
  return (
    <div className="space-y-1 text-sm">
      <p>
        Taken from {cycle.provider?.name ?? "your provider"} for {cycle.number}
        {cycle.plan_bytes ? ` (${formatBytes(cycle.plan_bytes)} plan)` : ""}: this cycle started on{" "}
        <span className="font-semibold">{isoDay(cycle.start)}</span> and renews on{" "}
        <span className="font-semibold">{isoDay(cycle.end)}</span>.
      </p>
      <p className="text-xs text-muted">
        {cycle.pending
          ? "Today is the renewal day; the new cycle's figures appear once the provider shows them."
          : "It follows the line the house is on, so switching lines switches the cycle."}
      </p>
    </div>
  );
}

export function PlanForm() {
  const { settings, isAdmin, setSettings } = useAdmin();
  const [values, setValues] = useState<PlanFormValues>({ billingDay: "1", quotaGb: "" });
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (settings)
      setValues({ billingDay: String(settings.billing_day ?? 1), quotaGb: settings.quota_gb ? String(settings.quota_gb) : "" });
  }, [settings]);

  if (settings?.cycle.source === "isp") return <CycleFromProvider cycle={settings.cycle} />;

  return (
    <form
      className="space-y-4"
      onSubmit={async (event) => {
        event.preventDefault();
        const payload = parsePlanForm(values);
        if (typeof payload === "string") {
          setMessage({ tone: "error", text: payload });
          return;
        }
        setBusy(true);
        setMessage(null);
        try {
          setSettings(await apiSend<AppSettings>("/api/settings", "PUT", payload));
          setMessage({ tone: "ok", text: "Plan saved." });
        } catch (err) {
          setMessage({ tone: "error", text: err instanceof Error ? err.message : "Could not save the plan" });
        } finally {
          setBusy(false);
        }
      }}
    >
      <fieldset disabled={!isAdmin} className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="billing-day" className="text-sm font-medium">
            Renewal day of the month
          </label>
          <p className="mb-2 text-xs text-muted">
            The day the quota renews. “This cycle” starts on it. With your provider connected above, it's read from there.
          </p>
          <input
            id="billing-day"
            inputMode="numeric"
            className="field max-w-[8rem]"
            value={values.billingDay}
            onChange={(e) => setValues((v) => ({ ...v, billingDay: e.target.value }))}
          />
        </div>
        <div>
          <label htmlFor="quota-gb" className="text-sm font-medium">
            Plan size in GB
          </label>
          <p className="mb-2 text-xs text-muted">As your provider's app shows it. Leave empty to hide what's left.</p>
          <input
            id="quota-gb"
            inputMode="decimal"
            className="field max-w-[8rem]"
            placeholder="e.g. 140"
            value={values.quotaGb}
            onChange={(e) => setValues((v) => ({ ...v, quotaGb: e.target.value }))}
          />
        </div>
      </fieldset>
      {isAdmin ? (
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? "Saving…" : "Save plan"}
        </button>
      ) : (
        <p className="text-sm text-ink-2">Sign in as admin to change the plan.</p>
      )}
      {message && (
        <p className={`text-sm ${message.tone === "error" ? "text-critical" : "text-ink-2"}`} role={message.tone === "error" ? "alert" : "status"}>
          {message.text}
        </p>
      )}
    </form>
  );
}
