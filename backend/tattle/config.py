"""Settings from the environment (.env next to docker-compose.yml).

Everything has a default, and the network values are worked out from the
host's routing table when not given, so `docker compose up` works as is.
Router adapters and providers are set up in the web app, not here.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Settings:
    db_path: str
    nft_snapshot_path: str
    gateway_status_path: str
    leases_path: str
    arp_path: str
    oui_path: str
    dhcp_events_dir: str
    mdns_enabled: bool
    static_dir: str
    lan_network: ipaddress.IPv4Network
    server_ip: str
    admin_password: str
    timezone: ZoneInfo
    poll_seconds: float
    router_poll_seconds: float
    max_mbps: float  # faster than this between two readings means a counter restarted
    host: str
    port: int
    demo: bool
    mode: str  # what the installer set up: 'gateway' | 'router' | 'quota' ('' = work it out)

    @property
    def data_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.db_path))


def _default_route(path: str = "/proc/net/route") -> tuple[str, str] | None:
    """(interface, gateway IP) of the IPv4 default route."""
    try:
        with open(path) as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    return parts[0], socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except (OSError, ValueError):
        pass
    return None


def _local_network(interface: str, path: str = "/proc/net/route") -> ipaddress.IPv4Network | None:
    """The directly connected subnet on `interface`."""
    try:
        with open(path) as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) >= 8 and parts[0] == interface and parts[1] != "00000000" and parts[2] == "00000000":
                    net = socket.inet_ntoa(struct.pack("<L", int(parts[1], 16)))
                    mask = socket.inet_ntoa(struct.pack("<L", int(parts[7], 16)))
                    return ipaddress.ip_network(f"{net}/{mask}", strict=False)
    except (OSError, ValueError):
        pass
    return None


def _outgoing_ip(target: str) -> str | None:
    """The address this host uses to reach `target` (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((target, 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def load_settings() -> Settings:
    env = os.environ
    route = _default_route()
    server_ip = env.get("TATTLE_SERVER_IP") or _outgoing_ip(route[1] if route else "192.0.2.1") or "127.0.0.1"
    lan = env.get("TATTLE_LAN")
    network = ipaddress.ip_network(lan, strict=False) if lan else (_local_network(route[0]) if route else None)
    return Settings(
        db_path=env.get("TATTLE_DB", "/data/tattle.db"),
        nft_snapshot_path=env.get("TATTLE_NFT_SNAPSHOT", "/run/tattle/nft.json"),
        gateway_status_path=env.get("TATTLE_GATEWAY_STATUS", "/run/tattle/gateway.json"),
        leases_path=env.get("TATTLE_LEASES", "/var/lib/tattle/dnsmasq.leases"),
        arp_path=env.get("TATTLE_ARP", "/proc/net/arp"),
        oui_path=env.get("TATTLE_OUI", "/app/data/manuf"),
        dhcp_events_dir=env.get("TATTLE_DHCP_EVENTS", "/var/lib/tattle/dhcp"),
        mdns_enabled=env.get("TATTLE_MDNS", "1") == "1",
        static_dir=env.get("TATTLE_STATIC", "/app/static"),
        lan_network=network or ipaddress.ip_network(f"{server_ip}/24", strict=False),
        server_ip=server_ip,
        admin_password=env.get("TATTLE_ADMIN_PASSWORD", ""),
        timezone=_timezone(env.get("TZ")),
        poll_seconds=float(env.get("TATTLE_POLL_SECONDS", "10")),
        router_poll_seconds=float(env.get("TATTLE_ROUTER_POLL_SECONDS", "60")),
        max_mbps=float(env.get("TATTLE_MAX_MBPS", "1000")),
        host=env.get("TATTLE_HOST", "0.0.0.0"),
        port=int(env.get("TATTLE_PORT", "8090")),
        demo=env.get("TATTLE_DEMO", "0") == "1",
        mode=env.get("TATTLE_MODE", "").strip().lower(),
    )
