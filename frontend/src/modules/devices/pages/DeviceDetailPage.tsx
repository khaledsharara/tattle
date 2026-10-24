import { Link, Navigate, useParams } from "react-router-dom";

import {
  ColumnChart,
  DIRECTION_COLORS,
  Notice,
  RangeBar,
  deviceColor,
  formatBytes,
  formatRate,
  lowerFirst,
  useAdmin,
  usePolling,
  useRange,
  type DeviceDetail,
} from "../../shared";
import { IdentityList } from "../components/IdentityList";
import { MergePanel } from "../components/MergePanel";
import { RenameForm } from "../components/RenameForm";
import { activityLine, deviceFacts, identityLine } from "../utils/deviceText";

const SERIES = [
  { key: "rx", label: "Downloaded", color: DIRECTION_COLORS.rx },
  { key: "tx", label: "Uploaded", color: DIRECTION_COLORS.tx },
];

export function DeviceDetailPage() {
  const { id } = useParams();
  const range = useRange("today");
  const { isAdmin } = useAdmin();
  const { data, error, refreshing, reload } = usePolling<DeviceDetail>(`/api/devices/${id}?${range.query}`, 10_000);

  // An entry that was merged lives on as part of another device: go there.
  const mergedInto = error?.match(/^merged:(\d+)$/)?.[1];
  if (mergedInto) return <Navigate to={`/devices/${mergedInto}?${range.query}`} replace />;

  if (error && !data) {
    return (
      <div className="space-y-4">
        <Link to={`/devices?${range.query}`} className="text-sm text-ink-2 underline underline-offset-4">
          Back to devices
        </Link>
        <Notice title="Could not load this device" tone="error">
          {error}
        </Notice>
      </div>
    );
  }
  if (!data) return <div className="panel h-64 animate-pulse" aria-label="Loading" />;

  const { device } = data;
  const points = data.series.map((p) => ({ t: p.t, values: { rx: p.rx, tx: p.tx } }));

  return (
    <div className="space-y-6">
      <Link to={`/devices?${range.query}`} className="text-sm text-ink-2 underline underline-offset-4">
        All devices
      </Link>

      <header className="flex flex-wrap items-start gap-4">
        <span className="mt-2 h-4 w-4 shrink-0 rounded" style={{ background: deviceColor(device.id, data.color_order) }} aria-hidden />
        <div className="min-w-0 flex-1">
          <h1 className="readout break-words text-3xl">{device.name}</h1>
          <p className="mt-1 text-sm text-ink-2">{identityLine(device)}</p>
          <p className="mt-1 flex items-center gap-2 text-sm">
            <span className={`h-2 w-2 rounded-full ${device.active ? "bg-good" : "bg-hairline"}`} aria-hidden />
            {activityLine(device)}
            {device.active && <span className="text-ink-2 tabular">· {formatRate(device.rx_bps + device.tx_bps)}</span>}
          </p>
        </div>
      </header>

      <RangeBar state={range} busy={refreshing} />

      <dl className={`panel grid grid-cols-2 gap-5 p-5 sm:grid-cols-4 ${refreshing ? "opacity-60" : ""}`}>
        <div>
          <dt className="text-sm text-ink-2">Total, {lowerFirst(data.period.label)}</dt>
          <dd className="mt-1 text-2xl font-semibold">{formatBytes(device.rx + device.tx)}</dd>
        </div>
        <div>
          <dt className="text-sm text-ink-2">Downloaded</dt>
          <dd className="mt-1 text-2xl font-semibold">{formatBytes(device.rx)}</dd>
        </div>
        <div>
          <dt className="text-sm text-ink-2">Uploaded</dt>
          <dd className="mt-1 text-2xl font-semibold">{formatBytes(device.tx)}</dd>
        </div>
        {data.period.key === "today" ? (
          <div>
            <dt className="text-sm text-ink-2">Right now</dt>
            <dd className="mt-1 text-2xl font-semibold">{formatRate(device.rx_bps + device.tx_bps)}</dd>
          </div>
        ) : (
          <div>
            <dt className="text-sm text-ink-2">Today</dt>
            <dd className="mt-1 text-2xl font-semibold">{formatBytes(device.today_rx + device.today_tx)}</dd>
          </div>
        )}
      </dl>

      <section className={`panel p-5 ${refreshing ? "opacity-60" : ""}`}>
        <ColumnChart
          title={data.period.bucket === "hour" ? "Each hour" : "Each day"}
          points={points}
          series={SERIES}
          bucket={data.period.bucket}
        />
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section className="panel p-5" aria-labelledby="facts-title">
          <h2 id="facts-title" className="mb-3 text-sm font-semibold">
            What Tattle knows about it
          </h2>
          <dl className="space-y-3 text-sm">
            {deviceFacts(device).map((fact) => (
              <div key={fact.label}>
                <dt className="text-ink-2">{fact.label}</dt>
                <dd className="font-medium tabular">{fact.value}</dd>
                {fact.hint && <dd className="mt-0.5 text-xs text-muted">{fact.hint}</dd>}
              </div>
            ))}
          </dl>
        </section>

        <section className="panel space-y-6 p-5" aria-labelledby="manage-title">
          <h2 id="manage-title" className="text-sm font-semibold">
            Name and merge
          </h2>
          {isAdmin ? (
            <>
              <div className="space-y-2">
                <p className="text-sm text-ink-2">Shown everywhere instead of the name the device reports.</p>
                <RenameForm device={device} onSaved={reload} />
              </div>
              {!device.is_server && <MergePanel deviceId={device.id} deviceName={device.name} />}
            </>
          ) : (
            <p className="text-sm text-ink-2">
              <Link to="/settings" className="text-ink underline underline-offset-4">
                Sign in as admin
              </Link>{" "}
              to rename this device or merge it with another entry.
            </p>
          )}
        </section>
      </div>

      <IdentityList device={device} onChanged={reload} />
    </div>
  );
}
