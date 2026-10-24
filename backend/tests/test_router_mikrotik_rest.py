"""MikroTik RouterOS REST adapter against a fake router.

Payload shapes follow what RouterOS 7.25 (CHR) answered in a test VM: every
value is a string, MACs are upper case, /ip/kid-control/device lists ARP
neighbours (internet side included) once a kid profile exists, and errors come
as {"error": 401|400|500, "message": ..., "detail": ...}. MACs are from the
RFC 7042 documentation range; addresses, names and passwords are made up.
"""

from __future__ import annotations

import base64
import json
import urllib.parse

import pytest

from tattle.providers.http import Response
from tattle.routers import registry
from tattle.routers.base import RouterError
from tattle.routers.mikrotik_rest import MikrotikRest

USER, PASSWORD = "tattle", "read-only-pass-1"

DEVICES = [
    {".id": "*1", "bytes-down": "13656815", "bytes-up": "1968475", "ip-address": "192.168.88.20", "mac-address": "00:00:5E:00:53:20", "name": ""},
    {".id": "*2", "bytes-down": "39073", "bytes-up": "23856", "ip-address": "192.0.2.1", "mac-address": "00:00:5E:00:53:01", "name": ""},  # the modem
    {".id": "*3", "bytes-down": "5000", "bytes-up": "600", "ip-address": "fe80::2,2001:db8::30", "mac-address": "00:00:5E:00:53:30", "name": ""},
    {".id": "*4", "bytes-down": "70", "bytes-up": "80", "ip-address": "192.168.88.21", "mac-address": "00:00:5E:00:53:21", "name": "living-room-tv"},
    {".id": "*5", "bytes-down": "1", "bytes-up": "1", "ip-address": "192.168.88.21", "mac-address": "00:00:5E:00:53:21", "name": ""},  # twice
    {".id": "*6", "bytes-down": "0", "bytes-up": "0", "ip-address": "fe80::99", "mac-address": "00:00:5E:00:53:99", "name": ""},  # ISP's IPv6 router
]
ARP = [
    {".id": "*1", "interface": "bridge", "mac-address": "00:00:5E:00:53:20"},
    {".id": "*2", "interface": "ether1", "mac-address": "00:00:5E:00:53:01"},
    {".id": "*3", "interface": "bridge", "mac-address": "00:00:5E:00:53:21"},
]
NEIGHBOURS = [{".id": "*1", "interface": "ether1", "mac-address": "00:00:5E:00:53:99"}]
LEASES = [{".id": "*1", "mac-address": "00:00:5E:00:53:20", "host-name": "maya-iphone"}, {".id": "*2", "mac-address": "00:00:5E:00:53:21"}]
INTERFACES = [
    {".id": "*1", "name": "ether1", "rx-byte": "14022527", "tx-byte": "2182710"},
    {".id": "*9", "name": "pppoe-out1", "rx-byte": "9000000000", "tx-byte": "800000000"},
]
PPPOE = [{".id": "*9", "disabled": "false", "interface": "ether1", "name": "pppoe-out1", "running": "true", "user": "example-user@isp"}]


class FakeRouterOS:
    def __init__(self, **tables):
        self.tables = {
            "ip/kid-control/device": DEVICES,
            "ip/kid-control": [{".id": "*1", "name": "tattle", "mon": "0s-1d"}],
            "ip/arp": ARP,
            "ipv6/neighbor": NEIGHBOURS,
            "ip/dhcp-server/lease": LEASES,
            "interface": INTERFACES,
            "interface/pppoe-client": PPPOE,
        }
        self.tables.update(tables)
        self.status: dict[str, tuple[int, dict]] = {}
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request) -> Response:
        url = urllib.parse.urlsplit(request.url)
        assert url.path.startswith("/rest/") and request.method == "GET"
        path = url.path[len("/rest/") :]
        query = dict(urllib.parse.parse_qsl(url.query))
        self.requests.append((path, query))
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            return self._json(401, {"error": 401, "message": "Unauthorized"})
        if path in self.status:
            return self._json(*self.status[path])
        if path not in self.tables:
            return self._json(400, {"detail": f"no such command or directory ({path})", "error": 400, "message": "Bad Request"})
        rows = [r for r in self.tables[path] if all(r.get(k) == v for k, v in query.items() if not k.startswith("."))]
        return self._json(200, rows)

    @staticmethod
    def _json(status, body):
        return Response(status, {"content-type": "application/json"}, json.dumps(body), "https://192.168.88.1/rest")


def _router(fake, **values):
    return MikrotikRest({"url": "192.168.88.1", "user": USER, "password": PASSWORD, **values}, transport=fake)


