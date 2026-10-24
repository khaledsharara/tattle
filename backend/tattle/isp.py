"""Official usage figures from the internet provider.

The provider keeps the counter the quota is charged against. Tattle signs in
the way the provider's own website or app does (one plugin per provider, see
providers/), reads every home line on the account every 15 minutes, and shows
the provider's figures next to what Tattle counted.

Credentials never leave the server. When a house has more than one line (two
numbers on one connection, switched by changing the router's internet login),
Tattle works out which one is in use: from the router's login when a router
adapter can read it, otherwise from which line's usage is moving.

    python -m tattle.isp read     # read every signed-in account now
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

from . import db, providers
from .providers import Challenge, Line, Provider, ProviderError, SessionExpired, Usage

log = logging.getLogger("tattle.isp")

READ_INTERVAL_SECONDS = 900
STARTUP_DELAY_SECONDS = 30
SIGN_IN_TIMEOUT_SECONDS = 900  # how long a half-finished sign-in (waiting for a code) is kept
# Providers that keep a daily history only keep about a month of it; Tattle keeps
# every day it has read, so its record grows past that.
HISTORY_DAYS = 45
SHOWN_DAYS = 14
BILLING_INTERVAL_SECONDS = 6 * 3600  # bills change monthly
PACE_DAYS = 7  # "at this pace" = the house's average over the last full week
# A line whose usage grew by more than this between two readings, while the
# others stayed put, is the one the house is using.
IN_USE_SIGNAL_BYTES = 50 * 1000**2
ROUTER_LOGIN_SETTING = "router_internet_login"
ROUTER_LINK_SETTING = "router_link"
LIVE_LINE_SETTING = "live_line"
LIVE_SOURCE_SETTING = "live_line_source"

T = TypeVar("T")


# --- dates ---------------------------------------------------------------------------
def renewal_date(text: str | None) -> dt.date | None:
    """ISO dates; older e& readings were stored the way e& writes them, 25/10/2026."""
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text or "", pattern).date()
        except ValueError:
            continue
    return None


def _month_before(day: dt.date) -> dt.date:
    year, month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    for candidate in (day.day, 30, 29, 28):
        try:
            return dt.date(year, month, candidate)
        except ValueError:
            continue
    raise ValueError(day)


def _month_after(day: dt.date, anchor: int) -> dt.date:
    """The same day of the next month, or its last day when the month is shorter."""
    year, month = (day.year, day.month + 1) if day.month < 12 else (day.year + 1, 1)
    for candidate in (anchor, 30, 29, 28):
        try:
            return dt.date(year, month, candidate)
        except ValueError:
            continue
    raise ValueError(day)


# --- which line is in use --------------------------------------------------------------
def live_line(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """(line, how Tattle knows): the router's internet login names it, or the
    line whose usage is moving, or the only line there is."""
    rows = conn.execute("SELECT number, provider FROM isp_lines ORDER BY number").fetchall()
    numbers = [row["number"] for row in rows]
    login = db.get_setting(conn, ROUTER_LOGIN_SETTING)
    if login:
        by_provider: dict[str, list[str]] = {}
        for row in rows:
            by_provider.setdefault(row["provider"], []).append(row["number"])
        for provider_id, ids in by_provider.items():
            try:
                match = providers.get(provider_id).match_wan_login(login, ids)
            except ProviderError:
                continue
            if match:
                return match, "router"
    stored = db.get_setting(conn, LIVE_LINE_SETTING)
    if stored in numbers:
        return stored, db.get_setting(conn, LIVE_SOURCE_SETTING) or "usage"
    if len(numbers) == 1:
        return numbers[0], "only"
    return None, None


def set_live_line(conn: sqlite3.Connection, number: str | None, source: str, now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    if db.get_setting(conn, LIVE_LINE_SETTING) == (number or "") and db.get_setting(conn, LIVE_SOURCE_SETTING) == source:
        return
    db.set_setting(conn, LIVE_LINE_SETTING, number or "")
    db.set_setting(conn, LIVE_SOURCE_SETTING, source)
    record_switch(conn, number, source, now)


def record_switch(conn: sqlite3.Connection, number: str | None, source: str, now: int | None = None) -> None:
    last = conn.execute("SELECT number FROM line_switches ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
    if last is not None and last["number"] == number:
        return
    conn.execute(
        "INSERT INTO line_switches(ts, number, source) VALUES(?, ?, ?)",
        (int(time.time()) if now is None else now, number, source),
    )


def infer_live_line(conn: sqlite3.Connection, now: int) -> None:
    """With no router to ask, the line in use is the one whose usage just grew
    while every other line stayed put."""
    if db.get_setting(conn, ROUTER_LOGIN_SETTING):
        return  # the router's login decides
    growing = []
    for row in conn.execute("SELECT number FROM isp_lines").fetchall():
        latest = conn.execute(
            "SELECT ts, remaining FROM isp_readings WHERE number = ? ORDER BY ts DESC LIMIT 2", (row["number"],)
        ).fetchall()
        if len(latest) < 2 or None in (latest[0]["remaining"], latest[1]["remaining"]):
            continue
        if latest[0]["ts"] - latest[1]["ts"] > 4 * READ_INTERVAL_SECONDS:
            continue  # too far apart to tell when the use happened
        if latest[1]["remaining"] - latest[0]["remaining"] > IN_USE_SIGNAL_BYTES:
            growing.append(row["number"])
    if len(growing) == 1:
        set_live_line(conn, growing[0], "usage", now)


# --- keeping the figures fresh ------------------------------------------------------------
@dataclass
class PendingSignIn:
    provider: str
    values: dict[str, str]
    challenge: Challenge
    created: float


class IspService:
    """Every conversation with providers, one at a time: refreshing a token can
    retire the previous one, so two readers at once could sign each other out."""

    def __init__(self, db_path: str, instances: dict[str, Provider] | None = None):
        self.db_path = db_path
        self.instances: dict[str, Provider] = dict(instances or {})
        self.lock = threading.Lock()
        self.pending: dict[str, PendingSignIn] = {}

    def provider(self, provider_id: str) -> Provider:
        if provider_id not in self.instances:
            self.instances[provider_id] = providers.get(provider_id)()
        return self.instances[provider_id]

    # --- signing in ---------------------------------------------------------------
    def start_sign_in(self, provider_id: str, values: dict[str, str]) -> dict:
        """{"done": True} once signed in and read, or {"pending": id, "challenge": {...}}."""
        provider = self.provider(provider_id)
        values = {key: str(value).strip() for key, value in values.items() if value is not None}
        for field in provider.fields:
            if not field.optional and not values.get(field.name):
                raise ValueError(f"Fill in “{field.label}”")
        with self.lock:
            return self._continue(provider_id, provider, values, provider.start_sign_in(values))

    def answer(self, pending_id: str, answer: str) -> dict:
        with self.lock:
            cutoff = time.time() - SIGN_IN_TIMEOUT_SECONDS
            self.pending = {key: value for key, value in self.pending.items() if value.created >= cutoff}
            pending = self.pending.pop(pending_id, None)
            if pending is None:
                raise ValueError("That sign-in took too long. Start again.")
            provider = self.provider(pending.provider)
            try:
                result = provider.answer(pending.values, pending.challenge, answer.strip())
            except ProviderError:
                self.pending[pending_id] = pending  # a mistyped code can be typed again
                raise
            return self._continue(pending.provider, provider, pending.values, result)

    def _continue(self, provider_id: str, provider: Provider, values: dict[str, str], result: dict | Challenge) -> dict:
        if isinstance(result, Challenge):
            pending_id = secrets.token_urlsafe(16)
            self.pending[pending_id] = PendingSignIn(provider_id, values, result, time.time())
            return {
                "pending": pending_id,
                "challenge": {"kind": result.kind, "prompt": result.prompt, "image": result.image, "url": result.url},
            }
        identifier = provider.account_name(values)
        conn = db.connect(self.db_path)
        try:
            now = int(time.time())
            conn.execute(
                "INSERT INTO provider_accounts(provider, identifier, credentials, status, error, created, updated) "
                "VALUES(?, ?, ?, 'ok', NULL, ?, ?) ON CONFLICT(provider, identifier) DO UPDATE SET "
                "credentials = excluded.credentials, status = 'ok', error = NULL, updated = excluded.updated",
                (provider_id, identifier, json.dumps(result), now, now),
            )
            account_id = conn.execute(
                "SELECT id FROM provider_accounts WHERE provider = ? AND identifier = ?", (provider_id, identifier)
            ).fetchone()["id"]
            self._read_account(conn, account_id)
            infer_live_line(conn, now)
        finally:
            conn.close()
        return {"done": True}

    # --- reading -------------------------------------------------------------------
    def read_all(self) -> None:
        with self.lock:
            conn = db.connect(self.db_path)
            try:
                for row in conn.execute("SELECT id FROM provider_accounts WHERE status != 'expired'").fetchall():
                    self._read_account(conn, row["id"])
                infer_live_line(conn, int(time.time()))
            finally:
                conn.close()

    def _read_account(self, conn: sqlite3.Connection, account_id: int) -> None:
        row = conn.execute("SELECT * FROM provider_accounts WHERE id = ?", (account_id,)).fetchone()
        now = int(time.time())
        credentials = json.loads(row["credentials"])
        before = json.dumps(credentials, sort_keys=True)
        try:
            provider = self.provider(row["provider"])
            lines = self._call(conn, account_id, provider, credentials, provider.lines)
            for line in lines:
                conn.execute(
                    "INSERT INTO isp_lines(number, login_id, provider, plan, account_number, meta) VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(number) DO UPDATE SET login_id = excluded.login_id, provider = excluded.provider, "
                    "plan = COALESCE(excluded.plan, isp_lines.plan), "
                    "account_number = COALESCE(excluded.account_number, isp_lines.account_number), meta = excluded.meta",
                    (line.id, account_id, row["provider"], line.plan, line.account, json.dumps(line.meta)),
                )
                try:
                    usage = self._call(conn, account_id, provider, credentials, lambda c, line=line: provider.usage(c, line))
                except ProviderError as exc:
                    if exc.signed_out:
                        raise
                    conn.execute("UPDATE isp_lines SET error = ? WHERE number = ?", (str(exc), line.id))
                    continue
                self._store(conn, line.id, usage, now)
                if provider.has_daily:
                    self._store_history(conn, account_id, provider, credentials, line)
                if provider.has_billing:
                    self._store_bills(conn, account_id, provider, credentials, line, now)
            conn.execute("UPDATE provider_accounts SET status = 'ok', error = NULL, updated = ? WHERE id = ?", (now, account_id))
        except ProviderError as exc:
            log.warning("reading %s for %s failed: %s", row["provider"], row["identifier"], exc)
            conn.execute(
                "UPDATE provider_accounts SET status = ?, error = ?, updated = ? WHERE id = ?",
                ("expired" if exc.signed_out else "error", str(exc), now, account_id),
            )
        finally:
            if json.dumps(credentials, sort_keys=True) != before:
                self._save_credentials(conn, account_id, credentials)

    def _call(
        self, conn: sqlite3.Connection, account_id: int, provider: Provider, credentials: dict, fetch: Callable[[dict], T]
    ) -> T:
        try:
            return fetch(credentials)
        except SessionExpired:
            fresh = provider.refresh(dict(credentials))
            credentials.clear()
            credentials.update(fresh)
            # Saved at once: the old refresh token may already be retired.
            self._save_credentials(conn, account_id, credentials)
            return fetch(credentials)

    @staticmethod
    def _save_credentials(conn: sqlite3.Connection, account_id: int, credentials: dict) -> None:
        conn.execute("UPDATE provider_accounts SET credentials = ? WHERE id = ?", (json.dumps(credentials), account_id))

    @staticmethod
    def _store(conn: sqlite3.Connection, number: str, usage: Usage, now: int) -> None:
        live = live_line(conn)[0] == number
        counted = conn.execute(
            "SELECT COALESCE(SUM(value), 0) AS total FROM counter_state WHERE name IN ('attributed_rx', 'attributed_tx')"
        ).fetchone()["total"]
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO isp_readings(number, ts, total, remaining, renews, cycle_start, live, counted, items) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    number,
                    now,
                    usage.total,
                    usage.remaining,
                    usage.renews.isoformat() if usage.renews else None,
                    usage.cycle_start.isoformat() if usage.cycle_start else None,
                    int(live),
                    counted,
                    json.dumps([bundle.to_json() for bundle in usage.bundles]),
                ),
            )
            conn.execute("UPDATE isp_lines SET error = NULL, read_at = ? WHERE number = ?", (now, number))

    def _store_bills(
        self, conn: sqlite3.Connection, account_id: int, provider: Provider, credentials: dict, line: Line, now: int
    ) -> None:
        row = conn.execute("SELECT billing_at FROM isp_lines WHERE number = ?", (line.id,)).fetchone()
        if row["billing_at"] and now - row["billing_at"] < BILLING_INTERVAL_SECONDS:
            return
        try:
            bills = self._call(conn, account_id, provider, credentials, lambda c: provider.billing(c, line))
        except ProviderError as exc:
            if exc.signed_out:
                raise
            log.warning("reading the bills for %s failed: %s", line.id, exc)
            return
        if bills is not None:
            conn.execute("UPDATE isp_lines SET billing = ?, billing_at = ? WHERE number = ?", (json.dumps(bills), now, line.id))

    def _store_history(
        self, conn: sqlite3.Connection, account_id: int, provider: Provider, credentials: dict, line: Line
    ) -> None:
        today = dt.date.today()
        start = today - dt.timedelta(days=HISTORY_DAYS)
        try:
            days = self._call(conn, account_id, provider, credentials, lambda c: provider.daily(c, line, start, today))
        except ProviderError as exc:
            if exc.signed_out:
                raise
            log.warning("reading the daily history for %s failed: %s", line.id, exc)
            return
        with db.transaction(conn):
            conn.executemany(
                "INSERT INTO isp_daily(number, day, bytes) VALUES(?, ?, ?) "
                "ON CONFLICT(number, day) DO UPDATE SET bytes = excluded.bytes",
                [(line.id, day, size) for day, size in days.items()],
            )


class IspPoller(threading.Thread):
    def __init__(self, service: IspService, interval: float = READ_INTERVAL_SECONDS):
        super().__init__(name="isp", daemon=True)
        self.service = service
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        if self.stop_event.wait(STARTUP_DELAY_SECONDS):
            return
        while not self.stop_event.is_set():
            try:
                self.service.read_all()
            except Exception:  # keep trying; the dashboard shows each account's state
                log.exception("reading the provider failed")
            self.stop_event.wait(self.interval)


# --- what the dashboard shows ---------------------------------------------------------------
def _item(item: dict) -> dict:
    """A stored bundle, the same shape whichever version of Tattle stored it."""
    renews = renewal_date(item.get("renews"))
    return {
        "kind": item.get("kind", "plan"),
        "name": item.get("name") or item.get("bundle"),
        "label": item.get("label"),
        "total_bytes": item.get("total_bytes"),
        "remaining_bytes": item.get("remaining_bytes"),
        "renews": renews.isoformat() if renews else None,
        "is_data": bool(item.get("is_data", True)),
    }


def usage_check(readings: list[sqlite3.Row]) -> dict | None:
    """The provider's count next to Tattle's over the longest recent stretch in
    which the line was the one in use and nothing was added to it (a renewal or a
    bundle bought). Based on what is left, so use of extra bundles counts too."""
    if not readings or not readings[-1]["live"] or readings[-1]["remaining"] is None:
        return None
    latest = start = readings[-1]
    for earlier in reversed(readings[:-1]):
        if (
            not earlier["live"]
            or earlier["remaining"] is None
            or earlier["remaining"] < start["remaining"]
            or earlier["counted"] > start["counted"]
        ):
            break
        start = earlier
    if start is latest:
        return None
    return {
        "since": start["ts"],
        "until": latest["ts"],
        "isp": start["remaining"] - latest["remaining"],
        "counted": latest["counted"] - start["counted"],
    }


def _day_bounds(day: dt.date, tz: ZoneInfo) -> tuple[int, int]:
    following = day + dt.timedelta(days=1)
    return (
        int(dt.datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()),
        int(dt.datetime(following.year, following.month, following.day, tzinfo=tz).timestamp()),
    )


def daily_comparison(conn: sqlite3.Connection, tz: ZoneInfo, now: float) -> list[dict]:
    """Each recent day: what the provider charged across all lines (the house
    may switch lines; Tattle counts the whole house) next to what Tattle counted."""
    charged: dict[str, dict[str, int]] = {}
    for row in conn.execute("SELECT number, day, bytes FROM isp_daily"):
        charged.setdefault(row["day"], {})[row["number"]] = row["bytes"]
    first_hour = conn.execute("SELECT MIN(hour) AS first FROM usage_hourly").fetchone()["first"]
    today = dt.datetime.fromtimestamp(now, tz).date()
    days = []
    for back in range(SHOWN_DAYS):
        day = today - dt.timedelta(days=back)
        start, end = _day_bounds(day, tz)
        counted = None
        if first_hour is not None and first_hour < end:
            counted = conn.execute(
                "SELECT COALESCE(SUM(rx + tx), 0) AS total FROM usage_hourly WHERE hour >= ? AND hour < ?", (start, end)
            ).fetchone()["total"]
        by_line = charged.get(day.isoformat(), {})
        if not by_line and counted is None:
            continue
        days.append(
            {
                "day": day.isoformat(),
                "start": start,
                "isp": sum(by_line.values()) if by_line else None,
                "by_line": by_line,
                "counted": counted,
                # Tattle counted the whole day: not the day it started, and the day is over.
                "complete": counted is not None and first_hour <= start and back > 0,
            }
        )
    return days


def house_pace(conn: sqlite3.Connection, today: dt.date) -> int | None:
    """What the house used per day over the last full week, all lines added up
    (whichever line is in use, the house's use moves with it)."""
    start = today - dt.timedelta(days=PACE_DAYS)
    rows = conn.execute(
        "SELECT day, SUM(bytes) AS total FROM isp_daily WHERE day >= ? AND day < ? GROUP BY day",
        (start.isoformat(), today.isoformat()),
    ).fetchall()
    if len(rows) >= 3:
        return int(sum(row["total"] for row in rows) / len(rows))
    # Providers without a daily history: what was left a week ago against now.
    return _pace_from_readings(conn, today)


def _pace_from_readings(conn: sqlite3.Connection, today: dt.date) -> int | None:
    since = int(time.time()) - PACE_DAYS * 86400
    used, days = 0, 0.0
    for row in conn.execute("SELECT number FROM isp_lines").fetchall():
        readings = conn.execute(
            "SELECT ts, remaining FROM isp_readings WHERE number = ? AND ts >= ? AND remaining IS NOT NULL ORDER BY ts",
            (row["number"], since),
        ).fetchall()
        for earlier, later in zip(readings, readings[1:]):
            if later["remaining"] <= earlier["remaining"]:  # skip renewals and bundles bought
                used += earlier["remaining"] - later["remaining"]
        if len(readings) >= 2:
            days = max(days, (readings[-1]["ts"] - readings[0]["ts"]) / 86400)
    return int(used / days) if days >= 2 else None


def _provider_info(provider_id: str) -> dict:
    try:
        cls = providers.get(provider_id)
    except ProviderError:
        return {"id": provider_id, "name": provider_id, "status": "beta", "unit_base": 1000, "currency": None, "docs": ""}
    return {
        "id": cls.id,
        "name": cls.name,
        "status": cls.status,
        "unit_base": cls.unit_base,
        "currency": cls.currency,
        "docs": cls.docs,
    }


def _in_use_since(conn: sqlite3.Connection, number: str, used_days: list[dt.date], tz: ZoneInfo) -> dt.date | None:
    switch = conn.execute(
        "SELECT ts FROM line_switches WHERE number = ? ORDER BY ts DESC LIMIT 1", (number,)
    ).fetchone()
    if used_days:
        since = used_days[-1]
        days = set(used_days)
        while since - dt.timedelta(days=1) in days:
            since -= dt.timedelta(days=1)
        if switch is None or dt.datetime.fromtimestamp(switch["ts"], tz).date() <= since:
            return since
    return dt.datetime.fromtimestamp(switch["ts"], tz).date() if switch else None


def _line_summary(
    conn: sqlite3.Connection,
    line: sqlite3.Row,
    account: sqlite3.Row,
    live_number: str | None,
    pace: int | None,
    now: float,
    tz: ZoneInfo,
    counting: bool,
) -> dict:
    readings = conn.execute(
        "SELECT * FROM isp_readings WHERE number = ? AND ts >= ? ORDER BY ts", (line["number"], int(now) - 40 * 86400)
    ).fetchall()
    latest = readings[-1] if readings else None
    live = live_number == line["number"]
    used_days = [
        dt.date.fromisoformat(row["day"])
        for row in conn.execute("SELECT day FROM isp_daily WHERE number = ? AND bytes > 0 ORDER BY day", (line["number"],))
    ]
    in_use_since = _in_use_since(conn, line["number"], used_days, tz) if live else None
    remaining = latest["remaining"] if latest else None
    days_left = remaining / pace if pace and remaining is not None else None
    renews = renewal_date(latest["renews"]) if latest else None
    return {
        "number": line["number"],
        "plan": line["plan"],
        "provider": _provider_info(line["provider"]),
        "live": live,
        "status": account["status"],
        "error": line["error"] or account["error"],
        "read_at": line["read_at"],
        "login_id": account["id"],
        "renews": renews.isoformat() if renews else None,
        "last_used": used_days[-1].isoformat() if used_days else None,
        "in_use_since": in_use_since.isoformat() if in_use_since else None,
        # At the house's pace of the last week: how long what is left lasts.
        "pace": {
            "per_day": pace,
            "days_left": days_left,
            "runs_out": now + days_left * 86400 if live and days_left is not None and remaining else None,
        },
        "reading": None
        if latest is None
        else {
            "ts": latest["ts"],
            "total": latest["total"],
            "remaining": latest["remaining"],
            "renews": renews.isoformat() if renews else None,
            "items": [_item(item) for item in json.loads(latest["items"])],
        },
        "check": usage_check(readings) if counting else None,
    }


def _lines(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], dict, list[tuple]]:
    accounts = conn.execute(
        "SELECT id, provider, identifier, status, error, updated FROM provider_accounts ORDER BY id"
    ).fetchall()
    by_id = {row["id"]: row for row in accounts}
    pairs = [
        (line, by_id[line["login_id"]])
        for line in conn.execute("SELECT * FROM isp_lines ORDER BY number").fetchall()
        if line["login_id"] in by_id
    ]
    number, source = live_line(conn)
    return (
        accounts,
        {"router_login": db.get_setting(conn, ROUTER_LOGIN_SETTING) or None, "live_number": number, "live_source": source},
        pairs,
    )


