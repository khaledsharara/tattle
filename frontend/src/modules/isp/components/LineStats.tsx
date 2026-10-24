import { formatBytes, splitBytes, type IspLine } from "../../shared";
import { daysUntil, formatIsoDay, formatWhen, inDaysText, lastsText } from "../utils/format";

/** The three things to know at a glance: what is left, how long it lasts, when it renews. */
export function LineStats({ line }: { line: IspLine }) {
  const remaining = line.reading?.remaining ?? null;
  const left = remaining != null ? splitBytes(remaining) : null;
  const { per_day: perDay, days_left: daysLeft, runs_out: runsOut } = line.pace;

  return (
    <dl className="grid gap-4 sm:grid-cols-3">
      <Tile label="Left now">
        <span className="readout text-4xl">{left?.value ?? "—"}</span>
        {left && <span className="ml-1.5 text-sm text-ink-2">{left.unit}</span>}
      </Tile>

      <Tile label="At this week's pace">
        {perDay == null ? (
          <span className="text-sm text-ink-2">Needs a few days of readings</span>
        ) : remaining === 0 ? (
          <span className="text-sm text-ink-2">Nothing left{line.renews ? ` until ${formatIsoDay(line.renews, true)}` : ""}</span>
        ) : (
          <>
            <span className="readout text-2xl">{daysLeft != null ? lastsText(daysLeft) : "—"}</span>
            <span className="mt-1 block text-sm text-ink-2">
              {runsOut ? `runs out around ${formatWhen(runsOut)} · ` : "if it were in use · "}
              the house uses {formatBytes(perDay)} a day
            </span>
          </>
        )}
      </Tile>

      <Tile label="Plan renews">
        {line.renews ? (
          <>
            <span className="readout text-2xl">{formatIsoDay(line.renews)}</span>
            <span className="mt-1 block text-sm text-ink-2">{inDaysText(daysUntil(line.renews))}</span>
          </>
        ) : (
          <span className="text-sm text-ink-2">Not known yet</span>
        )}
      </Tile>
    </dl>
  );
}

function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="panel p-5">
      <dt className="text-sm text-ink-2">{label}</dt>
      <dd className="mt-2">{children}</dd>
    </div>
  );
}
