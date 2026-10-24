from __future__ import annotations

from helpers import host_clock

import json
import socket
import struct
import time

from tattle import db
from tattle.collector import Collector, LiveState
from tattle.identity import Identity, find_links, name_strength, normalise_name, os_hint, parse_mdns

IOS_OPTIONS = "1,121,3,6,15,108,114,119,252"
DAY = 86400


# --- names and kinds ---------------------------------------------------------------
def test_names_normalise_across_sources():
    assert normalise_name("Maya’s iPhone") == "mayas-iphone"
    assert normalise_name("Mayas-iPhone.local") == "mayas-iphone"
    assert normalise_name("Mayas-iPhone-2") == "mayas-iphone"  # Bonjour conflict suffix
    assert normalise_name("Galaxy-A54") == "galaxy-a54"


def test_name_strength():
    assert name_strength("mayas-iphone") == "personal"
    assert name_strength("mayas-iphone-se") == "personal"
    assert name_strength("galaxy-a54") == "model"
    assert name_strength("desktop-4f2k9q1") == "model"
    assert name_strength("iphone") == "none"
    assert name_strength("android-3f9a2b7c1d0e4f55") == "none"
    assert name_strength("hostname-44-80-6e") == "none"


def test_os_hint_from_dhcp_and_model():
    assert os_hint(IOS_OPTIONS, None, None) == "iPhone or iPad"
    assert os_hint(IOS_OPTIONS + ",95,44,46", None, None) == "Mac"
    assert os_hint("1,3,6,15,26,28,51,58,59,43", "android-dhcp-13", None) == "Android"
    assert os_hint("1,3,6,15,31,33,43,44,46,47,119,121,249,252", "MSFT 5.0", None) == "Windows"
    assert os_hint(IOS_OPTIONS, None, "iPad13,1") == "iPad"
    assert os_hint(None, None, None) is None


# --- Bonjour -------------------------------------------------------------------------
def _name(text: str) -> bytes:
    return b"".join(bytes([len(p.encode())]) + p.encode() for p in text.split(".")) + b"\x00"


def _rr(name: str, rtype: int, rdata: bytes) -> bytes:
    return _name(name) + struct.pack("!HHIH", rtype, 1, 120, len(rdata)) + rdata


def test_parse_mdns_response():
    txt = bytes([len("model=iPhone14,2")]) + b"model=iPhone14,2"
    records = [
        _rr("Mayas-iPhone.local", 1, socket.inet_aton("192.168.1.45")),
        _rr("_companion-link._tcp.local", 12, _name("Maya’s iPhone._companion-link._tcp.local")),
        _rr("Maya’s iPhone._device-info._tcp.local", 16, txt),
    ]
    packet = struct.pack("!HHHHHH", 0, 0x8400, 0, len(records), 0, 0) + b"".join(records)
    facts = parse_mdns(packet, "192.168.1.45")
    assert facts.names == {"Mayas-iPhone", "Maya’s iPhone"}
    assert facts.model == "iPhone14,2"


def test_parse_mdns_ignores_queries_and_foreign_addresses():
    record = _rr("Other.local", 1, socket.inet_aton("192.168.1.99"))
    response = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + record
    assert parse_mdns(response, "192.168.1.45").names == set()
    query = struct.pack("!HHHHHH", 0, 0, 0, 1, 0, 0) + _rr("Mayas-iPhone.local", 1, socket.inet_aton("192.168.1.45"))
    assert parse_mdns(query, "192.168.1.45").names == set()


# --- linking rules -------------------------------------------------------------------
NOW = 10 * DAY
MIN = 60


def ident(i, mac, names=(), sessions=(), kind="iPhone or iPad", fp=IOS_OPTIONS + "|", first_seen=0):
    item = Identity(i, mac, None, first_seen, kind, fp)
    item.names = {normalise_name(n): n for n in names}
    item.sessions = sorted(sessions)
    return item


def alternating(gap, rounds=3, stint=40 * MIN):
    """Sessions for one phone moving between two networks `rounds` times."""
    a, b, t = [], [], DAY
    for _ in range(rounds):
        a.append((t, t + stint))
        t += stint + gap
        b.append((t, t + stint))
        t += stint + gap
    return a, b


