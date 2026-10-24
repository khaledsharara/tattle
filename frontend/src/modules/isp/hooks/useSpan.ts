import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import type { IspSpan } from "../../shared";
import type { SpanState } from "../types";

const SPANS: IspSpan[] = ["7d", "30d", "cycle", "all", "custom"];

/**
 * The span a number page shows, kept in the URL (`?span=30d`, or
 * `?span=custom&from=…&to=…`) so it survives reloads. Its own parameter, so
 * it never clashes with the dashboard's `range`.
 */
export function useSpan(defaultSpan: IspSpan = "30d"): SpanState {
  const [params, setParams] = useSearchParams();
  const raw = params.get("span") as IspSpan | null;
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const requested = raw && SPANS.includes(raw) ? raw : defaultSpan;
  const range = requested === "custom" && !(from && to) ? defaultSpan : requested;

  const query = useMemo(() => {
    const q = new URLSearchParams({ span: range });
    if (range === "custom") {
      q.set("from", from);
      q.set("to", to);
    }
    return q.toString();
  }, [range, from, to]);

  const setRange = useCallback(
    (next: string, nextFrom?: string, nextTo?: string) => {
      setParams(
        (prev) => {
          const q = new URLSearchParams(prev);
          q.set("span", next);
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
