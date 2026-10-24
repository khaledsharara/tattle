from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network

import pytest

from tattle.gateway import config
from tattle.gateway.config import ConfigError

ROUTES = [
    {"dst": "default", "gateway": "198.51.100.1", "dev": "wlan0", "protocol": "dhcp", "metric": 600, "flags": []},
    {"dst": "default", "gateway": "192.0.2.1", "dev": "eth0", "protocol": "static", "metric": 100, "flags": []},
]
ADDRS = [
    {
        "ifindex": 2,
        "ifname": "eth0",
        "addr_info": [
            {"family": "inet", "local": "192.0.2.10", "prefixlen": 24, "scope": "global", "label": "eth0"},
            {"family": "inet", "local": "192.0.2.11", "prefixlen": 24, "scope": "global", "secondary": True},
        ],
    }
]


def fake_ip(routes=ROUTES, addrs=ADDRS):
    def ip(*args):
        if args[:2] == ("-4", "route"):
            return routes
        if args[:2] == ("-4", "addr"):
            return [a for a in addrs if a["ifname"] == args[-1]]
        raise AssertionError(args)

    return ip


def test_everything_detected_from_the_network():
    cfg = config.load({}, fake_ip())
    assert cfg.lan_if == "eth0"  # the lowest-metric default route
    assert cfg.server_ip == IPv4Address("192.0.2.10")  # the primary address
    assert cfg.lan == IPv4Network("192.0.2.0/24")
    assert cfg.router_ip == IPv4Address("192.0.2.1")
    assert (cfg.dhcp_start, cfg.dhcp_end) == (IPv4Address("192.0.2.55"), IPv4Address("192.0.2.254"))
    assert cfg.dns == ("192.0.2.1",)  # the router, by default
    assert cfg.dhcp_lease == "2h"
    assert cfg.local_name == "tattle.lan"
    assert not cfg.filter_aaaa and not cfg.skip_route_check


def test_environment_wins():
    env = {
        "TATTLE_LAN_IF": "eth0",
        "TATTLE_SERVER_IP": "192.0.2.11",
        "TATTLE_LAN": "192.0.2.0/24",
        "TATTLE_ROUTER_IP": "192.0.2.254",
        "TATTLE_DHCP_RANGE": "192.0.2.100, 192.0.2.200",
        "TATTLE_DHCP_LEASE": "30m",
        "TATTLE_DNS": "203.0.113.53,198.51.100.53",
        "TATTLE_FILTER_AAAA": "1",
        "TATTLE_LOCAL_NAME": "Usage.Home",
        "TATTLE_SKIP_ROUTE_CHECK": "true",
        "TATTLE_PROBE_INTERVAL": "1",
    }
    cfg = config.load(env, fake_ip(routes=[]))
    assert cfg.server_ip == IPv4Address("192.0.2.11")
    assert cfg.router_ip == IPv4Address("192.0.2.254")
    assert (str(cfg.dhcp_start), str(cfg.dhcp_end)) == ("192.0.2.100", "192.0.2.200")
    assert cfg.dhcp_lease == "30m"
    assert cfg.dns == ("203.0.113.53", "198.51.100.53")
    assert cfg.filter_aaaa and cfg.skip_route_check
    assert cfg.local_name == "usage.home"
    assert cfg.probe_interval == 5.0  # floor: never flood the network with probes


def test_default_range_avoids_server_and_router():
    lan = IPv4Network("192.0.2.0/24")
    start, end = config.default_dhcp_range(lan, {IPv4Address("192.0.2.100"), IPv4Address("192.0.2.1")})
    assert (str(start), str(end)) == ("192.0.2.101", "192.0.2.254")
    start, end = config.default_dhcp_range(lan, {IPv4Address("192.0.2.254"), IPv4Address("192.0.2.200")})
    assert (str(start), str(end)) == ("192.0.2.55", "192.0.2.199")


def test_default_range_in_a_big_network_stays_near_the_top():
    lan = IPv4Network("10.99.0.0/16")
    start, end = config.default_dhcp_range(lan, {IPv4Address("10.99.0.1")})
    assert (str(start), str(end)) == ("10.99.255.55", "10.99.255.254")


def test_tiny_network_is_refused():
    with pytest.raises(ConfigError, match="too small"):
        config.default_dhcp_range(IPv4Network("192.0.2.0/29"), set())


@pytest.mark.parametrize(
    "env, message",
    [
        ({"TATTLE_DHCP_RANGE": "192.0.2.5,192.0.2.50"}, "includes this server"),
        ({"TATTLE_DHCP_RANGE": "192.0.2.1,192.0.2.9"}, "includes the router"),
        ({"TATTLE_DHCP_RANGE": "192.0.2.200,192.0.2.100"}, "lowest address first"),
        ({"TATTLE_DHCP_RANGE": "192.0.2.100"}, "first,last"),
        ({"TATTLE_DHCP_RANGE": "198.51.100.10,198.51.100.20"}, "must be inside"),
        ({"TATTLE_DHCP_LEASE": "2 hours"}, "TATTLE_DHCP_LEASE"),
        ({"TATTLE_DNS": "192.0.2.10"}, "can't be this server"),
        ({"TATTLE_DNS": "dns.example"}, "not an IPv4 address"),
        ({"TATTLE_ROUTER_IP": "198.51.100.1"}, "outside the home network"),
        ({"TATTLE_ROUTER_IP": "192.0.2.10"}, "can't both be"),
        ({"TATTLE_LAN": "nonsense"}, "not a network"),
        ({"TATTLE_PROBE_INTERVAL": "soon"}, "number of seconds"),
    ],
)
def test_bad_settings_explain_themselves(env, message):
    with pytest.raises(ConfigError, match=message):
        config.load(env, fake_ip())


@pytest.mark.parametrize(
    "env",
    [
        {"TATTLE_LAN_IF": "eth0\ndhcp-script=/bin/sh"},
        {"TATTLE_LOCAL_NAME": "tattle.lan/1.2.3.4\naddress=/example.com"},
        {"TATTLE_DHCP_LEASE": "2h\nuser=nobody"},
    ],
)
def test_values_cannot_inject_config_lines(env):
    with pytest.raises(ConfigError):
        config.load(env, fake_ip())


def test_missing_network_is_explained():
    with pytest.raises(ConfigError, match="TATTLE_LAN_IF"):
        config.load({}, fake_ip(routes=[]))
    with pytest.raises(ConfigError, match="no IPv4 address"):
        config.load({"TATTLE_LAN_IF": "eth1"}, fake_ip())
    with pytest.raises(ConfigError, match="TATTLE_ROUTER_IP"):
        config.load({"TATTLE_LAN_IF": "eth0"}, fake_ip(routes=[ROUTES[0]]))


def test_ip_failures_count_as_nothing_detected():
    def broken(*args):
        raise OSError("ip: not found")

    with pytest.raises(ConfigError, match="TATTLE_LAN_IF"):
        config.load({}, broken)
    cfg = config.load(
        {"TATTLE_LAN_IF": "eth0", "TATTLE_SERVER_IP": "192.0.2.10", "TATTLE_LAN": "192.0.2.0/24", "TATTLE_ROUTER_IP": "192.0.2.1"},
        broken,
    )
    assert cfg.lan_if == "eth0"
