import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import type { RangeKey } from "../types/api";

const KEYS: RangeKey[] = ["today", "yesterday", "7d", "30d", "cycle", "custom"];

export interface RangeState {
  range: RangeKey;
  from: string;
  to: string;
  /** Query string for the API, e.g. `range=custom&from=2026-10-01&to=2026-10-05`. */
  query: string;
  setRange: (range: RangeKey, from?: string, to?: string) => void;
}

/** The selected period lives in the URL, so it survives reloads and moving between pages. */
export function useRange(defaultRange: RangeKey = "cycle"): RangeState {
  const [params, setParams] = useSearchParams();
  const raw = params.get("range") as RangeKey | null;
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const requested = raw && KEYS.includes(raw) ? raw : defaultRange;
  // A custom range without both dates falls back to the default.
  const range = requested === "custom" && !(from && to) ? defaultRange : requested;

  const query = useMemo(() => {
    const q = new URLSearchParams({ range });
    if (range === "custom" && from && to) {
      q.set("from", from);
      q.set("to", to);
    }
    return q.toString();
  }, [range, from, to]);

  const setRange = useCallback(
    (next: RangeKey, nextFrom?: string, nextTo?: string) => {
      setParams(
        (prev) => {
          const q = new URLSearchParams(prev);
          q.set("range", next);
          if (next === "custom" && nextFrom && nextTo) {
            q.set("from", nextFrom);
            q.set("to", nextTo);
          } else {
            q.delete("from");
            q.delete("to");
          }
          return q;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  return { range, from, to, query, setRange };
}
