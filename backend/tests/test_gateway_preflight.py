from __future__ import annotations

import socket
import struct
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from tattle.gateway import preflight
from tattle.gateway.config import GatewayConfig

CFG = GatewayConfig(
    lan_if="eth0",
    server_ip=IPv4Address("192.0.2.10"),
    lan=IPv4Network("192.0.2.0/24"),
    router_ip=IPv4Address("192.0.2.1"),
    dhcp_start=IPv4Address("192.0.2.100"),
    dhcp_end=IPv4Address("192.0.2.200"),
)
HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def hex4(ip: str) -> str:
    """The way the kernel prints an IPv4 address in /proc/net: a host-order word."""
    return f"{struct.unpack('=I', socket.inet_aton(ip))[0]:08X}"


def hex6(ip: str) -> str:
    raw = socket.inet_pton(socket.AF_INET6, ip)
    return "".join(f"{w:08X}" for w in struct.unpack("=4I", raw))


def row(n: int, addr: str, port: int, state: str) -> str:
    return f"   {n}: {addr}:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000 0 0 1234 2\n"


def test_listeners_parses_proc_net():
    udp = HEADER + row(0, hex4("0.0.0.0"), 67, "07") + row(1, hex4("127.0.0.53"), 53, "07") + row(2, hex4("192.0.2.10"), 5353, "07")
    assert preflight.listeners(udp, 67, "07") == ["0.0.0.0"]
    assert preflight.listeners(udp, 53, "07") == ["127.0.0.53"]
    tcp6 = HEADER + row(0, hex6("::"), 53, "0A") + row(1, hex6("fe80::1"), 53, "01")  # second one isn't listening
    assert preflight.listeners(tcp6, 53, "0A") == ["::"]


def test_bound_reads_v4_and_v6_tables(tmp_path: Path):
    (tmp_path / "udp").write_text(HEADER + row(0, hex4("0.0.0.0"), 67, "07"))
    (tmp_path / "udp6").write_text(HEADER + row(0, hex6("::"), 67, "07"))
    assert preflight.bound(67, "udp", str(tmp_path)) == ["0.0.0.0", "::"]
    assert preflight.bound(67, "tcp", str(tmp_path)) == []  # missing tables are fine


def test_runtime_detection():
    init = "         0          0 4294967295\n"
    assert preflight.check_runtime(init, "", podman=False).ok
    rootless = preflight.check_runtime("         0       1000          1\n         1     100000      65536\n", "", podman=False)
    assert not rootless.ok and "rootless" in rootless.detail
    snap = "123 45 0:23 /var/snap/docker/common/var-lib-docker/containers/abc/hostname /etc/hostname rw - ext4 /dev/sda1 rw\n"
    result = preflight.check_runtime(init, snap, podman=False)
    assert not result.ok and "Snap" in result.detail and "get.docker.com" in result.detail
    assert not preflight.check_runtime(init, "", podman=True).ok


def test_kernel_version():
    assert preflight.check_kernel("6.1.0-27-amd64").ok
    assert preflight.check_kernel("5.10.0-32-arm64").ok
    old = preflight.check_kernel("4.19.0-26-amd64")
    assert not old.ok and "5.10" in old.detail


def test_ip_forward_tells_how_to_fix():
    assert preflight.check_ip_forward("1\n").ok
    off = preflight.check_ip_forward("0\n")
    assert not off.ok
    assert "net.ipv4.ip_forward=1" in off.detail and "/etc/sysctl.d/" in off.detail


def addr(dynamic: bool, local: str = "192.0.2.10") -> list:
    info = {"family": "inet", "local": local, "prefixlen": 24, "scope": "global"}
    if dynamic:
        info.update(dynamic=True, valid_life_time=86000)
    return [{"ifname": "eth0", "addr_info": [info]}]


def test_static_address():
    assert preflight.check_static_address(CFG, addr(False)).ok
    leased = preflight.check_static_address(CFG, addr(True))
    assert not leased.ok and "fixed address" in leased.detail
    missing = preflight.check_static_address(CFG, addr(False, "192.0.2.99"))
    assert not missing.ok and "TATTLE_SERVER_IP" in missing.detail


def test_route_must_leave_through_the_lan_interface():
    good = [{"dst": "1.1.1.1", "gateway": "192.0.2.1", "dev": "eth0", "prefsrc": "192.0.2.10"}]
    assert preflight.check_route(CFG, good).ok
    wifi = preflight.check_route(CFG, [{"dst": "1.1.1.1", "gateway": "198.51.100.1", "dev": "wlan0"}])
    assert not wifi.ok and "wlan0" in wifi.detail
    assert not preflight.check_route(CFG, []).ok
    from dataclasses import replace

    assert preflight.check_route(replace(CFG, skip_route_check=True), []).ok


def test_dhcp_port():
    def never_called():
        raise AssertionError("bind test only runs when something holds the port")

    assert preflight.check_dhcp_port([], never_called).ok
    taken = preflight.check_dhcp_port(["0.0.0.0"], lambda: OSError(98, "Address already in use"))
    assert not taken.ok and "Pi-hole" in taken.detail and "docker compose" in taken.detail
    shared = preflight.check_dhcp_port(["0.0.0.0"], lambda: None)
    assert shared.ok and "libvirt" in shared.detail


def test_dns_port_ignores_local_resolvers():
    def never_called():
        raise AssertionError

    assert preflight.check_dns_port(CFG, ["127.0.0.53", "127.0.0.54", "192.0.2.11"], never_called).ok
    clash = preflight.check_dns_port(CFG, ["0.0.0.0"], lambda: OSError(98, "Address already in use"))
    assert not clash.ok and "192.0.2.10 port 53" in clash.detail
    assert preflight.check_dns_port(CFG, ["::"], lambda: None).ok  # it shares the port (SO_REUSEADDR)


def test_run_checks_with_fake_proc(tmp_path: Path, monkeypatch):
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "uid_map").write_text("         0          0 4294967295\n")
    (tmp_path / "self" / "mountinfo").write_text("")
    (tmp_path / "sys" / "net" / "ipv4").mkdir(parents=True)
    (tmp_path / "sys" / "net" / "ipv4" / "ip_forward").write_text("0\n")
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "udp").write_text(HEADER)
    monkeypatch.setattr(preflight.os.path, "exists", lambda path: False)

    def ip(*args):
        if args[1] == "addr":
            return addr(False)
        return [{"dst": "1.1.1.1", "gateway": "192.0.2.1", "dev": "eth0"}]

    checks = {c.id: c for c in preflight.run_checks(CFG, ip=ip, proc=str(tmp_path))}
    assert list(checks) == ["runtime", "kernel", "ip_forward", "static_ip", "route", "dhcp_port", "dns_port"]
    assert not checks["ip_forward"].ok
    assert all(c.ok for name, c in checks.items() if name != "ip_forward")
    assert [c.id for c in preflight.run_checks(CFG, ports=False, ip=ip, proc=str(tmp_path))][-1] == "route"
