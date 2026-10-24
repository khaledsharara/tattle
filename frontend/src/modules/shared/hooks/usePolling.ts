import { useCallback, useEffect, useRef, useState } from "react";

import { apiGet } from "../utils/api";

interface PollingState<T> {
  data: T | null;
  error: string | null;
  /** True while a request for a *different* URL is in flight; old data stays on screen. */
  refreshing: boolean;
  reload: () => void;
}

/**
 * GET `url` now and every `intervalMs`. While a new URL loads, the previous
 * data is kept so charts hold their frame instead of flashing empty.
 */
export function usePolling<T>(url: string | null, intervalMs = 10_000): PollingState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [tick, setTick] = useState(0);
  const lastUrl = useRef<string | null>(null);

  const reload = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    const urlChanged = lastUrl.current !== url;
    if (urlChanged) setRefreshing(true);

    apiGet<T>(url, controller.signal)
      .then((result) => {
        lastUrl.current = url;
        setData(result);
        setError(null);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : "Could not reach Tattle");
      })
      .finally(() => {
        if (!controller.signal.aborted) setRefreshing(false);
      });

    return () => controller.abort();
  }, [url, tick]);

  useEffect(() => {
    if (!url || intervalMs <= 0) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") setTick((n) => n + 1);
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [url, intervalMs]);

  return { data, error, refreshing, reload };
}
