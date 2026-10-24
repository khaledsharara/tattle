import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import type { AppSettings } from "../types/api";
import { apiGet, apiSend } from "../utils/api";

interface AdminContextValue {
  settings: AppSettings | null;
  isAdmin: boolean;
  login: (password: string) => Promise<void>;
  /** First run: the setup code from the install, and the admin password to use from now on. */
  setup: (code: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  setSettings: (settings: AppSettings) => void;
}

const AdminContext = createContext<AdminContextValue | null>(null);

export function AdminProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<AppSettings | null>(null);

  const refresh = useCallback(async () => {
    setSettings(await apiGet<AppSettings>("/api/settings"));
  }, []);

  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);

  const login = useCallback(
    async (password: string) => {
      await apiSend("/api/auth/login", "POST", { password });
      await refresh();
    },
    [refresh],
  );

  const setup = useCallback(
    async (code: string, password: string) => {
      await apiSend("/api/auth/setup", "POST", { code, password });
      await refresh();
    },
    [refresh],
  );

  const logout = useCallback(async () => {
    await apiSend("/api/auth/logout", "POST");
    await refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({ settings, isAdmin: Boolean(settings?.is_admin), login, setup, logout, refresh, setSettings }),
    [settings, login, setup, logout, refresh],
  );
  return <AdminContext.Provider value={value}>{children}</AdminContext.Provider>;
}

export function useAdmin(): AdminContextValue {
  const value = useContext(AdminContext);
  if (!value) throw new Error("useAdmin must be used inside <AdminProvider>");
  return value;
}
