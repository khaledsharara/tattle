import { BrowserRouter, Route, Routes } from "react-router-dom";

import { DeviceDetailPage, DevicesPage } from "./modules/devices";
import { LinePage } from "./modules/isp";
import { OverviewPage } from "./modules/overview";
import { SettingsPage } from "./modules/settings";
import { AdminProvider, AppShell, Notice, StatusProvider } from "./modules/shared";

export function App() {
  return (
    <BrowserRouter>
      <StatusProvider>
        <AdminProvider>
          <AppShell>
            <Routes>
              <Route path="/" element={<OverviewPage />} />
              <Route path="/devices" element={<DevicesPage />} />
              <Route path="/devices/:id" element={<DeviceDetailPage />} />
              <Route path="/isp/:number" element={<LinePage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<Notice title="There is no page here">Use the menu above to get back.</Notice>} />
            </Routes>
          </AppShell>
        </AdminProvider>
      </StatusProvider>
    </BrowserRouter>
  );
}
