"""Vodafone Egypt home internet (ADSL/VDSL): quota from web.vodafone.com.eg.

Signs in the way the Vodafone website does, with its public sign-in client
("website", no secret): open the sign-in page, post the mobile number and
password to the form it shows, take the code from the redirect, trade it for an
access + refresh token pair. Then reads the ADSL profile the "ADSL management"
page loads (DXL product inventory, @type=ADSLProfile).

Vodafone reports home internet quotas in KB of 1024 bytes (a 200 GB plan shows
as 209,715,200), so 1 GB = 1024 MB here.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
import time
import urllib.parse
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from .base import Bundle, Field, Line, Provider, ProviderError, SessionExpired, Usage
from .http import Http, Response, Transport

WEB_URL = "https://web.vodafone.com.eg"
AUTH_URL = WEB_URL + "/auth/realms/vf-realm/protocol/openid-connect"
CLIENT_ID = "website"
REDIRECT_URI = WEB_URL + "/ar/KClogin"
PRODUCT_PATH = "/services/dxl/pim/product"
CAIRO = ZoneInfo("Africa/Cairo")
VODAFONE_UNIT = 1024
_UNIT_POWERS = {"B": 0, "BYTE": 0, "BYTES": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4}


class VodafoneError(ProviderError):
    """Something Vodafone refused or could not answer; the message is fit to show."""


class Rejected(VodafoneError):
    """Vodafone turned the number or password down (not a network problem)."""


def normalise_mobile(raw: str) -> str:
    """Vodafone's sign-in wants the mobile number in local form: 01xxxxxxxxx."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("20") and len(digits) == 12:
        digits = "0" + digits[2:]
    if not (len(digits) == 11 and digits.startswith("01")):
        raise ValueError("Type the Vodafone mobile number you sign in with, e.g. 01012345678")
    return digits


