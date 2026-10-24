import { Link, useParams } from "react-router-dom";

import { Notice, ProviderBadge, StatusLabel, usePolling, type IspLineDetail } from "../../shared";
import { BillingPanel } from "../components/BillingPanel";
import { BundleList } from "../components/BundleList";
import { DailyUsage } from "../components/DailyUsage";
import { LineDetails } from "../components/LineDetails";
import { LineStats } from "../components/LineStats";
import { useSpan } from "../hooks/useSpan";

/** Everything Tattle knows about one internet line. */
export function LinePage() {
  const { number = "" } = useParams();
  const span = useSpan("30d");
  const { data, error, refreshing } = usePolling<IspLineDetail>(`/api/isp/lines/${encodeURIComponent(number)}?${span.query}`, 60_000);

  return (
    <div className="space-y-6">
      <Link to="/" className="text-sm text-ink-2 underline-offset-4 hover:text-ink hover:underline">
        <span aria-hidden>←</span> Overview
      </Link>

      {!data ? (
        error ? (
          <Notice title="Could not load this line" tone="error">
            {error}
          </Notice>
        ) : (
          <div className="panel h-64 animate-pulse" aria-label="Loading" />
        )
      ) : (
        <>
          <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
            <div>
              <p className="flex items-center gap-2 text-sm text-ink-2">
                {data.line.provider.name} home internet <ProviderBadge provider={data.line.provider} />
              </p>
              <h1 className="readout text-4xl tabular">{data.line.number}</h1>
            </div>
            <p className="text-sm text-ink-2">
              {data.line.live ? <StatusLabel status="good">In use now</StatusLabel> : "Not in use right now"}
            </p>
          </header>

          <LineStats line={data.line} />

          <div className="grid gap-6 lg:grid-cols-2">
            <section className="panel space-y-4 p-5" aria-labelledby="bundles-title">
              <h2 id="bundles-title" className="text-sm font-semibold">
                What's on it
              </h2>
              {data.line.reading ? (
                <BundleList items={data.line.reading.items} provider={data.line.provider.name} />
              ) : (
                <p className="text-sm text-ink-2">Not read yet.</p>
              )}
            </section>
            <section className="panel space-y-4 p-5" aria-labelledby="details-title">
              <h2 id="details-title" className="text-sm font-semibold">
                Details
              </h2>
              <LineDetails detail={data} />
            </section>
          </div>

          <section className="panel space-y-4 p-5" aria-labelledby="daily-title">
            <h2 id="daily-title" className="text-sm font-semibold">
              Use per day
            </h2>
            {data.has_history ? (
              <DailyUsage detail={data} span={span} busy={refreshing} />
            ) : (
              <p className="text-sm text-ink-2">
                {data.line.provider.name} doesn't share a daily history, so this line has none here. What's left is read
                every 15 minutes, and the device pages show what Tattle counted each day.
              </p>
            )}
          </section>

          {data.billing && (
            <section className="panel space-y-4 p-5" aria-labelledby="bills-title">
              <h2 id="bills-title" className="text-sm font-semibold">
                Bills
              </h2>
              <BillingPanel billing={data.billing} provider={data.line.provider} />
            </section>
          )}
        </>
      )}
    </div>
  );
}