def test_device_counters_map_direction_and_skip_the_internet_side():
    fake = FakeRouterOS()
    counters = {c.mac: c for c in _router(fake).device_counters()}
    # the modem (ARP on ether1) and the ISP's IPv6 router (neighbour on ether1) are left out
    assert set(counters) == {"00:00:5e:00:53:20", "00:00:5e:00:53:30", "00:00:5e:00:53:21"}
    phone = counters["00:00:5e:00:53:20"]
    assert (phone.rx, phone.tx) == (13_656_815, 1_968_475)  # bytes-down is the download
    assert (phone.ip, phone.hostname) == ("192.168.88.20", "maya-iphone")  # name from the DHCP lease
    assert counters["00:00:5e:00:53:30"].ip is None  # IPv6 only
    tv = counters["00:00:5e:00:53:21"]
    assert (tv.rx, tv.hostname) == (70, "living-room-tv")  # the first entry wins; Kid Control's name first
    devices_query = dict(fake.requests)["ip/kid-control/device"]
    assert devices_query[".proplist"] == "mac-address,ip-address,bytes-down,bytes-up,name"


def test_https_by_default_and_certificate_check_off():
    fake = FakeRouterOS()
    router = _router(fake)
    assert router.http.base_url == "https://192.168.88.1"
    assert router.verify_tls is False
    assert _router(fake, verify_tls="yes").verify_tls is True


def test_without_a_kid_profile_explains_the_setup():
    fake = FakeRouterOS(**{"ip/kid-control/device": [], "ip/kid-control": []})
    with pytest.raises(RouterError, match="/ip kid-control add name=tattle mon=0s-1d"):
        _router(fake).device_counters()


def test_a_profile_and_no_devices_yet_is_fine():
    fake = FakeRouterOS(**{"ip/kid-control/device": []})
    assert _router(fake).device_counters() == []


def test_wan_counters_and_login():
    fake = FakeRouterOS()
    router = _router(fake)
    assert router.wan_counters() == (14_022_527, 2_182_710)  # rx-byte: what came in from the internet
    assert ("interface", {"name": "ether1", ".proplist": "name,rx-byte,tx-byte"}) in fake.requests
    assert router.wan_login() == "example-user@isp"  # the running PPPoE client
    assert router.wan_modulus is None and router.device_modulus is None


def test_pppoe_wan_leaves_out_the_port_it_dials_over():
    fake = FakeRouterOS()
    router = _router(fake, wan_interface="pppoe-out1")
    assert router.wan_counters() == (9_000_000_000, 800_000_000)
    assert "00:00:5e:00:53:01" not in {c.mac for c in router.device_counters()}  # ARP on ether1, under pppoe-out1


def test_wan_login_without_pppoe():
    assert _router(FakeRouterOS(**{"interface/pppoe-client": []})).wan_login() is None
    disabled = [dict(PPPOE[0], running="false", disabled="true", name="pppoe-old")]
    assert _router(FakeRouterOS(**{"interface/pppoe-client": disabled})).wan_login() is None


def test_unknown_wan_interface():
    with pytest.raises(RouterError, match="no interface called 'ether9'"):
        _router(FakeRouterOS(), wan_interface="ether9").wan_counters()


def test_test_summary():
    assert _router(FakeRouterOS()).test() == [
        "3 devices with traffic counters",
        "internet counters 14,022,527 B received / 2,182,710 B sent",
        "internet login example-user@isp",
    ]


def test_wrong_password():
    with pytest.raises(RouterError, match="refused the user name or password"):
        _router(FakeRouterOS(), password="wrong").device_counters()


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (500, {"detail": "not enough permissions (9)", "error": 500, "message": "Internal Server Error"}, "read, api and rest-api"),
        (500, {"detail": "std failure: not allowed (9)", "error": 500, "message": "Internal Server Error"}, "read, api and rest-api"),
        (404, {"error": 404, "message": "Not Found"}, "RouterOS 7.1 or newer"),
        (400, {"detail": "no such command or directory (kid-control)", "error": 400, "message": "Bad Request"}, "cannot show the Kid Control devices"),
        (503, {"error": 503, "message": "Service Unavailable"}, "status 503"),
    ],
)
def test_refusals_read_well(status, body, message):
    fake = FakeRouterOS()
    fake.status["ip/kid-control/device"] = (status, body)
    with pytest.raises(RouterError, match=message):
        _router(fake).device_counters()


def test_ipv6_menu_missing_is_not_fatal():
    fake = FakeRouterOS()
    del fake.tables["ipv6/neighbor"]
    assert len(_router(fake).device_counters()) == 4  # the ISP's IPv6 router can no longer be told apart


def test_garbled_counter():
    fake = FakeRouterOS(**{"ip/kid-control/device": [dict(DEVICES[0], **{"bytes-down": "lots"})]})
    with pytest.raises(RouterError, match="byte counter"):
        _router(fake).device_counters()


def test_unreachable_router():
    router = MikrotikRest({"url": "https://127.0.0.1:1", "user": USER, "password": PASSWORD})
    with pytest.raises(RouterError, match="Could not reach the router"):
        router.wan_counters()


def test_registered_as_beta():
    assert registry()["mikrotik_rest"] is MikrotikRest
    assert MikrotikRest.describe()["status"] == "beta"
