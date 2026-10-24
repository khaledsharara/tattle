import { useState } from "react";

export interface PeriodPreset {
  key: string;
  label: string;
}

/** A selected period with custom dates: the dashboard's range, or a number page's span. */
export interface PeriodState {
  range: string;
  from: string;
  to: string;
  // Method syntax, so states with a narrower key type (RangeState) still fit.
  setRange(key: string, from?: string, to?: string): void;
}

const PRESETS: PeriodPreset[] = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
  { key: "cycle", label: "This cycle" },
];

function todayIso(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

/** One row of period filters above everything it scopes. */
export function RangeBar({
  state,
  busy,
  presets = PRESETS,
  earliest,
}: {
  state: PeriodState;
  busy?: boolean;
  presets?: PeriodPreset[];
  /** First day with data, for the date pickers. */
  earliest?: string;
}) {
  const [customOpen, setCustomOpen] = useState(state.range === "custom");
  const [from, setFrom] = useState(state.from || todayIso());
  const [to, setTo] = useState(state.to || todayIso());

  return (
    <div className="flex flex-wrap items-center gap-2" aria-busy={busy}>
      <div role="radiogroup" aria-label="Period" className="flex flex-wrap gap-1 rounded-xl bg-surface p-1 ring-1 ring-hairline">
        {presets.map((preset) => {
          const selected = state.range === preset.key && !customOpen;
          return (
            <button
              key={preset.key}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => {
                setCustomOpen(false);
                state.setRange(preset.key);
              }}
              className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${
                selected ? "bg-ink text-page" : "text-ink-2 hover:bg-ink/5"
              }`}
            >
              {preset.label}
            </button>
          );
        })}
        <button
          type="button"
          role="radio"
          aria-checked={customOpen}
          onClick={() => setCustomOpen(true)}
          className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${
            customOpen ? "bg-ink text-page" : "text-ink-2 hover:bg-ink/5"
          }`}
        >
          Dates…
        </button>
      </div>

      {customOpen && (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (from && to && from <= to) state.setRange("custom", from, to);
          }}
        >
          <label className="sr-only" htmlFor="range-from">
            From
          </label>
          <input id="range-from" type="date" className="field w-auto" value={from} min={earliest} max={to} onChange={(e) => setFrom(e.target.value)} />
          <span className="text-sm text-muted">to</span>
          <label className="sr-only" htmlFor="range-to">
            To
          </label>
          <input id="range-to" type="date" className="field w-auto" value={to} min={from} max={todayIso()} onChange={(e) => setTo(e.target.value)} />
          <button type="submit" className="btn-primary" disabled={!from || !to || from > to}>
            Show
          </button>
        </form>
      )}
    </div>
  );
}
