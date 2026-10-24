"""Aussie Broadband (Australia): broadband usage from the My Aussie API.

Signs in like my.aussiebroadband.com.au: the user name and password are posted
to the auth service, which answers with the session's lifetime (expiresIn) and
a `myaussie_cookie` cookie that the API accepts. The services list names each
NBN/Opticomm service; its usage endpoint gives the MB used so far and the days
left in the billing period.

Most Aussie plans are unlimited: then there is no quota to compare with, and
the plan is listed with an unknown size. Aussie limits how often its API is
called; a reading every 15 minutes stays well inside that, and Tattle only signs
in again when the session has run out.
"""

from __future__ import annotations

import datetime as dt
import time
from http.cookies import CookieError, SimpleCookie
from typing import Callable
from zoneinfo import ZoneInfo

from .base import Bundle, Challenge, Field, Line, Provider, ProviderError, SessionExpired, Usage
from .http import Http, Response, Transport

LOGIN_URL = "https://myaussie-auth.aussiebroadband.com.au/login"
API_URL = "https://myaussie-api.aussiebroadband.com.au"
COOKIE = "myaussie_cookie"
BROADBAND_TYPES = ("NBN", "Opticomm")  # service types with a broadband usage meter
SYDNEY = ZoneInfo("Australia/Sydney")
MAX_PAGES = 10
HEADERS = {
    "Accept": "application/json",
    "Origin": "https://my.aussiebroadband.com.au",
    "Referer": "https://my.aussiebroadband.com.au/",
    "Cache-Control": "no-cache",
    "x-two-factor-auth-capable-client": "true",
}


class AussieError(ProviderError):
    """Something Aussie Broadband refused or could not answer; fit to show."""


class Rejected(AussieError):
    """Aussie turned the user name or password down."""


def mb_to_bytes(value) -> int | None:
    """Aussie counts in MB; 1 MB is taken as 1,000,000 bytes (unverified)."""
    try:
        return int(round(float(value) * 1000**2))
    except (TypeError, ValueError):
        return None


def parse_usage(data: dict, plan: str | None, today: dt.date) -> Usage:
    """usedMb so far and the days left in the period. remainingMb only appears
    on plans with a quota (unverified; most plans are unlimited)."""
    used = mb_to_bytes(data.get("usedMb"))
    remaining = mb_to_bytes(data.get("remainingMb")) if data.get("remainingMb") is not None else None
    total = used + remaining if used is not None and remaining is not None else None
    renews = cycle_start = None
    try:
        renews = today + dt.timedelta(days=int(data["daysRemaining"]))
        cycle_start = renews - dt.timedelta(days=int(data["daysTotal"]))
    except (KeyError, TypeError, ValueError):
        pass
    label = None if remaining is not None else "No quota (unlimited plan)"
    bundle = Bundle("plan", plan, total_bytes=total, remaining_bytes=remaining, renews=renews, label=label)
    return Usage(bundles=[bundle], renews=renews, cycle_start=cycle_start)


def _cookie(http: Http, response: Response) -> str | None:
    """The session cookie from the jar (real network) or the Set-Cookie header (tests)."""
    for cookie in http.jar:
        if cookie.name == COOKIE and cookie.value:
            return cookie.value
    raw = response.headers.get("set-cookie")
    return _parse_set_cookie(raw) if raw else None


def _parse_set_cookie(raw: str) -> str | None:
    parsed: SimpleCookie = SimpleCookie()
    try:
        parsed.load(raw)
    except CookieError:
        return None
    morsel = parsed.get(COOKIE)
    return morsel.value if morsel and morsel.value else None


def _session(result: dict | Challenge) -> dict:
    if not isinstance(result, dict):  # only the first sign-in step may ask for a code
        raise AussieError("Aussie Broadband asked for another code. Start the sign-in again from Settings.", signed_out=True)
    return result


def _mentions_two_factor(data) -> bool:
    text = str(data).lower()
    return any(word in text for word in ("two factor", "two-factor", "2fa", "mfa"))


