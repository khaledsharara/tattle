"""WE (Telecom Egypt) home internet: quota from the My WE website (my.te.eg).

Reads the figures the way the website does:

1. captcha.te.eg hands out a captcha token; most of the time it also wants the
   characters in a picture typed (requireInteraction), sometimes it does not,
2. the landline ("FBB" + number without the leading 0) and password sign in
   with that token and get a csrf token plus session cookies,
3. the subscribed offerings name the main offering id(s),
4. queryFreeUnit lists every quota bucket on the line (main, extras, gifts).

A captcha can't be solved unattended, so the session is kept alive between
readings: getUserRoleCz says whether it still works and refreshAppToken renews
it. When WE ends the session and wants a new captcha, Settings asks the person
to sign in again.

WE's offer totals are in GB; the per-bucket details carry a unit code.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import replace
from zoneinfo import ZoneInfo

from .base import Bundle, Challenge, Field, Line, Provider, ProviderError, SessionExpired, Usage
from .http import Http, Transport

BASE_URL = "https://my.te.eg/echannel/service/besapp/base/rest/busiservice"
CAPTCHA_URL = "https://captcha.te.eg/api/Captcha/GenerateCaptcha"
CAIRO = ZoneInfo("Africa/Cairo")
WE_UNIT = 1000  # unverified: WE does not say whether 1 GB is 1000 or 1024 MB

# Sent with every echannel call, as the website does. The captcha call must go
# WITHOUT Origin/Referer: captcha.te.eg then answers InvalidMerchantRequest.
WEB_HEADERS = {
    "channelid": "702",
    "languagecode": "en-US",
    "isselfcare": "true",
    "delegatorsubsid": "",
    "iscoporate": "false",
    "ismobile": "false",
    "Origin": "https://my.te.eg",
    "Referer": "https://my.te.eg/echannel/",
}
# measureUnit codes in freeUnitBeanDetailList -> power of the unit base (B, KB, MB, GB).
_UNIT_POWERS = {"1106": 0, "1107": 1, "1108": 2, "1109": 3}
MAIN_DATA_ITEM = "C_TED_Primary_Fixed_Data"  # the plan's own internet bucket
_APP_ONLY = ("gaming", "stream", "racing")  # buckets usable by some apps only


class WeError(ProviderError):
    """Something WE refused or could not answer; the message is fit to show."""


class Rejected(WeError):
    """WE turned the sign-in down (password, captcha), not a network problem."""


# --- numbers -------------------------------------------------------------------
def normalise_landline(raw: str) -> str:
    """The landline as people write it: digits, leading 0, area code included."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits.startswith("0") or not 9 <= len(digits) <= 11:
        raise ValueError("Type the landline with its area code, starting with 0, e.g. 0221234567")
    return digits


def account_id(landline: str) -> str:
    """WE's id for a home internet line: FBB + the number without its leading 0."""
    return "FBB" + landline[1:]


def number_in(text: str | None) -> str | None:
    """The landline inside a router login, e.g. 0221234567@tedata.net.eg."""
    match = re.search(r"\d{8,11}", text or "")
    if not match:
        return None
    digits = match.group(0)
    return digits if digits.startswith("0") else "0" + digits


# --- WE's answers ----------------------------------------------------------------
def _ret_code(data: dict) -> tuple[str | None, str | None]:
    header = data.get("header") if isinstance(data.get("header"), dict) else {}
    code = header.get("retCode")
    return (str(code) if code is not None else None), header.get("retMsg")


