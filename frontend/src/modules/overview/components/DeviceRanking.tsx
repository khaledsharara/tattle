import { Link } from "react-router-dom";

import { deviceColor, formatBytes, type Overview } from "../../shared";

const SHOWN = 10;

/** Who used it: devices ranked by total for the selected period. */
export function DeviceRanking({ overview, rangeQuery }: { overview: Overview; rangeQuery: string }) {
  const devices = overview.devices.slice(0, SHOWN);
  const max = Math.max(1, ...devices.map((d) => d.rx + d.tx));
  const hidden = overview.devices.length - devices.length;

  return (
    <section aria-labelledby="ranking-title">
      <h2 id="ranking-title" className="mb-3 text-sm font-semibold">
        By device
      </h2>
      {devices.length === 0 ? (
        <p className="text-sm text-ink-2">No internet use counted in this period.</p>
      ) : (
        <ol className="space-y-3">
          {devices.map((device) => {
            const total = device.rx + device.tx;
            return (
              <li key={device.id}>
                <Link to={`/devices/${device.id}?${rangeQuery}`} className="group block">
                  <div className="flex items-baseline justify-between gap-3 text-sm">
                    <span className="truncate group-hover:underline group-hover:underline-offset-4">{device.name}</span>
                    <span className="shrink-0 font-semibold tabular">{formatBytes(total)}</span>
                  </div>
                  <div className="mt-1 h-1.5 w-full rounded-full bg-track">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${Math.max(1, (total / max) * 100)}%`,
                        background: deviceColor(device.id, overview.color_order),
                      }}
                    />
                  </div>
                  <p className="mt-1 text-xs text-muted tabular">
                    {formatBytes(device.rx)} down · {formatBytes(device.tx)} up
                  </p>
                </Link>
              </li>
            );
          })}
        </ol>
      )}
      {hidden > 0 && (
        <Link to={`/devices?${rangeQuery}`} className="mt-4 inline-block text-sm text-ink-2 underline underline-offset-4">
          {hidden} more on the Devices page
        </Link>
      )}
    </section>
  );
}
