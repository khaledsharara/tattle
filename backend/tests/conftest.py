from __future__ import annotations

import ipaddress
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tattle.config import Settings


class HostFiles:
    """Fake versions of the files the collector reads on the server."""

    def __init__(self, root: Path):
        self.root = root
        self.nft = root / "nft.json"
        self.leases = root / "dnsmasq.leases"
        self.arp = root / "arp"
        self.leases.write_text("")
        self.arp.write_text("IP address HW type Flags HW address Mask Device\n")
        self._clock = time.time() - 600  # snapshots are written "now", 10 s apart

    @property
    def clock(self) -> float:
        return self._clock

    def write_counters(self, up: dict[str, int], down: dict[str, int], server=(0, 0), handle=7, advance=10.0):
        def elems(values):
            return [{"elem": {"val": ip, "counter": {"packets": 1, "bytes": b}}} for ip, b in values.items()]

        data = {
            "nftables": [
                {"metainfo": {"version": "1.0.9"}},
                {"table": {"family": "inet", "name": "tattle", "handle": handle}},
                {"set": {"family": "inet", "name": "dev_up", "table": "tattle", "elem": elems(up)}},
                {"set": {"family": "inet", "name": "dev_down", "table": "tattle", "elem": elems(down)}},
                {"counter": {"family": "inet", "name": "server_up", "packets": 1, "bytes": server[0]}},
                {"counter": {"family": "inet", "name": "server_down", "packets": 1, "bytes": server[1]}},
            ]
        }
        self.nft.write_text(json.dumps(data))
        self._clock += advance
        os.utime(self.nft, (self._clock, self._clock))
        return self._clock

    def write_leases(self, *leases: tuple[str, str, str | None, str | None]):
        lines = [f"1791800000 {mac} {ip} {host or '*'} {cid or '*'}" for mac, ip, host, cid in leases]
        self.leases.write_text("\n".join(lines) + "\n")
        stamp = self.leases.stat().st_mtime + 1
        os.utime(self.leases, (stamp, stamp))


@pytest.fixture
def host(tmp_path: Path) -> HostFiles:
    return HostFiles(tmp_path)


@pytest.fixture
def settings(tmp_path: Path, host: HostFiles) -> Settings:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>t</title>")
    return Settings(
        db_path=str(tmp_path / "tattle.db"),
        nft_snapshot_path=str(host.nft),
        leases_path=str(host.leases),
        arp_path=str(host.arp),
        oui_path=str(tmp_path / "manuf-missing"),
        dhcp_events_dir=str(tmp_path / "dhcp"),
        mdns_enabled=False,
        static_dir=str(static),
        lan_network=ipaddress.ip_network("192.168.1.0/24"),
        server_ip="192.168.1.10",
        gateway_status_path=str(tmp_path / "gateway.json"),
        admin_password="s3cret",
        timezone=ZoneInfo("Africa/Cairo"),
        poll_seconds=10,
        router_poll_seconds=60,
        max_mbps=10_000,
        host="127.0.0.1",
        port=0,
        demo=False,
        mode="",
    )


@pytest.fixture
def make_settings(settings: Settings):
    def _make(**changes) -> Settings:
        return replace(settings, **changes)

    return _make

