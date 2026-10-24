import type { Bucket } from "../types/api";

const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

let unitBase = 1000;

/**
 * GB the way the connected provider counts them: some (e& Egypt) count
 * 1 GB = 1024 MB, most 1000 MB. Using theirs keeps figures equal to their app's.
 */
export function setUnitBase(base: number): void {
  unitBase = base === 1024 ? 1024 : 1000;
}

export function getUnitBase(): number {
  return unitBase;
}

export function splitBytes(bytes: number): { value: string; unit: string } {
  if (!Number.isFinite(bytes) || bytes <= 0) return { value: "0", unit: "MB" };
  let unit = 0;
  let value = bytes;
  while (value >= unitBase && unit < UNITS.length - 1) {
    value /= unitBase;
    unit += 1;
  }
  const digits = value >= 100 || unit === 0 ? 0 : value >= 10 ? 1 : 2;
  return { value: value.toFixed(digits), unit: UNITS[unit] };
}

export function formatBytes(bytes: number): string {
  const { value, unit } = splitBytes(bytes);
  return `${value} ${unit}`;
}

/** Compact axis ticks: 0, 500 MB, 1 GB, 1.5 GB. */
export function formatAxisBytes(bytes: number): string {
  if (bytes === 0) return "0";
  const { value, unit } = splitBytes(bytes);
  return `${Number(value)} ${unit}`;
}

/** Speeds in megabits, the way lines are sold. */
export function formatRate(bytesPerSecond: number): string {
  const mbps = (bytesPerSecond * 8) / 1_000_000;
  if (mbps <= 0) return "0 Mbps";
  if (mbps < 0.1) return `${Math.max(1, Math.round(mbps * 1000))} kbps`;
  return `${mbps < 10 ? mbps.toFixed(1) : Math.round(mbps)} Mbps`;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const dayLongFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });
const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatBucket(ts: number, bucket: Bucket, long = false): string {
  const date = new Date(ts * 1000);
  if (bucket === "hour") {
    const end = new Date((ts + 3600) * 1000);
    return long ? `${timeFmt.format(date)} – ${timeFmt.format(end)}` : timeFmt.format(date);
  }
  return long ? dayLongFmt.format(date) : dayFmt.format(date);
}

export function formatDay(ts: number): string {
  return dayFmt.format(new Date(ts * 1000));
}

export function formatDateTime(ts: number): string {
  return fullFmt.format(new Date(ts * 1000));
}

export function formatAgo(ts: number | null, now = Date.now() / 1000): string {
  if (!ts) return "never";
  const seconds = Math.max(0, Math.round(now - ts));
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours} h ago`;
  return formatDay(ts);
}

export function percent(part: number, whole: number): string {
  if (whole <= 0) return "0%";
  const value = (part / whole) * 100;
  return `${value < 1 && value > 0 ? value.toFixed(1) : Math.round(value)}%`;
}

/** "Since Oct 5" -> "since Oct 5": for period labels placed mid-sentence. */
export function lowerFirst(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1);
}
