import { LineCards } from "../../isp";
import { ColumnChart, Notice, RangeBar, useAdmin, usePolling, useRange, useStatus, type Overview } from "../../shared";
import { DeviceRanking } from "../components/DeviceRanking";
import { HealthPanel } from "../components/HealthPanel";
import { PeriodSummary } from "../components/PeriodSummary";
import { QuotaMeter } from "../components/QuotaMeter";
import { SetupChecklist } from "../components/SetupChecklist";
import { usageSeries } from "../utils/series";

export function OverviewPage() {
  const range = useRange("today");
  const { isAdmin } = useAdmin();
  const { counting } = useStatus();
  const cycle = usePolling<Overview>("/api/overview?range=cycle", 15_000);
  const selected = usePolling<Overview>(range.range === "cycle" || !counting ? null : `/api/overview?${range.query}`, 10_000);
  const overview = range.range === "cycle" ? cycle.data : selected.data;
  const error = range.range === "cycle" ? cycle.error : selected.error;
  const chart = overview ? usageSeries(overview) : null;

  return (
    <div className="space-y-6">
      <SetupChecklist />
      {cycle.data ? (
        // Without device counts the meter needs the provider's figures to say anything.
        (counting || cycle.data.quota?.source === "isp") && <QuotaMeter cycle={cycle.data} canEdit={isAdmin} counting={counting} />
      ) : cycle.error ? (
        <Notice title="Could not load usage" tone="error">
          {cycle.error}. The dashboard retries every few seconds.
        </Notice>
      ) : (
        <div className="panel h-64 animate-pulse" aria-label="Loading" />
      )}

      <LineCards canEdit={isAdmin} />

      {!counting && (
        <section className="panel p-5" aria-label="System status">
          <HealthPanel />
        </section>
      )}

      {counting && <RangeBar state={range} busy={selected.refreshing} />}

      {counting && error && !overview && (
        <Notice title="Could not load this period" tone="error">
          {error}
        </Notice>
      )}

      {counting && overview && chart && (
        <div className={`space-y-6 transition-opacity ${selected.refreshing ? "opacity-60" : ""}`}>
          <PeriodSummary overview={overview} />
          <div className="grid gap-6 lg:grid-cols-3">
            <section className="panel p-5 lg:col-span-2">
              <ColumnChart
                title={overview.series.bucket === "hour" ? "Each hour" : "Each day"}
                points={chart.points}
                series={chart.series}
                bucket={overview.series.bucket}
              />
            </section>
            <div className="panel p-5">
              <DeviceRanking overview={overview} rangeQuery={range.query} />
            </div>
          </div>
          <section className="panel p-5" aria-label="System status">
            <HealthPanel />
          </section>
        </div>
      )}
    </div>
  );
}
