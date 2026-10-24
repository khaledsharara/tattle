from __future__ import annotations

from dataclasses import replace
from ipaddress import IPv4Address, IPv4Network

from tattle.gateway import dnsmasq
from tattle.gateway.config import GatewayConfig
from tattle.gateway.paths import Paths

CFG = GatewayConfig(
    lan_if="eth0",
    server_ip=IPv4Address("192.0.2.10"),
    lan=IPv4Network("192.0.2.0/24"),
    router_ip=IPv4Address("192.0.2.1"),
    dhcp_start=IPv4Address("192.0.2.100"),
    dhcp_end=IPv4Address("192.0.2.200"),
    dns=("192.0.2.1",),
)
PATHS = Paths("/run/tattle", "/var/lib/tattle")


def options(cfg: GatewayConfig = CFG) -> list[str]:
    return [line for line in dnsmasq.render(cfg, PATHS).splitlines() if line and not line.startswith("#")]


def test_config_lines():
    lines = options()
    for expected in (
        "port=53",
        "bind-interfaces",
        "interface=eth0",
        "listen-address=192.0.2.10",
        "except-interface=lo",
        "no-resolv",
        "server=192.0.2.1",
        "user=root",
        "group=",  # without it dnsmasq calls setgroups(), which needs CAP_SETGID even as root
        "dhcp-authoritative",
        "dhcp-broadcast",
        "dhcp-range=192.0.2.100,192.0.2.200,2h",
        "dhcp-option=option:router,192.0.2.10",
        "dhcp-option=option:dns-server,192.0.2.10,192.0.2.1",
        "dhcp-leasefile=/var/lib/tattle/dnsmasq.leases",
        "log-dhcp",
        "log-async=25",
        "log-facility=-",
        "address=/tattle.lan/192.0.2.10",
        "stop-dns-rebind",
        "domain-needed",
        "bogus-priv",
    ):
        assert expected in lines, expected
    assert "filter-AAAA" not in lines
    assert not any(line.startswith(("dhcp-script", "dhcp-host", "log-queries")) for line in lines)


def test_options_follow_settings():
    lines = options(replace(CFG, filter_aaaa=True, dns=("203.0.113.53", "198.51.100.53"), local_name="usage.home"))
    assert "filter-AAAA" in lines
    assert [line for line in lines if line.startswith("server=")] == ["server=203.0.113.53", "server=198.51.100.53"]
    assert "address=/usage.home/192.0.2.10" in lines


def test_every_line_is_a_plain_option():
    for line in options(replace(CFG, filter_aaaa=True)):
        key = line.split("=", 1)[0]
        assert key.replace("-", "").isalnum(), line
