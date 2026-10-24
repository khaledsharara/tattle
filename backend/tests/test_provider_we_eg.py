"""WE (Telecom Egypt) provider, driven against a fake transport.

Payloads are shaped like the ones the My WE website returns (api_client.py and
headless.py in the references), with obviously fake numbers and names.
"""

from __future__ import annotations

import json

import pytest

from tattle.providers.base import Challenge, ProviderError, SessionExpired
from tattle.providers.http import Response
from tattle.providers.we_eg import WeEg, parse_free_units

LANDLINE = "0221234567"
PASSWORD = "s3cret-pw"
CSRF = "csrf-token-abc"
SUBSCRIBER = "SUB-0001"
OFFER = "OFFER-0001"

# measureUnit 1109 = GB, 1108 = MB.
FREE_UNITS = [
    {
        "offerName": "VDSL 200GB",
        "total": 200,
        "used": 150,
        "remain": 50,
        "effectiveTime": 1_767_225_600_000,  # 2026-01-01
        "expireTime": 1_769_903_999_000,  # 2026-01-31 23:59:59
        "freeUnitBeanDetailList": [
            {
                "itemCode": "C_TED_Primary_Fixed_Data",
                "offeringName": "VDSL 200GB",
                "measureUnit": "1109",
                "initialAmount": 200,
                "currentAmount": 50,
                "remainingDaysForRenewal": 12,
                "effectiveTime": 1_767_225_600_000,
                "expireTime": 1_769_903_999_000,
            },
            {
                "itemCode": "C_Extra_Gaming",
                "offeringName": "Gaming add-on",
                "measureUnit": "1109",
                "initialAmount": 20,
                "currentAmount": 5,
                "remainingDaysForRenewal": 12,
            },
        ],
    }
]


def _json(payload, status=200):
    return Response(status, {"content-type": "application/json"}, json.dumps(payload), "https://my.te.eg/x")


class FakeWe:
    """Routes WE's calls by URL; flags let a test flip behaviour."""

    def __init__(self, *, require_interaction=False, bad_password=False):
        self.require_interaction = require_interaction
        self.bad_password = bad_password
        self.calls: list[str] = []
        self.session_valid = True
        self.captcha_without_origin = None  # set True/False by the captcha call

    def __call__(self, request) -> Response:
        url = request.url
        self.calls.append(url)
        if "GenerateCaptcha" in url:
            self.captcha_without_origin = "Origin" not in request.headers and "Referer" not in request.headers
            return _json(
                {
                    "status": "Success",
                    "token": "captcha-token-1",
                    "requireInteraction": self.require_interaction,
                    "captcha": "iVBORw0KGgoAAAANfake" if self.require_interaction else "",
                }
            )
        if "userAuthenticate" in url:
            if self.bad_password:
                return _json({"header": {"retCode": "1", "retMsg": "Invalid password"}})
            return _json(
                {
                    "header": {"retCode": "0"},
                    "body": {
                        "token": CSRF,
                        "subscriber": {"subscriberId": SUBSCRIBER},
                        "customer": {"custName": "Test Customer"},
                    },
                }
            )
        if "getUserRoleCz" in url:
            return _json({"header": {"retCode": "0"}} if self.session_valid else {"header": {"retCode": "1"}})
        if "refreshAppToken" in url:
            return _json({"header": {"retCode": "0"}, "body": {"token": CSRF + "-new"}})
        if "getSubscribedOfferings" in url:
            if not self.session_valid:
                return _json({"header": {"retCode": "1", "retMsg": "Session expired"}})
            return _json({"header": {"retCode": "0"}, "body": {"offeringList": [{"mainOfferingId": OFFER}]}})
        if "queryFreeUnit" in url:
            if not self.session_valid:
                return _json({"header": {"retCode": "1", "retMsg": "Session expired"}})
            return _json({"header": {"retCode": "0"}, "body": FREE_UNITS})
        raise AssertionError(f"unexpected call {url}")


