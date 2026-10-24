"""Gateway settings: read from the environment (the installer writes them to
``.env``), with anything left out detected from the server's own network setup.

Every value ends up in an nftables ruleset or a dnsmasq config file, so each one
is validated strictly here: a stray newline must never become a config line.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network

IpRunner = Callable[..., list]  # ip("-4", "route", "show", "default") -> parsed `ip -j` output

DEFAULT_LEASE = "2h"
DEFAULT_LOCAL_NAME = "tattle.lan"
DEFAULT_PROBE_SECONDS = 600.0
RANGE_WINDOW = 200  # the default DHCP range comes from the top this-many addresses

_IFNAME = re.compile(r"^[A-Za-z0-9_.:@-]{1,15}$")
_LEASE = re.compile(r"^(?:\d+[smhdw]?|infinite)$")
_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


class ConfigError(Exception):
    """A setting is missing or wrong; the message says what to put in .env."""


@dataclass(frozen=True)
class GatewayConfig:
    lan_if: str
    server_ip: IPv4Address
    lan: IPv4Network
    router_ip: IPv4Address
    dhcp_start: IPv4Address
    dhcp_end: IPv4Address
    dhcp_lease: str = DEFAULT_LEASE
    dns: tuple[str, ...] = ()  # upstream servers dnsmasq forwards to
    filter_aaaa: bool = False
    local_name: str = DEFAULT_LOCAL_NAME
    skip_route_check: bool = False
    probe_interval: float = DEFAULT_PROBE_SECONDS


def run_ip(*args: str) -> list:
    """`ip -j ...` parsed; an empty list when ip prints nothing (e.g. no routes)."""
    out = subprocess.run(["ip", "-j", *args], capture_output=True, text=True, timeout=10, check=True).stdout
    return json.loads(out) if out.strip() else []


def default_route(routes: list, lan_if: str | None = None) -> dict | None:
    """The default route the kernel would pick (lowest metric), optionally on one interface."""
    candidates = [
        r
        for r in routes
        if r.get("dst") in ("default", "0.0.0.0/0") and r.get("gateway") and (lan_if is None or r.get("dev") == lan_if)
    ]
    return min(candidates, key=lambda r: r.get("metric", 0), default=None)


def ipv4_addresses(addrs: list, lan_if: str) -> list[dict]:
    """The interface's global IPv4 addresses, primary first (as `ip -j addr` lists them)."""
    result: list[dict] = []
    for link in addrs:
        if link.get("ifname") != lan_if:
            continue
        for info in link.get("addr_info", []):
            if info.get("family") == "inet" and info.get("scope", "global") == "global":
                result.append(info)
    return result


def default_dhcp_range(lan: IPv4Network, exclude: set[IPv4Address]) -> tuple[IPv4Address, IPv4Address]:
    """The longest run of free addresses near the top of the subnet.

    Routers usually sit at .1 and servers low in the range, and devices given a
    fixed address by hand tend to be low too, so the top ~200 are the safest.
    """
    first, last = int(lan.network_address) + 1, int(lan.broadcast_address) - 1
    if last - first < 8:
        raise ConfigError(f"The network {lan} is too small to hand out addresses. Set TATTLE_LAN to the home network, e.g. 192.168.1.0/24.")
    taken = {int(ip) for ip in exclude}
    best: tuple[int, int] | None = None
    start = None
    for n in range(max(first, last - RANGE_WINDOW + 1), last + 2):
        if n <= last and n not in taken:
            start = n if start is None else start
            continue
        if start is not None and (best is None or n - start >= best[1] - best[0] + 1):
            best = (start, n - 1)
        start = None
    if best is None:
        raise ConfigError("Couldn't find free addresses for DHCP. Set TATTLE_DHCP_RANGE in .env, e.g. 192.168.1.50,192.168.1.250.")
    return IPv4Address(best[0]), IPv4Address(best[1])


