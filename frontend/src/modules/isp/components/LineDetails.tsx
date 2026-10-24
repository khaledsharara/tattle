import { formatAgo, formatBytes, formatDateTime, percent, type IspLineDetail } from "../../shared";
import { formatIsoDay, formatKbps } from "../utils/format";

/** Facts about the line that don't fit the tiles: plan, when it was in use, the line's speed. */
export function LineDetails({ detail }: { detail: IspLineDetail }) {
  const { line, link, cycle } = detail;
  const name = line.provider.name;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2.5 text-sm">
      {line.plan && <Row label="Plan">{line.plan}</Row>}
      {cycle && (
        <Row label="This cycle">
          {formatIsoDay(cycle.start, true)} – {formatIsoDay(cycle.end, true)}
        </Row>
      )}
      {line.live ? (
        <Row label="In use">{line.in_use_since ? `Since ${formatIsoDay(line.in_use_since)}` : "Yes, it's the line the house is on"}</Row>
      ) : (
        <Row label="Last used">{line.last_used ? formatIsoDay(line.last_used) : "Not in the history Tattle has"}</Row>
      )}
      {link && (
        <Row label="Line speed">
          {link.state === "up" ? (
            <>
              {formatKbps(link.down_kbps)} down · {formatKbps(link.up_kbps)} up
              <span className="text-muted"> (what the router's line synced at, {formatAgo(link.ts)})</span>
            </>
          ) : (
            `The router's line is ${link.state} (${formatAgo(link.ts)})`
          )}
        </Row>
      )}
      {line.check && (
        <Row label="Checked">
          Since {formatDateTime(line.check.since)}, {name} counted {formatBytes(line.check.isp)} and Tattle{" "}
          {formatBytes(line.check.counted)}
          {line.check.isp > 0 ? ` (${percent(line.check.counted, line.check.isp)} of ${name}'s figure)` : ""}
        </Row>
      )}
      <Row label={`From ${name}`}>
        {line.status === "expired"
          ? `Signed out: sign in again from Settings (figures from ${formatAgo(line.read_at)})`
          : line.error
            ? `Last read failed (${line.error}); trying again`
            : `Read ${formatAgo(line.read_at)}, every 15 minutes`}
      </Row>
    </dl>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-ink-2">{label}</dt>
      <dd>{children}</dd>
    </>
  );
}
