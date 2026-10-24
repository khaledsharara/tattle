from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tattle.gateway import dhcplog

# The shape of dnsmasq's log-dhcp output with log-facility=- and --keep-in-foreground
# (2.91), with documentation-range addresses (RFC 5737 / RFC 7042) and made-up names.
LOG = """dnsmasq-dhcp[42]: 433998375 vendor class: MSFT 5.0
dnsmasq-dhcp[42]: 433998375 client provides name: example-laptop
dnsmasq-dhcp[42]: 433998375 DHCPREQUEST(eth0) 192.0.2.3 00:00:5e:00:53:0a
dnsmasq-dhcp[42]: 433998375 tags: eth0
dnsmasq-dhcp[42]: 433998375 DHCPACK(eth0) 192.0.2.3 00:00:5e:00:53:0a example-laptop
dnsmasq-dhcp[42]: 433998375 requested options: 1:netmask, 3:router, 6:dns-server, 15:domain-name,
dnsmasq-dhcp[42]: 433998375 requested options: 31:router-discovery, 33:static-route, 43:vendor-encap,
dnsmasq-dhcp[42]: 433998375 requested options: 44:netbios-ns, 46:netbios-nodetype, 47:netbios-scope,
dnsmasq-dhcp[42]: 433998375 requested options: 119:domain-search, 121:classless-static-route,
dnsmasq-dhcp[42]: 433998375 requested options: 249, 252, 114
dnsmasq-dhcp[42]: 433998375 next server: 192.0.2.100
dnsmasq-dhcp[42]: 3962351096 DHCPDISCOVER(eth0) 00:00:5e:00:53:0b
dnsmasq-dhcp[42]: 3962351096 DHCPOFFER(eth0) 192.0.2.106 00:00:5e:00:53:0b
dnsmasq-dhcp[42]: 3962351096 requested options: 1:netmask, 121:classless-static-route, 3:router,
dnsmasq-dhcp[42]: 3962351096 requested options: 6:dns-server, 15:domain-name, 108:ipv6-only,
dnsmasq-dhcp[42]: 3962351096 requested options: 114, 119:domain-search, 162, 252
dnsmasq[42]: started, version 2.91 cachesize 1000
"""

WINDOWS = dhcplog.Fingerprint(
    "00:00:5e:00:53:0a", "1,3,6,15,31,33,43,44,46,47,119,121,249,252,114".split(","), "MSFT 5.0", "example-laptop"
)
IPHONE = dhcplog.Fingerprint("00:00:5e:00:53:0b", "1,121,3,6,15,108,114,119,162,252".split(","), None, None)


def parse(lines) -> list[dhcplog.Fingerprint]:
    parser = dhcplog.FingerprintParser()
    return [r for r in (parser.feed(line) for line in lines) if r]


def test_parser_reads_windows_and_iphone_requests():
    assert parse(LOG.splitlines(keepends=True)) == [WINDOWS, IPHONE]


def test_lines_without_dnsmasq_prefix_still_parse():
    """journalctl -o cat (the old host setup) gave the bare "<xid> ..." text."""
    bare = [dhcplog.strip_prefix(line) + "\n" for line in LOG.splitlines()]
    assert parse(bare) == [WINDOWS, IPHONE]


@pytest.mark.parametrize(
    "line",
    [
        "dnsmasq-dhcp[42]: 7 vendor class: x",  # log-facility=- with --keep-in-foreground
        "Oct 24 10:00:00 dnsmasq-dhcp[42]: 7 vendor class: x",  # without it: a timestamp first
        "Oct  4 09:05:00 dnsmasq-dhcp[42]: 7 vendor class: x",  # single-digit day is space-padded
        "<30>Oct 24 10:00:00 dnsmasq-dhcp[42]: 7 vendor class: x",  # syslog priority, if ever present
        "dnsmasq-dhcp: 7 vendor class: x",  # --no-daemon (debug) echo without a PID
        "7 vendor class: x",  # no prefix at all
    ],
)
def test_prefix_variants(line):
    assert dhcplog.strip_prefix(line + "\n") == "7 vendor class: x"


def test_other_text_is_left_alone():
    assert dhcplog.strip_prefix("dnsmasq[1]: started, version 2.91") == "started, version 2.91"
    assert dhcplog.strip_prefix("something else: 7 x") == "something else: 7 x"


