"""Turns raw counters into per-device, per-hour usage.

Where the counts come from depends on the setup:

* gateway mode: every tick reads the nftables snapshot the gateway container
  writes (bytes per LAN IP since the table was loaded), subtracts the previous
  reading and books the difference on whichever device holds that IP *at that
  moment*, according to the dnsmasq leases;
* router mode: a router adapter that keeps per-device counters is read every
  minute or so, and the difference per MAC is booked the same way;
* quota-only: no per-device counts; the provider's figures are all there is.

A router adapter can also report the router's own internet counters, the line
in use (its internet login) and the line speed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from . import db, identity, isp, routers
from .config import Settings
from .oui import VendorLookup
from .providers import ProviderError
from .routers import RouterAdapter, RouterError
from .sources import Lease, LeaseReader, read_counter_snapshot, read_neighbours

log = logging.getLogger("tattle.collector")

SERVER_KEY = "server"
# merge_reason of merges an admin made by hand; those are never undone automatically.
ADMIN_MERGE_REASON = "Merged by admin"
# Beyond this gap a 32-bit router counter may have wrapped more than once.
MAX_ROUTER_GAP_SECONDS = 600
# The gateway writes a snapshot every 5 s; older than this and it has stopped.
SNAPSHOT_FRESH_SECONDS = 90
ROUTER_SETTING = "router"  # {"adapter": id, "values": {...}}, set in Settings
DHCP_CHECK_SECONDS = 300
LAST_SEEN_WRITE_SECONDS = 60
SESSION_GAP_SECONDS = 180  # quieter than this and a new session starts
LINK_INTERVAL_SECONDS = 600
NAME_WRITE_SECONDS = 3600


@dataclass
class LiveState:
    """Shared with the API; guarded by `lock`."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    rates: dict[int, tuple[float, float]] = field(default_factory=dict)  # device -> (rx B/s, tx B/s)
    last_tick: float | None = None
    snapshot_at: float | None = None
    router_polled_at: float | None = None
    router_error: str | None = None
    router_dhcp_enabled: bool | None = None
    router_dhcp_checked_at: float | None = None
    router_internet_login: str | None = None
    router_name: str | None = None
    lease_count: int = 0
    collector_error: str | None = None
    mode: str = "quota"  # how devices are counted now: 'gateway' | 'router' | 'quota'


def counter_delta(previous: int, current: int, modulus: int | None) -> int:
    """Bytes between two readings of a running counter. One that went down
    restarted (a reboot, a reconnect), unless it wraps and only wrapped."""
    if current >= previous:
        return current - previous
    if modulus and previous - current > modulus // 2:
        return (current - previous) % modulus
    return current


