/** "2026-10-25" as a local date, with no time-zone shift. */
export function isoDate(day: string): Date {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date);
}

const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });
const shortDayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const whenFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });

/** "2026-10-25" -> "Sun, Oct 25". */
export function formatIsoDay(day: string, short = false): string {
  return (short ? shortDayFmt : dayFmt).format(isoDate(day));
}

/** Whole days from today until `day` (negative once it has passed). */
export function daysUntil(day: string, now = new Date()): number {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((isoDate(day).getTime() - today.getTime()) / 86_400_000);
}

export function inDaysText(days: number): string {
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  return days > 0 ? `in ${days} days` : `${-days} days ago`;
}

/** How long something lasts: "9 hours", "1.3 days", "12 days". */
export function lastsText(days: number): string {
  if (days < 1) {
    const hours = Math.max(1, Math.round(days * 24));
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  return days < 10 ? `${days.toFixed(1)} days` : `${Math.round(days)} days`;
}

export function formatWhen(ts: number): string {
  return whenFmt.format(new Date(ts * 1000));
}

/** Amounts in the provider's currency; a plain number when it doesn't say which. */
export function formatMoney(value: number, currency: string | null): string {
  if (!currency) return value.toFixed(2);
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}

/** Line speeds the way lines are sold: "30.7 Mbps". */
export function formatKbps(kbps: number | null): string {
  return kbps == null ? "—" : `${(kbps / 1000).toFixed(1)} Mbps`;
}
