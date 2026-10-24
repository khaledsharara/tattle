"""gateway.json: the gateway's health, for the dashboard's health panel and `status`.

Written atomically every ~30 s and whenever something important changes.
Every "detail" is a plain sentence that tells a non-expert what to do.
Shape (version 1)::

    {"version": 1, "updated": <unix>, "state": "running"|"stopped"|"refused"|"off",
     "lan_if": str, "server_ip": str, "router_ip": str,
     "preflight": [{"id": str, "ok": bool, "detail": str}],
     "forwarding": {"ok": bool|null, "detail": str},
     "docker_forward": {"ok": bool|null, "backend": str|null, "chain": str|null, "detail": str},
     "dhcp": {"leases": int, "foreign_servers": [{"ip": str, "mac": str, "seen": <unix>}],
              "probed_at": <unix>|null, "detail": str},
     "ipv6_adverts": {"seen": bool|null, "routers": [str], "checked_at": <unix>|null, "detail": str}}
"""

from __future__ import annotations

import json
import threading
import time

from .paths import atomic_write
from .preflight import Check

VERSION = 1
FOREIGN_TTL = 3600  # forget a foreign DHCP server an hour after it last answered


class GatewayStatus:
    def __init__(self, path: str, lan_if: str = "", server_ip: str = "", router_ip: str = ""):
        self.path = path
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.state = "starting"
        self.lan_if, self.server_ip, self.router_ip = lan_if, server_ip, router_ip
        self.preflight: list[Check] = []
        self.forwarding: dict = {"ok": None, "detail": "Not checked yet."}
        self.docker_forward: dict = {"ok": None, "backend": None, "chain": None, "detail": "Not checked yet."}
        self.leases = 0
        self.foreign: dict[str, dict] = {}  # server ip -> {"ip", "mac", "seen"}
        self.probed_at: float | None = None
        self.dhcp_detail = "Not checked yet."
        self.ipv6: dict = {"seen": None, "routers": [], "checked_at": None, "detail": "Not checked yet."}
        self.foreign_ttl = FOREIGN_TTL

    # --- updates (any thread) -----------------------------------------------------------

    def set(self, **values) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def saw_foreign_server(self, ip: str, mac: str, now: float | None = None) -> bool:
        """Record another DHCP server; True when it is new (worth writing out now)."""
        with self.lock:
            new = ip not in self.foreign
            self.foreign[ip] = {"ip": ip, "mac": mac, "seen": int(now or time.time())}
            return new

    def probed(self, now: float | None = None) -> None:
        with self.lock:
            self.probed_at = now or time.time()

    # --- output ------------------------------------------------------------------------------

    def as_dict(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self.lock:
            self.foreign = {ip: s for ip, s in self.foreign.items() if now - s["seen"] < self.foreign_ttl}
            foreign = sorted(self.foreign.values(), key=lambda s: s["ip"])
            if foreign:
                names = ", ".join(f"{s['ip']} ({s['mac']})" for s in foreign)
                dhcp_detail = (
                    f"Another DHCP server is handing out addresses on this network: {names}. Devices that get an "
                    "address from it skip Tattle and aren't counted. Turn off DHCP on the router, and set every Wi-Fi "
                    "extender's DHCP server to Off (not Auto)."
                )
            else:
                dhcp_detail = self.dhcp_detail
            return {
                "version": VERSION,
                "updated": int(now),
                "state": self.state,
                "lan_if": self.lan_if,
                "server_ip": self.server_ip,
                "router_ip": self.router_ip,
                "preflight": [c.as_dict() for c in self.preflight],
                "forwarding": dict(self.forwarding),
                "docker_forward": dict(self.docker_forward),
                "dhcp": {
                    "leases": self.leases,
                    "foreign_servers": foreign,
                    "probed_at": int(self.probed_at) if self.probed_at else None,
                    "detail": dhcp_detail,
                },
                "ipv6_adverts": {**self.ipv6, "routers": list(self.ipv6.get("routers", []))},
            }

    def write(self) -> None:
        """One writer at a time, so an older picture never replaces a newer one."""
        with self.write_lock:
            atomic_write(self.path, json.dumps(self.as_dict(), indent=1) + "\n")


def read(path: str) -> dict | None:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
