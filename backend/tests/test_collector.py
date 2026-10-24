from __future__ import annotations

from helpers import host_clock

from tattle import db
from tattle.collector import Collector, LiveState


def usage(settings, key: str) -> tuple[int, int]:
    conn = db.connect(settings.db_path)
    row = conn.execute(
        "SELECT COALESCE(SUM(u.rx), 0) AS rx, COALESCE(SUM(u.tx), 0) AS tx FROM usage_hourly u "
        "JOIN devices d ON d.id = u.device_id WHERE d.key = ?",
        (key,),
    ).fetchone()
    return row["rx"], row["tx"]


def new_collector(settings) -> Collector:
    db.init(settings.db_path)
    return Collector(settings, LiveState(), clock=host_clock(settings))


def test_counts_deltas_per_device(settings, host):
    host.write_leases(("aa:aa:aa:aa:aa:01", "192.168.1.50", "phone", None))
    collector = new_collector(settings)

    host.write_counters(up={"192.168.1.50": 1_000}, down={"192.168.1.50": 50_000})
    collector.tick()
    assert usage(settings, "mac:aa:aa:aa:aa:aa:01") == (50_000, 1_000)

    host.write_counters(up={"192.168.1.50": 1_500}, down={"192.168.1.50": 80_000})
    collector.tick()
    assert usage(settings, "mac:aa:aa:aa:aa:aa:01") == (80_000, 1_500)


def test_same_snapshot_is_not_counted_twice(settings, host):
    host.write_leases(("aa:aa:aa:aa:aa:01", "192.168.1.50", "phone", None))
    collector = new_collector(settings)
    host.write_counters(up={"192.168.1.50": 10}, down={"192.168.1.50": 20})
    collector.tick()
    collector.tick()
    assert usage(settings, "mac:aa:aa:aa:aa:aa:01") == (20, 10)


def test_ip_handed_to_another_device_is_attributed_to_the_new_holder(settings, host):
    host.write_leases(("aa:aa:aa:aa:aa:01", "192.168.1.50", "old", None))
    collector = new_collector(settings)
    host.write_counters(up={}, down={"192.168.1.50": 100})
    collector.tick()

    host.write_leases(("bb:bb:bb:bb:bb:02", "192.168.1.50", "new", None))
    host.write_counters(up={}, down={"192.168.1.50": 350})
    collector.tick()

    assert usage(settings, "mac:aa:aa:aa:aa:aa:01") == (100, 0)
    assert usage(settings, "mac:bb:bb:bb:bb:bb:02") == (250, 0)


def test_table_reload_counts_from_zero(settings, host):
    host.write_leases(("aa:aa:aa:aa:aa:01", "192.168.1.50", "phone", None))
    collector = new_collector(settings)
    host.write_counters(up={}, down={"192.168.1.50": 5_000}, handle=7)
    collector.tick()
    # Table reloaded (reboot): counters restart, new handle.
    host.write_counters(up={}, down={"192.168.1.50": 300}, handle=9)
    collector.tick()
    assert usage(settings, "mac:aa:aa:aa:aa:aa:01") == (5_300, 0)


def test_restart_continues_from_saved_counters(settings, host):
    host.write_leases(("aa:aa:aa:aa:aa:01", "192.168.1.50", "phone", None))
    collector = new_collector(settings)
    host.write_counters(up={}, down={"192.168.1.50": 1_000})
    collector.tick()

    restarted = new_collector(settings)
    host.write_counters(up={}, down={"192.168.1.50": 1_400})
    restarted.tick()
    assert usage(settings, "mac:aa:aa:aa:aa:aa:01") == (1_400, 0)


def test_client_id_distinguishes_devices_sharing_a_mac(settings, host):
    # A MAC-NAT extender may present one MAC for several clients.
    host.write_leases(
        ("0e:00:5e:00:53:e1", "192.168.1.60", "tablet", "01:aa:00:00:00:00:01"),
        ("0e:00:5e:00:53:e1", "192.168.1.61", "tv", "01:aa:00:00:00:00:02"),
    )
    collector = new_collector(settings)
    host.write_counters(up={}, down={"192.168.1.60": 10, "192.168.1.61": 20})
    collector.tick()
    assert usage(settings, "id:01:aa:00:00:00:00:01") == (10, 0)
    assert usage(settings, "id:01:aa:00:00:00:00:02") == (20, 0)


def test_device_without_lease_is_keyed_by_ip(settings, host):
    collector = new_collector(settings)
    host.write_counters(up={"192.168.1.77": 5}, down={})
    collector.tick()
    assert usage(settings, "ip:192.168.1.77") == (0, 5)


def test_server_traffic_is_its_own_device(settings, host):
    collector = new_collector(settings)
    host.write_counters(up={}, down={}, server=(100, 900))
    collector.tick()
    assert usage(settings, "server") == (900, 100)


def test_idle_devices_with_leases_are_listed(settings, host):
    host.write_leases(("00:00:5e:00:53:25", "192.168.1.74", None, None))
    collector = new_collector(settings)
    collector.tick()
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM devices WHERE key = 'mac:00:00:5e:00:53:25'").fetchone()[0] == 1


def test_live_rates_use_snapshot_interval(settings, host):
    host.write_leases(("aa:aa:aa:aa:aa:01", "192.168.1.50", "phone", None))
    collector = new_collector(settings)
    host.write_counters(up={}, down={"192.168.1.50": 0})
    collector.tick()
    host.write_counters(up={}, down={"192.168.1.50": 10_000}, advance=5.0)
    collector.tick()
    device_id = collector.key_ids["mac:aa:aa:aa:aa:aa:01"]
    assert collector.live.rates[device_id] == (2_000.0, 0.0)
