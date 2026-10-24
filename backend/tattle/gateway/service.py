"""`run`: one process that sets the gateway up and keeps it that way.

It supervises dnsmasq and a few worker threads. If dnsmasq exits or a worker
dies unexpectedly, the whole process exits non-zero and Docker restarts the
container. On SIGTERM it stops dnsmasq but leaves the nftables table and the
Docker exception in place: devices keep routing (and being counted) through
their current leases, so an upgrade or restart never cuts the internet.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import random
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from . import dhcplog, dnsmasq, dockerfw, nft, preflight, probe, status
from .config import GatewayConfig
from .paths import Paths, atomic_write
from .status import GatewayStatus

log = logging.getLogger("tattle.gateway")

EXPORT_SECONDS = 5
MAINTAIN_SECONDS = 60
STATUS_SECONDS = 30
PROBE_LISTEN_SECONDS = 3.0
FIRST_PROBE_SECONDS = 10.0
RESUME_SECONDS = 900  # a report this recent survives a restart
LINE_QUEUE = 5000  # log lines waiting for the fingerprint worker; beyond this they're dropped


class Gateway:
    def __init__(self, cfg: GatewayConfig, paths: Paths):
        self.cfg, self.paths = cfg, paths
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.force_probe = threading.Event()
        self.table = nft.Table(cfg)
        self.status = GatewayStatus(paths.status, cfg.lan_if, str(cfg.server_ip), str(cfg.router_ip))
        self.status.foreign_ttl = max(self.status.foreign_ttl, 3 * cfg.probe_interval)
        self.store = dhcplog.FingerprintStore(paths.dhcp_dir)
        self.forwarding = nft.ForwardingTest()
        self.lines: queue.Queue[str] = queue.Queue(maxsize=LINE_QUEUE)
        self.dropped_lines = 0
        self.latest: dict | None = None  # last exported listing, for the forwarding self-test
        self.leases_mtime = 0.0
        self.dnsmasq: subprocess.Popen | None = None
        self.threads: list[threading.Thread] = []

    # --- lifecycle ---------------------------------------------------------------------

    def run(self) -> int:
        self.paths.create()
        self.resume_status()
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        signal.signal(signal.SIGUSR1, lambda *_: self.force_probe.set())

        checks = preflight.run_checks(self.cfg)
        self.status.set(preflight=checks)
        if failed := [c for c in checks if not c.ok]:
            return self.refuse(failed)
        try:
            state = self.table.ensure(before_reload=self.export_listing)
        except nft.NftError as exc:
            detail = f"Couldn't load Tattle's firewall rules: {exc}"
            return self.refuse([preflight.Check("nftables", False, detail)])
        log.info("Firewall table %s", {"kept": "kept with its counters", "created": "created", "reloaded": "updated"}[state])
        self.update_docker_forward()
        self.export_snapshot()

        atomic_write(self.paths.dnsmasq_conf, dnsmasq.render(self.cfg, self.paths))
        self.dnsmasq = dnsmasq.start(self.paths.dnsmasq_conf)
        self.spawn("dnsmasq-log", self.read_dnsmasq_log)
        self.spawn("fingerprints", self.record_fingerprints)
        self.spawn("exporter", self.every(EXPORT_SECONDS, self.export_and_sync))
        self.spawn("maintenance", self.every(MAINTAIN_SECONDS, self.maintain, first_delay=MAINTAIN_SECONDS))
        self.spawn("status", self.every(STATUS_SECONDS, self.write_status))
        self.spawn("dhcp-watch", self.watch_dhcp_servers)
        self.spawn("prober", self.probe_loop)
        self.status.set(state="running")
        self.write_status()
        log.info("Gateway running on %s (%s); handing out %s-%s", self.cfg.lan_if, self.cfg.server_ip, self.cfg.dhcp_start, self.cfg.dhcp_end)

        code = 0
        while not self.stop.is_set():
            if self.dnsmasq.poll() is not None:
                log.error("dnsmasq exited with code %s; exiting so Docker restarts the gateway", self.dnsmasq.returncode)
                code = 1
                break
            if self.failed.is_set():
                code = 1
                break
            self.stop.wait(1)
        self.shutdown(code)
        return code

    def resume_status(self) -> None:
        """Keep the last verdicts across a restart, so the health panel doesn't go blank for minutes."""
        previous = status.read(self.paths.status)
        if not previous or time.time() - previous.get("updated", 0) > RESUME_SECONDS:
            return
        forwarding = previous.get("forwarding") or {}
        if forwarding.get("ok") is not None:
            self.forwarding.ok, self.forwarding.detail = forwarding["ok"], str(forwarding.get("detail", ""))
            self.status.set(forwarding={"ok": self.forwarding.ok, "detail": self.forwarding.detail})
        if (previous.get("ipv6_adverts") or {}).get("checked_at"):
            self.status.set(ipv6=previous["ipv6_adverts"])
        for server in (previous.get("dhcp") or {}).get("foreign_servers") or []:
            self.status.saw_foreign_server(str(server["ip"]), str(server["mac"]), server["seen"])

    def refuse(self, failed: list[preflight.Check]) -> int:
        """Explain, record it for the dashboard, and exit non-zero (Docker retries later)."""
        for check in failed:
            print(f"Tattle gateway can't start: {check.detail}", file=sys.stderr, flush=True)
        known = {c.id for c in self.status.preflight}
        self.status.set(state="refused", preflight=[*self.status.preflight, *(c for c in failed if c.id not in known)])
        self.write_status()
        return 2

    def shutdown(self, code: int) -> None:
        self.stop.set()
        self.force_probe.set()  # wake the prober so it sees `stop`
        if self.dnsmasq is not None and self.dnsmasq.poll() is None:
            self.dnsmasq.terminate()
            try:
                self.dnsmasq.wait(5)
            except subprocess.TimeoutExpired:
                self.dnsmasq.kill()
        self.export_snapshot()
        self.status.set(state="stopped" if code == 0 else "failed")
        self.write_status()
        log.info("Gateway stopped; firewall rules left in place so devices stay online")

    # --- threads -----------------------------------------------------------------------

    def spawn(self, name: str, target: Callable[[], None]) -> None:
        def body() -> None:
            try:
                target()
            except Exception:
                log.exception("Worker %s crashed", name)
                self.failed.set()

        thread = threading.Thread(target=body, name=name, daemon=True)
        self.threads.append(thread)
        thread.start()

    def every(self, seconds: float, task: Callable[[], None], first_delay: float = 0) -> Callable[[], None]:
        """Run `task` periodically; expected trouble (commands failing) is logged, not fatal."""

        def loop() -> None:
            if self.stop.wait(first_delay):
                return
            while True:
                try:
                    task()
                except (OSError, subprocess.SubprocessError, nft.NftError) as exc:
                    log.warning("%s failed: %s", getattr(task, "__name__", "task"), exc)
                if self.stop.wait(seconds):
                    return

        return loop

    def read_dnsmasq_log(self) -> None:
        """Copy dnsmasq's output to our log and queue it for fingerprinting; never blocks dnsmasq."""
        assert self.dnsmasq is not None and self.dnsmasq.stdout is not None
        for line in self.dnsmasq.stdout:
            text = dhcplog.strip_prefix(line)
            detail = dhcplog.LINE.match(text)
            if not detail or detail.group(2).startswith("DHCP"):
                print(f"dnsmasq: {text}", flush=True)
            try:
                self.lines.put_nowait(line)
            except queue.Full:
                self.dropped_lines += 1

    def record_fingerprints(self) -> None:
        parser = dhcplog.FingerprintParser()
        while not self.stop.is_set():
            try:
                line = self.lines.get(timeout=1)
            except queue.Empty:
                continue
            if fp := parser.feed(line):
                try:
                    self.store.add_fingerprint(fp)
                except OSError as exc:
                    log.warning("Couldn't save the DHCP fingerprint of %s: %s", fp.mac, exc)

    # --- periodic work -----------------------------------------------------------------

    def export_listing(self, listing: dict) -> None:
        """Called with the old table's contents just before it is replaced."""
        atomic_write(self.paths.nft_snapshot, json.dumps(listing))

    def export_snapshot(self) -> None:
        snapshot = self.table.snapshot()
        if snapshot is None:
            return  # table gone (e.g. `systemctl restart nftables`); maintenance puts it back
        raw, self.latest = snapshot
        atomic_write(self.paths.nft_snapshot, raw)

    def export_and_sync(self) -> None:
        self.export_snapshot()
        try:
            mtime = os.stat(self.paths.leases).st_mtime
        except OSError:
            return
        if mtime != self.leases_mtime:  # a lease changed: record its address and client id now
            self.leases_mtime = mtime
            leases = dhcplog.read_leases(self.paths.leases)
            self.status.set(leases=dhcplog.active_leases(leases, time.time()))
            self.store.sync_leases(leases)

    def update_docker_forward(self) -> None:
        state = dockerfw.apply(self.cfg.lan_if)
        if state.ok is False:
            log.warning("%s", state.detail)
        self.status.set(docker_forward=state.as_dict())

    def maintain(self) -> None:
        state = self.table.ensure(before_reload=self.export_listing)
        if state != "kept":
            log.warning("Firewall table was missing or changed; %s it", state)
            self.export_snapshot()
        self.update_docker_forward()
        self.forwarding.update(nft.named_counters(self.latest))
        docker = self.status.docker_forward
        if docker.get("ok") is False:
            self.status.set(forwarding={"ok": False, "detail": docker["detail"]})
        else:
            self.status.set(forwarding={"ok": self.forwarding.ok, "detail": self.forwarding.detail})

    def write_status(self) -> None:
        leases = dhcplog.read_leases(self.paths.leases)
        self.status.set(leases=dhcplog.active_leases(leases, time.time()))
        try:
            self.store.sync_leases(leases)
        except OSError as exc:
            log.warning("Couldn't update DHCP records: %s", exc)
        self.status.write()

    # --- probes ---------------------------------------------------------------------------

    def watch_dhcp_servers(self) -> None:
        """Listen for other DHCP servers' replies; reopen the socket if the link goes away (cable out)."""
        while not self.stop.is_set():
            try:
                with probe.open_dhcp_listener(self.cfg.lan_if) as sock:
                    self.listen_for_dhcp_servers(sock)
            except OSError as exc:
                self.status.set(dhcp_detail=f"Couldn't watch for other DHCP servers ({exc.strerror or exc}).")
                log.warning("DHCP watch: %s; retrying in 30 s", exc)
                self.stop.wait(30)

    def listen_for_dhcp_servers(self, sock) -> None:
        server = str(self.cfg.server_ip)
        while not self.stop.is_set():
            try:
                frame, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            if len(addr) > 2 and addr[2] == probe.PACKET_OUTGOING:
                continue  # our own dnsmasq's replies
            reply = probe.parse_dhcp_reply(frame)
            if reply is None or reply.kind not in (probe.DHCP_OFFER, probe.DHCP_ACK, probe.DHCP_NAK):
                continue
            if reply.server == server:
                continue
            if self.status.saw_foreign_server(reply.server, reply.src_mac):
                log.warning("Another DHCP server answered: %s (%s)", reply.server, reply.src_mac)
                self.status.write()

    def probe_loop(self) -> None:
        try:
            mac = probe.interface_mac(self.cfg.lan_if)
        except (OSError, ValueError) as exc:
            self.status.set(dhcp_detail=f"Couldn't read {self.cfg.lan_if}'s MAC address ({exc}), so no DHCP probes are sent.")
            return
        delay = FIRST_PROBE_SECONDS
        while True:
            self.force_probe.wait(delay)
            if self.stop.is_set():
                return
            self.force_probe.clear()
            self.probe_dhcp(mac)
            self.probe_ipv6(mac)
            self.status.write()
            delay = self.cfg.probe_interval * random.uniform(0.9, 1.1)

    def probe_dhcp(self, mac: bytes) -> None:
        try:
            probe.send_frame(self.cfg.lan_if, probe.dhcp_discover(mac, probe.new_xid()))
        except OSError as exc:
            self.status.set(dhcp_detail=f"Couldn't send a DHCP probe ({exc.strerror or exc}).")
            return
        if self.stop.wait(PROBE_LISTEN_SECONDS):
            return
        self.status.probed()
        self.status.set(dhcp_detail="No other DHCP server answered: only Tattle hands out addresses.")

    def probe_ipv6(self, mac: bytes) -> None:
        now = int(time.time())
        if probe.ipv6_disabled(self.cfg.lan_if):
            self.status.set(
                ipv6={
                    "seen": None,
                    "routers": [],
                    "checked_at": now,
                    "detail": "IPv6 is turned off on this server, so Tattle can't check whether the router announces "
                    "IPv6. If devices still get IPv6 from the router, that traffic isn't counted.",
                }
            )
            return
        try:
            adverts = probe.solicit_routers(self.cfg.lan_if, mac, PROBE_LISTEN_SECONDS, time.monotonic)
        except OSError as exc:
            self.status.set(
                ipv6={"seen": None, "routers": [], "checked_at": now, "detail": f"Couldn't check for IPv6 routers ({exc.strerror or exc})."}
            )
            return
        routers = sorted(src for src, advert in adverts.items() if advert.lifetime > 0)
        if routers:
            detail = (
                f"A router announces IPv6 ({', '.join(routers)}). Devices can send IPv6 traffic straight to it, and "
                "Tattle only counts IPv4. Turn off IPv6 (or its router advertisements) in the router's settings, or "
                "set TATTLE_FILTER_AAAA=1."
            )
        else:
            detail = "No router announces IPv6 here, so all internet traffic goes through Tattle."
        self.status.set(ipv6={"seen": bool(routers), "routers": routers, "checked_at": now, "detail": detail})
