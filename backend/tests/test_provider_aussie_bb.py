"""Aussie Broadband provider against a fake transport.

Shapes follow pyaussiebb: /login answers {"expiresIn": N} + a myaussie_cookie,
/services lists services, /broadband/{id}/usage gives usedMb etc.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tattle.providers.base import Challenge, ProviderError, SessionExpired
from tattle.providers.http import Response
from tattle.providers.aussie_bb import AussieBb, parse_usage

USER = "user@example.com"
PASSWORD = "pw-abc"
TODAY = dt.date(2026, 10, 24)

SERVICES = {
    "data": [
        {"service_id": 12345, "type": "NBN", "plan": "NBN 100/20", "nextBillDate": "2026-11-01T13:00:00Z"},
        {"service_id": 99999, "type": "PhoneMobile", "plan": "Mobile"},
    ],
    "links": {"next": None},
}
USAGE = {"usedMb": 54321, "downloadedMb": 50000, "uploadedMb": 4321, "daysTotal": 28, "daysRemaining": 4}


def _json(payload, status=200, set_cookie=None):
    headers = {"content-type": "application/json"}
    if set_cookie:
        headers["set-cookie"] = set_cookie
    return Response(status, headers, json.dumps(payload), "https://myaussie-api.aussiebroadband.com.au/x")


class FakeAussie:
    def __init__(self, *, bad_password=False, two_factor=False):
        self.bad_password = bad_password
        self.two_factor = two_factor
        self.calls: list[str] = []

    def __call__(self, request) -> Response:
        url = request.url
        self.calls.append(url)
        if url.endswith("/login"):
            if self.bad_password:
                return _json({"message": "Invalid credentials"}, status=422)
            if self.two_factor:
                return _json({"message": "Two factor auth required"}, status=200)
            return _json({"expiresIn": 3600}, status=200, set_cookie="myaussie_cookie=cookie-1; Path=/; HttpOnly")
        if url.endswith("/2fa/send"):
            return _json({"sent": True})
        if url.endswith("/2fa/verify"):
            return _json({"verified": True})
        if "/services" in url:
            return _json(SERVICES)
        if "/broadband/12345/usage" in url:
            return _json(USAGE)
        raise AssertionError(f"unexpected call {url}")


def _provider(fake):
    return AussieBb(transport=fake, today=lambda: TODAY)


def test_sign_in_and_usage():
    provider = _provider(FakeAussie())
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD})
    assert creds["cookie"] == "cookie-1"
    lines = provider.lines(creds)
    assert [line.id for line in lines] == ["12345"]  # the mobile service is dropped
    usage = provider.usage(creds, lines[0])
    plan = usage.bundles[0]
    assert plan.remaining_bytes is None  # unlimited: no remainingMb in the payload
    assert plan.total_bytes is None
    assert usage.renews == TODAY + dt.timedelta(days=4)
    assert usage.cycle_start == TODAY + dt.timedelta(days=4) - dt.timedelta(days=28)


def test_usage_with_quota_plan_has_remaining():
    usage = parse_usage({"usedMb": 1000, "remainingMb": 4000, "daysTotal": 30, "daysRemaining": 10}, "Capped", TODAY)
    plan = usage.bundles[0]
    assert plan.total_bytes == 5000 * 1000**2
    assert plan.remaining_bytes == 4000 * 1000**2


def test_wrong_password_is_friendly_error():
    provider = _provider(FakeAussie(bad_password=True))
    with pytest.raises(ProviderError) as exc:
        provider.start_sign_in({"username": USER, "password": "nope"})
    assert "user name or password" in str(exc.value).lower()
    assert not exc.value.signed_out


def test_two_factor_challenge_then_answer():
    fake = FakeAussie(two_factor=True)
    provider = _provider(fake)
    challenge = provider.start_sign_in({"username": USER, "password": PASSWORD})
    assert isinstance(challenge, Challenge)
    assert challenge.kind == "code"
    fake.two_factor = False  # after verifying the code the account signs in normally
    creds = provider.answer({"username": USER, "password": PASSWORD}, challenge, "123456")
    assert creds["cookie"] == "cookie-1"


def test_session_expired_then_refresh():
    fake = FakeAussie()
    provider = _provider(fake)
    creds = provider.start_sign_in({"username": USER, "password": PASSWORD})
    lines = provider.lines(creds)
    creds["expires_at"] = 0  # session ran out
    with pytest.raises(SessionExpired):
        provider.usage(creds, lines[0])
    fresh = provider.refresh(creds)
    assert fresh["cookie"] == "cookie-1"
    assert provider.usage(fresh, lines[0]).bundles


def test_refresh_rejected_signs_out():
    provider = _provider(FakeAussie(bad_password=True))
    with pytest.raises(ProviderError) as exc:
        provider.refresh({"username": USER, "password": "nope"})
    assert exc.value.signed_out is True
