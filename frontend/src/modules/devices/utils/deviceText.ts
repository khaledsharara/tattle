import { formatAgo, formatDateTime, type Device } from "../../shared";
import type { DeviceFact, DeviceSort } from "../types";

/** One line saying what Tattle knows about where a device comes from. */
export function identityLine(device: Device): string {
  if (device.is_server) return "This server, its apps and downloads";
  const parts: string[] = [];
  if (device.kind) parts.push(device.kind);
  if (device.vendor) parts.push(device.vendor);
  else if (device.private_mac) parts.push("private Wi-Fi address");
  const otherName = device.aka.find((name) => name !== device.name);
  if (otherName) parts.push(`calls itself “${otherName}”`);
  if (device.identities.length > 1) parts.push(`${device.identities.length} addresses`);
  if (device.ip) parts.push(device.ip);
  const line = parts.join(", ");
  if (!line) return "No details reported";
  // "iPhone or iPad" keeps its small i.
  return /^i[A-Z]/.test(line) ? line : line.charAt(0).toUpperCase() + line.slice(1);
}

export function activityLine(device: Device): string {
  if (device.active) return "Using the internet now";
  if (device.last_active) return `Last used the internet ${formatAgo(device.last_active)}`;
  return `Connected ${formatAgo(device.last_seen)}, no internet use yet`;
}

export function deviceFacts(device: Device): DeviceFact[] {
  const facts: DeviceFact[] = [];
  if (device.kind) facts.push({ label: "Kind of device", value: device.kind, hint: device.model ? `Model ${device.model}` : undefined });
  if (device.aka.length) facts.push({ label: "Names it announces", value: device.aka.join(", ") });
  if (device.ip) facts.push({ label: "Address", value: device.ip });
  if (device.mac)
    facts.push({
      label: "Hardware address",
      value: device.mac,
      hint: device.private_mac
        ? "A private address the device made up for this Wi-Fi network. It uses a different one on each network name."
        : undefined,
    });
  if (device.vendor) facts.push({ label: "Maker", value: device.vendor });
  facts.push({ label: "First seen", value: formatDateTime(device.first_seen) });
  facts.push({ label: "Last seen", value: formatDateTime(device.last_seen) });
  return facts;
}

export function matchesSearch(device: Device, search: string): boolean {
  const q = search.trim().toLowerCase();
  if (!q) return true;
  return [device.name, device.hostname, device.mac, device.ip, device.vendor, device.kind, ...device.aka]
    .filter(Boolean)
    .some((field) => String(field).toLowerCase().includes(q));
}

export function sortDevices(devices: Device[], sort: DeviceSort): Device[] {
  const copy = [...devices];
  if (sort === "name") copy.sort((a, b) => a.name.localeCompare(b.name));
  else if (sort === "recent") copy.sort((a, b) => (b.last_active ?? b.last_seen) - (a.last_active ?? a.last_seen));
  else copy.sort((a, b) => b.rx + b.tx - (a.rx + a.tx));
  return copy;
}