def current_cycle(conn: sqlite3.Connection, tz: ZoneInfo, now: float | None = None) -> dict | None:
    """The quota cycle the provider is counting right now, from its own renewal
    dates, so nobody has to type a renewal day. None when no line has one.

    The cycle runs from the line's previous renewal date (the day it last
    renewed, or the start the provider gives) up to the renewal date it gives
    now. Right after a renewal, before a reading shows the new date (providers
    can lag), the cycle is rolled forward a month and marked pending: the
    provider's figures then still belong to the cycle that just ended."""
    now = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(now, tz).date()
    _, live, pairs = _lines(conn)
    numbers = [line["number"] for line, _ in pairs]
    number = live["live_number"]
    if number is None and numbers:
        marks = ",".join("?" * len(numbers))
        row = conn.execute(
            f"SELECT number FROM isp_daily WHERE bytes > 0 AND number IN ({marks}) ORDER BY day DESC LIMIT 1", numbers
        ).fetchone()
        number = row["number"] if row else None
    if number is None:
        return None
    rows = conn.execute(
        "SELECT ts, renews, cycle_start FROM isp_readings WHERE number = ? AND renews IS NOT NULL ORDER BY ts", (number,)
    ).fetchall()
    dates: list[dt.date] = []
    for row in rows:
        day = renewal_date(row["renews"])
        if day and (not dates or day != dates[-1]):
            dates.append(day)
    if not dates:
        return None
    end = dates[-1]
    given_start = renewal_date(rows[-1]["cycle_start"])
    earlier = [day for day in dates[:-1] if day < end]
    start = given_start or (earlier[-1] if earlier else _month_before(end))
    pending = end <= today
    if pending:
        anchor = end.day
        start = end
        while (following := _month_after(start, anchor)) <= today:
            start = following
        end = _month_after(start, anchor)

    latest = conn.execute(
        "SELECT items FROM isp_readings WHERE number = ? AND ts = ?", (number, rows[-1]["ts"])
    ).fetchone()
    data = [item for item in map(_item, json.loads(latest["items"])) if item["is_data"]]
    sized = [item for item in data if item["total_bytes"] is not None and item["remaining_bytes"] is not None]
    plan = sum(item["total_bytes"] for item in sized if item["kind"] == "plan")
    used = sum(max(0, item["total_bytes"] - item["remaining_bytes"]) for item in sized)
    remaining = sum(item["remaining_bytes"] or 0 for item in data)
    line_row = next(line for line, _ in pairs if line["number"] == number) if number in numbers else None
    return {
        "number": number,
        "provider": _provider_info(line_row["provider"]) if line_row else None,
        "start": start,
        "end": end,
        "pending": pending,
        "read_at": rows[-1]["ts"],
        "plan_bytes": plan or None,
        # Everything on the line this cycle: the plan plus extra bundles bought on top.
        "bytes": None if pending else used + remaining,
        "used": None if pending else used,
        "remaining": None if pending else remaining,
    }


