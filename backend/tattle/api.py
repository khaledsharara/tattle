from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, db, identity, isp, periods, providers, routers
from .admin import ADMIN_HASH_SETTING
from .auth import COOKIE_NAME, SESSION_SECONDS, AdminAuth
from .collector import ADMIN_MERGE_REASON, ROUTER_SETTING, SERVER_KEY, Collector, LiveState
from .config import Settings, load_settings
from .oui import is_private_mac
from .providers import Provider, ProviderError
from .routers import RouterError

log = logging.getLogger("tattle.api")

ACTIVE_SECONDS = 120
NEW_DEVICE_SECONDS = 86400
COLOR_SLOTS = 7  # categorical palette slots; everything else is "Other"
# Providers also charge line overhead (PPPoE headers), so they read a few percent above Tattle.
MIN_COUNTED_SHARE = 0.9
MASK = "••••••••"  # stands in for a saved secret in forms; sending it back keeps the secret
GATEWAY_STALE_SECONDS = 120


# --- request bodies -----------------------------------------------------------
class RenameBody(BaseModel):
    name: str | None = Field(default=None, max_length=60)


class MergeBody(BaseModel):
    into: int


class SettingsBody(BaseModel):
    # Only used when no provider gives the renewal date.
    billing_day: int | None = Field(default=None, ge=1, le=28)
    quota_gb: float | None = Field(default=None, gt=0, le=100_000)


class LoginBody(BaseModel):
    password: str = Field(max_length=200)


class SetupBody(BaseModel):
    code: str = Field(max_length=12)
    password: str = Field(min_length=8, max_length=200)


class SignInBody(BaseModel):
    provider: str = Field(max_length=40)
    values: dict[str, str] = Field(default_factory=dict)


class AnswerBody(BaseModel):
    pending: str = Field(max_length=64)
    answer: str = Field(max_length=4000)  # a pasted sign-in address can be long


class LiveLineBody(BaseModel):
    number: str | None = Field(default=None, max_length=40)


class RouterBody(BaseModel):
    adapter: str | None = Field(default=None, max_length=40)
    values: dict[str, str] = Field(default_factory=dict)


