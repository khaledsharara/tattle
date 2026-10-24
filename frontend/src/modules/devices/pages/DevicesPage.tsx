import { useMemo, useState } from "react";

import { Notice, RangeBar, usePolling, useRange, type DeviceList as DeviceListData } from "../../shared";
import { DeviceList } from "../components/DeviceList";
import { IdentityLog } from "../components/IdentityLog";
import { MergeSuggestions } from "../components/MergeSuggestions";
import type { DeviceSort } from "../types";
import { matchesSearch, sortDevices } from "../utils/deviceText";

const SORTS: { key: DeviceSort; label: string }[] = [
  { key: "usage", label: "Most data" },
  { key: "recent", label: "Recently active" },
  { key: "name", label: "Name" },
];

export function DevicesPage() {
  const range = useRange("today");
  const { data, error, refreshing, reload } = usePolling<DeviceListData>(`/api/devices?${range.query}`, 10_000);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<DeviceSort>("usage");

  const devices = useMemo(
    () => sortDevices((data?.devices ?? []).filter((d) => matchesSearch(d, search)), sort),
    [data, search, sort],
  );
  const newCount = data?.devices.filter((d) => d.is_new).length ?? 0;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="readout text-3xl">Devices</h1>
          <p className="mt-1 text-sm text-ink-2">
            Every device Tattle has seen. Open a device to rename it.
            {newCount > 0 && ` ${newCount} new in the last day.`}
          </p>
        </div>
      </div>

      <MergeSuggestions onChanged={reload} />
      <IdentityLog />

      <RangeBar state={range} busy={refreshing} />

      <div className="flex flex-wrap items-center gap-3">
        <label htmlFor="device-search" className="sr-only">
          Search devices
        </label>
        <input
          id="device-search"
          type="search"
          className="field max-w-xs"
          placeholder="Search by name, address or maker"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <label htmlFor="device-sort" className="text-sm text-ink-2">
          Sort
        </label>
        <select id="device-sort" className="field w-auto" value={sort} onChange={(e) => setSort(e.target.value as DeviceSort)}>
          {SORTS.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      {error && !data && (
        <Notice title="Could not load devices" tone="error">
          {error}
        </Notice>
      )}
      {data && devices.length === 0 && (
        <Notice title={search ? "No device matches that search" : "No devices yet"}>
          {search ? "Try part of the name, the address or the maker." : "Devices appear here as they use the internet."}
        </Notice>
      )}
      {data && devices.length > 0 && (
        <div className={refreshing ? "opacity-60 transition-opacity" : "transition-opacity"}>
          <DeviceList
            devices={devices}
            colorOrder={data.color_order}
            periodLabel={data.period.label}
            rangeQuery={range.query}
            showToday={data.period.key !== "today"}
          />
        </div>
      )}
    </div>
  );
}
