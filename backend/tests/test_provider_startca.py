"""Start.ca provider against a fake transport.

The usage XML follows the Home Assistant startca component (used/grace/total,
each with download/upload, in bytes). The API key is fake.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tattle.providers.base import ProviderError
from tattle.providers.http import Response
from tattle.providers.startca import Startca, cycle, parse_usage

API_KEY = "fake-api-key-000000"

USAGE_XML = """<?xml version="1.0"?>
<usage>
  <used><download>268435456000</download><upload>10737418240</upload></used>
  <grace><download>0</download><upload>0</upload></grace>
  <total><download>268435456000</download><upload>10737418240</upload></total>
</usage>
"""


def _xml(body, status=200):
    return Response(status, {"content-type": "application/xml"}, body, "https://www.start.ca/support/usage/api")


class FakeStartca:
    def __init__(self, *, status=200, body=USAGE_XML):
        self.status = status
        self.body = body
        self.calls: list[str] = []

    def __call__(self, request) -> Response:
        self.calls.append(request.url)
        return _xml(self.body, self.status)


def _provider(fake, today=dt.date(2026, 10, 24)):
    return Startca(transport=fake, today=lambda: today)


def test_sign_in_validates_key_then_usage_with_cap():
    fake = FakeStartca()
    provider = _provider(fake)
    creds = provider.start_sign_in({"api_key": API_KEY, "cap_gb": "500"})
    assert creds["cap_gb"] == 500
    assert any("key=" + API_KEY in c for c in fake.calls)  # key passed in the query
    line = provider.lines(creds)[0]
    usage = provider.usage(creds, line)
    plan = usage.bundles[0]
    assert plan.total_bytes == 500 * 1000**3
    used = 268435456000 + 10737418240  # download + upload bytes, as the API gives them
    assert plan.remaining_bytes == 500 * 1000**3 - used
    assert usage.cycle_start == dt.date(2026, 10, 1)
    assert usage.renews == dt.date(2026, 11, 1)


def test_usage_without_cap_has_no_total():
    provider = _provider(FakeStartca())
    creds = provider.start_sign_in({"api_key": API_KEY})
    usage = provider.usage(creds, provider.lines(creds)[0])
    assert usage.bundles[0].total_bytes is None
    assert usage.total is None


def test_billing_day_shapes_cycle():
    # Billing day 15, today is the 24th: the cycle runs 15 Oct -> 15 Nov.
    provider = _provider(FakeStartca(), today=dt.date(2026, 10, 24))
    creds = provider.start_sign_in({"api_key": API_KEY, "billing_day": "15"})
    usage = provider.usage(creds, provider.lines(creds)[0])
    assert usage.cycle_start == dt.date(2026, 10, 15)
    assert usage.renews == dt.date(2026, 11, 15)


def test_cycle_before_billing_day():
    # Today is the 10th, billing day 15: the cycle started 15 Sep, renews 15 Oct.
    start, end = cycle(dt.date(2026, 10, 10), 15)
    assert start == dt.date(2026, 9, 15)
    assert end == dt.date(2026, 10, 15)


def test_bad_key_is_friendly_error():
    provider = _provider(FakeStartca(status=403))
    with pytest.raises(ProviderError) as exc:
        provider.start_sign_in({"api_key": "nope"})
    assert "api key" in str(exc.value).lower()


def test_empty_usage_document_is_error():
    # An empty but valid document parses to zeros, not an error.
    figures = parse_usage("<usage><used><download>0</download></used></usage>")
    assert figures["used"] == 0
    # A document with no <usage> node is an error.
    with pytest.raises(ProviderError):
        parse_usage("<other/>")


def test_cap_validation_rejects_text():
    provider = _provider(FakeStartca())
    with pytest.raises(ValueError):
        provider.start_sign_in({"api_key": API_KEY, "cap_gb": "lots"})


def test_account_name_is_not_the_key():
    provider = _provider(FakeStartca())
    name = provider.account_name({"api_key": API_KEY})
    assert API_KEY not in name
    assert name.startswith("start-")
