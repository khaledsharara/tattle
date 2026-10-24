"""Date ranges in the household's local time (the TZ setting, daylight saving included)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

RANGE_KEYS = ("today", "yesterday", "7d", "30d", "cycle", "custom")


@dataclass(frozen=True)
class Period:
    key: str
    start: int  # unix seconds, inclusive
    end: int  # unix seconds, exclusive
    label: str

    @property
    def bucket(self) -> str:
        return "hour" if self.end - self.start <= 2 * 86400 else "day"


def local_midnight(day: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, time(0), tzinfo=tz).timestamp())


def cycle_start(today: date, billing_day: int) -> date:
    """Most recent date (<= today) whose day-of-month is the renewal day."""
    if today.day >= billing_day:
        return today.replace(day=billing_day)
    first_of_month = today.replace(day=1)
    prev_month = first_of_month - timedelta(days=1)
    return prev_month.replace(day=billing_day)


def next_cycle_start(start: date, billing_day: int) -> date:
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return date(year, month, billing_day)


def manual_cycle(today: date, billing_day: int) -> tuple[date, date]:
    """The cycle from a renewal day typed in Settings: (first day, next renewal)."""
    start = cycle_start(today, billing_day)
    return start, next_cycle_start(start, billing_day)


def resolve(
    key: str,
    tz: ZoneInfo,
    *,
    cycle: tuple[date, date] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    now: float | None = None,
) -> Period:
    """`cycle` is (first day, renewal day) of the quota cycle, needed for "cycle"."""
    now_dt = datetime.fromtimestamp(now, tz) if now is not None else datetime.now(tz)
    today = now_dt.date()
    now_ts = int(now_dt.timestamp()) + 1
    if key == "today":
        return Period(key, local_midnight(today, tz), now_ts, "Today")
    if key == "yesterday":
        day = today - timedelta(days=1)
        return Period(key, local_midnight(day, tz), local_midnight(today, tz), "Yesterday")
    if key == "7d":
        return Period(key, local_midnight(today - timedelta(days=6), tz), now_ts, "Last 7 days")
    if key == "30d":
        return Period(key, local_midnight(today - timedelta(days=29), tz), now_ts, "Last 30 days")
    if key == "cycle":
        if cycle is None:
            raise ValueError("cycle range needs the quota cycle")
        start = cycle[0]
        return Period(key, local_midnight(start, tz), now_ts, f"Since {start:%b} {start.day}")
    if key == "custom":
        if start_date is None or end_date is None or end_date < start_date:
            raise ValueError("custom range needs from <= to")
        end_excl = min(local_midnight(end_date + timedelta(days=1), tz), now_ts)
        label = f"{start_date:%b} {start_date.day} – {end_date:%b} {end_date.day}"
        return Period(key, local_midnight(start_date, tz), end_excl, label)
    raise ValueError(f"unknown range {key!r}")


def bucket_starts(period: Period, tz: ZoneInfo) -> list[int]:
    """Start of every hour/day bucket inside the period."""
    if period.bucket == "hour":
        first = period.start // 3600 * 3600
        return list(range(first, period.end, 3600))
    day = datetime.fromtimestamp(period.start, tz).date()
    starts = []
    while (ts := local_midnight(day, tz)) < period.end:
        starts.append(ts)
        day += timedelta(days=1)
    return starts


def bucket_of(hour: int, period: Period, tz: ZoneInfo) -> int:
    if period.bucket == "hour":
        return hour
    return local_midnight(datetime.fromtimestamp(hour, tz).date(), tz)
