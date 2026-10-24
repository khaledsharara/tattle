import { Link } from "react-router-dom";

import { formatBytes, formatDay, lowerFirst, percent, splitBytes, type Overview } from "../../shared";
import { meterSegments, usedOnly, withUncounted } from "../utils/series";

const GAP_PX = 2;

/**
 * The household's plan as one bar: each device's share of this cycle, in its
 * colour, and the unused remainder as empty track. With the provider's figures,
 * "used" is the provider's, and what it charged beyond Tattle's count shows as
 * its own segment. Without per-device counts the bar is simply what is used.
 */
export function QuotaMeter({ cycle, canEdit, counting }: { cycle: Overview; canEdit: boolean; counting: boolean }) {
  const counted = cycle.totals.rx + cycle.totals.tx;
  const quota = cycle.quota;
  const size = quota?.bytes ?? null;
  const used = quota?.used ?? counted;
  const fromProvider = quota?.source === "isp" ? quota.used : null;
  const segments = counting ? withUncounted(meterSegments(cycle), fromProvider) : usedOnly(fromProvider);
  const capacity = size !== null ? Math.max(size, used) : used;
  const over = size !== null && used > size;
  const left = size !== null ? Math.max(0, size - used) : 0;
  const extra = size !== null && quota?.plan_bytes ? size - quota.plan_bytes : 0;
  const reading = splitBytes(used);

  return (
    <section className="panel p-5 sm:p-7" aria-labelledby="meter-title">
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-3">
        <div>
          <h1 id="meter-title" className="text-sm text-ink-2">
            Used {lowerFirst(cycle.period.label)}
          </h1>
          <p className="mt-1 flex items-baseline gap-3">
            <span className="readout text-readout">{reading.value}</span>
            <span className="readout text-2xl text-ink-2">{reading.unit}</span>
          </p>
        </div>
        {quota ? (
          <dl className="grid grid-cols-2 gap-x-8 gap-y-1 text-sm sm:text-right">
            <dt className="text-ink-2">Plan</dt>
            <dd className="font-semibold tabular">{formatBytes(quota.plan_bytes ?? size ?? 0)}</dd>
            {extra > 0 && (
              <>
                <dt className="text-ink-2">Extra bundles</dt>
                <dd className="font-semibold tabular">{formatBytes(extra)}</dd>
              </>
            )}
            <dt className="text-ink-2">{over ? "Over by" : "Left"}</dt>
            <dd className={`font-semibold tabular ${over ? "text-critical" : ""}`}>
              {quota.pending ? "Renewing…" : formatBytes(over && size !== null ? used - size : left)}
            </dd>
            <dt className="text-ink-2">Renews</dt>
            <dd className="font-semibold">{formatDay(quota.cycle_end)}</dd>
            {quota.source === "isp" && (
              <dd className="col-span-2 text-xs text-muted">
                {quota.pending
                  ? `${quota.provider?.name ?? "The provider"} has not shown the new cycle yet.`
                  : `${quota.provider?.name ?? "Provider"} figures for ${quota.number}.${
                      counting ? ` Tattle counted ${formatBytes(quota.counted)}.` : ""
                    }`}
              </dd>
            )}
          </dl>
        ) : (
          <p className="max-w-xs text-sm text-ink-2">
            {canEdit ? (
              <>
                <Link to="/settings" className="text-ink underline underline-offset-4">
                  Add your plan size
                </Link>{" "}
                to see how much is left this cycle.
              </>
            ) : (
              "Shares of everything used this cycle."
            )}
          </p>
        )}
      </div>

      <div
        className="mt-6 flex h-6 w-full overflow-hidden rounded-md bg-track"
        role="img"
        aria-label={
          size !== null
            ? `${percent(used, size)} of the plan used`
            : `${formatBytes(used)} used this cycle, split by device`
        }
      >
        {capacity > 0 &&
          segments.map((segment, i) => (
            <div
              key={segment.key}
              className="h-full"
              title={`${segment.label}: ${formatBytes(segment.bytes)}`}
              style={{
                flexBasis: `${(segment.bytes / capacity) * 100}%`,
                background: segment.color,
                marginRight: i < segments.length - 1 ? GAP_PX : 0,
              }}
            />
          ))}
      </div>
      {size !== null && (
        <div className="mt-1.5 flex justify-between text-xs text-muted tabular">
          <span>0</span>
          <span>{percent(used, size)} used</span>
          <span>{formatBytes(size)}</span>
        </div>
      )}

      {segments.length > 0 ? (
        <ul className="mt-5 grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
          {segments.map((segment) => {
            const label = (
              <>
                <span className="mt-1.5 h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: segment.color }} aria-hidden />
                <span className="min-w-0">
                  <span className="block truncate">{segment.label}</span>
                  <span className="text-ink-2 tabular">
                    {formatBytes(segment.bytes)} · {percent(segment.bytes, used)}
                  </span>
                </span>
              </>
            );
            return (
              <li key={segment.key}>
                {segment.deviceId !== null ? (
                  <Link to={`/devices/${segment.deviceId}?range=cycle`} className="flex gap-2 rounded hover:text-ink">
                    {label}
                  </Link>
                ) : (
                  <span className="flex gap-2">{label}</span>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="mt-5 text-sm text-ink-2">
          {counting
            ? "Nothing counted yet this cycle. Usage appears as devices go online."
            : "Nothing used yet this cycle, as far as the provider says."}
        </p>
      )}
    </section>
  );
}