def overview(conn: sqlite3.Connection, tz: ZoneInfo, counting: bool = True) -> dict:
    now = time.time()
    accounts, live, pairs = _lines(conn)
    pace = house_pace(conn, dt.datetime.fromtimestamp(now, tz).date())
    return {
        **live,
        "accounts": [
            {
                "id": row["id"],
                "provider": _provider_info(row["provider"]),
                "identifier": row["identifier"],
                "status": row["status"],
                "error": row["error"],
                "updated": row["updated"],
            }
            for row in accounts
        ],
        "lines": [
            _line_summary(conn, line, account, live["live_number"], pace, now, tz, counting) for line, account in pairs
        ],
    }


SPANS = ("7d", "30d", "cycle", "all", "custom")
MAX_SPAN_DAYS = 400


def line_detail(
    conn: sqlite3.Connection,
    tz: ZoneInfo,
    number: str,
    span: str,
    start: dt.date | None,
    end: dt.date | None,
    counting: bool = True,
) -> dict | None:
    """Everything about one line: what is left, its daily use over a chosen span
    (the provider's history, kept by Tattle past the provider's month), bills and
    line speed."""
    now = time.time()
    _, live, pairs = _lines(conn)
    found = next(((line, account) for line, account in pairs if line["number"] == number), None)
    if found is None:
        return None
    line, account = found
    today = dt.datetime.fromtimestamp(now, tz).date()
    summary = _line_summary(conn, line, account, live["live_number"], house_pace(conn, today), now, tz, counting)

    renews = renewal_date(summary["reading"]["renews"]) if summary["reading"] else None
    cycle = None
    if renews:
        given = conn.execute(
            "SELECT cycle_start FROM isp_readings WHERE number = ? ORDER BY ts DESC LIMIT 1", (number,)
        ).fetchone()
        cycle_start = renewal_date(given["cycle_start"]) if given else None
        cycle = {
            "start": (cycle_start or _month_before(renews)).isoformat(),
            "end": (renews - dt.timedelta(days=1)).isoformat(),
        }
    earliest_row = conn.execute("SELECT MIN(day) AS day FROM isp_daily WHERE number = ?", (number,)).fetchone()
    earliest = dt.date.fromisoformat(earliest_row["day"]) if earliest_row["day"] else today

    if span == "custom":
        if not start or not end or start > end:
            raise ValueError("Pick a start date on or before the end date")
        first, last = start, min(end, today)
    elif span == "7d":
        first, last = today - dt.timedelta(days=6), today
    elif span == "cycle" and cycle:
        first, last = dt.date.fromisoformat(cycle["start"]), min(today, dt.date.fromisoformat(cycle["end"]))
    elif span == "all":
        first, last = earliest, today
    else:
        span = "30d"
        first, last = today - dt.timedelta(days=29), today
    first = max(first, last - dt.timedelta(days=MAX_SPAN_DAYS - 1))

    stored = {
        row["day"]: row["bytes"]
        for row in conn.execute(
            "SELECT day, bytes FROM isp_daily WHERE number = ? AND day >= ? AND day <= ?",
            (number, first.isoformat(), last.isoformat()),
        )
    }
    other_lines = {
        row["day"]
        for row in conn.execute(
            "SELECT DISTINCT day FROM isp_daily WHERE number != ? AND bytes > 0 AND day >= ? AND day <= ?",
            (number, first.isoformat(), last.isoformat()),
        )
    }
    has_history = bool(stored) or earliest_row["day"] is not None
    first_hour = conn.execute("SELECT MIN(hour) AS first FROM usage_hourly").fetchone()["first"] if counting else None
    days = []
    day = first
    while day <= last:
        day_start, day_end = _day_bounds(day, tz)
        key = day.isoformat()
        charged = stored.get(key, 0 if has_history and day >= earliest else None)  # history lists only days with use
        # Tattle counts the whole house, so its day only compares with this
        # line's when no other line was used that day and it counted all of it.
        counted = None
        if charged and key not in other_lines and first_hour is not None and first_hour <= day_start and day < today:
            counted = conn.execute(
                "SELECT COALESCE(SUM(rx + tx), 0) AS total FROM usage_hourly WHERE hour >= ? AND hour < ?",
                (day_start, day_end),
            ).fetchone()["total"]
        days.append({"day": key, "start": day_start, "isp": charged, "counted": counted, "shared": key in other_lines})
        day += dt.timedelta(days=1)

    known = [d for d in days if d["isp"] is not None]
    used = [d for d in known if d["isp"]]
    busiest = max(used, key=lambda d: d["isp"]) if used else None
    billing = json.loads(line["billing"]) if line["billing"] else None
    link = None
    if summary["live"]:
        raw = db.get_setting(conn, ROUTER_LINK_SETTING)
        link = json.loads(raw) if raw else None
    return {
        "line": summary,
        "cycle": cycle,
        "has_history": has_history,
        "range": {"span": span, "from": first.isoformat(), "to": last.isoformat(), "earliest": earliest.isoformat()},
        "days": days,
        "totals": {
            "isp": sum(d["isp"] for d in known),
            "days_used": len(used),
            "average": int(sum(d["isp"] for d in used) / len(used)) if used else None,
            "busiest": {"day": busiest["day"], "start": busiest["start"], "isp": busiest["isp"]} if busiest else None,
        },
        "billing": None
        if billing is None
        else {
            **billing,
            "read_at": line["billing_at"],
            "lines_on_account": billing["bills"][0].get("lines") if billing.get("bills") else None,
        },
        "link": link,
    }


