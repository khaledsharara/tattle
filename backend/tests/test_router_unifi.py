"""UniFi adapter against a fake console and a fake self-hosted server.

Self-hosted answers follow UniFi Network 10.0.162 in a test container: / is a
302, /api/login takes JSON, a wrong password is 400 api.err.Invalid, no
session is 401 api.err.LoginRequired (also for /api/auth/login), a wrong site
is 401 api.err.NoSiteContext. Console behaviour (/ answers 200, /api/auth/login,
/proxy/network, x-csrf-token) follows the unpoller library (unifi.go). Client
fields follow unpoller's Client struct; the direction follows Ubiquiti's own
client list, which shows tx_bytes / wired-tx_bytes as "Down". MACs are from
the RFC 7042 documentation range; names, keys and passwords are made up.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from tattle.providers.http import Response
from tattle.routers import registry
from tattle.routers.base import RouterError
from tattle.routers.unifi import Unifi

USER, PASSWORD, API_KEY = "tattle", "view-only-pass-1", "fake-api-key-0000"

CLIENTS = [
    {"mac": "00:00:5e:00:53:20", "ip": "192.168.1.20", "hostname": "maya-iphone", "is_wired": False, "tx_bytes": 7_340_032, "rx_bytes": 524_288},
    {
        "mac": "00:00:5E:00:53:21", "ip": "192.168.1.61", "name": "living-room-tv", "hostname": "android-0000",
        "is_wired": True, "wired-tx_bytes": "104857600", "wired-rx_bytes": "2097152", "tx_bytes": 1000, "rx_bytes": 2000,
    },  # fmt: skip
    {"mac": "00:00:5e:00:53:22", "is_wired": "false"},  # just joined, no counters yet
    {"ip": "192.168.1.23"},  # no MAC
]


def _json(status, body, url, headers=None):
    return Response(status, {"content-type": "application/json", **(headers or {})}, json.dumps(body), url)


def _meta(rc="ok", msg=None, data=()):
    meta = {"rc": rc, **({"msg": msg} if msg else {})}
    return {"meta": meta, "data": list(data)}


class FakeController:
    """console=True: UniFi OS (UDM, UCG...). console=False: a self-hosted UniFi Network server."""

    def __init__(self, *, console=True, root_status=None, login_status=200, login_body=None):
        self.console = console
        self.root_status = root_status if root_status is not None else (200 if console else 302)
        self.login_status, self.login_body = login_status, login_body
        self.signed_in = False
        self.csrf = "csrf-token-1"
        self.logins = 0
        self.expire_times = 0  # how many stat/sta calls answer 401 before working
        self.next_csrf: str | None = None
        self.seen: list[tuple[str, str, dict]] = []

    def __call__(self, request) -> Response:
        url = urllib.parse.urlsplit(request.url)
        path = url.path
        self.seen.append((request.method, path, dict(request.headers)))
        if path == "/":
            return Response(self.root_status, {"location": "/manage"} if self.root_status == 302 else {}, "", request.url)
        if path == "/api/auth/login":
            if not self.console:
                return _json(401, _meta("error", "api.err.LoginRequired"), request.url)
            return self._login(request, {"x-csrf-token": self.csrf})
        if path == "/api/login":
            if self.console:
                return Response(404, {}, "", request.url)
            return self._login(request, {})
        prefix = "/proxy/network" if self.console else ""
        assert path.startswith(prefix + "/api/"), path
        api = path[len(prefix) :]
        if self.console and request.headers.get("X-API-Key"):
            if request.headers["X-API-Key"] != API_KEY:
                return _json(401, {"error": {"code": 401, "message": "Unauthorized"}}, request.url)
        else:
            if not self.signed_in or (self.console and request.headers.get("X-CSRF-Token") != self.csrf):
                return _json(401, _meta("error", "api.err.LoginRequired"), request.url)
        if api == "/api/self/sites":
            return _json(200, _meta(data=[{"name": "default", "desc": "Default"}, {"name": "x7k2p9q1", "desc": "Garden"}]), request.url)
        if api != "/api/s/default/stat/sta":
            return _json(401, _meta("error", "api.err.NoSiteContext"), request.url)
        if self.expire_times:
            self.expire_times -= 1
            self.signed_in = False
            return _json(401, _meta("error", "api.err.LoginRequired"), request.url)
        headers = {}
        if self.next_csrf:
            self.csrf, self.next_csrf = self.next_csrf, None
            headers["x-updated-csrf-token"] = self.csrf
        return _json(200, _meta(data=CLIENTS), request.url, headers)

    def _login(self, request, headers):
        sent = json.loads(request.body)
        if self.login_status != 200:
            return _json(self.login_status, self.login_body or {}, request.url)
        if sent != {"username": USER, "password": PASSWORD}:
            if self.console:
                return _json(401, {"code": "AUTHENTICATION_FAILED_INVALID_CREDENTIALS", "message": "Invalid username or password"}, request.url)
            return _json(400, _meta("error", "api.err.Invalid"), request.url)
        self.logins += 1
        self.signed_in = True
        return _json(200, {"unique_id": "0", "username": USER} if self.console else _meta(), request.url, headers)

    def paths(self) -> list[str]:
        return [f"{method} {path}" for method, path, _ in self.seen]


def _unifi(fake, **values):
    return Unifi({"url": "192.168.1.1", "user": USER, "password": PASSWORD, **values}, transport=fake)


def test_console_signs_in_and_maps_download_direction():
    fake = FakeController(console=True)
    router = _unifi(fake)
    counters = {c.mac: c for c in router.device_counters()}
    assert fake.paths() == ["GET /", "POST /api/auth/login", "GET /proxy/network/api/s/default/stat/sta"]
    assert fake.seen[-1][2]["X-CSRF-Token"] == "csrf-token-1"
    assert set(counters) == {"00:00:5e:00:53:20", "00:00:5e:00:53:21"}  # no counters / no MAC: skipped
    phone = counters["00:00:5e:00:53:20"]
    assert (phone.rx, phone.tx) == (7_340_032, 524_288)  # tx_bytes = sent to the phone = its download
    assert (phone.ip, phone.hostname) == ("192.168.1.20", "maya-iphone")
    tv = counters["00:00:5e:00:53:21"]
    assert (tv.rx, tv.tx) == (104_857_600, 2_097_152)  # wired: the wired-* counters, as strings
    assert tv.hostname == "living-room-tv"  # the name given in UniFi beats the DHCP host name


def test_self_hosted_server():
    fake = FakeController(console=False)
    router = _unifi(fake, url="https://192.168.1.10:8443")
    assert router.test() == ["2 devices with traffic counters", "site default, signed in to a UniFi Network server"]
    assert fake.paths() == ["GET /", "POST /api/login", "GET /api/s/default/stat/sta"]


def test_self_hosted_behind_a_proxy_that_answers_root():
    """/ said 'console', but /api/auth/login answers 401 LoginRequired as self-hosted servers do."""
    fake = FakeController(console=False, root_status=200)
    assert len(_unifi(fake).device_counters()) == 2
    assert fake.paths()[1:3] == ["POST /api/auth/login", "POST /api/login"]


def test_signs_in_again_once_when_the_session_ran_out():
    fake = FakeController(console=True)
    router = _unifi(fake)
    router.device_counters()
    fake.expire_times = 1
    assert len(router.device_counters()) == 2
    assert fake.logins == 2


def test_does_not_loop_when_signed_out_again():
    fake = FakeController(console=False)
    fake.expire_times = 5
    with pytest.raises(RouterError, match="signed Tattle out right after signing in"):
        _unifi(fake).device_counters()
    assert fake.logins == 2


def test_follows_a_new_csrf_token():
    fake = FakeController(console=True)
    router = _unifi(fake)
    fake.next_csrf = "csrf-token-2"
    router.device_counters()
    router.device_counters()
    assert fake.seen[-1][2]["X-CSRF-Token"] == "csrf-token-2"
    assert fake.logins == 1


@pytest.mark.parametrize("console", [True, False])
def test_wrong_password(console):
    with pytest.raises(RouterError, match="refused the user name or password"):
        _unifi(FakeController(console=console), password="wrong").device_counters()


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (429, {"message": "Too many requests"}, "wait a few minutes"),
        (499, {"code": "MFA_AUTH_REQUIRED"}, "local account"),
        (502, {}, "status 502"),
    ],
)
def test_console_sign_in_refusals(status, body, message):
    with pytest.raises(RouterError, match=message):
        _unifi(FakeController(console=True, login_status=status, login_body=body)).device_counters()


def test_wrong_site_lists_the_real_ones():
    fake = FakeController(console=False)
    with pytest.raises(RouterError, match="no site called 'home'; it has: default, x7k2p9q1"):
        _unifi(fake, site="home").device_counters()
    assert fake.logins == 1  # a wrong site is not a lost session


def test_api_key_skips_signing_in():
    fake = FakeController(console=True)
    router = Unifi({"url": "https://192.168.1.1", "api_key": API_KEY}, transport=fake)
    assert router.test() == ["2 devices with traffic counters", "site default, with an API key"]
    assert fake.paths() == ["GET /proxy/network/api/s/default/stat/sta"]
    assert fake.seen[0][2]["X-API-Key"] == API_KEY


def test_api_key_refused():
    with pytest.raises(RouterError, match="refused the API key"):
        Unifi({"api_key": "wrong-key"}, transport=FakeController(console=True)).device_counters()


def test_needs_credentials():
    with pytest.raises(RouterError, match="user name and password, or an API key"):
        Unifi({"url": "https://192.168.1.1"}, transport=FakeController()).device_counters()


def test_unreachable_controller():
    with pytest.raises(RouterError, match="Could not reach the UniFi controller"):
        Unifi({"url": "https://127.0.0.1:1", "user": USER, "password": PASSWORD}).device_counters()


def test_registered_as_beta():
    assert registry()["unifi"] is Unifi
    assert Unifi.describe()["status"] == "beta"
    assert Unifi.capabilities == frozenset({"device_counters"})  # stat/health has WAN rates, not totals
