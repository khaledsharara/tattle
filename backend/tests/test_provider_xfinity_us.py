"""Xfinity provider against a fake transport.

The OAuth (PKCE) token exchange and the Galileo GraphQL InternetDataUsage shape
follow the hassio-xfinity-usage and csobrinho references; values are fake.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tattle.providers.base import Challenge, ProviderError, SessionExpired
from tattle.providers.http import Response
from tattle.providers.xfinity_us import XfinityUs, authorize_url, code_in, code_verifier, parse_usage

CODE = "oi-0123456789abcdef0123456789abcdef"

USAGE_LIMITED = {
    "data": {
        "accountByServiceAccountId": {
            "internet": {
                "usage": {
                    "monthlyUsage": [
                        {
                            "policy": "limited",
                            "startDate": "2026-10-01",
                            "endDate": "2026-10-31",
                            "daysRemaining": 7,
                            "currentUsage": {"value": 558, "unit": "GB"},
                            "allowableUsage": {"value": 1.23, "unit": "TB"},
                            "overage": False,
                        }
                    ]
                }
            }
        }
    }
}
USAGE_UNLIMITED = {
    "data": {
        "accountByServiceAccountId": {
            "internet": {
                "usage": {
                    "monthlyUsage": [
                        {
                            "policy": "unlimited",
                            "startDate": "2026-10-01",
                            "endDate": "2026-10-31",
                            "daysRemaining": 7,
                            "currentUsage": {"value": 800, "unit": "GB"},
                            "allowableUsage": {"value": 0, "unit": "GB"},
                        }
                    ]
                }
            }
        }
    }
}


def _json(payload, status=200):
    return Response(status, {"content-type": "application/json"}, json.dumps(payload), "https://gw.api.dh.comcast.com/x")


class FakeXfinity:
    def __init__(self, *, bad_code=False, usage=USAGE_LIMITED, refresh_bad=False):
        self.bad_code = bad_code
        self.usage = usage
        self.refresh_bad = refresh_bad
        self.calls: list[str] = []

    def __call__(self, request) -> Response:
        url = request.url
        self.calls.append(url)
        if url.startswith("https://xerxes-sub.xerxessecure.com") and "/oauth/token" in url:
            body = request.body.decode() if request.body else ""
            if "refresh_token" in body and self.refresh_bad:
                return _json({"error": "invalid_grant"}, status=400)
            if "authorization_code" in body and self.bad_code:
                return _json({"error": "invalid_request", "error_description": "Bad code"}, status=400)
            return _json(
                {"access_token": "acc-1", "refresh_token": "ref-1", "id_token": "id-1", "token_type": "Bearer", "expires_in": 3600}
            )
        if url.startswith("https://gw.api.dh.comcast.com"):
            return _json(self.usage)
        raise AssertionError(f"unexpected call {url}")


def test_start_sign_in_returns_paste_challenge():
    provider = XfinityUs(transport=FakeXfinity())
    challenge = provider.start_sign_in({})
    assert isinstance(challenge, Challenge)
    assert challenge.kind == "paste"
    assert challenge.url.startswith("https://xerxes-sub.xerxessecure.com")
    assert "code_challenge=" in challenge.url
    assert "code_verifier" in challenge.state


def test_answer_exchanges_code_and_reads_usage():
    provider = XfinityUs(transport=FakeXfinity())
    challenge = provider.start_sign_in({})
    creds = provider.answer({}, challenge, f"xfinitydigitalhome://auth?code={CODE}&state=s")
    assert creds["access"] == "acc-1"
    line = provider.lines(creds)[0]
    usage = provider.usage(creds, line)
    plan = usage.bundles[0]
    assert plan.total_bytes == int(round(1.23 * 1000**4))  # 1.23 TB, decimal
    assert plan.remaining_bytes == plan.total_bytes - 558 * 1000**3
    assert usage.renews == dt.date(2026, 11, 1)


def test_unlimited_plan_has_no_quota():
    usage = parse_usage(USAGE_UNLIMITED, "Xfinity")
    assert usage.bundles[0].total_bytes is None
    assert usage.data == []


def test_bad_paste_is_friendly_error():
    provider = XfinityUs(transport=FakeXfinity())
    challenge = provider.start_sign_in({})
    with pytest.raises(ProviderError) as exc:
        provider.answer({}, challenge, "0.ac.not-the-right-code")
    assert "code" in str(exc.value).lower()


def test_code_in_prefers_oi_code_over_intermediate():
    assert code_in(f"xfinitydigitalhome://auth?code={CODE}") == CODE
    assert code_in(CODE) == CODE
    assert code_in("0.ac.1234") is None


def test_session_expired_then_refresh():
    fake = FakeXfinity()
    provider = XfinityUs(transport=fake)
    challenge = provider.start_sign_in({})
    creds = provider.answer({}, challenge, CODE)
    line = provider.lines(creds)[0]
    creds["expires_at"] = 0
    with pytest.raises(SessionExpired):
        provider.usage(creds, line)
    fresh = provider.refresh(creds)
    assert fresh["access"] == "acc-1"
    assert provider.usage(fresh, line).bundles


def test_refresh_rejected_signs_out():
    provider = XfinityUs(transport=FakeXfinity(refresh_bad=True))
    with pytest.raises(ProviderError) as exc:
        provider.refresh({"refresh": "ref-1"})
    assert exc.value.signed_out is True


def test_refresh_without_token_signs_out():
    provider = XfinityUs(transport=FakeXfinity())
    with pytest.raises(ProviderError) as exc:
        provider.refresh({})
    assert exc.value.signed_out is True


def test_authorize_url_and_verifier_are_pkce():
    verifier = code_verifier()
    url = authorize_url(verifier, "state-1", "activity-1")
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=" in url
