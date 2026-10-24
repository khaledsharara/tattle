"""Checks run before the gateway touches the network.

Turning a server into the house's gateway goes wrong in a handful of known ways
(forwarding off, an address that will vanish, a second DHCP server...). Each
check returns a sentence the person at the keyboard can act on; `run` refuses
to start while any of them fails, and the dashboard shows the same sentences.
"""

from __future__ import annotations

import os
import re
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass

from .config import GatewayConfig, IpRunner, ipv4_addresses, run_ip

MIN_KERNEL = (5, 10)  # nftables table comments (5.10), inet-family NAT (5.2)
RESTART = "docker compose --profile gateway up -d gateway"
SYSCTL_FIX = (
    "echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/90-tattle.conf && sudo sysctl -p /etc/sysctl.d/90-tattle.conf"
)
TCP_LISTEN, UDP_BOUND = "0A", "07"


@dataclass(frozen=True)
class Check:
    id: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"id": self.id, "ok": self.ok, "detail": self.detail}


# --- /proc parsing (pure) -------------------------------------------------------------


def _hex_ip(value: str) -> str:
    """/proc/net prints addresses as host-order 32-bit words."""
    raw = b"".join(struct.pack("=I", int(value[i : i + 8], 16)) for i in range(0, len(value), 8))
    return socket.inet_ntop(socket.AF_INET if len(raw) == 4 else socket.AF_INET6, raw)


def listeners(proc_text: str, port: int, state: str) -> list[str]:
    """Local addresses bound to `port` in a /proc/net/{tcp,udp}[6] table."""
    found = []
    for line in proc_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or ":" not in parts[1] or parts[3] != state:
            continue
        addr, _, hex_port = parts[1].rpartition(":")
        try:
            if int(hex_port, 16) == port:
                found.append(_hex_ip(addr))
        except ValueError:
            continue
    return found


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def bound(port: int, proto: str, proc: str = "/proc/net") -> list[str]:
    state = TCP_LISTEN if proto == "tcp" else UDP_BOUND
    return [addr for table in (proto, proto + "6") for addr in listeners(_read(f"{proc}/{table}"), port, state)]


def try_bind(kind: int, addr: str, port: int, reuseport: bool) -> OSError | None:
    """Bind the way dnsmasq will; None means dnsmasq will get the port too.

    Another program on the port isn't always a conflict: a dnsmasq bound to a
    different interface (libvirt's virbr0, for one) shares port 67 happily.
    """
    try:
        with socket.socket(socket.AF_INET, kind) as sock:
            if reuseport and hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((addr, port))
            if kind == socket.SOCK_STREAM:
                sock.listen(1)
    except OSError as exc:
        return exc
    return None


# --- checks ------------------------------------------------------------------------


def check_runtime(uid_map: str, mountinfo: str, podman: bool) -> Check:
    """Rootless and sandboxed Docker installs can't change the real network."""
    lines = [line.split() for line in uid_map.strip().splitlines()]
    if podman:
        return Check("runtime", False, "This is running under Podman. Gateway mode needs Docker Engine (from get.docker.com) running as root.")
    if lines and lines != [["0", "0", "4294967295"]]:
        return Check(
            "runtime",
            False,
            "This container runs in a user namespace (rootless Docker, or Docker with userns-remap), so it can't change "
            "the server's firewall or hand out addresses. Gateway mode needs regular, rootful Docker from get.docker.com.",
        )
    if "/var/snap/docker/" in mountinfo or "/snap/docker/" in mountinfo:
        return Check(
            "runtime",
            False,
            "Docker here was installed from Snap, whose confinement breaks gateway mode. Remove it (sudo snap remove docker) "
            "and install Docker from get.docker.com, then run the Tattle installer again.",
        )
    if "/.local/share/docker/" in mountinfo:
        return Check("runtime", False, "This looks like rootless Docker. Gateway mode needs regular, rootful Docker from get.docker.com.")
    return Check("runtime", True, "Running under rootful Docker.")


def check_kernel(release: str) -> Check:
    match = re.match(r"(\d+)\.(\d+)", release)
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    if version < MIN_KERNEL:
        return Check(
            "kernel",
            False,
            f"This server's Linux kernel ({release}) is too old for Tattle's firewall rules; it needs "
            f"{MIN_KERNEL[0]}.{MIN_KERNEL[1]} or newer. Update the operating system (any release from 2021 on works).",
        )
    return Check("kernel", True, f"Linux {release}.")


def check_ip_forward(value: str) -> Check:
    if value.strip() == "1":
        return Check("ip_forward", True, "IP forwarding is on.")
    return Check(
        "ip_forward",
        False,
        "IP forwarding is off, so this server can't pass devices' traffic on to the router (Docker's nftables "
        f"firewall doesn't turn it on). Turn it on for good with: {SYSCTL_FIX}",
    )