def health_check(conn: sqlite3.Connection) -> dict | None:
    accounts = conn.execute("SELECT provider, identifier, status FROM provider_accounts").fetchall()
    if not accounts:
        return None
    names = {row["provider"]: _provider_info(row["provider"])["name"] for row in accounts}
    expired = [f"{names[row['provider']]} {row['identifier']}" for row in accounts if row["status"] == "expired"]
    failing = [f"{names[row['provider']]} {row['identifier']}" for row in accounts if row["status"] == "error"]
    check = {"id": "isp", "label": "Provider figures"}
    if expired:
        return {**check, "ok": False, "detail": f"Signed out of {', '.join(expired)}: sign in again from Settings"}
    if failing:
        return {**check, "ok": False, "detail": f"Could not read {', '.join(failing)} last time; retrying"}
    newest = conn.execute("SELECT MAX(read_at) AS ts FROM isp_lines").fetchone()["ts"]
    detail = "Read " + (f"{int(time.time() - newest) // 60} min ago" if newest else "not yet")
    return {**check, "ok": True, "detail": detail}


# --- command line -------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    from .config import load_settings

    settings = load_settings()
    db.init(settings.db_path)
    if argv == ["read"]:
        IspService(settings.db_path).read_all()
        print(json.dumps(overview(db.connect(settings.db_path), settings.timezone), indent=2))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
