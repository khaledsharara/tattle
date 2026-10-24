import { formatBytes, formatRate, lowerFirst, type Overview } from "../../shared";

function Figure({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-sm text-ink-2">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold">{value}</dd>
      {note && <dd className="mt-0.5 text-xs text-muted">{note}</dd>}
    </div>
  );
}

export function PeriodSummary({ overview }: { overview: Overview }) {
  const { totals, live, period } = overview;
  return (
    <dl className="panel grid grid-cols-2 gap-5 p-5 sm:grid-cols-4">
      <Figure label={`Total, ${lowerFirst(period.label)}`} value={formatBytes(totals.rx + totals.tx)} />
      <Figure label="Downloaded" value={formatBytes(totals.rx)} />
      <Figure label="Uploaded" value={formatBytes(totals.tx)} />
      <Figure
        label="Right now"
        value={formatRate(live.rx_bps + live.tx_bps)}
        note={live.active_devices === 1 ? "1 device using the internet" : `${live.active_devices} devices using the internet`}
      />
    </dl>
  );
}