def test_unfinished_transactions_cannot_grow_forever():
    parser = dhcplog.FingerprintParser()
    for xid in range(dhcplog.MAX_TRANSACTIONS + 5):
        parser.feed(f"{xid} DHCPDISCOVER(eth0) 00:00:5e:00:53:01 \n")
    assert len(parser.current) <= dhcplog.MAX_TRANSACTIONS


def test_store_writes_the_files_the_collector_reads(tmp_path: Path):
    store = dhcplog.FingerprintStore(str(tmp_path / "dhcp"))
    assert store.add_fingerprint(WINDOWS)
    path = tmp_path / "dhcp" / "00005e00530a.json"
    record = json.loads(path.read_text())
    assert record["mac"] == "00:00:5e:00:53:0a"
    assert record["options"] == "1,3,6,15,31,33,43,44,46,47,119,121,249,252,114"
    assert record["vendor_class"] == "MSFT 5.0"
    assert record["hostname"] == "example-laptop"
    assert isinstance(record["updated"], int)
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert oct((tmp_path / "dhcp").stat().st_mode & 0o777) == "0o755"
    assert [p.name for p in (tmp_path / "dhcp").iterdir()] == ["00005e00530a.json"]  # no temporary files left


def test_unchanged_fingerprint_leaves_the_file_alone(tmp_path: Path):
    store = dhcplog.FingerprintStore(str(tmp_path))
    store.add_fingerprint(IPHONE)
    path = tmp_path / "00005e00530b.json"
    os.utime(path, (1000, 1000))
    assert not store.add_fingerprint(IPHONE)
    assert path.stat().st_mtime == 1000  # the dashboard only re-reads files whose mtime changed


def test_lease_sync_adds_address_and_client_id_without_erasing(tmp_path: Path):
    store = dhcplog.FingerprintStore(str(tmp_path))
    store.add_fingerprint(WINDOWS)
    leases = tmp_path / "dnsmasq.leases"
    leases.write_text(
        "duid 00:01:00:01:00:00:00:00:00:00:5e:00:53:01\n"
        "1791800000 00:00:5E:00:53:0A 192.0.2.3 cleaned-name 01:00:00:5e:00:53:0a\n"
        "1791800000 00:00:5e:00:53:0c 192.0.2.4 * *\n"
        "garbage\n"
    )
    parsed = dhcplog.read_leases(str(leases))
    assert [(lease.mac, lease.hostname, lease.client_id) for lease in parsed] == [
        ("00:00:5e:00:53:0a", "cleaned-name", "01:00:00:5e:00:53:0a"),
        ("00:00:5e:00:53:0c", None, None),
    ]
    assert store.sync_leases(parsed) == 2
    record = json.loads((tmp_path / "00005e00530a.json").read_text())
    assert record["ip"] == "192.0.2.3"
    assert record["client_id"] == "01:00:00:5e:00:53:0a"
    assert record["hostname"] == "example-laptop"  # the name as sent beats dnsmasq's cleaned-up copy
    assert record["options"].startswith("1,3,6")
    bare = json.loads((tmp_path / "00005e00530c.json").read_text())
    assert bare == {"mac": "00:00:5e:00:53:0c", "ip": "192.0.2.4", "updated": bare["updated"]}
    assert store.sync_leases(parsed) == 0


def test_store_rejects_anything_that_is_not_a_mac(tmp_path: Path):
    store = dhcplog.FingerprintStore(str(tmp_path))
    assert not store.merge("../../etc/passwd", {"ip": "192.0.2.1"})
    assert list(tmp_path.iterdir()) == []


def test_active_leases_counts_unexpired_and_infinite():
    leases = [
        dhcplog.LeaseLine(0, "00:00:5e:00:53:01", "192.0.2.1", None, None),
        dhcplog.LeaseLine(200, "00:00:5e:00:53:02", "192.0.2.2", None, None),
        dhcplog.LeaseLine(50, "00:00:5e:00:53:03", "192.0.2.3", None, None),
    ]
    assert dhcplog.active_leases(leases, now=100) == 2


def test_fingerprints_are_recognised():
    from tattle.identity import os_hint

    assert os_hint(",".join(IPHONE.options), None, None) == "iPhone or iPad"
    assert os_hint(",".join(WINDOWS.options), "MSFT 5.0", None) == "Windows"
