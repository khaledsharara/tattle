import { ColumnChart, RangeBar, formatBytes, percent, slotColor, type IspLineDetail, type PeriodPreset } from "../../shared";
import type { SpanState } from "../types";
import { formatIsoDay } from "../utils/format";

/** The line's use per day from the provider's history, over a span the reader picks. */
export function DailyUsage({ detail, span, busy }: { detail: IspLineDetail; span: SpanState; busy: boolean }) {
  const name = detail.line.provider.name;
  const series = [{ key: "isp", label: name, color: slotColor(0) }];
  const presets: PeriodPreset[] = [
    { key: "7d", label: "7 days" },
    { key: "30d", label: "30 days" },
    ...(detail.cycle ? [{ key: "cycle", label: "This cycle" }] : []),
    { key: "all", label: "Everything" },
  ];
  const { days, totals, range } = detail;
  const points = days.map((day) => ({ t: day.start, values: { isp: day.isp ?? 0 } }));
  const compared = days.filter((day) => day.counted != null);

  return (
    <div className="space-y-5">
      <RangeBar state={span} busy={busy} presets={presets} earliest={range.earliest} />

      <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-3">
        <Figure label={`${formatIsoDay(range.from, true)} – ${formatIsoDay(range.to, true)}`}>{formatBytes(totals.isp)}</Figure>
        <Figure label="A day, on days used">{totals.average != null ? formatBytes(totals.average) : "—"}</Figure>
        <Figure label="Busiest day">
          {totals.busiest ? (
            <>
              {formatBytes(totals.busiest.isp)}
              <span className="block text-xs font-normal text-ink-2">{formatIsoDay(totals.busiest.day)}</span>
            </>
          ) : (
            "—"
          )}
        </Figure>
      </dl>

      <div className={busy ? "opacity-60 transition-opacity" : "transition-opacity"}>
        <ColumnChart title={`Each day, as ${name} counted it`} points={points} series={series} bucket="day" />
      </div>

      <p className="text-sm text-ink-2">
        Providers keep only about a month of daily history. Tattle keeps every day it reads, so older days stay here: this
        line's record starts on {formatIsoDay(range.earliest)}.
      </p>

      <details className="group text-sm">
        <summary className="cursor-pointer text-ink-2 underline underline-offset-4 hover:text-ink">
          Day by day{compared.length > 0 ? ", with Tattle's count" : ""}
        </summary>
        <table className="mt-3 w-full">
          <thead className="text-left text-xs text-muted">
            <tr>
              <th scope="col" className="py-1.5 pr-4 font-medium">
                Day
              </th>
              <th scope="col" className="py-1.5 pr-4 text-right font-medium">
                {name}
              </th>
              <th scope="col" className="py-1.5 pr-4 text-right font-medium">
                Tattle
              </th>
              <th scope="col" className="py-1.5 text-right font-medium">
                Share
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-hairline">
            {[...days].reverse().map((day) => (
              <tr key={day.day}>
                <th scope="row" className="py-2 pr-4 text-left font-normal">
                  {formatIsoDay(day.day)}
                  {day.shared && <span className="block text-xs text-muted">another line was used too</span>}
                </th>
                <td className="whitespace-nowrap py-2 pr-4 text-right tabular">{day.isp == null ? "—" : formatBytes(day.isp)}</td>
                <td className="whitespace-nowrap py-2 pr-4 text-right tabular">{day.counted == null ? "—" : formatBytes(day.counted)}</td>
                <td className="whitespace-nowrap py-2 text-right tabular">
                  {day.counted != null && day.isp ? percent(day.counted, day.isp) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs text-muted">
          Tattle counts the whole house, so its figure is shown only for full days it counted when this was the only
          line in use.
        </p>
      </details>
    </div>
  );
}

function Figure({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-ink-2">{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold tabular">{children}</dd>
    </div>
  );
}