def test_same_personal_name_links_automatically():
    a = ident(1, "0a:00:00:00:00:01", ["Mayas-iPhone"], [(0, 3600)])
    b = ident(2, "1e:00:00:00:00:02", ["Maya’s iPhone"], [(3700, 7200)])
    (link,) = find_links([a, b], NOW)
    assert link.automatic and "Maya" in link.reason


def test_online_together_means_different_devices():
    a = ident(1, "0a:00:00:00:00:01", ["Mayas-iPhone"], [(0, 3600)])
    b = ident(2, "1e:00:00:00:00:02", ["Mayas-iPhone"], [(1000, 2000)])
    assert find_links([a, b], NOW) == []


def test_clean_quick_hand_overs_merge_automatically():
    sa, sb = alternating(gap=15)
    (link,) = find_links([ident(1, "0a:00:00:00:00:01", sessions=sa), ident(2, "1e:00:00:00:00:02", sessions=sb)], NOW)
    assert link.automatic and "moved from one to the other" in link.reason


def test_one_hand_over_is_only_a_suggestion():
    sa, sb = alternating(gap=15, rounds=1)
    (link,) = find_links([ident(1, "0a:00:00:00:00:01", sessions=sa), ident(2, "1e:00:00:00:00:02", sessions=sb)], NOW)
    assert not link.automatic


def test_not_merged_before_watching_long_enough():
    sa, sb = alternating(gap=15)
    a = ident(1, "0a:00:00:00:00:01", sessions=sa, first_seen=NOW - 3600)
    b = ident(2, "1e:00:00:00:00:02", sessions=sb, first_seen=NOW - 3600)
    assert all(not link.automatic for link in find_links([a, b], NOW))


def test_busy_moments_count_for_less():
    # Another iPhone also starts up every time the phone changes network, so no
    # hand-over can be told apart from the coincidence.
    sa, sb = alternating(gap=15)
    sc = [(start + 5, start + 30) for start, _ in sorted(sa + sb)[1:]]
    a, b = ident(1, "0a:00:00:00:00:01", sessions=sa), ident(2, "1e:00:00:00:00:02", sessions=sb)
    c = ident(3, "2e:00:00:00:00:03", sessions=sc)
    links = {(l.a.id, l.b.id): l for l in find_links([a, b, c], NOW)}
    assert not links.get((1, 2)) or not links[(1, 2)].automatic


def test_different_kinds_are_not_linked():
    sa, sb = alternating(gap=15)
    a = ident(1, "0a:00:00:00:00:01", sessions=sa, kind="Android", fp="1,3,6|android-dhcp-13")
    b = ident(2, "1e:00:00:00:00:02", sessions=sb)
    assert find_links([a, b], NOW) == []


def test_real_macs_are_not_linked_by_behaviour():
    sa, sb = alternating(gap=15)
    assert find_links([ident(1, "00:00:5e:00:53:01", sessions=sa), ident(2, "00:00:5e:00:53:02", sessions=sb)], NOW) == []


# --- collector end to end ------------------------------------------------------------
def new_collector(settings) -> Collector:
    db.init(settings.db_path)
    return Collector(settings, LiveState(), clock=host_clock(settings))


def device_id(conn, key):
    return conn.execute("SELECT id FROM devices WHERE key = ?", (key,)).fetchone()["id"]


def test_dhcp_fingerprint_files_set_the_kind(settings, host, tmp_path):
    host.write_leases(("0a:00:5e:00:53:76", "192.168.1.129", None, "01:0a:00:5e:00:53:76"))
    collector = new_collector(settings)
    collector.tick()
    events = tmp_path / "dhcp"
    events.mkdir()
    (events / "0a62a83dbb76.json").write_text(
        json.dumps({"mac": "0a:00:5e:00:53:76", "client_id": "01:0a:00:5e:00:53:76", "options": IOS_OPTIONS})
    )
    collector.ingest_identity_hints()
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT os_hint, fingerprint FROM devices WHERE key = 'id:01:0a:00:5e:00:53:76'").fetchone()
    assert row["os_hint"] == "iPhone or iPad" and row["fingerprint"].startswith(IOS_OPTIONS)


