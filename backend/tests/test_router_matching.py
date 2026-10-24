from __future__ import annotations

from helpers import host_clock

from tattle import db
from tattle.collector import Collector, LiveState
from tattle.routers import DeviceCounter, RouterAdapter


class FakeRouter(RouterAdapter):
    """Like the D-Link: router-wide counters (32-bit), the internet login, the line."""

    id = "fake"
    name = "Fake router"
    capabilities = frozenset({"wan_counters", "wan_login", "link_status", "dhcp_enabled"})
    wan_modulus = 2**32

    def __init__(self, readings):
        super().__init__({})
        self.readings = list(readings)

    def wan_counters(self):
        return self.readings.pop(0)

    def dhcp_enabled(self):
        return False

    def wan_login(self):
        return "ETIS_0221234567"

    def link_status(self):
        return {"state": "up", "up_kbps": 5_000, "down_kbps": 30_000}


class CountingRouter(RouterAdapter):
    """Like OpenWrt or UniFi: running totals per device."""

    id = "counting"
    name = "Counting router"
    capabilities = frozenset({"device_counters"})
    poll_seconds = 0

    def __init__(self):
        super().__init__({})
        self.devices: list[DeviceCounter] = []

    def device_counters(self):
        return list(self.devices)


def new_collector(settings) -> Collector:
    db.init(settings.db_path)
    return Collector(settings, LiveState(), clock=host_clock(settings))


def wan_totals(settings):
    conn = db.connect(settings.db_path)
    return conn.execute(
        "SELECT SUM(rx) rx, SUM(tx) tx, SUM(dev_rx) dev_rx, SUM(dev_tx) dev_tx FROM wan_hourly"
    ).fetchone()


def usage(settings, key: str) -> tuple[int, int]:
    conn = db.connect(settings.db_path)
    row = conn.execute(
        "SELECT COALESCE(SUM(u.rx), 0) AS rx, COALESCE(SUM(u.tx), 0) AS tx FROM usage_hourly u "
        "JOIN devices d ON d.id = u.device_id WHERE d.key = ?",
        (key,),
    ).fetchone()
    return row["rx"], row["tx"]


def test_router_comparison_covers_the_same_interval(settings, host):
    host.write_leases(("00:00:5e:00:53:01", "192.168.1.50", "phone", None))
    collector = new_collector(settings)
    collector.router = FakeRouter([(1_000, 100), (1_000 + 5_300, 100 + 400)])

    # Traffic counted before the first router poll must not enter the comparison.
    host.write_counters(up={"192.168.1.50": 50}, down={"192.168.1.50": 9_999})
    collector.tick()
    collector.poll_router()
    host.write_counters(up={"192.168.1.50": 350}, down={"192.168.1.50": 9_999 + 5_000})
    collector.tick()
    collector.state["router_ts"] -= 60  # a minute between polls
    collector.poll_router()

    row = wan_totals(settings)
    assert (row["rx"], row["tx"]) == (5_300, 400)
    assert (row["dev_rx"], row["dev_tx"]) == (5_000, 300)


def test_router_counter_wrap_is_handled(settings, host):
    collector = new_collector(settings)
    collector.router = FakeRouter([(2**32 - 1_000, 0), (500, 0)])
    host.write_counters(up={}, down={})
    collector.tick()
    collector.poll_router()
    collector.state["router_ts"] -= 60
    collector.poll_router()
    assert wan_totals(settings)["rx"] == 1_500


def test_router_reboot_is_not_counted_as_traffic(settings, host):
    collector = new_collector(settings)
    collector.router = FakeRouter([(3_000_000_000, 0), (20_000, 0)])
    host.write_counters(up={}, down={})
    collector.tick()
    collector.poll_router()
    collector.state["router_ts"] -= 60
    collector.poll_router()
    assert wan_totals(settings)["rx"] == 20_000


def test_router_poll_records_which_line_is_in_use(settings):
    from tattle.isp import ROUTER_LINK_SETTING, ROUTER_LOGIN_SETTING

    collector = new_collector(settings)
    collector.router = FakeRouter([(0, 0)])
    collector.poll_router()
    assert db.get_setting(collector.conn, ROUTER_LOGIN_SETTING) == "ETIS_0221234567"
    assert collector.live.router_internet_login == "ETIS_0221234567"
    assert '"down_kbps": 30000' in db.get_setting(collector.conn, ROUTER_LINK_SETTING)


def test_router_mode_books_per_mac_differences(settings, host):
    collector = new_collector(settings)
    router = CountingRouter()
    collector.router = router
    phone, tv = "00:00:5e:00:53:21", "00:00:5e:00:53:22"

    # First sight: the totals so far were before Tattle watched.
    router.devices = [DeviceCounter(phone, rx=9_000_000, tx=1_000_000, ip="192.168.1.41", hostname="maya-iphone")]
    collector.tick()
    assert collector.live.mode == "router"
    assert usage(settings, f"mac:{phone}") == (0, 0)

    collector.prev_router_counters_at -= 60
    router.devices = [
        DeviceCounter(phone, rx=9_500_000, tx=1_100_000, ip="192.168.1.41"),
        DeviceCounter(tv, rx=40_000, tx=2_000, ip="192.168.1.42", hostname="living-room-tv"),
    ]
    collector.tick()
    assert usage(settings, f"mac:{phone}") == (500_000, 100_000)
    assert usage(settings, f"mac:{tv}") == (0, 0)

    # The TV leaves and comes back with restarted counters: all of it is new.
    collector.prev_router_counters_at -= 60
    router.devices = [DeviceCounter(phone, rx=9_600_000, tx=1_100_000)]
    collector.tick()
    collector.prev_router_counters_at -= 60
    router.devices = [DeviceCounter(phone, rx=9_600_000, tx=1_100_000), DeviceCounter(tv, rx=3_000, tx=100)]
    collector.tick()
    assert usage(settings, f"mac:{phone}") == (600_000, 100_000)
    assert usage(settings, f"mac:{tv}") == (3_000, 100)

    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT hostname FROM devices WHERE key = ?", (f"mac:{tv}",)).fetchone()[0] == "living-room-tv"


def test_router_mode_skips_impossible_jumps(settings, make_settings):
    custom = make_settings(max_mbps=100)  # 100 Mbps line: ~1.1 GB a minute at most
    collector = new_collector(custom)
    router = CountingRouter()
    collector.router = router
    mac = "00:00:5e:00:53:31"
    router.devices = [DeviceCounter(mac, rx=0, tx=0)]
    collector.tick()
    collector.prev_router_counters_at -= 60
    router.devices = [DeviceCounter(mac, rx=50_000_000_000, tx=0)]
    collector.tick()
    assert usage(custom, f"mac:{mac}") == (0, 0)


def test_gateway_snapshot_wins_over_router_counters(settings, host):
    host.write_leases(("00:00:5e:00:53:01", "192.168.1.50", "phone", None))
    collector = new_collector(settings)
    collector.router = CountingRouter()
    host.write_counters(up={"192.168.1.50": 10}, down={"192.168.1.50": 100})
    collector.tick()
    assert collector.live.mode == "gateway"
    assert usage(settings, "mac:00:00:5e:00:53:01") == (100, 10)
