"""Ogero provider against a fake transport.

The HTML follows pyogero's fixtures (MyConsumptionGrid rows, the dashboard line
selector, the bill table); numbers and names are fake.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tattle.providers.base import ProviderError, SessionExpired
from tattle.providers.http import Response
from tattle.providers.ogero_lb import OgeroLb, parse_bills, parse_consumption, parse_html, size

USER = "user@example.com"
PASSWORD = "pw-ogero"
TODAY = dt.date(2026, 10, 24)

DASHBOARD = """
<html><body>
<select name="changnumber" id="changnumber">
  <option value="01000000" value2="L000001" selected>L000001 / 01 000000</option>
  <option value="01000001" value2="L000002">L000002 / 01 000001</option>
</select>
</body></html>
"""
CONSUMPTION = """
<html><body>
<div class="MyConsumptionLeft">
  <div class="MyConsumptionGrid"><span>Current Bundle</span><span>up to 50Mbps (FUP)</span></div>
  <div class="MyConsumptionGrid"><span>Total Quota</span><span>800 GB FUP</span></div>
  <div class="MyConsumptionGrid"><span>Total Consumption</span><span>300 GB</span></div>
  <div class="MyConsumptionGrid"><span>Extra Consumption</span><span>0 GB</span></div>
  <div class="MyConsumptionGrid"><span>Consumption Until</span><span>24/10/2026 12:00</span></div>
</div>
</body></html>
"""
BILL = """
<html><body>
<div class="BillOutstandingSection1">Your Outstanding bills value: <span>3,950,000 L.L.</span></div>
<table class="BillTable">
  <tr><th></th><th>Bill Value</th><th>Payment Status</th><th>Operations</th></tr>
  <tr><td><div class="BillDate"><div>May</div><span>2026</span></div></td>
      <td><div class="BillAmount">3,950,000 L.L.</div></td><td>Not paid</td><td></td></tr>
  <tr><td><div class="BillDate"><div>Apr</div><span>2026</span></div></td>
      <td><div class="BillAmount">2,820,000 L.L.</div></td><td>Paid</td><td></td></tr>
</table>
</body></html>
"""
LOGGED_OUT = '<html><script language="javascript">window.location="login.php?error=You are required to login first";</script></html>'


def _html(body, status=200):
    return Response(status, {"content-type": "text/html"}, body, "https://ogero.gov.lb/myogero/x")


class FakeOgero:
    def __init__(self, *, bad_password=False, logged_out=False):
        self.bad_password = bad_password
        self.logged_out = logged_out
        self.calls: list[str] = []

    def __call__(self, request) -> Response:
        url = request.url
        self.calls.append(url)
        if url.endswith("/API/Login.php"):
            if self.bad_password:
                return Response(400, {"content-type": "application/json"}, json.dumps({"error": {"code": 1, "message": "Wrong username or password"}}), url)
            return Response(200, {"content-type": "application/json"}, json.dumps({"SessionID": "sess-1"}), url)
        if self.logged_out:
            return _html(LOGGED_OUT)
        if "mobileapp.dashboard.php" in url:
            return _html(DASHBOARD)
        if "consumption.php" in url:
            return _html(CONSUMPTION)
        if "bill.php" in url:
            return _html(BILL)
        raise AssertionError(f"unexpected call {url}")


def _provider(fake):
    return OgeroLb(transport=fake, today=lambda: TODAY)


def test_sign_in_lines_and_usage():
    provider = _provider(FakeOgero())
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD})
    assert creds["session"] == "sess-1"
    lines = provider.lines(creds)
    assert [line.id for line in lines] == ["L000001", "L000002"]
    usage = provider.usage(creds, lines[0])
    plan = usage.bundles[0]
    assert plan.total_bytes == 800 * 1000**3
    assert plan.remaining_bytes == (800 - 300) * 1000**3
    assert usage.cycle_start == dt.date(2026, 10, 1)
    assert usage.renews == dt.date(2026, 11, 1)


def test_billing_reads_outstanding_and_table():
    provider = _provider(FakeOgero())
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD})
    line = provider.lines(creds)[0]
    bills = provider.billing(creds, line)
    assert bills["open_amount"] == 3_950_000
    assert bills["currency"] == "LBP"
    assert len(bills["bills"]) == 2
    assert len(bills["unpaid"]) == 1  # only the "Not paid" one


def test_wrong_password_is_friendly_error():
    provider = _provider(FakeOgero(bad_password=True))
    with pytest.raises(ProviderError) as exc:
        provider.start_sign_in({"username": USER, "password": "nope"})
    assert "username or password" in str(exc.value).lower()
    assert not exc.value.signed_out


def test_logged_out_page_raises_session_expired():
    fake = FakeOgero()
    provider = _provider(fake)
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD})
    fake.logged_out = True
    with pytest.raises(SessionExpired):
        provider.lines(creds)


def test_refresh_relogins():
    fake = FakeOgero()
    provider = _provider(fake)
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD})
    fresh = provider.refresh(creds)
    assert fresh["session"] == "sess-1"


def test_refresh_rejected_signs_out():
    provider = _provider(FakeOgero(bad_password=True))
    with pytest.raises(ProviderError) as exc:
        provider.refresh({"username": USER, "password": "nope"})
    assert exc.value.signed_out is True


def test_size_handles_unlimited_and_units():
    assert size("Unlimited") == float("inf")
    assert size("800 GB FUP") == 800 * 1000**3
    assert size("512 MB") == 512 * 1000**2
    assert size("??") is None


def test_unlimited_quota_has_no_total():
    html = CONSUMPTION.replace("800 GB FUP", "Unlimited")
    usage = parse_consumption(parse_html(html), TODAY)
    assert usage.bundles[0].total_bytes is None


def test_parse_bills_direct():
    bills = parse_bills(parse_html(BILL))
    assert [b["value"] for b in bills["bills"]] == [3_950_000, 2_820_000]
