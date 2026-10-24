"""Vodafone Egypt provider against a fake transport.

Shapes follow the website flow (Keycloak auth-code) and the DXL product
inventory ADSL profile (parts.productOffering[].productOfferingTerm[].quota);
values are in KB of 1024, as the reference notes.
"""

from __future__ import annotations

import json

import pytest

from tattle.providers.base import ProviderError, SessionExpired
from tattle.providers.http import Response
from tattle.providers.vodafone_eg import VodafoneEg, parse_adsl_profile

NUMBER = "01012345678"
PASSWORD = "pw-123"
LOGIN_FORM = "https://web.vodafone.com.eg/auth/realms/vf-realm/login-actions/authenticate?session_code=xyz"

# 200 GB in KB-of-1024 (209,715,200), as the reference saw it.
PROFILE = [
    {
        "parts": {
            "productOffering": [
                {
                    "name": "VDSL 200",
                    "specification": {"characteristicsValue": [{"name": "matrixFlag", "value": "Y"}]},
                    "productOfferingTerm": [
                        {
                            "quota": {"total": 209_715_200, "consumed": 157_286_400, "amount": 52_428_800, "units": "KB"},
                            "validFor": {"endDateTime": "2026-02-01T00:00:00Z"},
                        }
                    ],
                }
            ]
        }
    }
]


def _json(payload, status=200):
    return Response(status, {"content-type": "application/json"}, json.dumps(payload), "https://web.vodafone.com.eg/x")


def _html(body, status=200, location=None):
    headers = {"content-type": "text/html"}
    if location:
        headers["location"] = location
    return Response(status, headers, body, "https://web.vodafone.com.eg/x")


LOGIN_PAGE = f'<html><body><form method="post" action="{LOGIN_FORM}"><input name="username"></form></body></html>'
LOGIN_PAGE_ERROR = (
    '<html><body><form method="post" action="%s">'
    '<span id="input-error">Invalid username or password.</span></form></body></html>' % LOGIN_FORM
)


class FakeVodafone:
    def __init__(self, *, bad_password=False, profile=PROFILE):
        self.bad_password = bad_password
        self.profile = profile
        self.refresh_ok = True
        self.calls: list[str] = []

    def __call__(self, request) -> Response:
        url = request.url
        self.calls.append(url)
        if url.endswith("/openid-connect/auth") or "/openid-connect/auth?" in url:
            return _html(LOGIN_PAGE)
        if "login-actions/authenticate" in url:
            if self.bad_password:
                return _html(LOGIN_PAGE_ERROR, status=200)
            return _html("", status=302, location="https://web.vodafone.com.eg/ar/KClogin?code=auth-code-1&state=s")
        if url.endswith("/openid-connect/token"):
            body = request.body.decode() if request.body else ""
            if "refresh_token" in body and not self.refresh_ok:
                return _json({"error": "invalid_grant"}, status=400)
            return _json({"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600})
        if "/dxl/pim/product" in url:
            return _json(self.profile)
        raise AssertionError(f"unexpected call {url}")


def test_sign_in_through_website_flow():
    fake = FakeVodafone()
    provider = VodafoneEg(transport=fake)
    creds = provider.start_sign_in({"number": NUMBER, "password": PASSWORD})
    assert creds["access"] == "access-1"
    assert creds["refresh"] == "refresh-1"
    assert any("code=auth-code-1" not in c for c in fake.calls)  # the code was posted to the token endpoint


def test_usage_bundles_in_bytes_kb_base():
    provider = VodafoneEg(transport=FakeVodafone())
    creds = provider.start_sign_in({"number": NUMBER, "password": PASSWORD})
    line = provider.lines(creds)[0]
    usage = provider.usage(creds, line)
    plan = next(b for b in usage.bundles if b.kind == "plan")
    assert plan.total_bytes == 209_715_200 * 1024  # KB of 1024
    assert plan.remaining_bytes == 52_428_800 * 1024
    assert usage.renews is not None and usage.renews.isoformat() == "2026-02-01"


def test_wrong_password_shows_vodafones_message():
    provider = VodafoneEg(transport=FakeVodafone(bad_password=True))
    with pytest.raises(ProviderError) as exc:
        provider.start_sign_in({"number": NUMBER, "password": "nope"})
    assert "Invalid username or password" in str(exc.value)
    assert not exc.value.signed_out


def test_refresh_uses_refresh_token():
    fake = FakeVodafone()
    provider = VodafoneEg(transport=fake)
    creds = provider.start_sign_in({"number": NUMBER, "password": PASSWORD})
    creds["expires_at"] = 0  # force staleness
    fresh = provider.refresh(creds)
    assert fresh["access"] == "access-1"


def test_expired_session_raises_then_refresh_relogins():
    fake = FakeVodafone()
    provider = VodafoneEg(transport=fake)
    creds = provider.start_sign_in({"number": NUMBER, "password": PASSWORD})
    line = provider.lines(creds)[0]
    creds["expires_at"] = 0
    with pytest.raises(SessionExpired):
        provider.usage(creds, line)
    fake.refresh_ok = False  # refresh token retired; falls back to a fresh sign-in
    fresh = provider.refresh(creds)
    assert fresh["access"] == "access-1"
    creds2 = {**creds, **fresh}
    assert provider.usage(creds2, line).remaining is not None


def test_refresh_relogin_rejected_signs_out():
    fake = FakeVodafone(bad_password=True)
    provider = VodafoneEg(transport=fake)
    fake.refresh_ok = False
    with pytest.raises(ProviderError) as exc:
        provider.refresh({"number": NUMBER, "password": "nope", "refresh": ""})
    assert exc.value.signed_out is True


def test_parse_handles_missing_units_as_kb():
    profile = [
        {
            "parts": {
                "productOffering": [
                    {"name": "ADSL", "productOfferingTerm": [{"quota": {"total": 209_715_200, "amount": 104_857_600}}]}
                ]
            }
        }
    ]
    usage = parse_adsl_profile(profile)
    plan = usage.bundles[0]
    assert plan.kind == "plan"
    assert plan.total_bytes == 209_715_200 * 1024


def test_no_internet_usage_is_friendly_error():
    provider = VodafoneEg(transport=FakeVodafone(profile=[]))
    creds = provider.start_sign_in({"number": NUMBER, "password": PASSWORD})
    line = provider.lines(creds)[0]
    with pytest.raises(ProviderError) as exc:
        provider.usage(creds, line)
    assert "home internet" in str(exc.value).lower()
