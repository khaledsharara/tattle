import { formatBytes, percent, type IspItem } from "../../shared";
import { formatIsoDay } from "../utils/format";

/** The line's plan and any bundles bought on top of it. */
export function BundleList({ items, provider }: { items: IspItem[]; provider: string }) {
  const bundles = items.filter((item) => item.is_data);
  if (bundles.length === 0) return <p className="text-sm text-ink-2">{provider} lists no data bundle on this line.</p>;
  return (
    <ul className="space-y-5">
      {bundles.map((item, i) => (
        <li key={`${item.kind}-${item.name}-${i}`}>
          <Bundle item={item} provider={provider} />
        </li>
      ))}
    </ul>
  );
}

function Bundle({ item, provider }: { item: IspItem; provider: string }) {
  const title = `${item.kind === "plan" ? "Plan" : "Extra bundle"}${item.name ? ` · ${item.name}` : ""}`;
  const left = item.remaining_bytes != null ? formatBytes(item.remaining_bytes) : "—";

  if (item.total_bytes == null || item.remaining_bytes == null) {
    // Some providers send what is left of an extra bundle, not its size; some plans are unlimited.
    return (
      <div className="space-y-0.5 text-sm">
        <p className="text-ink-2">{title}</p>
        <p>
          <strong className="tabular">{left}</strong> left
          <span className="text-muted"> ({provider} doesn't say what size it is)</span>
        </p>
      </div>
    );
  }

  const used = Math.max(0, item.total_bytes - item.remaining_bytes);
  const share = item.total_bytes > 0 ? Math.min(1, used / item.total_bytes) : 0;
  return (
    <div className="space-y-1.5 text-sm">
      <p className="text-ink-2">{title}</p>
      <p>
        <strong className="tabular">{formatBytes(used)}</strong> used of {formatBytes(item.total_bytes)}
      </p>
      <div
        className="h-2 w-full overflow-hidden rounded bg-track"
        role="img"
        aria-label={`${percent(used, item.total_bytes)} of ${formatBytes(item.total_bytes)} used`}
      >
        <div className="h-full rounded bg-ink" style={{ width: `${share * 100}%` }} />
      </div>
      <dl className="flex flex-wrap gap-x-6 gap-y-1">
        <div className="flex gap-1.5">
          <dt className="text-ink-2">Left</dt>
          <dd className="font-semibold tabular">{left}</dd>
        </div>
        {item.renews && (
          <div className="flex gap-1.5">
            <dt className="text-ink-2">Renews</dt>
            <dd className="font-semibold">{formatIsoDay(item.renews, true)}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}
