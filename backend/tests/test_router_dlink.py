"""D-Link DSL-245GE adapter against fake router pages.

The pages are made up and carry only what the adapter reads, in the form it
parses: Login.asp answers "1" for a good md5-hashed password; the data pages
come back without their data (the sign-in page) until the router knows the
client IP; Statistics.asp holds "EthRxTk00"/"EthTxTk00", Internet_setup.asp a
pppLists array with the PPPoE user third, home_DSL_Status.asp
"state|up|down|..." and Network.asp `var lan_value = {...}`. The PPPoE user
and password are made up.
"""

from __future__ import annotations

import hashlib
import urllib.parse

import pytest

from tattle.providers.http import Response
from tattle.routers import registry
from tattle.routers.base import RouterError
from tattle.routers.dlink_dsl245ge import DlinkDsl245ge

PASSWORD = "router-pass-1"
SIGN_IN_PAGE = "<html><head><title>Login</title></head><body><form name='login'></form></body></html>"
PAGES = {
    "/cgi-bin/get/New_GUI/Statistics.asp": '[{"EthRxTk00":"3912345678","EthTxTk00":"412345678","EthRxTk01":"0"}]',
    "/cgi-bin/get/New_GUI/Internet_setup.asp": 'var pppLists = ["1", "PPPoE", "ETIS_0221234567", "", "1492"];',
    "/cgi-bin/get/New_GUI/home_DSL_Status.asp": "up|5119 kbps|32766 kbps|N/A",
    "/cgi-bin/New_GUI/Network.asp": '<script>var lan_value = {"lan_ip":"192.168.1.1","lan_dhcp_type":"1"};</script>',
}


class FakeDlink:
    def __init__(self, pages=None):
        self.pages = dict(PAGES, **(pages or {}))
        self.signed_in = False
        self.logins: list[dict] = []

    def __call__(self, request) -> Response:
        url = urllib.parse.urlsplit(request.url)
        assert request.method == "GET"
        if url.path == "/cgi-bin/Login.asp":
            query = dict(urllib.parse.parse_qsl(url.query))
            self.logins.append(query)
            ok = query == {"User": "user", "Pwd": hashlib.md5(PASSWORD.encode()).hexdigest()}
            self.signed_in = ok
            return Response(200, {}, "1" if ok else "0", request.url)
        text = self.pages[url.path] if self.signed_in else SIGN_IN_PAGE
        return Response(200, {"content-type": "text/html"}, text, request.url)


def _router(fake, password=PASSWORD):
    return DlinkDsl245ge({"url": "192.168.1.1", "user": "user", "password": password}, transport=fake)


def test_signs_in_with_an_md5_password_when_a_page_comes_back_without_data():
    fake = FakeDlink()
    router = _router(fake)
    assert router.wan_counters() == (3_912_345_678, 412_345_678)
    assert len(fake.logins) == 1
    assert "Pwd" in fake.logins[0] and PASSWORD not in fake.logins[0].values()  # the hash, never the password
    router.wan_counters()
    assert len(fake.logins) == 1  # still signed in


def test_signs_in_again_after_the_router_forgot_the_session():
    fake = FakeDlink()
    router = _router(fake)
    router.wan_counters()
    fake.signed_in = False  # someone opened the web UI, which ends Tattle's sign-in
    assert router.wan_login() == "ETIS_0221234567"
    assert len(fake.logins) == 2


def test_wrong_password():
    with pytest.raises(RouterError, match="refused the user name or password"):
        _router(FakeDlink(), password="wrong").wan_counters()


def test_internet_login_from_the_ppp_list():
    assert _router(FakeDlink()).wan_login() == "ETIS_0221234567"
    empty = FakeDlink({"/cgi-bin/get/New_GUI/Internet_setup.asp": 'var pppLists = ["1", "PPPoE", "", ""];'})
    assert _router(empty).wan_login() is None


def test_line_status_order_is_state_up_down():
    assert _router(FakeDlink()).link_status() == {"state": "up", "up_kbps": 5119, "down_kbps": 32766}
    down = FakeDlink({"/cgi-bin/get/New_GUI/home_DSL_Status.asp": "down|N/A|N/A|N/A"})
    assert _router(down).link_status() == {"state": "down", "up_kbps": None, "down_kbps": None}


@pytest.mark.parametrize(("dhcp_type", "enabled"), [("1", True), ("0", False)])
def test_dhcp_server_state(dhcp_type, enabled):
    page = f'<script>var lan_value = {{"lan_ip":"192.168.1.1","lan_dhcp_type":"{dhcp_type}"}};</script>'
    assert _router(FakeDlink({"/cgi-bin/New_GUI/Network.asp": page})).dhcp_enabled() is enabled


def test_test_summary():
    assert _router(FakeDlink()).test() == [
        "internet counters 3,912,345,678 B received / 412,345,678 B sent",
        "internet login ETIS_0221234567",
        "line up, 32766 / 5119 kbps",
        "router DHCP on",
    ]


def test_page_without_its_data_after_signing_in():
    fake = FakeDlink({"/cgi-bin/get/New_GUI/Statistics.asp": "<html>maintenance</html>"})
    with pytest.raises(RouterError, match="not what Tattle expected"):
        _router(fake).wan_counters()


def test_counters_wrap_at_32_bits_and_no_device_counts():
    assert DlinkDsl245ge.wan_modulus == 2**32
    assert "device_counters" not in DlinkDsl245ge.capabilities
    assert registry()["dlink_dsl245ge"] is DlinkDsl245ge


def test_unreachable_router():
    with pytest.raises(RouterError, match="Could not reach the router"):
        DlinkDsl245ge({"url": "http://127.0.0.1:1", "password": PASSWORD}).wan_counters()
