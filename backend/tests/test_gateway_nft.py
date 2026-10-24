from __future__ import annotations

import json
from dataclasses import replace
from ipaddress import IPv4Address, IPv4Network

import pytest

from tattle.gateway import nft
from tattle.gateway.config import GatewayConfig

CFG = GatewayConfig(
    lan_if="eth0",
    server_ip=IPv4Address("192.0.2.10"),
    lan=IPv4Network("192.0.2.0/24"),
    router_ip=IPv4Address("192.0.2.1"),
    dhcp_start=IPv4Address("192.0.2.100"),
    dhcp_end=IPv4Address("192.0.2.200"),
)


def test_rules_route_count_and_never_redirect():
    text, digest = nft.render(CFG)
    assert text.startswith("table inet tattle {\n")
    assert f'comment "tattle-gateway sha256={digest}"' in text
    assert 'oifname "eth0" ip saddr 192.0.2.0/24 ip saddr != 192.0.2.10 ip daddr != 192.0.2.0/24 masquerade' in text
    assert 'oifname "eth0" icmp type redirect drop' in text
    assert "update @dev_up { ip saddr }" in text and "update @dev_down { ip daddr }" in text
    assert 'counter name "server_up"' in text and 'counter name "server_down"' in text
    assert "priority filter - 100" in text and "priority filter + 20" in text


def test_no_port_firewall():
    """Tattle routes and counts; filtering the owner's services is not its job."""
    text, _ = nft.render(CFG)
    for word in ("dport", "admin", "reject", "policy drop", "ct state"):
        assert word not in text


@pytest.mark.parametrize("lan, size", [("192.0.2.0/24", 254), ("10.99.0.0/16", 65534), ("10.0.0.0/8", 65536), ("192.0.2.0/30", 64)])
def test_set_size_follows_the_subnet(lan, size):
    cfg = replace(CFG, lan=IPv4Network(lan))
    assert nft.set_size(cfg) == size
    assert f"size {size}\n" in nft.render(cfg)[0]


def test_hash_is_stable_and_tracks_the_rules():
    assert nft.render(CFG) == nft.render(CFG)
    assert nft.render(CFG)[1] != nft.render(replace(CFG, lan_if="eth1"))[1]
    # Settings that don't change the rules (DHCP range, DNS) keep the table and its counters.
    assert nft.render(CFG)[1] == nft.render(replace(CFG, dhcp_end=IPv4Address("192.0.2.250"), dns=("203.0.113.53",)))[1]


def test_load_script_replaces_the_table_in_one_go():
    script = nft.load_script("table inet tattle {\n}\n")
    assert script.splitlines()[:2] == ["table inet tattle", "delete table inet tattle"]


def listing(comment: str | None, counters: dict[str, int] | None = None) -> dict:
    table = {"family": "inet", "name": "tattle", "handle": 9}
    if comment:
        table["comment"] = comment
    items = [{"metainfo": {"version": "1.1.3"}}, {"table": table}]
    for name, packets in (counters or {}).items():
        items.append({"counter": {"family": "inet", "name": name, "table": "tattle", "packets": packets, "bytes": packets * 100}})
    return {"nftables": items}


def test_reading_a_live_table():
    _, digest = nft.render(CFG)
    assert nft.live_hash(listing(f"tattle-gateway sha256={digest}")) == digest
    assert nft.live_hash(listing(None)) is None
    assert nft.live_hash(None) is None
    assert nft.named_counters(listing(None, {"fwd_seen": 3})) == {"fwd_seen": (3, 300)}


class FakeNft:
    def __init__(self, live: dict | None):
        self.live = live
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        if args[:2] == ["-j", "list"]:
            if self.live is None:
                raise nft.NftError("No such file or directory")
            return json.dumps(self.live)
        if args[:2] == ["-f", "-"]:
            return ""
        raise AssertionError(args)


def test_matching_table_is_kept_with_its_counters():
    table = nft.Table(CFG, FakeNft(None))
    table.nft = fake = FakeNft(listing(f"tattle-gateway sha256={table.hash}"))
    assert table.ensure(before_reload=lambda old: pytest.fail("no reload expected")) == "kept"
    assert [c[0][0] for c in fake.calls] == ["-j"]


def test_changed_rules_reload_after_a_last_snapshot():
    old = listing("tattle-gateway sha256=0000000000000000")
    fake = FakeNft(old)
    saved = []
    state = nft.Table(CFG, fake).ensure(before_reload=saved.append)
    assert state == "reloaded"
    assert saved == [old]
    assert fake.calls[-1][0] == ["-f", "-"] and "delete table inet tattle" in fake.calls[-1][1]


def test_missing_table_is_created():
    fake = FakeNft(None)
    assert nft.Table(CFG, fake).ensure(before_reload=lambda old: pytest.fail("nothing to save")) == "created"


def test_snapshot_returns_raw_and_parsed():
    live = listing("x")
    raw, parsed = nft.Table(CFG, FakeNft(live)).snapshot()
    assert json.loads(raw) == parsed == live
    assert nft.Table(CFG, FakeNft(None)).snapshot() is None


def test_forwarding_self_test():
    test = nft.ForwardingTest()
    test.update({"fwd_seen": (100, 0), "fwd_passed": (100, 0)})
    assert test.ok is None  # first reading
    test.update({"fwd_seen": (110, 0), "fwd_passed": (110, 0)})
    assert test.ok is None  # too little traffic to judge
    test.update({"fwd_seen": (300, 0), "fwd_passed": (295, 0)})
    assert test.ok is True and "passing" in test.detail
    test.update({"fwd_seen": (600, 0), "fwd_passed": (300, 0)})
    assert test.ok is False and "firewalld, ufw or Docker" in test.detail
    test.update({"fwd_seen": (5, 0), "fwd_passed": (5, 0)})  # counters went back: table reloaded
    assert test.ok is False  # verdict kept until there is new evidence
    test.update({"fwd_seen": (500, 0), "fwd_passed": (499, 0)})
    assert test.ok is True
    test.update({})  # no self-test counters in the listing
    assert test.ok is True
