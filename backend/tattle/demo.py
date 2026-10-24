"""Made-up data for trying Tattle without changing anything on the network,
and for the screenshots in the README:

    docker run --rm -p 8090:8090 -e TATTLE_DEMO=1 ghcr.io/khaledsharara/tattle

A household of ten devices over 45 days, two lines on a "Demo ISP" account
(the house switched lines 12 days ago), a phone whose private Wi-Fi addresses
Tattle linked on its own, and a few bills. Every name, address and figure is
invented. The data moves with the clock, so "today" always has data.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import time

from . import db, isp, providers
from .collector import SERVER_KEY
from .config import Settings
from .providers import Bundle, Field, Line, Provider, ProviderError, Usage

GB = 1000**3
MB = 1000**2
DAYS = 45
SEEDED_SETTING = "demo_seeded"
LINE_A, LINE_B = "HOME-4821", "HOME-4822"


class DemoIsp(Provider):
    """Listed only in the demo; signing in to it adds nothing."""

    id = "demo"
    name = "Demo ISP"
    country = "ZZ"
    status = "stable"
    unit_base = 1000
    currency = "USD"
    fields = (Field("account", "Account", placeholder="demo@example.com"),)
    notes = "Made-up figures for the demo."
    has_daily = True
    has_billing = True

    def start_sign_in(self, values: dict[str, str]) -> dict:
        raise ProviderError("This is the demo: the Demo ISP is already connected.")

    def lines(self, credentials: dict) -> list[Line]:
        return []

    def usage(self, credentials: dict, line: Line) -> Usage:
        return Usage([])


# name, kind (os hint), model, MACs (the first is shown), daily GB, when it is used
DEVICES = [
    ("Living room TV", None, None, ["00:00:5e:00:53:10"], 4.6, "evening"),
    ("Maya's iPhone", "iPhone or iPad", "iPhone15,2", ["0a:00:5e:00:53:11", "1e:00:5e:00:53:12"], 1.5, "allday"),
    ("Omar's Pixel", "Android", None, ["16:00:5e:00:53:13"], 1.1, "allday"),
    ("Work laptop", "Windows", None, ["00:00:5e:00:53:14"], 1.9, "office"),
    ("PlayStation 5", None, None, ["00:00:5e:00:53:15"], 2.4, "late"),
    ("Kids' iPad", "iPhone or iPad", "iPad13,1", ["0a:00:5e:00:53:16"], 1.0, "afternoon"),
    ("Dad's MacBook", "Mac", "Mac14,2", ["00:00:5e:00:53:17"], 0.8, "evening"),
    ("Kitchen speaker", None, None, ["00:00:5e:00:53:18"], 0.15, "allday"),
    ("Doorbell camera", None, None, ["00:00:5e:00:53:19"], 0.4, "allday"),
    ("Printer", None, None, ["00:00:5e:00:53:1a"], 0.02, "office"),
]
SHAPES = {
    # share of the day's use in each hour (normalised below)
    "evening": [0.2, 0.1, 0, 0, 0, 0, 0, 0, 0.1, 0.1, 0.1, 0.2, 0.3, 0.3, 0.3, 0.4, 0.6, 0.9, 1.4, 2, 2.4, 2.2, 1.5, 0.6],
    "allday": [0.3, 0.1, 0.1, 0.1, 0.1, 0.1, 0.3, 0.8, 1, 0.9, 0.8, 0.8, 1, 0.9, 0.8, 0.9, 1, 1.1, 1.3, 1.4, 1.5, 1.4, 1.1, 0.6],
    "office": [0, 0, 0, 0, 0, 0, 0, 0.2, 1.2, 1.6, 1.7, 1.6, 1.1, 1.5, 1.6, 1.5, 1.2, 0.6, 0.2, 0.1, 0.1, 0.1, 0, 0],
    "late": [1.2, 0.8, 0.3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.1, 0.2, 0.5, 0.8, 1.2, 1.6, 2, 2.2, 2, 1.6],
    "afternoon": [0, 0, 0, 0, 0, 0, 0, 0.2, 0.2, 0.1, 0.1, 0.1, 0.2, 0.6, 1.4, 1.8, 1.9, 1.6, 1.4, 1.2, 0.6, 0.2, 0, 0],
}


def live_rates(settings: Settings) -> dict[int, tuple[float, float]]:
    """A few devices busy "right now" (bytes per second down, up)."""
    conn = db.connect(settings.db_path)
    try:
        rates = {"Living room TV": (2_400_000.0, 60_000.0), "Maya's iPhone": (310_000.0, 42_000.0), "Work laptop": (95_000.0, 30_000.0)}
        ids = {row["custom_name"]: row["id"] for row in conn.execute("SELECT id, custom_name FROM devices WHERE custom_name IS NOT NULL")}
        return {ids[name]: rate for name, rate in rates.items() if name in ids}
    finally:
        conn.close()


def seed(settings: Settings) -> None:
    """Fill an empty database with the demo household (once)."""
    providers.registry()[DemoIsp.id] = DemoIsp
    conn = db.connect(settings.db_path)
    try:
        if db.get_setting(conn, SEEDED_SETTING):
            return
        with db.transaction(conn):
            _seed(conn, settings)
            db.set_setting(conn, SEEDED_SETTING, "1")
    finally:
        conn.close()


def _seed(conn, settings: Settings) -> None:
    rng = random.Random(7)
    tz = settings.timezone
    now = time.time()
    this_hour = int(now // 3600 * 3600)
    today = dt.datetime.fromtimestamp(now, tz).date()
    start_day = today - dt.timedelta(days=DAYS - 1)
    first = int(dt.datetime(start_day.year, start_day.month, start_day.day, tzinfo=tz).timestamp())

    server_id = conn.execute(
        "INSERT INTO devices(key, hostname, last_ip, first_seen, last_seen) VALUES(?, 'tattle', ?, ?, ?)",
        (SERVER_KEY, settings.server_ip, first, int(now)),
    ).lastrowid

    daily_totals: dict[str, int] = {}
    for index, (name, kind, model, macs, per_day, shape) in enumerate(DEVICES):
        ip = f"192.168.1.{40 + index}"
        busy_now = name in ("Living room TV", "Maya's iPhone", "Work laptop")
        last_active = int(now) - (20 if busy_now else rng.randint(9, 240) * 60)
        ids = []
        for n, mac in enumerate(macs):
            device_id = conn.execute(
                "INSERT INTO devices(key, mac, hostname, custom_name, last_ip, first_seen, last_seen, last_active, os_hint, model) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"mac:{mac}", mac, None, name if n == 0 else None, ip, first + n * 86400 * 9, int(now), last_active, kind, model),
            ).lastrowid
            ids.append(device_id)
        if len(ids) > 1:
            reason = "Same DHCP request shape, never online together, 4 clean hand-overs between Wi-Fi networks"
            for other in ids[1:]:
                conn.execute(
                    "UPDATE devices SET merged_into = ?, merge_reason = ?, merged_at = ? WHERE id = ?",
                    (ids[0], reason, int(now) - 6 * 86400, other),
                )
                conn.execute(
                    "INSERT INTO identity_events(ts, kind, device_id, other_id, reason) VALUES(?, 'merged', ?, ?, ?)",
                    (int(now) - 6 * 86400, other, ids[0], reason),
                )
        weights = SHAPES[shape]
        total_weight = sum(weights)
        rows = []
        for hour_start in range(first, this_hour + 1, 3600):
            local = dt.datetime.fromtimestamp(hour_start, tz)
            day_factor = 1.25 if local.weekday() >= 4 else 1.0  # busier weekends
            noise = rng.uniform(0.55, 1.45)
            spike = 6.0 if name == "PlayStation 5" and local.day % 9 == 0 and local.hour in (20, 21) else 1.0
            volume = per_day * GB * weights[local.hour] / total_weight * day_factor * noise * spike
            if volume < MB:
                continue
            member = ids[(local.day // 3) % len(ids)]  # the phone moves between its addresses
            upload_share = 0.04 if shape in ("evening", "late") else 0.12
            rx, tx = int(volume * (1 - upload_share)), int(volume * upload_share)
            rows.append((member, hour_start, rx, tx))
            key = local.date().isoformat()
            daily_totals[key] = daily_totals.get(key, 0) + rx + tx
        conn.executemany("INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(?, ?, ?, ?)", rows)
    conn.executemany(
        "INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(?, ?, ?, ?)",
        [(server_id, h, int(rng.uniform(5, 40) * MB), int(rng.uniform(1, 6) * MB)) for h in range(first, this_hour + 1, 3600)],
    )
    conn.execute(
        "INSERT INTO device_names(device_id, source, value, norm, first_seen, last_seen) "
        "SELECT id, 'mdns', 'Mayas-iPhone', 'mayas-iphone', first_seen, last_seen FROM devices WHERE key = ?",
        (f"mac:{DEVICES[1][3][1]}",),
    )
    _seed_isp(conn, rng, tz, now, today, daily_totals)


def _seed_isp(conn, rng: random.Random, tz, now: float, today: dt.date, counted_by_day: dict[str, int]) -> None:
    account = conn.execute(
        "INSERT INTO provider_accounts(provider, identifier, credentials, status, created, updated) "
        "VALUES('demo', 'demo@example.com', '{}', 'ok', ?, ?)",
        (int(now) - DAYS * 86400, int(now)),
    ).lastrowid
    switched = today - dt.timedelta(days=27)  # the house moved from line B to line A
    renews_a = today + dt.timedelta(days=9)
    renews_b = today + dt.timedelta(days=21)
    for number, plan in ((LINE_A, "Home 500 GB"), (LINE_B, "Home 500 GB")):
        conn.execute(
            "INSERT INTO isp_lines(number, login_id, provider, plan, account_number, meta, read_at) VALUES(?, ?, 'demo', ?, ?, '{}', ?)",
            (number, account, plan, "ACC-0001", int(now) - 240),
        )
    # The provider charges a little more than Tattle counts (line overhead).
    daily: dict[str, dict[str, int]] = {LINE_A: {}, LINE_B: {}}
    for key, counted in counted_by_day.items():
        line = LINE_A if dt.date.fromisoformat(key) >= switched else LINE_B
        daily[line][key] = int(counted * rng.uniform(1.02, 1.05))
    for line, days in daily.items():
        conn.executemany("INSERT INTO isp_daily(number, day, bytes) VALUES(?, ?, ?)", [(line, d, b) for d, b in days.items()])

    def used_since(line: str, since: dt.date) -> int:
        return sum(b for d, b in daily[line].items() if dt.date.fromisoformat(d) >= since)

    plan = 500 * GB
    cycle_a = renews_a - dt.timedelta(days=30)
    used_a = min(plan, used_since(LINE_A, cycle_a))
    left_b = 64 * GB
    readings = []
    for back in range(48, -1, -1):  # 12 hours of readings every 15 minutes
        ts = int(now) - back * 900
        share = back * 900 / 86400 * used_a / 30
        readings.append((LINE_A, ts, plan, int(plan - used_a + share), renews_a, cycle_a, 1, 0, plan - used_a + share))
        readings.append((LINE_B, ts, plan, left_b, renews_b, renews_b - dt.timedelta(days=30), 0, 0, left_b))
    counted_a = 0  # Tattle's running total: about 97% of what the provider charges
    previous_left: float | None = None
    for number, ts, total, remaining, renews, cycle_start, live, counted, left in readings:
        if number == LINE_A:
            if previous_left is not None:
                counted_a += int((previous_left - left) * 0.97)
            previous_left = left
            counted = counted_a
        bundles = [Bundle("plan", "Home 500 GB", total, max(0, int(left)), renews, label="Internet")]
        if number == LINE_A:
            bundles.append(Bundle("extra", "Weekend booster", 20 * GB, 7 * GB, None, label="Extra"))
            remaining += 7 * GB
        conn.execute(
            "INSERT INTO isp_readings(number, ts, total, remaining, renews, cycle_start, live, counted, items) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                number,
                ts,
                total + (20 * GB if number == LINE_A else 0),
                remaining,
                renews.isoformat(),
                cycle_start.isoformat(),
                live,
                counted,
                json.dumps([b.to_json() for b in bundles]),
            ),
        )
    bills = {
        "open_amount": 49.0,
        "unpaid": [{"due": (today + dt.timedelta(days=6)).isoformat(), "value": 49.0}],
        "bills": [
            {
                "from": (today - dt.timedelta(days=30 * (n + 1))).isoformat(),
                "to": (today - dt.timedelta(days=30 * n + 1)).isoformat(),
                "issued": (today - dt.timedelta(days=30 * n)).isoformat(),
                "due": (today - dt.timedelta(days=30 * n - 6)).isoformat(),
                "value": round(49.0 + math.sin(n) * 3, 2),
                "lines": 2,
            }
            for n in range(4)
        ],
    }
    conn.execute(
        "UPDATE isp_lines SET billing = ?, billing_at = ? WHERE number = ?", (json.dumps(bills), int(now), LINE_A)
    )
    isp.set_live_line(conn, LINE_A, "usage", int(dt.datetime(switched.year, switched.month, switched.day, tzinfo=tz).timestamp()))
    conn.execute("DELETE FROM line_switches")
    conn.execute(
        "INSERT INTO line_switches(ts, number, source) VALUES(?, ?, 'usage')",
        (int(dt.datetime(switched.year, switched.month, switched.day, 9, tzinfo=tz).timestamp()), LINE_A),
    )
    db.set_setting(conn, isp.ROUTER_LINK_SETTING, json.dumps({"state": "up", "down_kbps": 98_000, "up_kbps": 19_500, "ts": int(now)}))