class Collector(threading.Thread):
    def __init__(self, settings: Settings, live: LiveState, clock: Callable[[], float] = time.time):
        super().__init__(name="collector", daemon=True)
        self.settings = settings
        self.live = live
        self.clock = clock  # tests run on the fake host's clock
        self.stop_event = threading.Event()
        self.conn = db.connect(settings.db_path)
        self.leases = LeaseReader(settings.leases_path)
        self.vendors = VendorLookup(settings.oui_path)
        self.router: RouterAdapter | None = None
        self.router_config: str | None = None
        self.max_bytes_per_sec = settings.max_mbps * 1_000_000 / 8 * 1.5
        self.state: dict[str, int] = {
            row["name"]: row["value"] for row in self.conn.execute("SELECT name, value FROM counter_state")
        }
        self.key_ids: dict[str, int] = {}
        self.last_written: dict[int, tuple[float, str | None, str | None]] = {}
        self.server_id = self._ensure_server_device()
        self.prev_snapshot_at: float | None = None
        self.next_router_poll = 0.0
        self.next_router_counters = 0.0
        self.prev_router_counters_at: float | None = None
        self.next_dhcp_check = 0.0
        self.next_link_run = time.time() + 60
        self.dhcp_seen: dict[str, float] = {}  # fingerprint file -> mtime ingested
        self.names_written: dict[tuple[int, str, str], float] = {}
        self.leases_by_ip: dict[str, Lease] = {}
        self.neighbours: dict[str, str] = {}
        # device -> (session row id, start, end) of the stretch it is in now
        self.sessions: dict[int, tuple[int, int, int]] = {
            row["device_id"]: (row["id"], row["start"], row["end"])
            for row in self.conn.execute(
                "SELECT s.id, s.device_id, s.start, s.end FROM device_sessions s "
                "JOIN (SELECT device_id, MAX(end) AS end FROM device_sessions GROUP BY device_id) last "
                "ON last.device_id = s.device_id AND last.end = s.end"
            )
        }
        self.mdns = identity.MdnsListener(settings.server_ip) if settings.mdns_enabled else None

    # --- thread loop ---------------------------------------------------------
    def run(self) -> None:
        if self.mdns:
            self.mdns.start()
        while not self.stop_event.is_set():
            started = time.time()
            try:
                self.tick()
                self.ingest_identity_hints()
                if started >= self.next_link_run:
                    self.next_link_run = started + LINK_INTERVAL_SECONDS
                    self.link_identities()
                with self.live.lock:
                    self.live.collector_error = None
            except Exception as exc:  # keep collecting; surface the problem in /api/health
                log.exception("collector tick failed")
                with self.live.lock:
                    self.live.collector_error = f"{type(exc).__name__}: {exc}"
            self._load_router()
            if self.router and started >= self.next_router_poll:
                self.next_router_poll = started + self.settings.router_poll_seconds
                self.poll_router()
            self.stop_event.wait(max(1.0, self.settings.poll_seconds - (time.time() - started)))

    # --- devices -------------------------------------------------------------
    def _ensure_server_device(self) -> int:
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO devices(key, hostname, last_ip, first_seen, last_seen) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (SERVER_KEY, "tattle", self.settings.server_ip, now, now),
        )
        return self.conn.execute("SELECT id FROM devices WHERE key = ?", (SERVER_KEY,)).fetchone()["id"]

    def _device_for_ip(self, ip: str, lease: Lease | None, neighbours: dict[str, str], now: float) -> int:
        if lease:
            key = f"id:{lease.client_id}" if lease.client_id else f"mac:{lease.mac}"
            return self._device(key, lease.mac, lease.client_id, lease.hostname, ip, now)
        return self._device(f"ip:{ip}", neighbours.get(ip), None, None, ip, now)

    def _device_for_mac(self, mac: str, ip: str | None, hostname: str | None, now: float) -> int:
        """Router mode knows devices by MAC. Prefer an identity the gateway (DHCP)
        already made for the same MAC, so switching modes keeps the history."""
        key = f"mac:{mac}"
        for candidate in (f"id:01:{mac}", key):
            if candidate in self.key_ids or self.conn.execute(
                "SELECT 1 FROM devices WHERE key = ?", (candidate,)
            ).fetchone():
                key = candidate
                break
        return self._device(key, mac, None, hostname, ip, now)

    def _device(
        self, key: str, mac: str | None, client_id: str | None, hostname: str | None, ip: str | None, now: float
    ) -> int:
        device_id = self.key_ids.get(key)
        if device_id is None:
            row = self.conn.execute("SELECT id FROM devices WHERE key = ?", (key,)).fetchone()
            if row is None:
                cur = self.conn.execute(
                    "INSERT INTO devices(key, mac, client_id, hostname, vendor, last_ip, first_seen, last_seen) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, mac, client_id, hostname, self.vendors.lookup(mac), ip, int(now), int(now)),
                )
                device_id = int(cur.lastrowid)
                log.info("new device %s (%s, %s, %s)", device_id, key, ip, hostname)
            else:
                device_id = row["id"]
            self.key_ids[key] = device_id

        written = self.last_written.get(device_id)
        if written is None or now - written[0] >= LAST_SEEN_WRITE_SECONDS or written[1:] != (ip, hostname):
            self.conn.execute(
                "UPDATE devices SET last_ip = COALESCE(?, last_ip), last_seen = ?, hostname = COALESCE(?, hostname), "
                "mac = COALESCE(?, mac) WHERE id = ?",
                (ip, int(now), hostname, mac, device_id),
            )
            self.last_written[device_id] = (now, ip, hostname)
        if hostname:
            self._record_name(device_id, "dhcp", hostname, now)
        return device_id

    def _record_name(self, device_id: int, source: str, value: str, now: float) -> None:
        key = (device_id, source, value)
        if now - self.names_written.get(key, 0) < NAME_WRITE_SECONDS:
            return
        self.conn.execute(
            "INSERT INTO device_names(device_id, source, value, norm, first_seen, last_seen) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id, source, value) DO UPDATE SET last_seen = excluded.last_seen",
            (device_id, source, value, identity.normalise_name(value), int(now), int(now)),
        )
        self.names_written[key] = now

    def _in_lan(self, ip: str) -> bool:
        try:
            return ipaddress.ip_address(ip) in self.settings.lan_network
        except ValueError:
            return False

    # --- counters ------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        snap = read_counter_snapshot(self.settings.nft_snapshot_path)
        leases = self.leases.by_ip()
        neighbours = read_neighbours(self.settings.arp_path)
        self.leases_by_ip, self.neighbours = leases, neighbours
        gateway = snap is not None and now - snap.taken_at < SNAPSHOT_FRESH_SECONDS
        router_counts = self.router is not None and "device_counters" in self.router.capabilities
        mode = "gateway" if gateway else "router" if router_counts else "quota"

        if leases:
            with db.transaction(self.conn):
                # Make every device holding a lease visible, even before it uses the internet.
                for ip, lease in leases.items():
                    self._device_for_ip(ip, lease, neighbours, now)

        with self.live.lock:
            self.live.last_tick = now
            self.live.lease_count = len(leases)
            self.live.snapshot_at = snap.taken_at if snap else None
            self.live.mode = mode
        if mode == "router" and now >= self.next_router_counters:
            assert self.router is not None
            self.next_router_counters = now + self.router.poll_seconds
            self.tick_router(now)
            return
        if not gateway or snap is None or snap.taken_at == self.prev_snapshot_at:
            return

        table_reloaded = self.state.get("nft_handle") != snap.table_handle
        previous = {} if table_reloaded else self.state

        def delta(name: str, current: int) -> int:
            before = previous.get(name, 0)
            return current - before if current >= before else current  # counter restarted

        # Usage is booked on the identity that holds the IP; merged identities
        # are combined when reading, so a merge can always be undone.
        usage: dict[int, list[int]] = defaultdict(lambda: [0, 0])  # device -> [rx, tx]
        new_state: dict[str, int] = {"nft_handle": snap.table_handle}

        with db.transaction(self.conn):
            for direction, counters in (("down", snap.down), ("up", snap.up)):
                for ip, current in counters.items():
                    new_state[f"{direction}:{ip}"] = current
                    moved = delta(f"{direction}:{ip}", current)
                    if moved <= 0 or ip == self.settings.server_ip or not self._in_lan(ip):
                        continue
                    device_id = self._device_for_ip(ip, leases.get(ip), neighbours, now)
                    usage[device_id][0 if direction == "down" else 1] += moved

            for name, current, index in (("server_down", snap.server_down, 0), ("server_up", snap.server_up, 1)):
                new_state[name] = current
                moved = delta(name, current)
                if moved > 0:
                    usage[self.server_id][index] += moved

            stale = [name for name in self.state if name.startswith(("up:", "down:")) and name not in new_state]
            self._book(usage, new_state, stale, snap.taken_at)

        elapsed = snap.taken_at - self.prev_snapshot_at if self.prev_snapshot_at and not table_reloaded else None
        self.prev_snapshot_at = snap.taken_at
        self._set_rates(usage, elapsed)

    def tick_router(self, now: float) -> None:
        """Router mode: the router's running totals per MAC, booked as differences."""
        assert self.router is not None
        try:
            counters = self.router.device_counters()
        except (OSError, RouterError, ProviderError, ValueError) as exc:
            log.warning("reading the router's device counters failed: %s", exc)
            with self.live.lock:
                self.live.router_error = str(exc)
            return
        elapsed = now - self.prev_router_counters_at if self.prev_router_counters_at else None
        cap = elapsed * self.max_bytes_per_sec if elapsed else None
        usage: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        new_state: dict[str, int] = {}
        with db.transaction(self.conn):
            for counter in counters:
                mac = counter.mac.lower()
                device_id = self._device_for_mac(mac, counter.ip, counter.hostname, now)
                for index, direction, current in ((0, "rdown", counter.rx), (1, "rup", counter.tx)):
                    name = f"{direction}:{mac}"
                    new_state[name] = current
                    if name not in self.state:
                        continue  # first sight: the total so far was before Tattle watched
                    moved = counter_delta(self.state[name], current, self.router.device_modulus)
                    if cap is not None and moved > cap:
                        continue  # a counter that jumped like this restarted mid-way; skip it
                    usage[device_id][index] += moved
            # Absent devices keep their last totals: one that comes back with a lower
            # total has restarted counting, and what it shows is all new.
            self._book(usage, new_state, [], now)
        self.prev_router_counters_at = now
        self._set_rates(usage, elapsed)
        with self.live.lock:
            self.live.router_polled_at = now
            self.live.router_error = None

    def _book(self, usage: dict[int, list[int]], new_state: dict[str, int], stale: list[str], at: float) -> None:
        """Hourly rows, presence and the running totals; call inside a transaction."""
        new_state["attributed_rx"] = self.state.get("attributed_rx", 0) + sum(v[0] for v in usage.values())
        new_state["attributed_tx"] = self.state.get("attributed_tx", 0) + sum(v[1] for v in usage.values())
        hour = int(at // 3600 * 3600)
        for device_id, (rx, tx) in usage.items():
            if rx <= 0 and tx <= 0:
                continue
            self.conn.execute(
                "INSERT INTO usage_hourly(device_id, hour, rx, tx) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(device_id, hour) DO UPDATE SET rx = rx + excluded.rx, tx = tx + excluded.tx",
                (device_id, hour, rx, tx),
            )
            self.conn.execute(
                "UPDATE devices SET last_active = ?, last_seen = MAX(last_seen, ?) WHERE id = ?",
                (int(at), int(at), device_id),
            )
            if tx > 0:
                # Presence comes from what the device sends: after a phone moves to
                # another network, servers keep sending to its old address for a
                # few minutes, but nothing is sent from it.
                self._extend_session(device_id, int(at))
        self.conn.executemany("DELETE FROM counter_state WHERE name = ?", [(n,) for n in stale])
        self.conn.executemany(
            "INSERT INTO counter_state(name, value) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            list(new_state.items()),
        )
        for name in stale:
            self.state.pop(name, None)
        self.state.update(new_state)

    def _set_rates(self, usage: dict[int, list[int]], elapsed: float | None) -> None:
        with self.live.lock:
            if elapsed and elapsed > 0:
                self.live.rates = {d: (rx / elapsed, tx / elapsed) for d, (rx, tx) in usage.items()}
            else:
                self.live.rates = {}

    def _extend_session(self, device_id: int, at: int) -> None:
        current = self.sessions.get(device_id)
        if current and 0 <= at - current[2] <= SESSION_GAP_SECONDS:
            self.conn.execute("UPDATE device_sessions SET end = ? WHERE id = ?", (at, current[0]))
            self.sessions[device_id] = (current[0], current[1], at)
        elif not current or at > current[2]:
            cur = self.conn.execute(
                "INSERT INTO device_sessions(device_id, start, end) VALUES(?, ?, ?)", (device_id, at, at)
            )
            self.sessions[device_id] = (int(cur.lastrowid), at, at)

    # --- identity --------------------------------------------------------------
    def ingest_identity_hints(self) -> None:
        """DHCP fingerprints written by the dnsmasq hook, and Bonjour names."""
        now = time.time()
        updates: list[tuple[str, dict]] = []
        try:
            entries = list(os.scandir(self.settings.dhcp_events_dir))
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            try:
                mtime = entry.stat().st_mtime
                if self.dhcp_seen.get(entry.name) == mtime:
                    continue
                with open(entry.path) as fh:
                    record = json.load(fh)
                self.dhcp_seen[entry.name] = mtime
                updates.append((entry.name, record))
            except (OSError, ValueError):
                continue

        mdns: list[tuple[str, identity.MdnsFacts]] = []
        if self.mdns:
            while True:
                try:
                    mdns.append(self.mdns.results.get_nowait())
                except queue.Empty:
                    break
        if not updates and not mdns:
            return

        with db.transaction(self.conn):
            for _, record in updates:
                mac = str(record.get("mac", "")).lower()
                client_id = record.get("client_id")
                keys = [f"id:{client_id}"] if client_id else [f"id:01:{mac}", f"mac:{mac}"]
                row = None
                for key in keys:
                    row = self.conn.execute("SELECT id, model FROM devices WHERE key = ?", (key,)).fetchone()
                    if row:
                        break
                if row is None:
                    continue
                options, vendor = record.get("options"), record.get("vendor_class")
                self.conn.execute(
                    "UPDATE devices SET fingerprint = COALESCE(?, fingerprint), "
                    "os_hint = COALESCE(?, os_hint) WHERE id = ?",
                    (identity.fingerprint(options, vendor), identity.os_hint(options, vendor, row["model"]), row["id"]),
                )
                if record.get("hostname"):
                    self._record_name(row["id"], "dhcp", str(record["hostname"])[:64], now)
            for source_ip, facts in mdns:
                if not self._in_lan(source_ip):
                    continue
                device_id = self._device_for_ip(source_ip, self.leases_by_ip.get(source_ip), self.neighbours, now)
                for name in facts.names:
                    self._record_name(device_id, "mdns", name, now)
                if facts.model:
                    self.conn.execute(
                        "UPDATE devices SET model = ?, os_hint = COALESCE(?, os_hint) WHERE id = ?",
                        (facts.model[:40], identity.os_hint(None, None, facts.model), device_id),
                    )

    def load_sessions(self, now: int) -> dict[int, list[tuple[int, int]]]:
        """Presence stretches per identity over the look-back window."""
        sessions: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for row in self.conn.execute(
            "SELECT device_id, start, end FROM device_sessions WHERE end >= ? ORDER BY start",
            (now - identity.LOOKBACK_SECONDS,),
        ):
            sessions[row["device_id"]].append((row["start"], row["end"]))
        return sessions

    def load_identities(self, sessions: dict[int, list[tuple[int, int]]]) -> dict[int, identity.Identity]:
        """Shown devices with what is known about them: names, kind, combined presence."""
        roots = db.device_roots(self.conn)
        identities: dict[int, identity.Identity] = {}
        for row in self.conn.execute(
            "SELECT id, mac, custom_name, first_seen, os_hint, fingerprint FROM devices "
            "WHERE merged_into IS NULL AND key NOT LIKE 'ip:%' AND key != ?",
            (SERVER_KEY,),
        ):
            identities[row["id"]] = identity.Identity(
                row["id"], row["mac"], row["custom_name"], row["first_seen"], row["os_hint"], row["fingerprint"]
            )
        for row in self.conn.execute("SELECT device_id, value, norm FROM device_names"):
            target = identities.get(roots.get(row["device_id"], row["device_id"]))
            if target and row["norm"]:
                target.names.setdefault(row["norm"], row["value"])
        members: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for device_id, spans in sessions.items():
            members[roots.get(device_id, device_id)].extend(spans)
        for root, spans in members.items():
            if root in identities:
                identities[root].sessions = identity.merge_intervals(spans)
        return identities

    def link_identities(self) -> None:
        """Keep "which identities are one device" right without anyone's help.

        First undo automatic merges that turned out wrong (the identities were
        online together), then merge identities with strong evidence, and keep
        weaker candidates as suggestions. Merges made by the admin are never
        undone here.
        """
        now = int(time.time())
        sessions = self.load_sessions(now)

        with db.transaction(self.conn):
            self._split_contradicted_merges(sessions, now)

        identities = self.load_identities(sessions)

        # Pairs known to be different devices (dismissed, split by the admin, or
        # seen online together after an automatic merge).
        blocked = {
            (row["a_id"], row["b_id"])
            for row in self.conn.execute("SELECT a_id, b_id FROM merge_suggestions WHERE status = 'dismissed'")
        }
        links = [
            link
            for link in identity.find_links(list(identities.values()), now)
            if (min(link.a.id, link.b.id), max(link.a.id, link.b.id)) not in blocked
        ]
        merged_away: set[int] = set()
        with db.transaction(self.conn):
            for link in sorted(links, key=lambda item: -item.score):
                keep, fold = identity.merge_target(link.a, link.b)
                if link.automatic:
                    if keep.id in merged_away or fold.id in merged_away:
                        continue  # re-evaluated next round with the new grouping
                    self.conn.execute("UPDATE devices SET merged_into = ? WHERE merged_into = ?", (keep.id, fold.id))
                    self.conn.execute(
                        "UPDATE devices SET merged_into = ?, merge_reason = ?, merged_at = ? WHERE id = ?",
                        (keep.id, link.reason, now, fold.id),
                    )
                    self._log_identity_event(now, "merged", fold.id, keep.id, link.reason)
                    merged_away.add(fold.id)
                    log.info("merged device %s into %s: %s", fold.id, keep.id, link.reason)
                    continue
                a_id, b_id = sorted((keep.id, fold.id))
                self.conn.execute(
                    "INSERT INTO merge_suggestions(a_id, b_id, reason, score, created, updated) VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(a_id, b_id) DO UPDATE SET reason = excluded.reason, score = excluded.score, "
                    "updated = excluded.updated WHERE merge_suggestions.status = 'open'",
                    (a_id, b_id, link.reason, link.score, now, now),
                )
            current = {(min(link.a.id, link.b.id), max(link.a.id, link.b.id)) for link in links if not link.automatic}
            for row in self.conn.execute("SELECT id, a_id, b_id FROM merge_suggestions WHERE status = 'open'").fetchall():
                if (row["a_id"], row["b_id"]) not in current:
                    self.conn.execute("DELETE FROM merge_suggestions WHERE id = ?", (row["id"],))

    def _split_contradicted_merges(self, sessions: dict[int, list[tuple[int, int]]], now: int) -> None:
        """An identity merged automatically that was online at the same time as the
        rest of its device is a different device: split it off for good."""
        rows = self.conn.execute(
            "SELECT id, merged_into, merge_reason FROM devices WHERE merged_into IS NOT NULL"
        ).fetchall()
        roots = db.device_roots(self.conn)
        groups: dict[int, list[int]] = defaultdict(list)
        for device_id, root in roots.items():
            groups[root].append(device_id)
        for row in rows:
            if (row["merge_reason"] or "").startswith(ADMIN_MERGE_REASON):
                continue  # a person decided this one
            root = roots[row["id"]]
            rest = identity.merge_intervals(
                [span for other in groups[root] if other != row["id"] for span in sessions.get(other, [])]
            )
            together = identity.overlap_seconds(identity.merge_intervals(sessions.get(row["id"], [])), rest)
            if together <= identity.SPLIT_OVERLAP:
                continue
            reason = f"Online at the same time as the rest of the device for {together // 60 or 1} min"
            self.conn.execute(
                "UPDATE devices SET merged_into = NULL, merge_reason = NULL, merged_at = NULL WHERE id = ?", (row["id"],)
            )
            a_id, b_id = sorted((row["id"], root))
            self.conn.execute(
                "INSERT INTO merge_suggestions(a_id, b_id, reason, score, status, created, updated) "
                "VALUES(?, ?, ?, 0, 'dismissed', ?, ?) "
                "ON CONFLICT(a_id, b_id) DO UPDATE SET status = 'dismissed', reason = excluded.reason, updated = excluded.updated",
                (a_id, b_id, reason, now, now),
            )
            self._log_identity_event(now, "split", row["id"], root, reason)
            log.info("split device %s from %s: %s", row["id"], root, reason)

    def _log_identity_event(self, now: int, kind: str, device_id: int, other_id: int, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO identity_events(ts, kind, device_id, other_id, reason) VALUES(?, ?, ?, ?, ?)",
            (now, kind, device_id, other_id, reason),
        )

    # --- router --------------------------------------------------------------
    def _load_router(self) -> None:
        """The router adapter chosen in Settings; rebuilt when the settings change."""
        raw = db.get_setting(self.conn, ROUTER_SETTING)
        if raw == self.router_config:
            return
        self.router_config = raw
        self.router = None
        self.prev_router_counters_at = None
        name = None
        if raw:
            try:
                config = json.loads(raw)
                self.router = routers.create(config["adapter"], config.get("values") or {})
                name = self.router.name
            except (KeyError, ValueError, RouterError) as exc:
                log.warning("router settings not usable: %s", exc)
                with self.live.lock:
                    self.live.router_error = str(exc)
        with self.live.lock:
            self.live.router_name = name
            if self.router is None:
                self.live.router_polled_at = None
                self.live.router_dhcp_enabled = None
        self.next_router_poll = 0.0
        self.next_router_counters = 0.0
        self.next_dhcp_check = 0.0

    def poll_router(self) -> None:
        """What the adapter can tell besides device counters: the router's own
        internet counters, the line in use, the line speed, its DHCP server."""
        router = self.router
        assert router is not None
        now = time.time()
        try:
            if "wan_counters" in router.capabilities:
                self._poll_wan(router, now)
            dhcp_enabled = None
            if now >= self.next_dhcp_check:
                self.next_dhcp_check = now + DHCP_CHECK_SECONDS
                if "dhcp_enabled" in router.capabilities:
                    dhcp_enabled = router.dhcp_enabled()
                self._read_internet_login(router)
            with self.live.lock:
                if "device_counters" not in router.capabilities:
                    self.live.router_polled_at = now
                self.live.router_error = None
                if dhcp_enabled is not None:
                    self.live.router_dhcp_enabled = dhcp_enabled
                    self.live.router_dhcp_checked_at = now
        except (OSError, RouterError, ProviderError, ValueError, NotImplementedError) as exc:
            log.warning("router poll failed: %s", exc)
            with self.live.lock:
                self.live.router_error = str(exc)

    def _poll_wan(self, router: RouterAdapter, now: float) -> None:
        rx, tx = router.wan_counters()
        prev_ts = self.state.get("router_ts")
        if prev_ts is not None and 0 < now - prev_ts <= MAX_ROUTER_GAP_SECONDS:
            cap = (now - prev_ts) * self._line_bytes_per_sec()
            drx = counter_delta(self.state["router_rx"], rx, router.wan_modulus)
            dtx = counter_delta(self.state["router_tx"], tx, router.wan_modulus)
            if drx > cap or dtx > cap:  # router rebooted: counters restarted from zero
                drx = rx if rx <= cap else 0
                dtx = tx if tx <= cap else 0
            # What Tattle booked over the same interval (the tick right before this poll).
            dev_rx = self.state.get("attributed_rx", 0) - self.state.get("router_attr_rx", 0)
            dev_tx = self.state.get("attributed_tx", 0) - self.state.get("router_attr_tx", 0)
            hour = int(now // 3600 * 3600)
            self.conn.execute(
                "INSERT INTO wan_hourly(hour, rx, tx, dev_rx, dev_tx) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(hour) DO UPDATE SET rx = rx + excluded.rx, tx = tx + excluded.tx, "
                "dev_rx = dev_rx + excluded.dev_rx, dev_tx = dev_tx + excluded.dev_tx",
                (hour, drx, dtx, max(0, dev_rx), max(0, dev_tx)),
            )
        new_state = {
            "router_rx": rx,
            "router_tx": tx,
            "router_ts": int(now),
            "router_attr_rx": self.state.get("attributed_rx", 0),
            "router_attr_tx": self.state.get("attributed_tx", 0),
        }
        self.conn.executemany(
            "INSERT INTO counter_state(name, value) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            list(new_state.items()),
        )
        self.state.update(new_state)

    def _line_bytes_per_sec(self) -> float:
        """The most the internet line can carry: its sync speed when the router
        reports it (with headroom), else TATTLE_MAX_MBPS. Above it, a counter that
        went down restarted rather than wrapped."""
        raw = db.get_setting(self.conn, isp.ROUTER_LINK_SETTING)
        try:
            link = json.loads(raw) if raw else {}
            kbps = max(link.get("down_kbps") or 0, link.get("up_kbps") or 0)
        except (ValueError, AttributeError, TypeError):
            kbps = 0
        return kbps * 1000 / 8 * 1.5 if kbps else self.max_bytes_per_sec

    def _read_internet_login(self, router: RouterAdapter) -> None:
        """Which line the router uses now: with two lines on one connection, the
        house switches by changing the router's internet login."""
        if "wan_login" in router.capabilities:
            try:
                login = router.wan_login()
            except (OSError, RouterError, ProviderError, ValueError) as exc:
                log.warning("reading the router's internet login failed: %s", exc)
            else:
                if login and login != db.get_setting(self.conn, isp.ROUTER_LOGIN_SETTING):
                    log.info("router now signs in to the internet as %s", login)
                    db.set_setting(self.conn, isp.ROUTER_LOGIN_SETTING, login)
                    isp.record_switch(self.conn, isp.live_line(self.conn)[0], "router")
                with self.live.lock:
                    self.live.router_internet_login = login
        if "link_status" in router.capabilities:
            try:
                link = router.link_status()
            except (OSError, RouterError, ProviderError, ValueError) as exc:
                log.warning("reading the router's line status failed: %s", exc)
                return
            db.set_setting(self.conn, isp.ROUTER_LINK_SETTING, json.dumps({**link, "ts": int(time.time())}))
