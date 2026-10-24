from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from tattle import periods
from tattle.collector import counter_delta

CAIRO = ZoneInfo("Africa/Cairo")


def test_counter_delta_handles_wrap_and_restart():
    assert counter_delta(100, 400, 2**32) == 300
    assert counter_delta(2**32 - 50, 25, 2**32) == 75
    assert counter_delta(1_000, 20, None) == 20  # restarted, counted from zero
    assert counter_delta(1_000, 900, 2**32) == 900  # a small drop is a restart, not a wrap


def test_cycle_start_before_and_after_renewal_day():
    assert periods.cycle_start(date(2026, 10, 11), 15) == date(2026, 9, 15)
    assert periods.cycle_start(date(2026, 10, 15), 15) == date(2026, 10, 15)
    assert periods.cycle_start(date(2026, 1, 3), 20) == date(2025, 12, 20)
    assert periods.next_cycle_start(date(2026, 12, 20), 20) == date(2027, 1, 20)


def test_today_starts_at_local_midnight():
    now = datetime(2026, 10, 11, 17, 30, tzinfo=CAIRO).timestamp()
    period = periods.resolve("today", CAIRO, now=now)
    assert period.start == datetime(2026, 10, 11, tzinfo=CAIRO).timestamp()
    assert period.bucket == "hour"


def test_week_uses_day_buckets_across_dst_change():
    # Egypt leaves summer time on the last Thursday of October (2026-10-29).
    now = datetime(2026, 11, 2, 12, 0, tzinfo=CAIRO).timestamp()
    period = periods.resolve("7d", CAIRO, now=now)
    starts = periods.bucket_starts(period, CAIRO)
    assert len(starts) == 7
    assert [datetime.fromtimestamp(s, CAIRO).hour for s in starts] == [0] * 7


def test_custom_range_is_inclusive():
    now = datetime(2026, 10, 11, 12, tzinfo=CAIRO).timestamp()
    period = periods.resolve("custom", CAIRO, start_date=date(2026, 10, 1), end_date=date(2026, 10, 3), now=now)
    assert period.end - period.start == 3 * 86400
