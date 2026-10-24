import { OTHER_COLOR, deviceColor, slotColor, type ChartPoint, type ChartSeries, type Overview } from "../../shared";
import type { MeterSegment } from "../types";

/** Stacked series for the "when" chart: coloured devices bottom-up, then Other. */
export function usageSeries(overview: Overview): { series: ChartSeries[]; points: ChartPoint[] } {
  const series: ChartSeries[] = overview.series.top.map((d) => ({
    key: String(d.id),
    label: d.name,
    color: deviceColor(d.id, overview.color_order),
  }));
  if (overview.series.points.some((p) => (p.by_device.other ?? 0) > 0)) {
    series.push({ key: "other", label: "Other devices", color: OTHER_COLOR });
  }
  return {
    series,
    points: overview.series.points.map((p) => ({ t: p.t, values: p.by_device })),
  };
}

/** One segment per coloured device, the rest folded into "Other". */
export function meterSegments(overview: Overview): MeterSegment[] {
  const coloured: MeterSegment[] = [];
  let other = 0;
  for (const device of overview.devices) {
    const bytes = device.rx + device.tx;
    if (bytes <= 0) continue;
    if (overview.color_order.includes(device.id)) {
      coloured.push({
        key: String(device.id),
        label: device.name,
        bytes,
        color: deviceColor(device.id, overview.color_order),
        deviceId: device.id,
      });
    } else {
      other += bytes;
    }
  }
  coloured.sort((a, b) => overview.color_order.indexOf(a.deviceId ?? -1) - overview.color_order.indexOf(b.deviceId ?? -1));
  if (other > 0) coloured.push({ key: "other", label: "Other devices", bytes: other, color: OTHER_COLOR, deviceId: null });
  return coloured;
}

export const UNCOUNTED_COLOR =
  "repeating-linear-gradient(135deg, rgb(var(--muted) / 0.6) 0 3px, rgb(var(--muted) / 0.15) 3px 6px)";

/**
 * Scales Tattle's device segments to the provider's figure for the cycle: when
 * the provider charged more than Tattle counted (another line, or traffic around
 * Tattle) the rest becomes a "not counted by Tattle" segment; when Tattle counted
 * more (days on another line fall in this cycle) the devices shrink to fit.
 */
export function withUncounted(segments: MeterSegment[], providerUsed: number | null): MeterSegment[] {
  if (providerUsed === null) return segments;
  const counted = segments.reduce((sum, s) => sum + s.bytes, 0);
  if (counted > providerUsed) {
    const scale = counted > 0 ? providerUsed / counted : 0;
    return segments.map((s) => ({ ...s, bytes: s.bytes * scale }));
  }
  const rest = providerUsed - counted;
  if (rest <= 0) return segments;
  return [...segments, { key: "uncounted", label: "Not counted by Tattle", bytes: rest, color: UNCOUNTED_COLOR, deviceId: null }];
}

/** Quota-only setups: one segment, what the provider says is used. */
export function usedOnly(providerUsed: number | null): MeterSegment[] {
  if (!providerUsed) return [];
  return [{ key: "used", label: "Used", bytes: providerUsed, color: slotColor(0), deviceId: null }];
}