def _ip(value: str, name: str) -> IPv4Address:
    try:
        return IPv4Address(value.strip())
    except ValueError:
        raise ConfigError(f"{name}={value!r} is not an IPv4 address (like 192.168.1.10).") from None


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def load(env: Mapping[str, str] = os.environ, ip: IpRunner = run_ip) -> GatewayConfig:
    """Settings from `env`, detecting the rest with `ip` (injectable for tests)."""
    lan_if = env.get("TATTLE_LAN_IF", "").strip()
    try:
        routes = ip("-4", "route", "show", "default")
    except (OSError, subprocess.SubprocessError, ValueError):
        routes = []
    if not lan_if:
        route = default_route(routes)
        if route is None:
            raise ConfigError("Couldn't tell which network interface connects to the home network. Set TATTLE_LAN_IF in .env (for example TATTLE_LAN_IF=eth0).")
        lan_if = route["dev"]
    if not _IFNAME.match(lan_if):
        raise ConfigError(f"TATTLE_LAN_IF={lan_if!r} is not a valid interface name.")

    try:
        addrs = ipv4_addresses(ip("-4", "addr", "show", "dev", lan_if), lan_if)
    except (OSError, subprocess.SubprocessError, ValueError):
        addrs = []
    if env.get("TATTLE_SERVER_IP", "").strip():
        server_ip = _ip(env["TATTLE_SERVER_IP"], "TATTLE_SERVER_IP")
    elif addrs:
        server_ip = IPv4Address(addrs[0]["local"])
    else:
        raise ConfigError(f"{lan_if} has no IPv4 address. Connect it to the home network, or set TATTLE_LAN_IF to the interface that is.")

    if env.get("TATTLE_LAN", "").strip():
        try:
            lan = IPv4Network(env["TATTLE_LAN"].strip(), strict=False)
        except ValueError:
            raise ConfigError(f"TATTLE_LAN={env['TATTLE_LAN']!r} is not a network like 192.168.1.0/24.") from None
    else:
        prefix = next((a.get("prefixlen") for a in addrs if a.get("local") == str(server_ip)), None)
        if prefix is None:
            raise ConfigError(f"{server_ip} is not on {lan_if}. Set TATTLE_LAN to the home network, e.g. 192.168.1.0/24.")
        lan = IPv4Network(f"{server_ip}/{prefix}", strict=False)

    if env.get("TATTLE_ROUTER_IP", "").strip():
        router_ip = _ip(env["TATTLE_ROUTER_IP"], "TATTLE_ROUTER_IP")
    else:
        route = default_route(routes, lan_if)
        if route is None:
            raise ConfigError(f"Couldn't find the router: {lan_if} has no default route. Set TATTLE_ROUTER_IP in .env to the router's address (for example 192.168.1.1).")
        router_ip = _ip(route["gateway"], "router address")

    for name, value in (("TATTLE_SERVER_IP", server_ip), ("TATTLE_ROUTER_IP", router_ip)):
        if value not in lan:
            raise ConfigError(f"{name} {value} is outside the home network {lan}. Check TATTLE_LAN in .env.")
    if server_ip == router_ip:
        raise ConfigError(f"This server and the router can't both be {server_ip}. Set TATTLE_ROUTER_IP to the router's address.")

    raw_range = env.get("TATTLE_DHCP_RANGE", "").strip()
    if raw_range:
        parts = raw_range.split(",")
        if len(parts) != 2:
            raise ConfigError(f"TATTLE_DHCP_RANGE={raw_range!r} should be 'first,last', e.g. 192.168.1.50,192.168.1.250.")
        start, end = _ip(parts[0], "TATTLE_DHCP_RANGE"), _ip(parts[1], "TATTLE_DHCP_RANGE")
    else:
        start, end = default_dhcp_range(lan, {server_ip, router_ip})
    if not (start in lan and end in lan and start <= end):
        raise ConfigError(f"TATTLE_DHCP_RANGE {start}-{end} must be inside {lan}, lowest address first.")
    for what, value in (("this server", server_ip), ("the router", router_ip)):
        if start <= value <= end:
            raise ConfigError(f"TATTLE_DHCP_RANGE {start}-{end} includes {what} ({value}). Pick a range that leaves it out.")

    lease = env.get("TATTLE_DHCP_LEASE", DEFAULT_LEASE).strip() or DEFAULT_LEASE
    if not _LEASE.match(lease):
        raise ConfigError(f"TATTLE_DHCP_LEASE={lease!r} should look like 2h, 30m or 86400.")

    dns_raw = env.get("TATTLE_DNS", "").strip()
    dns = tuple(str(_ip(part, "TATTLE_DNS")) for part in dns_raw.split(",") if part.strip()) or (str(router_ip),)
    if str(server_ip) in dns:
        raise ConfigError("TATTLE_DNS can't be this server itself; use the router or a public resolver such as 1.1.1.1.")

    local_name = env.get("TATTLE_LOCAL_NAME", DEFAULT_LOCAL_NAME).strip().lower() or DEFAULT_LOCAL_NAME
    if not _HOSTNAME.match(local_name):
        raise ConfigError(f"TATTLE_LOCAL_NAME={local_name!r} is not a valid name like tattle.lan.")

    try:
        probe_interval = max(5.0, float(env.get("TATTLE_PROBE_INTERVAL", DEFAULT_PROBE_SECONDS)))
    except ValueError:
        raise ConfigError("TATTLE_PROBE_INTERVAL should be a number of seconds.") from None

    return GatewayConfig(
        lan_if=lan_if,
        server_ip=server_ip,
        lan=lan,
        router_ip=router_ip,
        dhcp_start=start,
        dhcp_end=end,
        dhcp_lease=lease,
        dns=dns,
        filter_aaaa=_flag(env, "TATTLE_FILTER_AAAA"),
        local_name=local_name,
        skip_route_check=_flag(env, "TATTLE_SKIP_ROUTE_CHECK"),
        probe_interval=probe_interval,
    )


def describe(cfg: GatewayConfig) -> list[tuple[str, str]]:
    """Label/value pairs for `status` and the start-up log."""
    return [
        ("Interface", cfg.lan_if),
        ("This server", str(cfg.server_ip)),
        ("Network", str(cfg.lan)),
        ("Router", str(cfg.router_ip)),
        ("Addresses handed out", f"{cfg.dhcp_start} to {cfg.dhcp_end}, {cfg.dhcp_lease} leases"),
        ("DNS forwarded to", ", ".join(cfg.dns)),
        ("Local name", f"{cfg.local_name} -> {cfg.server_ip}"),
        ("IPv6 answers (AAAA)", "filtered" if cfg.filter_aaaa else "passed through"),
    ]
