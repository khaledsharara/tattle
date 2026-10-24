import { Link } from "react-router-dom";

import { ProviderBadge, StatusLabel, formatBytes, splitBytes, usePolling, type IspLine, type IspStatus } from "../../shared";
import { formatIsoDay, lastsText } from "../utils/format";

/** One card per line on the Overview; each opens that line's page. */
export function LineCards({ canEdit }: { canEdit: boolean }) {
  const { data } = usePolling<IspStatus>("/api/isp", 60_000);
  if (!data) return null;

  if (data.lines.length === 0) {
    return (
      <section className="panel p-5 text-sm text-ink-2" aria-labelledby="isp-title">
        <h2 id="isp-title" className="mb-1 text-sm font-semibold text-ink">
          From your provider
        </h2>
        {canEdit ? (
          <p>
            <Link to="/settings" className="text-ink underline underline-offset-4">
              Connect your internet provider
            </Link>{" "}
            to see its own figures here: what's left, when it renews, and how that compares with Tattle's count.
          </p>
        ) : (
          <p>Your provider's own figures appear here once an admin connects it in Settings.</p>
        )}
      </section>
    );
  }

  const lines = [...data.lines].sort((a, b) => Number(b.live) - Number(a.live) || a.number.localeCompare(b.number));
  const several = lines.length > 1;
  return (
    <section className="grid gap-6 md:grid-cols-2" aria-label="Internet lines">
      {lines.map((line) => (
        <LineCard key={line.number} line={line} several={several} />
      ))}
    </section>
  );
}

function LineCard({ line, several }: { line: IspLine; several: boolean }) {
  const plan = line.reading?.items.find((item) => item.kind === "plan" && item.is_data);
  const extras = line.reading?.items.filter((item) => item.kind === "extra" && item.is_data) ?? [];
  const planUsed = plan?.total_bytes != null && plan.remaining_bytes != null ? plan.total_bytes - plan.remaining_bytes : null;
  const renews = line.renews ? `renews ${formatIsoDay(line.renews, true)}` : null;
  const left = line.reading?.remaining != null ? splitBytes(line.reading.remaining) : null;

  return (
    <Link
      to={`/isp/${encodeURIComponent(line.number)}`}
      className="panel group block space-y-3 p-5 transition-shadow hover:ring-ink/25"
      aria-label={`${line.provider.name} line ${line.number}: details`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="flex flex-wrap items-baseline gap-x-1.5">
          <span className="text-sm text-ink-2">{line.provider.name} ·</span>
          <span className="font-semibold tabular">{line.number}</span>
          <ProviderBadge provider={line.provider} />
        </p>
        {several && (
          <span className="text-sm text-ink-2">{line.live ? <StatusLabel status="good">In use now</StatusLabel> : "Not in use"}</span>
        )}
      </div>

      {left ? (
        <p className="flex flex-wrap items-baseline gap-x-2">
          <span className="readout text-5xl">{left.value}</span>
          <span className="text-sm text-ink-2">{left.unit} left</span>
        </p>
      ) : (
        <p className="text-sm text-ink-2">Not read yet.</p>
      )}

      <div className="space-y-1 text-sm text-ink-2">
        {plan && (
          <p>
            {plan.remaining_bytes === 0
              ? "Plan used up"
              : planUsed != null && plan.total_bytes != null
                ? `${formatBytes(planUsed)} of ${formatBytes(plan.total_bytes)} used`
                : "Unlimited plan"}
            {renews ? ` · ${renews}` : ""}
            {extras.length > 0 ? ` · ${extras.length === 1 ? "an extra bundle" : `${extras.length} extra bundles`}` : ""}
          </p>
        )}
        {line.live && line.pace.days_left != null && line.pace.per_day ? (
          <p>
            Lasts about <strong className="text-ink">{lastsText(line.pace.days_left)}</strong> at this week's pace (
            {formatBytes(line.pace.per_day)} a day)
          </p>
        ) : (
          !line.live && line.last_used && <p>Last used {formatIsoDay(line.last_used, true)}</p>
        )}
      </div>

      {line.status === "expired" ? (
        <StatusLabel status="critical">Signed out by {line.provider.name}: sign in again from Settings</StatusLabel>
      ) : (
        line.error && <StatusLabel status="warn">Last read failed; trying again</StatusLabel>
      )}

      <p className="text-right text-sm text-ink-2 group-hover:text-ink">
        Details <span aria-hidden>→</span>
      </p>
    </Link>
  );
}