# --- devices as the dashboard shows them -----------------------------------------
class DeviceIndex:
    """All identities, folded into the devices people see.

    An identity is what one DHCP client looks like (a MAC or client id); a
    device is an identity plus every identity merged into it. Usage stays
    recorded per identity, so merges can be undone.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.rows = {row["id"]: row for row in conn.execute("SELECT * FROM devices")}
        self.roots = db.device_roots(conn)
        self.members: dict[int, list[int]] = defaultdict(list)
        for device_id, root in self.roots.items():
            self.members[root].append(device_id)
        # root -> [(value, norm, source, last_seen)], newest first
        self.names: dict[int, list[tuple[str, str, str, int]]] = defaultdict(list)
        for row in conn.execute("SELECT device_id, source, value, norm, last_seen FROM device_names ORDER BY last_seen DESC"):
            root = self.roots.get(row["device_id"])
            if root is not None:
                self.names[root].append((row["value"], row["norm"], row["source"], row["last_seen"]))

    def root(self, device_id: int) -> int:
        return self.roots.get(device_id, device_id)

    def is_server(self, root: int) -> bool:
        return self.rows[root]["key"] == SERVER_KEY

    def kind(self, root: int) -> str | None:
        hints = [self.rows[m]["os_hint"] for m in self.members[root] if self.rows[m]["os_hint"]]
        return hints[0] if hints else None

    def custom_name(self, root: int) -> str | None:
        """The newest name the admin typed for any identity of this device."""
        named = [self.rows[m] for m in self.members[root] if self.rows[m]["custom_name"]]
        if not named:
            return None
        return max(named, key=lambda r: (r["custom_name_set_at"] or 0, r["id"] == root))["custom_name"]

    def display_name(self, root: int) -> str:
        row = self.rows[root]
        if row["key"] == SERVER_KEY:
            return row["custom_name"] or "Tattle (this server)"
        if custom := self.custom_name(root):
            return custom
        announced = self.names.get(root, [])
        for wanted in ("personal", "model"):
            for value, norm, _, _ in announced:
                if identity.name_strength(norm) == wanted:
                    return value
        if row["hostname"]:
            return row["hostname"]
        readable = [value for value, norm, _, _ in announced if identity.name_strength(norm) != "token"]
        if readable:
            return readable[0]
        if row["vendor"]:
            return f"{row['vendor']} device"
        kind = self.kind(root)
        if kind:
            return f"{kind} ({row['last_ip']})" if row["last_ip"] else kind
        return f"Unknown device ({row['last_ip']})" if row["last_ip"] else "Unknown device"

    def latest_member(self, root: int) -> sqlite3.Row:
        return max((self.rows[m] for m in self.members[root]), key=lambda r: (r["last_seen"], r["id"]))

    def payload(self, root: int, usage: list[int], today: list[int], rates: dict, now: float) -> dict:
        row = self.rows[root]
        members = [self.rows[m] for m in self.members[root]]
        latest = self.latest_member(root)
        last_active = max((m["last_active"] or 0 for m in members), default=0) or None
        rx_bps = sum(rates.get(m["id"], (0.0, 0.0))[0] for m in members)
        tx_bps = sum(rates.get(m["id"], (0.0, 0.0))[1] for m in members)
        first_seen = min(m["first_seen"] for m in members)
        aka: list[str] = []
        for value, norm, _, _ in self.names.get(root, []):
            if value not in aka and identity.name_strength(norm) != "token":
                aka.append(value)
        return {
            "id": root,
            "name": self.display_name(root),
            "custom_name": self.custom_name(root),
            "hostname": row["hostname"],
            "mac": latest["mac"],
            "private_mac": bool(latest["mac"] and is_private_mac(latest["mac"])),
            "vendor": row["vendor"],
            "kind": self.kind(root),
            "model": next((m["model"] for m in members if m["model"]), None),
            "aka": aka[:6],
            "ip": latest["last_ip"],
            "first_seen": first_seen,
            "last_seen": max(m["last_seen"] for m in members),
            "last_active": last_active,
            "is_server": row["key"] == SERVER_KEY,
            "is_new": self.custom_name(root) is None and row["key"] != SERVER_KEY and now - first_seen < NEW_DEVICE_SECONDS,
            "active": bool(last_active and now - last_active < ACTIVE_SECONDS),
            "rx": usage[0],
            "tx": usage[1],
            "today_rx": today[0],
            "today_tx": today[1],
            "rx_bps": rx_bps,
            "tx_bps": tx_bps,
            "identities": [
                {
                    "id": m["id"],
                    "mac": m["mac"],
                    "ip": m["last_ip"],
                    "last_seen": m["last_seen"],
                    "private_mac": bool(m["mac"] and is_private_mac(m["mac"])),
                    "primary": m["id"] == root,
                    "reason": m["merge_reason"],
                    "automatic": bool(m["merge_reason"]) and not m["merge_reason"].startswith(ADMIN_MERGE_REASON),
                }
                for m in sorted(members, key=lambda r: (r["id"] != root, -r["last_seen"]))
            ],
        }

    def brief(self, root: int) -> dict:
        latest = self.latest_member(root)
        return {
            "id": root,
            "name": self.display_name(root),
            "mac": latest["mac"],
            "kind": self.kind(root),
            "last_seen": max(self.rows[m]["last_seen"] for m in self.members[root]),
        }


def client_address(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if host in ("127.0.0.1", "::1") and forwarded:
        return forwarded.split(",")[0].strip()
    return host


def usage_between(conn: sqlite3.Connection, index: DeviceIndex, start: int, end: int) -> dict[int, list[int]]:
    """Usage per shown device (merged identities summed)."""
    totals: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for row in conn.execute(
        "SELECT device_id, SUM(rx) AS rx, SUM(tx) AS tx FROM usage_hourly "
        "WHERE hour >= ? AND hour < ? GROUP BY device_id",
        (start // 3600 * 3600, end),
    ):
        cell = totals[index.root(row["device_id"])]
        cell[0] += row["rx"]
        cell[1] += row["tx"]
    return totals


def color_order(conn: sqlite3.Connection, index: DeviceIndex, now: float) -> list[int]:
    """Devices that get their own colour: the biggest users of the last 30 days.

    Based on a fixed window rather than the selected range, so a device keeps
    its colour when the reader switches between Today / This cycle / ...
    """
    usage = usage_between(conn, index, int(now) - 30 * 86400, int(now) + 3600)
    ranked = sorted(usage.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0]))
    return [root for root, _ in ranked[:COLOR_SLOTS]]


def coverage_check(conn: sqlite3.Connection, tz, now: float) -> dict:
    """Whether Tattle sees everything the provider charges for, judged on the
    last full day. A router's own internet counter can't tell: many routers
    forward bulk traffic in hardware, past the counter."""
    check = {"id": "coverage", "label": "Traffic Tattle sees"}
    full = [day for day in isp.daily_comparison(conn, tz, now) if day["complete"] and day["isp"]]
    if not full:
        connected = conn.execute("SELECT 1 FROM isp_lines LIMIT 1").fetchone() is not None
        detail = (
            "Checked against your provider's count once Tattle has counted a full day"
            if connected
            else "Connect your provider in Settings to check Tattle against its count"
        )
        return {**check, "ok": True, "detail": detail}
    day = full[0]
    share = day["counted"] / day["isp"]
    when = datetime.fromtimestamp(day["start"], tz)
    detail = f"{when:%a} {when.day} {when:%b}: Tattle counted {share:.0%} of what your provider charged"
    if share < MIN_COUNTED_SHARE:
        detail += " (IPv6 traffic, devices with a fixed gateway, or the line used elsewhere?)"
    return {**check, "ok": share >= MIN_COUNTED_SHARE, "detail": detail}


def read_gateway_status(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def masked(adapter_id: str, values: dict[str, str]) -> dict[str, str]:
    """Router settings for the form: secrets replaced by MASK."""
    try:
        fields = routers.registry()[adapter_id].fields
    except KeyError:
        return {}
    secret = {f.name for f in fields if f.kind == "password"}
    return {key: (MASK if key in secret and value else value) for key, value in values.items()}


def _write_setup_code(path: str, code: str) -> None:
    try:
        with open(path, "w") as fh:
            fh.write(code + "\n")
        os.chmod(path, 0o600)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)


def merge_identities(conn: sqlite3.Connection, fold: int, keep: int, reason: str) -> None:
    """Show `fold` (and anything already merged into it) as part of `keep`."""
    now = int(time.time())
    conn.execute("UPDATE devices SET merged_into = ? WHERE merged_into = ?", (keep, fold))
    conn.execute(
        "UPDATE devices SET merged_into = ?, merge_reason = ?, merged_at = ? WHERE id = ?", (keep, reason, now, fold)
    )
    a, b = sorted((fold, keep))
    conn.execute(
        "INSERT INTO merge_suggestions(a_id, b_id, reason, score, status, created, updated) "
        "VALUES(?, ?, ?, 1, 'merged', ?, ?) "
        "ON CONFLICT(a_id, b_id) DO UPDATE SET status = 'merged', updated = excluded.updated",
        (a, b, reason, now, now),
    )


def create_app(
    settings: Settings | None = None,
    start_collector: bool = True,
    provider_instances: dict[str, Provider] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    db.init(settings.db_path)
    if settings.demo:
        from . import demo

        demo.seed(settings)
        start_collector = False
    setup = db.connect(settings.db_path)
    secret_hex = db.get_setting(setup, "auth_secret")
    if secret_hex is None:
        secret_hex = secrets.token_hex(32)
        db.set_setting(setup, "auth_secret", secret_hex)
    auth = AdminAuth(settings.admin_password, bytes.fromhex(secret_hex), db.get_setting(setup, ADMIN_HASH_SETTING))
    setup.close()
    setup_code_path = os.path.join(settings.data_dir, "setup-code.txt")
    if auth.setup_code and not settings.demo:
        _write_setup_code(setup_code_path, auth.setup_code)
        log.warning("No admin password yet. Open the dashboard and enter setup code %s", auth.setup_code)

    live = LiveState()
    if settings.demo:
        live.rates = demo.live_rates(settings)
    collector = Collector(settings, live)
    isp_service = isp.IspService(settings.db_path, provider_instances)
    isp_poller = isp.IspPoller(isp_service)
    tz = settings.timezone
    static_dir = Path(settings.static_dir).resolve()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_collector:
            collector.start()
            isp_poller.start()
        yield
        collector.stop_event.set()
        isp_poller.stop_event.set()

    app = FastAPI(title="Tattle", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def is_admin(request: Request) -> bool:
        # The demo's database is thrown away, so everyone may try every button.
        return settings.demo or auth.valid(request.cookies.get(COOKIE_NAME))

    def require_admin(request: Request) -> None:
        if not is_admin(request):
            raise HTTPException(401, "Log in as admin to make changes")

    def mode() -> str:
        with live.lock:
            current = live.mode
        return "demo" if settings.demo else current

    def counting() -> bool:
        """Devices are counted (or should be: a stopped gateway still shows in the checks)."""
        return mode() != "quota" or settings.mode in ("gateway", "router")

    def unit_base(conn: sqlite3.Connection) -> int:
        """How the dashboard writes GB: the way the connected provider counts."""
        row = conn.execute("SELECT provider FROM isp_lines ORDER BY read_at DESC LIMIT 1").fetchone()
        if row is None:
            return 1000
        try:
            return providers.get(row["provider"]).unit_base
        except ProviderError:
            return 1000

    def billing_day(conn: sqlite3.Connection) -> int | None:
        value = db.get_setting(conn, "billing_day")
        return int(value) if value else None

    def cycle_now(conn: sqlite3.Connection) -> tuple[tuple[date, date], dict | None]:
        """(first day, renewal day) of the quota cycle, and the provider's figures
        for it. The provider's own renewal dates win; the day typed in Settings
        is the fallback."""
        figures = isp.current_cycle(conn, tz)
        if figures is not None:
            return (figures["start"], figures["end"]), figures
        return periods.manual_cycle(datetime.now(tz).date(), billing_day(conn) or 1), None

    def quota_gb(conn: sqlite3.Connection) -> float | None:
        value = db.get_setting(conn, "quota_gb")
        return float(value) if value else None

    def period_from_query(conn: sqlite3.Connection, key: str, start: date | None, end: date | None) -> periods.Period:
        try:
            cycle = cycle_now(conn)[0] if key == "cycle" else None
            return periods.resolve(key, tz, cycle=cycle, start_date=start, end_date=end)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def live_rates() -> dict[int, tuple[float, float]]:
        with live.lock:
            return dict(live.rates)

    # --- read endpoints --------------------------------------------------------
    @app.get("/api/overview")
    def overview(
        range_key: str = Query("today", alias="range"),
        start: date | None = Query(None, alias="from"),
        end: date | None = Query(None, alias="to"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        period = period_from_query(conn, range_key, start, end)
        index = DeviceIndex(conn)

        per_device: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        per_bucket: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for row in conn.execute(
            "SELECT device_id, hour, rx, tx FROM usage_hourly WHERE hour >= ? AND hour < ?",
            (period.start // 3600 * 3600, period.end),
        ):
            root = index.root(row["device_id"])
            per_device[root][0] += row["rx"]
            per_device[root][1] += row["tx"]
            cell = per_bucket[periods.bucket_of(row["hour"], period, tz)][root]
            cell[0] += row["rx"]
            cell[1] += row["tx"]

        ranked = sorted(per_device.items(), key=lambda kv: kv[1][0] + kv[1][1], reverse=True)
        colors = color_order(conn, index, time.time())
        top_ids = [root for root in colors if sum(per_device.get(root, [0, 0])) > 0]
        points = []
        for bucket_start in periods.bucket_starts(period, tz):
            cells = per_bucket.get(bucket_start, {})
            by_device = {str(d): cells[d][0] + cells[d][1] for d in top_ids if d in cells}
            other = sum(v[0] + v[1] for d, v in cells.items() if d not in top_ids)
            if other:
                by_device["other"] = other
            points.append(
                {
                    "t": bucket_start,
                    "rx": sum(v[0] for v in cells.values()),
                    "tx": sum(v[1] for v in cells.values()),
                    "by_device": by_device,
                }
            )

        # The router's own counter next to what Tattle booked over the same polling intervals.
        router = None
        wan = conn.execute(
            "SELECT COUNT(*) AS hours, SUM(rx) AS rx, SUM(tx) AS tx, SUM(dev_rx) AS dev_rx, SUM(dev_tx) AS dev_tx "
            "FROM wan_hourly WHERE hour >= ? AND hour < ?",
            (period.start // 3600 * 3600, period.end),
        ).fetchone()
        if wan["hours"]:
            router = {
                "rx": wan["rx"],
                "tx": wan["tx"],
                "matched_rx": wan["dev_rx"],
                "matched_tx": wan["dev_tx"],
                "hours": wan["hours"],
            }

        quota = None
        cycle_days, figures = cycle_now(conn)
        cycle = periods.resolve("cycle", tz, cycle=cycle_days)
        counted = sum(v[0] + v[1] for v in usage_between(conn, index, cycle.start, cycle.end).values())
        cycle_end = periods.local_midnight(cycle_days[1], tz)
        if figures is not None:
            # The provider's own figures for the line in use. "counted" is what Tattle
            # saw in the same days; the rest went over another line or around Tattle.
            quota = {
                "source": "isp",
                "provider": figures["provider"],
                "number": figures["number"],
                "pending": figures["pending"],
                "bytes": figures["bytes"],
                "plan_bytes": figures["plan_bytes"],
                "used": figures["used"],
                "counted": counted,
                "cycle_start": cycle.start,
                "cycle_end": cycle_end,
            }
        elif (gb := quota_gb(conn)) is not None:
            size = int(gb * unit_base(conn) ** 3)
            quota = {
                "source": "manual",
                "provider": None,
                "number": None,
                "pending": False,
                "bytes": size,
                "plan_bytes": size,
                "used": counted,
                "counted": counted,
                "cycle_start": cycle.start,
                "cycle_end": cycle_end,
            }

        rates = live_rates()
        return {
            "period": {**period.__dict__, "bucket": period.bucket},
            "totals": {
                "rx": sum(v[0] for v in per_device.values()),
                "tx": sum(v[1] for v in per_device.values()),
            },
            "devices": [
                {
                    "id": root,
                    "name": index.display_name(root),
                    "rx": usage[0],
                    "tx": usage[1],
                    "is_server": index.is_server(root),
                }
                for root, usage in ranked
                if root in index.rows and usage[0] + usage[1] > 0
            ],
            "color_order": colors,
            "series": {
                "bucket": period.bucket,
                "top": [{"id": d, "name": index.display_name(d)} for d in top_ids if d in index.rows],
                "points": points,
            },
            "router": router,
            "quota": quota,
            "live": {
                "rx_bps": sum(r[0] for r in rates.values()),
                "tx_bps": sum(r[1] for r in rates.values()),
                "active_devices": len({index.root(d) for d, r in rates.items() if r[0] + r[1] > 0}),
            },
        }

    @app.get("/api/devices")
    def list_devices(
        range_key: str = Query("today", alias="range"),
        start: date | None = Query(None, alias="from"),
        end: date | None = Query(None, alias="to"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        period = period_from_query(conn, range_key, start, end)
        today = periods.resolve("today", tz)
        index = DeviceIndex(conn)
        usage = usage_between(conn, index, period.start, period.end)
        today_usage = usage_between(conn, index, today.start, today.end)
        rates = live_rates()
        now = time.time()
        items = [
            index.payload(root, usage.get(root, [0, 0]), today_usage.get(root, [0, 0]), rates, now)
            for root in index.members
            if index.rows[root]["merged_into"] is None
        ]
        items.sort(key=lambda d: (d["rx"] + d["tx"], d["last_seen"]), reverse=True)
        return {
            "period": {**period.__dict__, "bucket": period.bucket},
            "color_order": color_order(conn, index, now),
            "devices": items,
        }

    @app.get("/api/devices/{device_id}")
    def device_detail(
        device_id: int,
        range_key: str = Query("today", alias="range"),
        start: date | None = Query(None, alias="from"),
        end: date | None = Query(None, alias="to"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        index = DeviceIndex(conn)
        if device_id not in index.rows:
            raise HTTPException(404, "No such device")
        root = index.root(device_id)
        if root != device_id:
            raise HTTPException(409, f"merged:{root}")
        period = period_from_query(conn, range_key, start, end)
        today = periods.resolve("today", tz)
        members = index.members[root]
        placeholders = ",".join("?" * len(members))
        buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        total = [0, 0]
        for usage_row in conn.execute(
            f"SELECT hour, rx, tx FROM usage_hourly WHERE device_id IN ({placeholders}) AND hour >= ? AND hour < ?",
            (*members, period.start // 3600 * 3600, period.end),
        ):
            cell = buckets[periods.bucket_of(usage_row["hour"], period, tz)]
            cell[0] += usage_row["rx"]
            cell[1] += usage_row["tx"]
            total[0] += usage_row["rx"]
            total[1] += usage_row["tx"]
        today_usage = usage_between(conn, index, today.start, today.end).get(root, [0, 0])
        now = time.time()
        return {
            "period": {**period.__dict__, "bucket": period.bucket},
            "color_order": color_order(conn, index, now),
            "device": index.payload(root, total, today_usage, live_rates(), now),
            "series": [
                {"t": t, "rx": buckets[t][0], "tx": buckets[t][1]} for t in periods.bucket_starts(period, tz)
            ],
        }

    @app.get("/api/suggestions")
    def suggestions(conn: sqlite3.Connection = Depends(get_conn)):
        index = DeviceIndex(conn)
        items = []
        for row in conn.execute("SELECT * FROM merge_suggestions WHERE status = 'open' ORDER BY score DESC, updated DESC"):
            a, b = row["a_id"], row["b_id"]
            if a not in index.rows or b not in index.rows or index.root(a) != a or index.root(b) != b:
                continue
            items.append({"id": row["id"], "reason": row["reason"], "score": row["score"], "a": index.brief(a), "b": index.brief(b)})
        return {"suggestions": items}

    @app.get("/api/identity-events")
    def identity_events(limit: int = Query(20, ge=1, le=200), conn: sqlite3.Connection = Depends(get_conn)):
        """What Tattle merged or split on its own, newest first."""
        index = DeviceIndex(conn)
        events = []
        for row in conn.execute("SELECT * FROM identity_events ORDER BY ts DESC, id DESC LIMIT ?", (limit,)):
            if row["device_id"] not in index.rows or row["other_id"] not in index.rows:
                continue
            events.append(
                {
                    "id": row["id"],
                    "ts": row["ts"],
                    "kind": row["kind"],
                    "device": index.brief(index.root(row["device_id"])),
                    "other": index.brief(index.root(row["other_id"])),
                    "mac": index.rows[row["device_id"]]["mac"],
                    "reason": row["reason"],
                }
            )
        return {"events": events}

    @app.get("/api/health")
    def health(conn: sqlite3.Connection = Depends(get_conn)):
        now = time.time()
        with live.lock:
            state = {k: getattr(live, k) for k in live.__dataclass_fields__ if k not in ("lock", "rates")}
        if settings.demo:
            checks = [{"id": "demo", "label": "Demo", "ok": True, "detail": "Made-up data; nothing is being counted"}]
            return {"ok": True, "checks": checks, "time": now}

        def age(ts: float | None) -> str:
            return "never" if ts is None else f"{int(now - ts)}s ago"

        checks = [
            {
                "id": "collector",
                "label": "Usage collector",
                "ok": state["last_tick"] is not None and now - state["last_tick"] < 60 and not state["collector_error"],
                "detail": state["collector_error"] or f"Last run {age(state['last_tick'])}",
            }
        ]
        gateway_wanted = settings.mode == "gateway" or state["mode"] == "gateway"
        if gateway_wanted:
            checks.extend(gateway_checks(state, now, age))
        if state["router_name"]:
            router_check = {
                "id": "router",
                "label": state["router_name"],
                "ok": state["router_polled_at"] is not None and now - state["router_polled_at"] < 300,
                "detail": state["router_error"] or f"Read {age(state['router_polled_at'])}",
            }
            checks.append(router_check)
            if gateway_wanted and state["router_dhcp_enabled"] is not None:
                checks.append(
                    {
                        "id": "router_dhcp",
                        "label": "Router DHCP",
                        "ok": state["router_dhcp_enabled"] is False,
                        "detail": "Off, Tattle hands out addresses"
                        if state["router_dhcp_enabled"] is False
                        else "ON: devices can get addresses that skip Tattle. Turn it off on the router.",
                    }
                )
        elif settings.mode == "router":
            checks.append(
                {"id": "router", "label": "Router", "ok": False, "detail": "Choose your router in Settings to count devices"}
            )
        if counting():
            checks.append(coverage_check(conn, tz, now))
        if (isp_check := isp.health_check(conn)) is not None:
            checks.append(isp_check)
        return {"ok": all(c["ok"] for c in checks), "checks": checks, "time": now}

    def gateway_checks(state: dict, now: float, age) -> list[dict]:
        checks = [
            {
                "id": "counters",
                "label": "Gateway counters",
                "ok": state["snapshot_at"] is not None and now - state["snapshot_at"] < 60,
                "detail": f"Updated {age(state['snapshot_at'])}"
                if state["snapshot_at"]
                else "The gateway container isn't running: docker compose --profile gateway up -d",
            },
            {
                "id": "leases",
                "label": "Devices on Tattle",
                "ok": state["lease_count"] > 0,
                "detail": (
                    "1 device holds an address from Tattle"
                    if state["lease_count"] == 1
                    else f"{state['lease_count']} devices hold an address from Tattle"
                    if state["lease_count"]
                    else "No device has an address from Tattle yet: is the router's DHCP still on?"
                ),
            },
        ]
        status = read_gateway_status(settings.gateway_status_path)
        if status is None or now - status.get("updated", 0) > GATEWAY_STALE_SECONDS:
            return checks
        for item in status.get("preflight", []):
            if not item.get("ok"):
                checks.append({"id": f"gateway_{item['id']}", "label": "Gateway", "ok": False, "detail": item["detail"]})
        forwarding = status.get("forwarding") or {}
        if forwarding.get("ok") is not None:
            checks.append({"id": "forwarding", "label": "Forwarding", "ok": forwarding["ok"], "detail": forwarding["detail"]})
        foreign = (status.get("dhcp") or {}).get("foreign_servers") or []
        checks.append(
            {
                "id": "other_dhcp",
                "label": "Other DHCP servers",
                "ok": not foreign,
                "detail": "None answering"
                if not foreign
                else "Also handing out addresses: "
                + ", ".join(f"{s['ip']} ({s['mac']})" for s in foreign)
                + ". Turn off DHCP on the router and any Wi-Fi extender (set it to Off, not Auto).",
            }
        )
        adverts = status.get("ipv6_adverts") or {}
        if adverts.get("seen"):
            checks.append(
                {
                    "id": "ipv6",
                    "label": "IPv6",
                    "ok": False,
                    "detail": "The router advertises IPv6, so some traffic can skip Tattle. Turn off IPv6 on the router's LAN, "
                    "or set TATTLE_FILTER_AAAA=1.",
                }
            )
        return checks

    @app.get("/api/status")
    def status(conn: sqlite3.Connection = Depends(get_conn)):
        """What the app needs to know to lay itself out: how devices are counted,
        what is set up yet, and how to write GB."""
        with live.lock:
            router_name, leases = live.router_name, live.lease_count
        connected = conn.execute("SELECT COUNT(*) AS n FROM provider_accounts").fetchone()["n"]
        current = mode()
        return {
            "version": __version__,
            "mode": current,
            "intended_mode": settings.mode or None,
            "counting": current != "quota",
            "demo": settings.demo,
            "unit_base": unit_base(conn),
            "setup": {
                "admin": auth.enabled or settings.demo,
                "provider": connected > 0,
                "router": router_name is not None,
            },
            "router": router_name,
            "leases": leases,
            "server_ip": settings.server_ip,
        }

    @app.get("/api/settings")
    def get_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        (start, end), figures = cycle_now(conn)
        return {
            "billing_day": billing_day(conn),
            "quota_gb": quota_gb(conn),
            "cycle": {
                "source": "isp" if figures else "manual",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "number": figures["number"] if figures else None,
                "provider": figures["provider"] if figures else None,
                "plan_bytes": figures["plan_bytes"] if figures else None,
                "pending": bool(figures and figures["pending"]),
            },
            "admin_enabled": auth.enabled or settings.demo,
            "admin_setup_needed": auth.setup_code is not None and not settings.demo,
            "admin_from_environment": auth.from_environment,
            "is_admin": is_admin(request),
        }

    @app.get("/api/providers")
    def provider_catalog():
        return {"providers": [p for p in providers.catalog() if settings.demo or p["id"] != "demo"]}

    @app.get("/api/isp")
    def isp_status(conn: sqlite3.Connection = Depends(get_conn)):
        """The provider's own figures for each line, next to what Tattle counted."""
        return isp.overview(conn, tz, counting())

    @app.get("/api/isp/lines/{number}")
    def isp_line(
        number: str,
        span: str = Query("30d"),
        start: date | None = Query(None, alias="from"),
        end: date | None = Query(None, alias="to"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """One line: what is left, daily use over a span, bills, line speed."""
        if span not in isp.SPANS:
            raise HTTPException(400, f"span must be one of {', '.join(isp.SPANS)}")
        try:
            detail = isp.line_detail(conn, tz, number, span, start, end, counting())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if detail is None:
            raise HTTPException(404, "No such line")
        return detail

    @app.get("/api/routers")
    def router_catalog():
        return {"routers": routers.catalog()}

    @app.get("/api/router")
    def router_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        raw = db.get_setting(conn, ROUTER_SETTING)
        config = json.loads(raw) if raw else {"adapter": None, "values": {}}
        values = masked(config["adapter"], config.get("values") or {}) if is_admin(request) and config["adapter"] else {}
        with live.lock:
            error, polled = live.router_error, live.router_polled_at
        return {"adapter": config["adapter"], "values": values, "error": error, "read_at": polled}

    # --- admin endpoints -------------------------------------------------------
    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        client = client_address(request)
        if auth.locked_out(client):
            raise HTTPException(429, "Too many attempts, try again in a few minutes")
        if not auth.check_password(client, body.password):
            time.sleep(1)
            raise HTTPException(401, "Wrong password")
        response.set_cookie(
            COOKIE_NAME, auth.issue(), max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/"
        )
        return {"is_admin": True}

    @app.post("/api/auth/setup")
    def setup_admin(body: SetupBody, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
        """First run: the setup code from the install (or the log) and a new admin password."""
        if auth.setup_code is None:
            raise HTTPException(409, "An admin password is already set")
        client = client_address(request)
        if auth.locked_out(client):
            raise HTTPException(429, "Too many attempts, try again in a few minutes")
        if not auth.check_setup_code(client, body.code):
            time.sleep(1)
            raise HTTPException(401, "That setup code is not right. It's in data/setup-code.txt next to docker-compose.yml.")
        try:
            stored = auth.set_password(body.password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        db.set_setting(conn, ADMIN_HASH_SETTING, stored)
        try:
            os.remove(setup_code_path)
        except OSError:
            pass
        response.set_cookie(
            COOKIE_NAME, auth.issue(), max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/"
        )
        return {"is_admin": True}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"is_admin": False}

    @app.put("/api/settings", dependencies=[Depends(require_admin)])
    def put_settings(body: SettingsBody, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        with db.transaction(conn):
            if body.billing_day is not None:
                db.set_setting(conn, "billing_day", str(body.billing_day))
            db.set_setting(conn, "quota_gb", "" if body.quota_gb is None else str(body.quota_gb))
        return get_settings(request, conn)

    @app.patch("/api/devices/{device_id}", dependencies=[Depends(require_admin)])
    def rename_device(device_id: int, body: RenameBody, conn: sqlite3.Connection = Depends(get_conn)):
        name = (body.name or "").strip() or None
        root = db.device_roots(conn).get(device_id)
        if root is None:
            raise HTTPException(404, "No such device")
        with db.transaction(conn):
            # One name per device: clear names typed for its other identities.
            conn.execute("UPDATE devices SET custom_name = NULL WHERE merged_into = ?", (root,))
            conn.execute(
                "UPDATE devices SET custom_name = ?, custom_name_set_at = ? WHERE id = ?", (name, int(time.time()), root)
            )
        return {"id": root, "custom_name": name}

    @app.post("/api/devices/{device_id}/merge", dependencies=[Depends(require_admin)])
    def merge_device(device_id: int, body: MergeBody, conn: sqlite3.Connection = Depends(get_conn)):
        """Show a device as part of another one (the same phone under another address)."""
        index = DeviceIndex(conn)
        if device_id not in index.rows or body.into not in index.rows:
            raise HTTPException(404, "No such device")
        fold, keep = index.root(device_id), index.root(body.into)
        if fold == keep:
            raise HTTPException(400, "Pick a different device")
        if index.is_server(fold) or index.is_server(keep):
            raise HTTPException(400, "The server cannot be merged")
        with db.transaction(conn):
            merge_identities(conn, fold, keep, ADMIN_MERGE_REASON)
        return {"id": keep}

    @app.post("/api/devices/{device_id}/split", dependencies=[Depends(require_admin)])
    def split_device(device_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        """Undo a merge: show this identity as its own device again, and never re-merge the pair."""
        row = conn.execute("SELECT id, merged_into FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such device")
        if row["merged_into"] is None:
            raise HTTPException(400, "This device is not merged into another")
        now = int(time.time())
        root = db.device_roots(conn)[device_id]
        a, b = sorted((device_id, root))
        with db.transaction(conn):
            conn.execute(
                "UPDATE devices SET merged_into = NULL, merge_reason = NULL, merged_at = NULL WHERE id = ?", (device_id,)
            )
            conn.execute(
                "INSERT INTO merge_suggestions(a_id, b_id, reason, score, status, created, updated) "
                "VALUES(?, ?, 'Split by admin', 0, 'dismissed', ?, ?) "
                "ON CONFLICT(a_id, b_id) DO UPDATE SET status = 'dismissed', updated = excluded.updated",
                (a, b, now, now),
            )
        return {"id": device_id}

    @app.post("/api/suggestions/{suggestion_id}/accept", dependencies=[Depends(require_admin)])
    def accept_suggestion(suggestion_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        row = conn.execute("SELECT * FROM merge_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None or row["status"] != "open":
            raise HTTPException(404, "No such open suggestion")
        index = DeviceIndex(conn)
        a, b = index.root(row["a_id"]), index.root(row["b_id"])
        if a == b:
            raise HTTPException(400, "Already the same device")
        ra, rb = index.rows[a], index.rows[b]
        # Keep the one with a custom name, else the older one.
        if bool(ra["custom_name"]) != bool(rb["custom_name"]):
            keep, fold = (a, b) if ra["custom_name"] else (b, a)
        else:
            keep, fold = (a, b) if (ra["first_seen"], a) <= (rb["first_seen"], b) else (b, a)
        with db.transaction(conn):
            merge_identities(conn, fold, keep, f"{ADMIN_MERGE_REASON}: {row['reason']}")
        return {"id": keep}

    @app.post("/api/suggestions/{suggestion_id}/dismiss", dependencies=[Depends(require_admin)])
    def dismiss_suggestion(suggestion_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        updated = conn.execute(
            "UPDATE merge_suggestions SET status = 'dismissed', updated = ? WHERE id = ? AND status = 'open'",
            (int(time.time()), suggestion_id),
        ).rowcount
        if not updated:
            raise HTTPException(404, "No such open suggestion")
        return {"id": suggestion_id}

    def provider_call(action):
        try:
            return action()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/isp/sign-in", dependencies=[Depends(require_admin)])
    def isp_sign_in(body: SignInBody):
        """Start signing in to a provider: done at once, or a code/captcha to answer."""
        return provider_call(lambda: isp_service.start_sign_in(body.provider, body.values))

    @app.post("/api/isp/answer", dependencies=[Depends(require_admin)])
    def isp_answer(body: AnswerBody):
        return provider_call(lambda: isp_service.answer(body.pending, body.answer))

    @app.post("/api/isp/read", dependencies=[Depends(require_admin)])
    def isp_read_now(conn: sqlite3.Connection = Depends(get_conn)):
        provider_call(isp_service.read_all)
        return isp.overview(conn, tz, counting())

    @app.put("/api/isp/live", dependencies=[Depends(require_admin)])
    def isp_set_live(body: LiveLineBody, conn: sqlite3.Connection = Depends(get_conn)):
        """Say which line the house is using, for setups where Tattle can't tell."""
        if body.number is not None and not conn.execute("SELECT 1 FROM isp_lines WHERE number = ?", (body.number,)).fetchone():
            raise HTTPException(404, "No such line")
        isp.set_live_line(conn, body.number, "manual")
        return isp.overview(conn, tz, counting())

    @app.delete("/api/isp/accounts/{account_id}", dependencies=[Depends(require_admin)])
    def isp_remove(account_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        """Forget a sign-in and its credentials; its lines' past readings are kept."""
        with isp_service.lock, db.transaction(conn):
            conn.execute("DELETE FROM isp_lines WHERE login_id = ?", (account_id,))
            if not conn.execute("DELETE FROM provider_accounts WHERE id = ?", (account_id,)).rowcount:
                raise HTTPException(404, "No such sign-in")
        return isp.overview(conn, tz, counting())

    @app.put("/api/router", dependencies=[Depends(require_admin)])
    def set_router(body: RouterBody, conn: sqlite3.Connection = Depends(get_conn)):
        """Choose the router adapter; it is tested before it is saved."""
        if not body.adapter:
            db.set_setting(conn, ROUTER_SETTING, "")
            return {"adapter": None, "found": []}
        raw = db.get_setting(conn, ROUTER_SETTING)
        saved = json.loads(raw) if raw else {}
        old = saved.get("values", {}) if saved.get("adapter") == body.adapter else {}
        values = {key: (old.get(key, "") if value == MASK else value.strip()) for key, value in body.values.items()}
        try:
            found = routers.create(body.adapter, values).test()
        except NotImplementedError:
            found = []
        except (RouterError, ProviderError, OSError, ValueError) as exc:
            raise HTTPException(400, f"Could not read the router: {exc}") from exc
        db.set_setting(conn, ROUTER_SETTING, json.dumps({"adapter": body.adapter, "values": values}))
        return {"adapter": body.adapter, "found": found}

    # --- the single-page app ---------------------------------------------------
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and static_dir in candidate.parents:
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else {}
            return FileResponse(candidate, headers=headers)
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    return app
