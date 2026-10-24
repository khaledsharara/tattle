import { createContext, useContext, useMemo, type ReactNode } from "react";

import type { Status } from "../types/api";
import { setUnitBase } from "../utils/format";
import { usePolling } from "./usePolling";

interface StatusContextValue {
  status: Status | null;
  /** Devices are counted (gateway, router or demo), so device pages make sense. */
  counting: boolean;
  refresh: () => void;
}

const StatusContext = createContext<StatusContextValue | null>(null);

/** How Tattle is set up, so pages show only what this setup can know. */
export function StatusProvider({ children }: { children: ReactNode }) {
  const { data, reload } = usePolling<Status>("/api/status", 30_000);
  // Before the children render, so their figures are written the provider's way.
  if (data) setUnitBase(data.unit_base);

  const value = useMemo(
    () => ({ status: data ?? null, counting: data ? data.counting : true, refresh: reload }),
    [data, reload],
  );
  return <StatusContext.Provider value={value}>{children}</StatusContext.Provider>;
}

export function useStatus(): StatusContextValue {
  const value = useContext(StatusContext);
  if (!value) throw new Error("useStatus must be used inside <StatusProvider>");
  return value;
}