def normalise_landline(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits and (not digits.startswith("0") or not 9 <= len(digits) <= 11):
        raise ValueError("Type the landline with its area code, starting with 0, e.g. 0221234567")
    return digits


# --- the sign-in page ----------------------------------------------------------------
_VOID = {"br", "img", "input", "meta", "link", "hr", "wbr", "source"}

class _LoginPage(HTMLParser):
    """The form's action URL and any error message Keycloak shows on the page."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.errors: list[str] = []
        self._in_error = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "form" and self.action is None and values.get("action"):
            self.action = values["action"]
        classes = values.get("class", "")
        if values.get("id") == "input-error" or "kc-feedback-text" in classes or "alert-error" in classes:
            self._in_error += 1
        elif self._in_error and tag not in _VOID:
            self._in_error += 1

    def handle_endtag(self, tag: str) -> None:
        if self._in_error:
            self._in_error -= 1

    def handle_data(self, data: str) -> None:
        if self._in_error and data.strip():
            self.errors.append(data.strip())


def parse_login_page(html: str) -> _LoginPage:
    page = _LoginPage()
    page.feed(html or "")
    return page


# --- the ADSL profile ----------------------------------------------------------------
def _number(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    return float(match.group(0)) if match else None


def _bytes(value, unit: str | None) -> int | None:
    number = _number(value)
    power = _UNIT_POWERS.get(re.sub(r"[^A-Z]", "", (unit or "").upper()) or "KB")  # no unit: KB
    if number is None or power is None:
        return None
    return int(round(number * VODAFONE_UNIT**power))


def _renewal(text) -> dt.date | None:
    """validFor.endDateTime as a Cairo date; an end at 23:59:59 renews the next day."""
    if not isinstance(text, str) or not re.match(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return dt.date.fromisoformat(text[:10])
    if moment.tzinfo is not None:
        moment = moment.astimezone(CAIRO)
    return (moment + dt.timedelta(seconds=1)).date()


def _offering(item: dict) -> dict:
    offers = (item.get("parts") or {}).get("productOffering") if isinstance(item.get("parts"), dict) else None
    return offers[0] if isinstance(offers, list) and offers and isinstance(offers[0], dict) else {}


def _characteristics(item: dict) -> dict[str, str]:
    spec = _offering(item).get("specification")
    found = spec.get("characteristicsValue") if isinstance(spec, dict) else None
    if not isinstance(found, list):
        found = item.get("characteristic")
    return {
        str(c.get("name", "")).strip().lower(): str(c.get("value", "")).strip()
        for c in found or []
        if isinstance(c, dict)
    }


def _terms(item: dict) -> list[dict]:
    terms = _offering(item).get("productOfferingTerm")
    if terms is None:
        terms = item.get("productTerm")
    if isinstance(terms, dict):
        terms = [terms]
    return [t for t in terms or [] if isinstance(t, dict)]


def parse_adsl_profile(payload) -> Usage:
    """Every quota on the line. The main bundle carries matrixFlag=Y."""
    bundles: list[Bundle] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        is_main = _characteristics(item).get("matrixflag", "").lower() in {"y", "yes", "true", "1"}
        name = _offering(item).get("name") or item.get("name")
        for term in _terms(item):
            quota = term.get("quota")
            if not isinstance(quota, dict):
                continue
            unit = quota.get("units") or quota.get("unit")
            total = _bytes(quota.get("total"), unit)
            remaining = _bytes(quota.get("amount"), unit)
            consumed = _bytes(quota.get("consumed"), unit)
            if remaining is None and total is not None and consumed is not None:
                remaining = max(total - consumed, 0)
            if total is None and remaining is None:
                continue
            valid = term.get("validFor") if isinstance(term.get("validFor"), dict) else {}
            bundles.append(
                Bundle(
                    kind="plan" if is_main else "extra",
                    name=name,
                    total_bytes=total,
                    remaining_bytes=remaining,
                    renews=_renewal(valid.get("endDateTime")),
                )
            )
    if bundles and not any(b.kind == "plan" for b in bundles):
        first = bundles[0]
        bundles[0] = Bundle("plan", first.name, first.total_bytes, first.remaining_bytes, first.renews)
    plan = next((b for b in bundles if b.kind == "plan"), None)
    return Usage(bundles=bundles, renews=plan.renews if plan else None)


# --- the provider --------------------------------------------------------------------
class VodafoneEg(Provider):
    id = "vodafone_eg"
    name = "Vodafone Egypt home internet"
    country = "EG"
    status = "beta"
    unit_base = VODAFONE_UNIT
    currency = "EGP"
    fields = (
        Field(
            "number",
            "Vodafone mobile number",
            kind="tel",
            placeholder="01012345678",
            help="The mobile number you sign in to the Vodafone website or Ana Vodafone with.",
        ),
        Field("password", "Vodafone password", kind="password"),
        Field(
            "landline",
            "Home landline number",
            kind="tel",
            placeholder="0221234567",
            help="The landline your home internet is on, if Vodafone lists it under that number.",
            optional=True,
        ),
    )
    notes = "Signs in like the Vodafone website. Tattle keeps the password so it can sign in again when the session ends."
    docs = "docs/providers/vodafone_eg.md"

    def __init__(self, transport: Transport | None = None):
        self.transport = transport

    def account_name(self, values: dict[str, str]) -> str:
        return normalise_mobile(values.get("number", ""))

    # --- signing in -----------------------------------------------------------------
    def start_sign_in(self, values: dict[str, str]) -> dict:
        number = self.account_name(values)
        landline = normalise_landline(values.get("landline", ""))
        tokens = self._sign_in(number, values.get("password", ""))
        return {"number": number, "password": values.get("password", ""), "landline": landline, **tokens}

    def refresh(self, credentials: dict) -> dict:
        """The refresh token first; when Vodafone retired it, sign in again."""
        if credentials.get("refresh"):
            response = self._http().post(
                f"{AUTH_URL}/token",
                form={"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": credentials["refresh"]},
            )
            if response.status == 200:
                return {**credentials, **_tokens(response)}
            if response.status >= 500:
                raise VodafoneError(f"Vodafone is having trouble (it answered {response.status}). Tattle will try again.")
        try:
            return {**credentials, **self._sign_in(credentials["number"], credentials.get("password", ""))}
        except Rejected as exc:
            raise VodafoneError(f"Vodafone signed Tattle out: {exc}", signed_out=True) from exc

    # --- reading ------------------------------------------------------------------
    def lines(self, credentials: dict) -> list[Line]:
        return [Line(credentials.get("landline") or credentials["number"], meta={"party": credentials.get("landline") or ""})]

    def usage(self, credentials: dict, line: Line) -> Usage:
        if time.time() > float(credentials.get("expires_at") or 0) - 30:
            raise SessionExpired("The Vodafone session ran out")
        # Unverified: the mobile-internet profile is read with relatedParty.id =
        # the mobile number; the ADSL profile is assumed to follow the same shape.
        parties = [p for p in (line.meta.get("party"), credentials["number"]) if p]
        for party in dict.fromkeys(parties):
            response = self._http().get(_profile_url(party), headers=self._headers(credentials))
            if response.status in (401, 403):
                raise SessionExpired("The Vodafone session ran out")
            if response.status >= 500:
                raise VodafoneError(f"Vodafone is having trouble (it answered {response.status}). Tattle will try again.")
            if response.status != 200:
                continue
            usage = parse_adsl_profile(response.json())
            if usage.bundles:
                return usage
        raise VodafoneError(
            "Vodafone did not return home internet usage for this account. "
            "If you have Vodafone home internet on it, please open a test report."
        )

    # --- talking to Vodafone ---------------------------------------------------------
    def _http(self) -> Http:
        return Http(WEB_URL, transport=self.transport, who="Vodafone", error=VodafoneError)

    @staticmethod
    def _headers(credentials: dict) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credentials['access']}",
            "msisdn": credentials["number"],
            "clientId": "WebsiteConsumer",
            "Accept": "application/json",
            "Accept-Language": "AR",
            "Origin": WEB_URL,
            "Referer": WEB_URL + "/spa/adslManagement",
        }

    def _sign_in(self, number: str, password: str) -> dict:
        """The website's sign-in: page -> form post -> redirect with a code -> tokens."""
        http = self._http()
        query = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "state": secrets.token_hex(16),
            "response_mode": "query",
            "response_type": "code",
            "scope": "openid",
            "nonce": secrets.token_hex(8),
            "kc_locale": "en",
        }
        page = http.get(f"{AUTH_URL}/auth", params=query, headers={"Accept": "text/html"})
        form = parse_login_page(page.text)
        if page.status != 200 or not form.action:
            raise VodafoneError("Vodafone's sign-in page did not load. Try again in a few minutes.", status=page.status)
        posted = http.post(
            form.action,
            form={"username": number, "password": password},
            headers={"Accept": "text/html", "Origin": WEB_URL},
            follow_redirects=False,
        )
        code = _code_from(posted)
        if code is None:
            if posted.status >= 500:
                raise VodafoneError(f"Vodafone is having trouble (it answered {posted.status}).", status=posted.status)
            shown = parse_login_page(posted.text).errors
            message = " ".join(shown) if shown else "Vodafone did not accept the mobile number or password."
            raise Rejected(message, status=posted.status)
        response = http.post(
            f"{AUTH_URL}/token",
            form={"code": code, "grant_type": "authorization_code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI},
        )
        if response.status != 200:
            raise VodafoneError("Vodafone accepted the password but did not finish signing in. Try again.", status=response.status)
        return _tokens(response)


def _profile_url(party: str) -> str:
    # Written out by hand: Vodafone's own pages send "@" and "." unencoded.
    return (
        f"{WEB_URL}{PRODUCT_PATH}?relatedParty.id={urllib.parse.quote(party)}"
        "&place.@referredType=Local&@type=ADSLProfile"
    )


def _code_from(response: Response) -> str | None:
    """The authorization code in the redirect after a good password."""
    target = response.location or (response.url if "code=" in (response.url or "") else "")
    if response.status not in (301, 302, 303, 307, 308) and not target:
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(target or "").query)
    return (query.get("code") or [None])[0]


def _tokens(response: Response) -> dict:
    data = response.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise VodafoneError("Vodafone sent no sign-in token", status=response.status)
    return {
        "access": data["access_token"],
        "refresh": data.get("refresh_token") or "",
        "expires_at": time.time() + float(data.get("expires_in") or 300),
    }