def _number(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _bytes(value, power: int | None) -> int | None:
    number = _number(value)
    if number is None or power is None:
        return None
    return int(round(number * WE_UNIT**power))


def _day(ms, *, renewal: bool = False) -> dt.date | None:
    """A millisecond timestamp as a Cairo date. A renewal moment is read one
    second later, so an expiry at 23:59:59 renews the next day."""
    number = _number(ms)
    if number is None or number <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(number / 1000 + (1 if renewal else 0), CAIRO).date()
    except (OverflowError, OSError, ValueError):
        return None


def _bucket(detail: dict, offer: dict, today: dt.date) -> tuple[Bundle, dt.date | None]:
    item = str(detail.get("itemCode") or "")
    power = _UNIT_POWERS.get(str(detail.get("measureUnit")))
    renews = _day(detail.get("expireTime") or offer.get("expireTime"), renewal=True)
    days = _number(detail.get("remainingDaysForRenewal"))
    if renews is None and days is not None:
        renews = today + dt.timedelta(days=int(days))
    is_data = power is not None and not any(word in item.lower() for word in _APP_ONLY)
    bundle = Bundle(
        kind="plan" if item == MAIN_DATA_ITEM else "extra",
        name=detail.get("offeringName") or offer.get("offerName"),
        label=item or None,
        total_bytes=_bytes(detail.get("initialAmount"), power),
        remaining_bytes=_bytes(detail.get("currentAmount"), power),
        renews=renews,
        is_data=is_data,
    )
    return bundle, _day(detail.get("effectiveTime") or offer.get("effectiveTime"))


def _offer(offer: dict) -> tuple[Bundle, dt.date | None]:
    """An offer without bucket details: its totals, which WE gives in GB."""
    remaining = offer.get("actualRemain") if offer.get("actualRemain") is not None else offer.get("remain")
    bundle = Bundle(
        kind="extra",
        name=offer.get("offerName"),
        total_bytes=_bytes(offer.get("total"), 3),
        remaining_bytes=_bytes(remaining, 3),
        renews=_day(offer.get("expireTime"), renewal=True),
    )
    return bundle, _day(offer.get("effectiveTime"))


def parse_free_units(body, today: dt.date | None = None) -> Usage:
    """queryFreeUnit's body: one entry per offer, each split into buckets.
    Offers that share a bucket repeat it, so identical buckets count once."""
    today = today or dt.datetime.now(CAIRO).date()
    found: list[tuple[Bundle, dt.date | None]] = []
    seen: set[tuple] = set()
    for offer in body if isinstance(body, list) else []:
        if not isinstance(offer, dict):
            continue
        details = [d for d in offer.get("freeUnitBeanDetailList") or [] if isinstance(d, dict)]
        made = [_bucket(d, offer, today) for d in details] or [_offer(offer)]
        for bundle, start in made:
            key = (bundle.name, bundle.label, bundle.total_bytes, bundle.remaining_bytes, bundle.renews)
            if key not in seen:
                seen.add(key)
                found.append((bundle, start))
    if found and not any(b.kind == "plan" for b, _ in found):
        # No bucket carries the main item code: the first data bucket is the plan.
        index = next((i for i, (b, _) in enumerate(found) if b.is_data), 0)
        bundle, start = found[index]
        found[index] = (replace(bundle, kind="plan"), start)
    plan = next(((b, s) for b, s in found if b.kind == "plan" and b.is_data), (None, None))
    return Usage(
        bundles=[b for b, _ in found],
        renews=plan[0].renews if plan[0] else None,
        cycle_start=plan[1],
    )


def _captcha_image(raw: str) -> str:
    """WE sends the picture as a data: URL or as bare base64."""
    if raw.startswith("data:"):
        return raw
    kind = "png" if raw.startswith("iVBOR") else "jpeg"
    return f"data:image/{kind};base64,{raw}"


# --- the provider --------------------------------------------------------------------
class WeEg(Provider):
    id = "we_eg"
    name = "WE (Telecom Egypt)"
    country = "EG"
    status = "beta"
    unit_base = WE_UNIT
    currency = "EGP"
    fields = (
        Field(
            "landline",
            "Home landline number",
            kind="tel",
            placeholder="0221234567",
            help="The landline your WE internet is on, with its area code.",
        ),
        Field("password", "My WE password", kind="password", help="The password you use on my.te.eg."),
    )
    notes = (
        "WE usually shows a captcha at sign-in. Tattle keeps the session alive afterwards; "
        "if WE ends it, Settings asks you to sign in again."
    )
    docs = "docs/providers/we_eg.md"

    def __init__(self, transport: Transport | None = None):
        self.transport = transport

    def account_name(self, values: dict[str, str]) -> str:
        return normalise_landline(values.get("landline", ""))

    # --- signing in -----------------------------------------------------------------
    def start_sign_in(self, values: dict[str, str]) -> dict | Challenge:
        landline = self.account_name(values)
        http = self._http()
        captcha = self._captcha(http, landline)
        if not captcha.get("requireInteraction"):
            return self._sign_in(http, landline, values.get("password", ""), captcha["token"], "")
        return self._challenge(http, landline, captcha)

    def answer(self, values: dict[str, str], challenge: Challenge, answer: str) -> dict | Challenge:
        landline = challenge.state["landline"]
        http = self._http(challenge.state.get("cookies"))
        try:
            return self._sign_in(http, landline, values.get("password", ""), challenge.state["token"], answer.strip())
        except Rejected as exc:
            # A captcha token works once: show a fresh picture with WE's reason.
            fresh = self._captcha(http, landline)
            if not fresh.get("requireInteraction"):
                raise
            return self._challenge(
                http,
                landline,
                fresh,
                f"WE said: {exc} Type the characters in this new picture. "
                "If the password was wrong, cancel and start again.",
            )

    def refresh(self, credentials: dict) -> dict:
        """Renew the session without a captcha when WE allows it."""
        http = self._http(credentials.get("cookies"))
        acct = account_id(credentials["landline"])
        try:
            renewed = self._post(http, "/cz/v1/common/refreshAppToken", {"loginId": acct, "servNumber": acct}, credentials["csrf"])
        except SessionExpired:
            renewed = {}
        if _ret_code(renewed)[0] == "0":
            token = (renewed.get("body") or {}).get("token") if isinstance(renewed.get("body"), dict) else None
            fresh = {**credentials, "csrf": token or credentials["csrf"], "cookies": http.export_cookies()}
            if self._signed_in(http, fresh["csrf"]):
                return fresh
        # WE sometimes lets a sign-in through without a captcha; then no one has to type it.
        captcha = self._captcha(http, credentials["landline"])
        if not captcha.get("requireInteraction"):
            try:
                return self._sign_in(http, credentials["landline"], credentials.get("password", ""), captcha["token"], "")
            except Rejected as exc:
                raise WeError(f"WE signed Tattle out: {exc}", signed_out=True) from exc
        raise WeError("WE ended the session and wants a captcha typed: sign in again from Settings.", signed_out=True)

    # --- reading ------------------------------------------------------------------
    def lines(self, credentials: dict) -> list[Line]:
        http = self._http(credentials.get("cookies"))
        offers = self._read(
            http,
            credentials,
            "/cz/v1/auth/getSubscribedOfferings",
            {"msisdn": account_id(credentials["landline"]), "numberServiceType": "FBB", "groupId": ""},
        )
        listed = (offers.get("body") or {}).get("offeringList") if isinstance(offers.get("body"), dict) else None
        ids: list[str] = []
        for offer in listed or []:
            offer_id = offer.get("mainOfferingId") if isinstance(offer, dict) else None
            if offer_id and str(offer_id) not in ids:
                ids.append(str(offer_id))
        if not ids:
            raise WeError("WE shows no internet package on this landline")
        return [Line(credentials["landline"], account=credentials.get("customer") or None, meta={"offers": ids})]

    def usage(self, credentials: dict, line: Line) -> Usage:
        http = self._http(credentials.get("cookies"))
        body: list = []
        for offer_id in line.meta.get("offers") or []:
            answer = self._read(
                http,
                credentials,
                "/cz/cbs/bb/queryFreeUnit",
                {"subscriberId": credentials["subscriber"], "mainOfferId": offer_id, "needQueryPoint": "true"},
            )
            if isinstance(answer.get("body"), list):
                body.extend(answer["body"])
        usage = parse_free_units(body)
        if not usage.bundles:
            raise WeError("WE sent no quota for this landline")
        return usage

    @classmethod
    def match_wan_login(cls, login: str, line_ids: list[str]) -> str | None:
        """WE's router logins are usually the landline: 0221234567@tedata.net.eg."""
        number = number_in(login)
        return next((i for i in line_ids if number and i.lstrip("0") == number.lstrip("0")), None)

    # --- talking to WE ------------------------------------------------------------
    def _http(self, cookies: list[dict] | None = None) -> Http:
        http = Http(BASE_URL, transport=self.transport, who="WE", error=WeError)
        http.import_cookies(cookies)
        return http

    def _captcha(self, http: Http, landline: str) -> dict:
        response = http.post(
            CAPTCHA_URL, json={"merchantName": "E-Care", "serviceName": "Login", "identifier": landline}
        )
        data = response.json() if response.text else None
        if response.status != 200 or not isinstance(data, dict) or data.get("status") != "Success" or not data.get("token"):
            status = data.get("status") if isinstance(data, dict) else None
            raise WeError(
                f"WE's captcha service refused the request{f' ({status})' if status else ''}. Try again in a few minutes.",
                status=response.status,
            )
        return data

    def _challenge(self, http: Http, landline: str, captcha: dict, prompt: str | None = None) -> Challenge:
        if not captcha.get("captcha"):
            raise WeError("WE asked for a captcha but sent no picture. Try again in a minute.")
        return Challenge(
            "captcha",
            prompt or "Type the characters in the picture, as on the My WE website.",
            state={"landline": landline, "token": captcha["token"], "cookies": http.export_cookies()},
            image=_captcha_image(str(captcha["captcha"])),
        )

    def _sign_in(self, http: Http, landline: str, password: str, token: str, code: str) -> dict:
        payload = {
            "acctId": account_id(landline),
            "password": password,
            "imgCacheKey": token,
            "appLocale": "en-US",
            "isSelfcare": "Y",
            "isMobile": "N",
            "imgCode": code,
            "isConvergent": "0",
        }
        data = self._post(http, "/v1/auth/userAuthenticate", payload, "")
        code_, message = _ret_code(data)
        body = data.get("body") if isinstance(data.get("body"), dict) else {}
        csrf = body.get("token")
        subscriber = (body.get("subscriber") or {}).get("subscriberId")
        if code_ != "0" or not csrf or not subscriber:
            raise Rejected(message or "WE did not accept the landline number, password or captcha.")
        return {
            "landline": landline,
            "password": password,  # kept so Tattle can sign in again when WE skips the captcha
            "csrf": csrf,
            "subscriber": str(subscriber),
            "customer": (body.get("customer") or {}).get("custName") or "",
            "cookies": http.export_cookies(),
        }

    def _post(self, http: Http, path: str, payload: dict | None, csrf: str) -> dict:
        headers = {**WEB_HEADERS, "csrftoken": csrf}
        if payload is None:
            response = http.post(path, body=b"", headers={**headers, "Content-Type": "application/json"})
        else:
            response = http.post(path, json=payload, headers=headers)
        if response.status in (401, 403):
            raise SessionExpired("WE ended the session")
        if response.status >= 500:
            raise WeError(f"WE is having trouble (it answered {response.status}). Tattle will try again.", status=response.status)
        data = response.json()
        if not isinstance(data, dict):
            raise WeError("WE sent an answer Tattle could not read", status=response.status)
        return data

    def _signed_in(self, http: Http, csrf: str) -> bool:
        try:
            return _ret_code(self._post(http, "/cz/v1/user/getUserRoleCz", None, csrf))[0] == "0"
        except SessionExpired:
            return False

    def _read(self, http: Http, credentials: dict, path: str, payload: dict) -> dict:
        """A read call; a refusal from a signed-out session becomes SessionExpired."""
        try:
            data = self._post(http, path, payload, credentials["csrf"])
            code, message = _ret_code(data)
            if code != "0":
                if not self._signed_in(http, credentials["csrf"]):
                    raise SessionExpired("WE ended the session")
                raise WeError(message or "WE refused the request")
            return data
        finally:
            credentials["cookies"] = http.export_cookies()
