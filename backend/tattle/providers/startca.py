"""Start.ca (Canada): usage from Start.ca's official usage API.

Start.ca gives each account an API key (My Account -> tools). The usage API at
start.ca/support/usage/api?key=... returns an XML document with the bytes used,
in "grace" and total, each split into download and upload. The plan's cap is
not in the response, so the cap (in GB) is an optional field; without it Tattle
shows how much was used, not how much is left.

The response has no cycle dates. Start.ca bills monthly, so the cycle is taken
to be the calendar month, or a billing day if one is typed (unverified).
Bytes are real bytes, so no unit conversion is needed.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import xml.etree.ElementTree as ET
from typing import Callable
from zoneinfo import ZoneInfo

from .base import Bundle, Field, Line, Provider, ProviderError, Usage
from .http import Http, Transport

USAGE_URL = "https://www.start.ca/support/usage/api"
EASTERN = ZoneInfo("America/Toronto")  # Start.ca is an Ontario ISP
GB = 1000**3  # the cap is typed in GB; Start.ca's usage figures are already bytes


class StartcaError(ProviderError):
    """Something Start.ca refused or could not answer; fit to show."""


def _int(element: ET.Element | None) -> int:
    try:
        return int(float(element.text)) if element is not None and element.text else 0
    except ValueError:
        return 0


def _pair(parent: ET.Element | None) -> int:
    """download + upload under a <used>/<grace>/<total> element."""
    if parent is None:
        return 0
    return _int(parent.find("download")) + _int(parent.find("upload"))


def parse_usage(xml: str) -> dict[str, int]:
    """used/grace/total bytes (download + upload) from the usage XML."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise StartcaError("Start.ca sent an answer Tattle could not read") from exc
    usage = root if root.tag == "usage" else root.find(".//usage")
    if usage is None:
        raise StartcaError("Start.ca did not return usage. Check the API key is correct and still active.")
    return {"used": _pair(usage.find("used")), "grace": _pair(usage.find("grace")), "total": _pair(usage.find("total"))}


def cycle(today: dt.date, billing_day: int | None) -> tuple[dt.date, dt.date]:
    """(cycle start, cycle end+1). Without a billing day it is the calendar month."""
    if not billing_day:
        start = today.replace(day=1)
        return start, start + dt.timedelta(days=calendar.monthrange(today.year, today.month)[1])

    def clamp(year: int, month: int) -> dt.date:
        return dt.date(year, month, min(billing_day, calendar.monthrange(year, month)[1]))

    this_month = clamp(today.year, today.month)
    if today >= this_month:
        start = this_month
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return start, clamp(year, month)
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return clamp(year, month), this_month


class Startca(Provider):
    id = "startca"
    name = "Start.ca"
    country = "CA"
    status = "beta"
    unit_base = 1000
    currency = "CAD"
    fields = (
        Field(
            "api_key",
            "Start.ca usage API key",
            kind="password",
            help="From My Account at start.ca (the usage API key).",
        ),
        Field(
            "cap_gb",
            "Monthly cap in GB",
            placeholder="500",
            help="Your plan's monthly cap, so Tattle can show what is left. Leave empty for an unlimited plan.",
            optional=True,
        ),
        Field(
            "billing_day",
            "Billing day of the month",
            placeholder="1",
            help="The day your month resets, if it is not the 1st.",
            optional=True,
        ),
    )
    notes = "Uses the usage API key from your Start.ca account. The plan cap is not in the API, so add it to see what is left."
    docs = "docs/providers/startca.md"

    def __init__(self, transport: Transport | None = None, today: Callable[[], dt.date] | None = None):
        self.transport = transport
        self.today = today or (lambda: dt.datetime.now(EASTERN).date())

    def account_name(self, values: dict[str, str]) -> str:
        key = (values.get("api_key") or "").strip()
        if not key:
            raise ValueError("Paste your Start.ca usage API key")
        return "start-" + key[-6:]  # a stable, non-secret label for Settings

    def start_sign_in(self, values: dict[str, str]) -> dict:
        key = (values.get("api_key") or "").strip()
        credentials = {
            "api_key": key,
            "cap_gb": _opt_int(values.get("cap_gb"), "monthly cap"),
            "billing_day": _billing_day(values.get("billing_day")),
        }
        self._fetch(key)  # fail now, at sign-in, if the key is wrong
        return credentials

    def lines(self, credentials: dict) -> list[Line]:
        return [Line("startca", plan="Start.ca internet")]

    def usage(self, credentials: dict, line: Line) -> Usage:
        figures = parse_usage(self._fetch(credentials["api_key"]))
        start, end = cycle(self.today(), credentials.get("billing_day"))
        renews = end
        cap = credentials.get("cap_gb")
        used = figures["used"] + figures["grace"]
        if cap:
            total = cap * GB
            bundle = Bundle("plan", line.plan, total_bytes=total, remaining_bytes=max(total - used, 0), renews=renews)
        else:
            bundle = Bundle("plan", line.plan, total_bytes=None, remaining_bytes=None, renews=renews, label="No cap set")
        return Usage(bundles=[bundle], renews=renews, cycle_start=start)

    def _fetch(self, api_key: str) -> str:
        response = Http(transport=self.transport, who="Start.ca", error=StartcaError).get(
            USAGE_URL, params={"key": api_key}, headers={"Accept": "application/xml, text/xml, */*"}
        )
        if response.status in (401, 403):
            raise StartcaError("Start.ca did not accept the API key. Check it in your Start.ca account.", status=response.status)
        if response.status != 200:
            raise StartcaError(f"Start.ca answered {response.status}. Tattle will try again.", status=response.status)
        return response.text


def _opt_int(raw: str | None, what: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError(f"Type the {what} as a number, or leave it empty")
    return int(round(float(text)))


def _billing_day(raw: str | None) -> int | None:
    day = _opt_int(raw, "billing day")
    if day is not None and not 1 <= day <= 31:
        raise ValueError("Type a billing day between 1 and 31, or leave it empty")
    return day