def test_direct_sign_in_when_no_captcha_interaction():
    fake = FakeWe(require_interaction=False)
    provider = WeEg(transport=fake)
    creds = provider.start_sign_in({"landline": LANDLINE, "password": PASSWORD})
    assert creds["csrf"] == CSRF
    assert creds["subscriber"] == SUBSCRIBER
    assert fake.captcha_without_origin is True  # captcha sent without Origin/Referer


def test_captcha_challenge_then_answer():
    fake = FakeWe(require_interaction=True)
    provider = WeEg(transport=fake)
    challenge = provider.start_sign_in({"landline": LANDLINE, "password": PASSWORD})
    assert isinstance(challenge, Challenge)
    assert challenge.kind == "captcha"
    assert challenge.image.startswith("data:image/")
    creds = provider.answer({"landline": LANDLINE, "password": PASSWORD}, challenge, "AB12")
    assert creds["csrf"] == CSRF


def test_lines_and_usage_bundles_in_bytes():
    fake = FakeWe()
    provider = WeEg(transport=fake)
    creds = provider.start_sign_in({"landline": LANDLINE, "password": PASSWORD})
    lines = provider.lines(creds)
    assert [line.id for line in lines] == [LANDLINE]
    usage = provider.usage(creds, lines[0])
    plan = next(b for b in usage.bundles if b.kind == "plan")
    assert plan.total_bytes == 200 * 1000**3  # GB, decimal base
    assert plan.remaining_bytes == 50 * 1000**3
    assert usage.renews is not None and usage.renews.isoformat() == "2026-02-01"  # 23:59:59 renews next day
    assert usage.total == 200 * 1000**3


def test_wrong_password_is_friendly_error():
    provider = WeEg(transport=FakeWe(bad_password=True))
    with pytest.raises(ProviderError) as exc:
        provider.start_sign_in({"landline": LANDLINE, "password": "wrong"})
    assert "password" in str(exc.value).lower()
    assert not exc.value.signed_out


def test_usage_session_expired_then_refresh_without_captcha():
    fake = FakeWe()
    provider = WeEg(transport=fake)
    creds = provider.start_sign_in({"landline": LANDLINE, "password": PASSWORD})
    lines = provider.lines(creds)

    fake.session_valid = False  # WE drops the session
    with pytest.raises(SessionExpired):
        provider.usage(creds, lines[0])

    fake.session_valid = True  # refreshAppToken brings it back; no captcha needed
    fresh = provider.refresh(creds)
    assert fresh["csrf"] == CSRF + "-new"
    assert provider.usage(fresh, lines[0]).remaining is not None


def test_refresh_needs_captcha_signs_out():
    fake = FakeWe(require_interaction=True)
    provider = WeEg(transport=fake)
    fake.session_valid = False  # refresh fails and the captcha wants typing
    with pytest.raises(ProviderError) as exc:
        provider.refresh({"landline": LANDLINE, "password": PASSWORD, "csrf": CSRF, "subscriber": SUBSCRIBER})
    assert exc.value.signed_out is True


def test_match_wan_login_reads_landline():
    assert WeEg.match_wan_login("0221234567@tedata.net.eg", ["0221234567", "0109999999"]) == "0221234567"
    assert WeEg.match_wan_login("nothing-here", ["0221234567"]) is None


def test_parse_free_units_dedupes_repeated_buckets():
    body = FREE_UNITS + FREE_UNITS  # same offer twice (WE repeats shared buckets)
    usage = parse_free_units(body)
    labels = [b.label for b in usage.bundles]
    assert labels.count("C_TED_Primary_Fixed_Data") == 1


def test_landline_validation():
    provider = WeEg(transport=FakeWe())
    with pytest.raises(ValueError):
        provider.start_sign_in({"landline": "12", "password": PASSWORD})
