import { Link } from "react-router-dom";

import { deviceColor, formatBytes, formatRate, type Device } from "../../shared";
import { activityLine, identityLine } from "../utils/deviceText";

interface DeviceListProps {
  devices: Device[];
  colorOrder: number[];
  periodLabel: string;
  rangeQuery: string;
  /** Hide the separate "Today" column when the period already is today. */
  showToday: boolean;
}

function ActivityDot({ device }: { device: Device }) {
  return (
    <span
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${device.active ? "bg-good" : "bg-hairline"}`}
      aria-hidden
    />
  );
}

export function DeviceList({ devices, colorOrder, periodLabel, rangeQuery, showToday }: DeviceListProps) {
  return (
    <div className="panel overflow-hidden">
      {/* Wide screens: a table that lines numbers up. */}
      <table className="hidden w-full table-fixed text-left text-sm md:table">
        <colgroup>
          <col />
          <col className="w-28" />
          <col className="w-24" />
          <col className="w-24" />
          {showToday && <col className="w-24" />}
          <col className="w-32" />
        </colgroup>
        <thead className="border-b border-hairline text-xs text-muted">
          <tr>
            <th className="px-5 py-3 font-medium">Device</th>
            <th className="px-3 py-3 text-right font-medium">{periodLabel}</th>
            <th className="px-3 py-3 text-right font-medium">Down</th>
            <th className="px-3 py-3 text-right font-medium">Up</th>
            {showToday && <th className="px-3 py-3 text-right font-medium">Today</th>}
            <th className="px-5 py-3 text-right font-medium">Now</th>
          </tr>
        </thead>
        <tbody>
          {devices.map((device) => (
            <tr key={device.id} className="border-b border-hairline last:border-0 hover:bg-ink/[0.03]">
              <td className="px-5 py-3">
                <Link to={`/devices/${device.id}?${rangeQuery}`} className="group flex items-start gap-3">
                  <span
                    className="mt-1 h-3 w-3 shrink-0 rounded-sm"
                    style={{ background: deviceColor(device.id, colorOrder) }}
                    aria-hidden
                  />
                  <span className="min-w-0">
                    <span className="flex items-center gap-2">
                      <span className="truncate font-medium group-hover:underline group-hover:underline-offset-4">{device.name}</span>
                      {device.is_new && <span className="rounded bg-warn/25 px-1.5 py-0.5 text-[11px] font-medium text-ink">New</span>}
                    </span>
                    <span className="block truncate text-xs text-muted">{identityLine(device)}</span>
                  </span>
                </Link>
              </td>
              <td className="px-3 py-3 text-right font-semibold tabular">{formatBytes(device.rx + device.tx)}</td>
              <td className="px-3 py-3 text-right text-ink-2 tabular">{formatBytes(device.rx)}</td>
              <td className="px-3 py-3 text-right text-ink-2 tabular">{formatBytes(device.tx)}</td>
              {showToday && (
                <td className="px-3 py-3 text-right text-ink-2 tabular">{formatBytes(device.today_rx + device.today_tx)}</td>
              )}
              <td className="px-5 py-3 text-right tabular">
                <span className="inline-flex items-center justify-end gap-2" title={activityLine(device)}>
                  <ActivityDot device={device} />
                  {device.active ? formatRate(device.rx_bps + device.tx_bps) : <span className="text-muted">idle</span>}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* Phones: one row per device. */}
      <ul className="divide-y divide-hairline md:hidden">
        {devices.map((device) => (
          <li key={device.id}>
            <Link to={`/devices/${device.id}?${rangeQuery}`} className="flex items-start gap-3 px-4 py-3">
              <span className="mt-1.5 h-3 w-3 shrink-0 rounded-sm" style={{ background: deviceColor(device.id, colorOrder) }} aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="truncate font-medium">{device.name}</span>
                  {device.is_new && <span className="rounded bg-warn/25 px-1.5 py-0.5 text-[11px] font-medium">New</span>}
                </span>
                <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
                  <ActivityDot device={device} />
                  {activityLine(device)}
                </span>
              </span>
              <span className="shrink-0 text-right">
                <span className="block font-semibold tabular">{formatBytes(device.rx + device.tx)}</span>
                <span className="text-xs text-muted tabular">{formatBytes(device.today_rx + device.today_tx)} today</span>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