class AussieBb(Provider):
    id = "aussie_bb"
    name = "Aussie Broadband"
    country = "AU"
    status = "beta"
    unit_base = 1000
    currency = "AUD"
    fields = (
        Field("username", "My Aussie user name", placeholder="user@example.com", help="Usually your e-mail address."),
        Field("password", "My Aussie password", kind="password"),
    )
    notes = "Reads usage for your NBN services. Most Aussie plans are unlimited, so Tattle shows what you used, not what is left."
    docs = "docs/providers/aussie_bb.md"

    def __init__(self, transport: Transport | None = None, today: Callable[[], dt.date] | None = None):
        self.transport = transport
        self.today = today or (lambda: dt.datetime.now(SYDNEY).date())

    # --- signing in -----------------------------------------------------------------
    def start_sign_in(self, values: dict[str, str]) -> dict | Challenge:
        username, password = values.get("username", "").strip(), values.get("password", "")
        result = self._sign_in(username, password)
        if isinstance(result, dict):
            return {"username": username, "password": password, **result}
        return result

    def answer(self, values: dict[str, str], challenge: Challenge, answer: str) -> dict:
        """Unverified: Aussie only asks for a code on some accounts and actions."""
        http = self._http()
        response = http.post(
            f"{API_URL}/2fa/verify",
            json={"token": answer.strip()},
            headers={**HEADERS, "Cookie": f"{COOKIE}={challenge.state.get('cookie', '')}"},
        )
        if response.status >= 400:
            raise AussieError("Aussie Broadband did not accept that code. Check it and type it again.", status=response.status)
        result = _session(self._sign_in(values.get("username", "").strip(), values.get("password", ""), allow_code=False))
        return {"username": values.get("username", "").strip(), "password": values.get("password", ""), **result}

    def refresh(self, credentials: dict) -> dict:
        """Aussie sessions simply run out: sign in again with the stored password."""
        try:
            result = _session(self._sign_in(credentials["username"], credentials.get("password", ""), allow_code=False))
        except Rejected as exc:
            raise AussieError(f"Aussie Broadband signed Tattle out: {exc}", signed_out=True) from exc
        return {**credentials, **result}

    # --- reading ------------------------------------------------------------------
    def lines(self, credentials: dict) -> list[Line]:
        found: list[Line] = []
        url: str | None = f"{API_URL}/services?page=1"
        for _ in range(MAX_PAGES):
            if not url:
                break
            data = self._get(credentials, url)
            for service in data.get("data") or []:
                if isinstance(service, dict) and service.get("type") in BROADBAND_TYPES and service.get("service_id"):
                    found.append(
                        Line(
                            str(service["service_id"]),
                            plan=service.get("plan"),
                            meta={"type": service.get("type"), "next_bill": service.get("nextBillDate")},
                        )
                    )
            links = data.get("links") if isinstance(data.get("links"), dict) else {}
            url = links.get("next")
        if not found:
            raise AussieError("This Aussie Broadband account has no NBN service Tattle can read")
        return found

    def usage(self, credentials: dict, line: Line) -> Usage:
        data = self._get(credentials, f"{API_URL}/broadband/{line.id}/usage")
        return parse_usage(data, line.plan, self.today())

    # --- talking to Aussie ----------------------------------------------------------
    def _http(self) -> Http:
        return Http(transport=self.transport, who="Aussie Broadband", error=AussieError)

    def _sign_in(self, username: str, password: str, *, allow_code: bool = True) -> dict | Challenge:
        http = self._http()
        response = http.post(LOGIN_URL, json={"username": username, "password": password}, headers=HEADERS)
        data = response.json() if response.text else {}
        if response.status == 429:
            raise AussieError("Aussie Broadband is limiting sign-ins right now. Try again in a few minutes.", status=429)
        if response.status in (401, 403, 422):
            raise Rejected("Aussie Broadband did not accept the user name or password.", status=response.status)
        if response.status >= 400:
            raise AussieError(f"Aussie Broadband answered {response.status}. Try again later.", status=response.status)
        cookie = _cookie(http, response)
        if isinstance(data, dict) and data.get("expiresIn") and cookie:
            return {"cookie": cookie, "expires_at": time.time() + float(data["expiresIn"]) - 60}
        if allow_code and _mentions_two_factor(data):
            sent = http.post(
                f"{API_URL}/2fa/send",
                json={"method": "sms"},
                headers={**HEADERS, "Cookie": f"{COOKIE}={cookie or ''}"},
            )
            if sent.status >= 400:
                raise AussieError("Aussie Broadband wants a sign-in code but could not send one. Try again later.")
            return Challenge(
                "code",
                "Aussie Broadband texted a code to the mobile number on the account. Type it here.",
                state={"cookie": cookie or ""},
            )
        raise AussieError("Aussie Broadband signed in but sent no session. Try again later.", status=response.status)

    def _get(self, credentials: dict, url: str) -> dict:
        if time.time() > float(credentials.get("expires_at") or 0):
            raise SessionExpired("The Aussie Broadband session ran out")
        response = self._http().get(url, headers={**HEADERS, "Cookie": f"{COOKIE}={credentials.get('cookie', '')}"})
        if response.status in (401, 403):
            raise SessionExpired("The Aussie Broadband session ran out")
        if response.status == 429:
            raise AussieError("Aussie Broadband asked Tattle to slow down. Tattle will try again later.", status=429)
        if response.status != 200:
            raise AussieError(f"Aussie Broadband answered {response.status}. Tattle will try again.", status=response.status)
        data = response.json()
        if not isinstance(data, dict):
            raise AussieError("Aussie Broadband sent an answer Tattle could not read")
        return data
