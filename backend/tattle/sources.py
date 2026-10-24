"""Readers for the host data the collector consumes.

* the nftables counter snapshot the gateway container writes (gateway mode)
* the dnsmasq lease file (who holds which IP)
* the kernel neighbour table (MACs of devices without a lease)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class CounterSnapshot:
    taken_at: float
    table_handle: int
    up: dict[str, int] = field(default_factory=dict)  # LAN IP -> bytes sent to the internet
    down: dict[str, int] = field(default_factory=dict)  # LAN IP -> bytes received from the internet
    server_up: int = 0
    server_down: int = 0


def read_counter_snapshot(path: str) -> CounterSnapshot | None:
    try:
        taken_at = os.stat(path).st_mtime
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None

    snap: CounterSnapshot | None = None
    for item in data.get("nftables", []):
        if "table" in item and item["table"].get("name") == "tattle":
            snap = CounterSnapshot(taken_at=taken_at, table_handle=int(item["table"]["handle"]))
    if snap is None:
        return None

    for item in data["nftables"]:
        if "set" in item and item["set"].get("name") in ("dev_up", "dev_down"):
            target = snap.up if item["set"]["name"] == "dev_up" else snap.down
            for entry in item["set"].get("elem", []):
                elem = entry.get("elem") if isinstance(entry, dict) else None
                if elem and isinstance(elem.get("val"), str) and elem.get("counter"):
                    target[elem["val"]] = int(elem["counter"]["bytes"])
        elif "counter" in item:
            name = item["counter"].get("name")
            if name == "server_up":
                snap.server_up = int(item["counter"]["bytes"])
            elif name == "server_down":
                snap.server_down = int(item["counter"]["bytes"])
    return snap


@dataclass(frozen=True)
class Lease:
    expires: int
    mac: str
    ip: str
    hostname: str | None
    client_id: str | None


class LeaseReader:
    """Re-reads the dnsmasq lease file only when it changes."""

    def __init__(self, path: str):
        self.path = path
        self._mtime = -1.0
        self._by_ip: dict[str, Lease] = {}

    def by_ip(self) -> dict[str, Lease]:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            return self._by_ip
        if mtime != self._mtime:
            leases: dict[str, Lease] = {}
            with open(self.path) as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 4 or parts[0] == "duid":
                        continue
                    hostname = None if parts[3] == "*" else parts[3]
                    client_id = parts[4] if len(parts) > 4 and parts[4] != "*" else None
                    leases[parts[2]] = Lease(int(parts[0]), parts[1].lower(), parts[2], hostname, client_id)
            self._by_ip = leases
            self._mtime = mtime
        return self._by_ip


def read_neighbours(path: str) -> dict[str, str]:
    """IP -> MAC for complete entries in /proc/net/arp."""
    result: dict[str, str] = {}
    try:
        with open(path) as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and parts[2] != "0x0" and parts[3] != "00:00:00:00:00:00":
                    result[parts[0]] = parts[3].lower()
    except OSError:
        pass
    return result