def check_static_address(cfg: GatewayConfig, addrs: list) -> Check:
    infos = [a for a in ipv4_addresses(addrs, cfg.lan_if) if a.get("local") == str(cfg.server_ip)]
    if not infos:
        return Check(
            "static_ip",
            False,
            f"{cfg.server_ip} isn't an address of {cfg.lan_if} on this server. Fix TATTLE_SERVER_IP or TATTLE_LAN_IF in .env.",
        )
    if infos[0].get("dynamic"):
        return Check(
            "static_ip",
            False,
            f"This server got {cfg.server_ip} from the router's DHCP. Once the router's DHCP is off it would lose that "
            "address and the whole house would go offline. Give it a fixed address: re-run the Tattle installer, or set "
            f"{cfg.server_ip} as a manual (static) address in the server's network settings.",
        )
    return Check("static_ip", True, f"{cfg.server_ip} is a fixed address on {cfg.lan_if}.")


def check_route(cfg: GatewayConfig, route: list) -> Check:
    if cfg.skip_route_check:
        return Check("route", True, "Route check skipped (TATTLE_SKIP_ROUTE_CHECK=1).")
    info = route[0] if route else {}
    dev, via = info.get("dev"), info.get("gateway")
    if dev == cfg.lan_if and via == str(cfg.router_ip):
        return Check("route", True, f"Internet traffic leaves through {cfg.lan_if} via the router {cfg.router_ip}.")
    if not info:
        return Check("route", False, "This server has no route to the internet. Check its cable and network settings.")
    return Check(
        "route",
        False,
        f"This server reaches the internet through {dev or 'another interface'}"
        f"{f' via {via}' if via else ''}, not through {cfg.lan_if} via {cfg.router_ip}. Devices' traffic has to go back out "
        f"the way it came in. Unplug or disable the other connection (often Wi-Fi on a server that also has a cable), or "
        f"set TATTLE_LAN_IF and TATTLE_ROUTER_IP to the connection that leads to the router.",
    )


CULPRITS = "Pi-hole, AdGuard Home, another dnsmasq, isc-dhcp-server or NetworkManager's \"shared\" connection mode"


def check_dhcp_port(bound_addrs: list[str], bind: Callable[[], OSError | None]) -> Check:
    if not bound_addrs:
        return Check("dhcp_port", True, "No other DHCP server runs on this server.")
    if (error := bind()) is not None:
        return Check(
            "dhcp_port",
            False,
            f"Another DHCP server already runs on this server (UDP port 67 is taken: {error.strerror or error}). "
            f"It's usually {CULPRITS}. Turn its DHCP off, then start the gateway again: {RESTART}",
        )
    return Check(
        "dhcp_port",
        True,
        "Another DHCP server on this server shares port 67 on a different interface (libvirt's virbr0, for example). "
        "That's fine as long as it doesn't serve the home network.",
    )


def check_dns_port(cfg: GatewayConfig, bound_addrs: list[str], bind: Callable[[], OSError | None]) -> Check:
    clash = sorted({a for a in bound_addrs if a in (str(cfg.server_ip), "0.0.0.0", "::")})
    if not clash or (error := bind()) is None:
        return Check("dns_port", True, f"Port 53 on {cfg.server_ip} is free for Tattle's DNS.")
    return Check(
        "dns_port",
        False,
        f"Something else on this server already answers DNS on {cfg.server_ip} port 53 ({error.strerror or error}). "
        f"It's usually {CULPRITS}, or a DNS server listening on every address. Turn it off or limit it to other "
        f"addresses, then start the gateway again: {RESTART}",
    )


def run_checks(cfg: GatewayConfig, *, ports: bool = True, ip: IpRunner = run_ip, proc: str = "/proc") -> list[Check]:
    """Every check; `ports=False` when Tattle's own dnsmasq may be the one holding them."""
    try:
        addrs = ip("-4", "addr", "show", "dev", cfg.lan_if)
    except Exception:  # noqa: BLE001 - reported as a failed check below
        addrs = []
    try:
        route = ip("-4", "route", "get", "1.1.1.1")
    except Exception:  # noqa: BLE001
        route = []
    checks = [
        check_runtime(_read(f"{proc}/self/uid_map"), _read(f"{proc}/self/mountinfo"), os.path.exists("/run/.containerenv")),
        check_kernel(os.uname().release),
        check_ip_forward(_read(f"{proc}/sys/net/ipv4/ip_forward")),
        check_static_address(cfg, addrs),
        check_route(cfg, route),
    ]
    if ports:
        server = str(cfg.server_ip)
        checks.append(check_dhcp_port(bound(67, "udp", f"{proc}/net"), lambda: try_bind(socket.SOCK_DGRAM, "0.0.0.0", 67, True)))
        checks.append(
            check_dns_port(
                cfg,
                bound(53, "udp", f"{proc}/net") + bound(53, "tcp", f"{proc}/net"),
                lambda: try_bind(socket.SOCK_DGRAM, server, 53, False) or try_bind(socket.SOCK_STREAM, server, 53, False),
            )
        )
    return checks