def test_sessions_follow_traffic(settings, host):
    host.write_leases(("0a:00:00:00:00:01", "192.168.1.50", None, None))
    collector = new_collector(settings)
    for total in (100, 200, 300):
        host.write_counters(up={"192.168.1.50": total}, down={"192.168.1.50": total})
        collector.tick()
    # Only download (servers still sending to an address the phone has left): no presence.
    host.write_counters(up={"192.168.1.50": 300}, down={"192.168.1.50": 900})
    collector.tick()
    conn = db.connect(settings.db_path)
    sessions = conn.execute("SELECT start, end FROM device_sessions").fetchall()
    assert len(sessions) == 1 and sessions[0]["end"] - sessions[0]["start"] == 20


def test_linker_merges_same_name_and_respects_split(settings, host):
    host.write_leases(
        ("0a:00:00:00:00:01", "192.168.1.50", "Mayas-iPhone", None),
        ("1e:00:00:00:00:02", "192.168.1.51", "Mayas-iPhone", None),
    )
    collector = new_collector(settings)
    collector.tick()
    conn = db.connect(settings.db_path)
    a, b = device_id(conn, "mac:0a:00:00:00:00:01"), device_id(conn, "mac:1e:00:00:00:00:02")
    now = int(time.time())
    conn.execute(
        "INSERT INTO device_sessions(device_id, start, end) VALUES(?, ?, ?), (?, ?, ?)",
        (a, now - 7200, now - 3600, b, now - 3500, now - 60),
    )
    collector.link_identities()
    assert db.device_roots(conn)[b] == a
    assert conn.execute("SELECT kind FROM identity_events").fetchone()["kind"] == "merged"

    # Later both are seen sending at the same time: Tattle undoes its own merge, for good.
    conn.execute("INSERT INTO device_sessions(device_id, start, end) VALUES(?, ?, ?), (?, ?, ?)", (a, now - 300, now, b, now - 300, now))
    collector.link_identities()
    assert db.device_roots(conn)[b] == b
    assert [r["kind"] for r in conn.execute("SELECT kind FROM identity_events ORDER BY id")] == ["merged", "split"]
    collector.link_identities()
    assert db.device_roots(conn)[b] == b


def test_admin_merges_are_never_split_automatically(settings, host):
    host.write_leases(("0a:00:00:00:00:01", "192.168.1.50", None, None), ("1e:00:00:00:00:02", "192.168.1.51", None, None))
    collector = new_collector(settings)
    collector.tick()
    conn = db.connect(settings.db_path)
    a, b = device_id(conn, "mac:0a:00:00:00:00:01"), device_id(conn, "mac:1e:00:00:00:00:02")
    conn.execute("UPDATE devices SET merged_into = ?, merge_reason = 'Merged by admin' WHERE id = ?", (a, b))
    now = int(time.time())
    conn.execute("INSERT INTO device_sessions(device_id, start, end) VALUES(?, ?, ?), (?, ?, ?)", (a, now - 600, now, b, now - 600, now))
    collector.link_identities()
    assert db.device_roots(conn)[b] == a


def test_merged_traffic_stays_on_its_identity(settings, host):
    host.write_leases(
        ("aa:aa:aa:aa:aa:01", "192.168.1.50", "phone-old", None),
        ("aa:aa:aa:aa:aa:02", "192.168.1.51", "phone", None),
    )
    collector = new_collector(settings)
    collector.tick()
    conn = db.connect(settings.db_path)
    old, new = device_id(conn, "mac:aa:aa:aa:aa:aa:01"), device_id(conn, "mac:aa:aa:aa:aa:aa:02")
    conn.execute("UPDATE devices SET merged_into = ? WHERE id = ?", (new, old))
    host.write_counters(up={}, down={"192.168.1.50": 40})
    collector.tick()
    rows = dict(conn.execute("SELECT device_id, SUM(rx) FROM usage_hourly GROUP BY device_id").fetchall())
    assert rows == {old: 40}  # the merge is applied when reading, so it can be undone



def test_apple_device_ids_link_but_are_not_names():
    uid = "00000000-0000-4000-8000-0000000000aa"
    assert name_strength(normalise_name(uid)) == "token"
    a = ident(1, "0a:00:00:00:00:01", [uid], [(0, 3600)])
    b = ident(2, "1e:00:00:00:00:02", [uid], [(3700, 7200)])
    (link,) = find_links([a, b], NOW)
    assert link.automatic and "device ID" in link.reason
