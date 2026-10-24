"""Telenet / BASE provider against a fake transport.

The Okta sign-in and the /ocapi usage endpoints follow telenet_telemeter and
the VikingBar client; identifiers and figures are fake.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tattle.providers.base import ProviderError, SessionExpired
from tattle.providers.http import Response
from tattle.providers.telenet_be import TelenetBe, internet_products, parse_daily, parse_usage, state_token

USER = "user@example.com"
PASSWORD = "pw-xyz"
PRODUCT = "INET0001"

PLANS = [
    {
        "productType": "bundle",
        "label": "WIGO / internet + tv",
        "products": [{"productType": "internet", "identifier": PRODUCT, "label": "Internet 400 / FUP"}],
    }
]
CYCLE = {"billCycles": [{"startDate": "2026-10-01", "endDate": "2026-10-31"}]}
USAGE_FUP = {
    "internet": {
        "category": "FUP",
        "totalUsage": {"units": 123.5, "lastUsageDate": "2026-10-24T08:00:00.000+02:00"},
        "allocatedUsage": {"units": 400},
    }
}
DAILY = {
    "internetUsage": [
        {
            "totalUsage": {"peak": 100, "offPeak": 23.5},
            "dailyUsages": [
                {"date": "2026-10-23T00:00:00.000+02:00", "total": 12.0, "peak": 10, "offPeak": 2},
                {"date": "2026-10-24T00:00:00.000+02:00", "total": 8.0, "peak": 6, "offPeak": 2},
            ],
        }
    ]
}
SIGN_IN_PAGE = '<html><script>var x = {"stateToken":"tok\\x2D123","helpLinks":[]};</script></html>'


def _json(payload, status=200):
    return Response(status, {"content-type": "application/json"}, json.dumps(payload), "https://api.prd.telenet.be/x")


class FakeTelenet:
    def __init__(self, *, no_password=False, session_valid=True):
        self.no_password = no_password
        self.session_valid = session_valid
        self.calls: list[str] = []

    def __call__(self, request) -> Response:
        url = request.url
        self.calls.append(url)
        if "/ocapi/login/authorization/" in url:
            return Response(200, {"content-type": "text/html"}, SIGN_IN_PAGE, url)
        if url.endswith("/idp/idx/introspect"):
            return _json({"stateHandle": "handle-1"})
        if url.endswith("/auth/services/devicefingerprint"):
            return Response(200, {}, "", url)
        if url.endswith("/api/v1/internal/device/nonce"):
            return _json({"nonce": "n"})
        if url.endswith("/idp/idx/identify"):
            authenticators = [] if self.no_password else [{"type": "password", "id": "pw-id"}]
            return _json({"stateHandle": "handle-2", "authenticators": {"value": authenticators}})
        if url.endswith("/idp/idx/challenge"):
            return _json({"stateHandle": "handle-3"})
        if url.endswith("/idp/idx/challenge/answer"):
            return _json({"success": {"href": "https://api.prd.telenet.be/ocapi/login/callback/telenet_be?code=c"}})
        if "/ocapi/login/callback/" in url:
            return _json({"ok": True})
        # reads
        if not self.session_valid:
            return _json({"error": "unauthorized"}, status=401)
        if "product-subscriptions" in url:
            return _json(PLANS)
        if "billcycle-details" in url:
            return _json(CYCLE)
        if "/dailyusage" in url:
            return _json(DAILY)
        if url.rstrip("/").split("?")[0].endswith("/usage") or "/usage?" in url:
            return _json(USAGE_FUP)
        raise AssertionError(f"unexpected call {url}")


def test_state_token_unescapes_js():
    assert state_token(SIGN_IN_PAGE) == "tok-123"


def test_sign_in_and_usage_fup():
    fake = FakeTelenet()
    provider = TelenetBe(transport=fake)
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD, "brand": "telenet"})
    assert creds["brand"] == "telenet"
    lines = provider.lines(creds)
    assert [line.id for line in lines] == [PRODUCT]
    usage = provider.usage(creds, lines[0])
    plan = usage.bundles[0]
    assert plan.total_bytes == 400 * 1024**3
    assert plan.remaining_bytes == int(round((400 - 123.5) * 1024**3))
    assert usage.renews == dt.date(2026, 11, 1)  # day after the cycle's end
    assert usage.cycle_start == dt.date(2026, 10, 1)


def test_daily_history_in_bytes():
    provider = TelenetBe(transport=FakeTelenet())
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD})
    line = provider.lines(creds)[0]
    days = provider.daily(creds, line, dt.date(2026, 10, 1), dt.date(2026, 10, 31))
    assert days["2026-10-23"] == int(round(12.0 * 1024**3))
    assert days["2026-10-24"] == int(round(8.0 * 1024**3))


def test_base_brand_uses_base_hosts():
    fake = FakeTelenet()
    provider = TelenetBe(transport=fake)
    provider.start_sign_in({"username": USER, "password": PASSWORD, "brand": "base"})
    assert any("secure.base.be" in c for c in fake.calls)


def test_wrong_password_rejected():
    fake = FakeTelenet()

    def transport(request):
        if request.url.endswith("/idp/idx/challenge/answer"):
            return _json(
                {"messages": {"value": [{"message": "Password is incorrect."}]}}, status=401
            )
        return fake(request)

    provider = TelenetBe(transport=transport)
    with pytest.raises(ProviderError) as exc:
        provider.start_sign_in({"username": USER, "password": "wrong"})
    assert "incorrect" in str(exc.value).lower()
    assert not exc.value.signed_out


def test_passwordless_account_explained():
    provider = TelenetBe(transport=FakeTelenet(no_password=True))
    with pytest.raises(ProviderError) as exc:
        provider.start_sign_in({"username": USER, "password": PASSWORD})
    assert "password" in str(exc.value).lower()


def test_session_expired_then_refresh():
    fake = FakeTelenet()
    provider = TelenetBe(transport=fake)
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD})
    line = provider.lines(creds)[0]
    fake.session_valid = False
    with pytest.raises(SessionExpired):
        provider.usage(creds, line)
    fake.session_valid = True
    fresh = provider.refresh(creds)
    assert provider.usage(fresh, line).bundles


def test_refresh_rejected_signs_out():
    fake = FakeTelenet(no_password=True)
    provider = TelenetBe(transport=fake)
    with pytest.raises(ProviderError) as exc:
        provider.refresh({"brand": "telenet", "username": USER, "password": PASSWORD, "cookies": []})
    assert exc.value.signed_out is True


def test_unlimited_plan_has_no_quota():
    usage = parse_usage({"internet": {"category": "UNLIMITED", "totalUsage": {"units": 50}}}, (dt.date(2026, 10, 1), dt.date(2026, 10, 31)), "Giga")
    assert usage.bundles[0].total_bytes is None
    assert usage.data == []  # not counted as a quota


def test_internet_products_unwraps_bundle():
    assert [p["identifier"] for p in internet_products(PLANS)] == [PRODUCT]


def test_parse_daily_ignores_out_of_range():
    days = parse_daily(DAILY, dt.date(2026, 10, 24), dt.date(2026, 10, 24))
    assert set(days) == {"2026-10-24"}
