"""e& (Etisalat) Egypt: home internet quota from e&'s self-care website.

Reads the figures the way https://www.eand.com.eg/selfcare/ does:

1. e& texts a sign-in code to the mobile number on the line's contract,
2. the code is traded for an access + refresh token pair,
3. the account profile lists every line on the contract plus linked lines,
   each with the short code e& wants when asked for its usage,
4. each line's usage, daily history and bills are read, refreshing the access
   token when it runs out.

e& counts 1 GB = 1024 MB: a used-up 750 GB plan's daily history adds up to
about 768,000 MB.

    python -m tattle.providers.eand_eg dump    # print e&'s raw answers (debugging)
"""

from __future__ import annotations

import base64
import datetime as dt
import http.cookiejar
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .base import Bundle, Challenge, Field, Line, Provider, ProviderError, SessionExpired, Usage
from .http import USER_AGENT

BASE_URL = "https://www.eand.com.eg"
AUTH_PATH = "/eshopapi/selfcare/api/v1/auth"
SELFCARE_PATH = "/Saytar/rest/selfCare"
APP_VERSION = "29.2.0"  # what the self-care site sends with its requests (site build of 2026-10)
LANGUAGE_EN = "2"

CODE_INTERVAL_SECONDS = 60  # don't text the same mobile twice in a row by accident
EAND_UNIT = 1024
_UNIT_POWERS = {"KB": 1, "MB": 2, "GB": 3, "TB": 4}
# Bundle groups in e&'s usage answer: the plan, then extra bundles bought on top.
# (newMeterConsumption repeats the plan, so it is left out.)
_USAGE_GROUPS = {
    "ratePlanConsumption": "plan",
    "connectConsumption": "plan",
    "ratePlanAddOnConsumption": "extra",
    "connectAddOnConsumption": "extra",
}
HISTORY_INTERNET = "5"  # e&'s account-history type for internet use


class EandError(ProviderError):
    """Something e& refused or could not answer; the message is fit to show."""


class TokenExpired(SessionExpired):
    def __init__(self) -> None:
        super().__init__("e& signed Tattle out")


@dataclass
class Tokens:
    access: str
    refresh: str


@dataclass(frozen=True)
class EandLine:
    number: str  # as people write it, with the leading 0
    short_code: str | None
    plan: str | None
    account_number: str | None = None  # the billing account; it can cover several lines
    bill_cycle: str | None = None

# --- numbers -------------------------------------------------------------------
def normalise_number(raw: str) -> str:
    """The number as e&'s site accepts it: digits, leading 0, 9 to 11 long."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits.startswith("0") or not 9 <= len(digits) <= 11:
        raise ValueError("Type the number the way e& shows it, starting with 0, e.g. 0221234567")
    return digits


def _dial(number: str) -> str:
    """e& leaves the leading 0 out in its requests."""
    return number[1:] if number.startswith("0") else number


def _device_id(number: str) -> str:
    return base64.b64encode(_dial(number).encode()).decode()


def same_number(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.lstrip("0") == b.lstrip("0")


def number_in(text: str | None) -> str | None:
    """The phone number inside the router's internet login, e.g. ETIS_0221234567."""
    match = re.search(r"\d{8,11}", text or "")
    if not match:
        return None
    digits = match.group(0)
    return digits if digits.startswith("0") else "0" + digits


def is_home_line(number: str) -> bool:
    """Landlines (home internet) are 02/03/0xx numbers; mobiles are 11 digits starting 01."""
    return not (len(number) == 11 and number.startswith("01"))


# --- e&'s answers ------------------------------------------------------------------
def _xml(text: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise EandError("e& sent an answer Tattle could not read") from exc
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def _find(root: ET.Element, tag: str) -> ET.Element:
    found = root if root.tag == tag else root.find(f".//{tag}")
    if found is None:
        raise EandError("e& sent an answer Tattle could not read")
    status = _text(found, "status")
    if status is not None and status.lower() == "false":
        raise EandError(_text(found, "fault/userMessageEn") or "e& refused the request")
    return found


def _text(element: ET.Element, path: str) -> str | None:
    found = element.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def parse_lines(profile_xml: str) -> list[EandLine]:
    """Every line the signed-in account can see: its own contracts and linked dials."""
    response = _find(_xml(profile_xml), "getAccountProfileResponse")
    lines: dict[str, EandLine] = {}
    for path in ("contracts/contract", "linkedDials/contract"):
        for contract in response.findall(path):
            dial = _text(contract, "subscriberNumber")
            if not dial:
                continue
            number = dial if dial.startswith("0") else "0" + dial
            lines.setdefault(
                number,
                EandLine(
                    number,
                    _text(contract, "shdes"),
                    _text(contract, "ratePlan"),
                    _text(contract, "accountNumber"),
                    _text(contract, "billCycle"),
                ),
            )
    return list(lines.values())


def _day(text: str | None) -> str | None:
    """'2026-10-06T00:00:00+03:00' -> '2026-10-06'."""
    return text[:10] if text and re.match(r"\d{4}-\d{2}-\d{2}", text) else None


def _money(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def parse_bills(open_amount_xml: str, history_xml: str) -> dict:
    """What the line's billing account owes now and its recent bills (EGP).
    One account can cover several lines (mobiles on the same contract too)."""
    open_response = _find(_xml(open_amount_xml), "getOpenAmountResponse")
    history = _find(_xml(history_xml), "getCustomerBillHistoryResponse")
    return {
        "open_amount": _money(_text(open_response, "openAmount")),
        "unpaid": [
            {"due": _day(_text(bill, "billDueDate")), "value": _money(_text(bill, "billValue"))}
            for bill in open_response.findall("bills/bill")
        ],
        "bills": [
            {
                "from": _day(_text(item, "billDateFrom")),
                "to": _day(_text(item, "billDateTo")),
                "issued": _day(_text(item, "invoiceIssueDate")),
                "due": _day(_text(item, "billDueDate")),
                "value": _money(_text(item, "billValue")),
                "lines": int(_text(item, "numberOfLines") or 0),
            }
            for item in history.iter("billItem")
        ],
    }


def renewal_date(text: str | None) -> dt.date | None:
    """e& writes renewal dates as 25/10/2026."""
    for pattern in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text or "", pattern).date()
        except ValueError:
            continue
    return None


def to_bytes(value: str | None, unit: str | None) -> int | None:
    """'312.45', 'GB' -> bytes; None for unlimited bundles and non-data units (minutes, SMS)."""
    power = _unit_power(unit)
    if power is None or value is None:
        return None
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return None
    return int(round(number * EAND_UNIT**power))


def _unit_power(unit: str | None) -> int | None:
    letters = re.sub(r"[^A-Z]", "", (unit or "").upper())
    for prefix, short in (("KILO", "KB"), ("MEGA", "MB"), ("GIGA", "GB"), ("TERA", "TB")):
        if letters.startswith(prefix):
            letters = short
    return _UNIT_POWERS.get(letters)


def parse_usage(consumption_xml: str) -> Usage:
    """Every bundle on the line. Extra bundles come with what is left but not
    their size: e& does not send it (its own website shows only what is left)."""
    response = _find(_xml(consumption_xml), "getConsumptionResponse")
    bundles: list[Bundle] = []
    for group, kind in _USAGE_GROUPS.items():
        for bundle in response.findall(group):
            name, renews = _text(bundle, "productName"), _text(bundle, "renewalDate")
            for item in bundle.findall("consumptionList/consumption"):
                total = to_bytes(_text(item, "totalValue"), _text(item, "totalUnit"))
                remaining = to_bytes(_text(item, "remainingValue"), _text(item, "remainingUnit"))
                is_data = remaining is not None
                bundles.append(
                    Bundle(
                        kind=kind,
                        name=_text(item, "productName") or name,
                        label=_text(item, "consumedLabel") or _text(item, "label"),
                        total_bytes=total if is_data else None,
                        remaining_bytes=remaining,
                        renews=renewal_date(_text(item, "renewalDate") or renews),
                        is_data=is_data,
                    )
                )
    plan_renewal = next((b.renews for b in bundles if b.is_data and b.kind == "plan" and b.renews), None)
    return Usage(bundles=bundles, renews=plan_renewal)

def parse_history(history_xml: str) -> dict[str, int]:
    """e&'s account history: internet used per day, {"2026-10-10": bytes}."""
    response = _find(_xml(history_xml), "accountHistoryResponse")
    days: dict[str, int] = {}
    for entry in response.iter("accountHistoryItem"):
        day = _text(entry, "dayDate")
        for transaction in entry.iter("historyTransaction"):
            value, _, unit = (_text(transaction, "amount") or "").partition(" ")
            size = to_bytes(value, unit)
            if day and size is not None and _text(transaction, "type") == HISTORY_INTERNET:
                days[day] = days.get(day, 0) + size
    return days


# --- transport ---------------------------------------------------------------------
class EandClient:
    """Talks to e& exactly like the self-care site; holds no tokens itself."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _send(
        self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str], query: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], str]:
        url = self.base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        request = urllib.request.Request(url, data=body, method=method)
        for name, value in {
            "User-Agent": USER_AGENT,
            "applicationName": "WEB",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/selfcare/",
            **headers,
        }.items():
            request.add_header(name, value)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, _lower(response.headers.items()), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, _lower(exc.headers.items() if exc.headers else []), exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise EandError(f"Could not reach e&: {reason}") from exc

    def _auth(self, action: str, payload: dict, extra: dict[str, str] | None = None) -> tuple[dict[str, str], str]:
        status, headers, text = self._send(
            "POST",
            f"{AUTH_PATH}/{action}",
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "*/*", "accept-language": "en", **(extra or {})},
        )
        message = None
        try:
            answer = json.loads(text) if text else {}
            if isinstance(answer, dict):
                message = answer.get("msg") or answer.get("message")
                if answer.get("status") is False:
                    raise EandError(message or "e& refused the request", signed_out=status in (401, 403), status=status)
        except ValueError:
            pass
        if status != 200:
            raise EandError(message or f"e& answered {status}", signed_out=status in (401, 403), status=status)
        return headers, text

    @staticmethod
    def _tokens(headers: dict[str, str]) -> Tokens:
        access, refresh = headers.get("access-token"), headers.get("refresh-token")
        if not access or not refresh:
            raise EandError("e& accepted the code but sent no sign-in token")
        return Tokens(access, refresh)

    def send_code(self, number: str) -> None:
        self._auth(
            "sendOTP",
            {"msisdn": _dial(number), "deviceID": _device_id(number), "scope": "LOGIN", "type": "MOBILE"},
        )

    def sign_in(self, number: str, code: str) -> Tokens:
        headers, _ = self._auth(
            "login", {"deviceID": _device_id(number), "msisdn": _dial(number), "otp": code, "type": "OTPLogin"}
        )
        return self._tokens(headers)

    def refresh(self, tokens: Tokens) -> Tokens:
        try:
            headers, _ = self._auth("refresh", {}, {"access-token": tokens.access, "refresh-token": tokens.refresh})
        except EandError as exc:
            if exc.status not in (400, 401, 403):
                raise  # e& unreachable or having trouble: try again at the next reading
            raise EandError("e& signed Tattle out: send a new code", signed_out=True, status=exc.status) from exc
        return self._tokens(headers)

    def _selfcare(self, tokens: Tokens, path: str, param: str, request_xml: str) -> str:
        status, _, text = self._send(
            "GET",
            f"{SELFCARE_PATH}/{path}",
            query={param: request_xml},
            headers={"Content-Type": "text/xml", "Accept": "text/xml", "APP-Version": APP_VERSION, "auth": tokens.access},
        )
        if status in (401, 403):
            raise TokenExpired()
        if status != 200:
            raise EandError(f"e& answered {status}", status=status)
        return text

    def profile(self, tokens: Tokens, number: str) -> str:
        request = (
            f"<getUserAccountRequest><deviceID>{_device_id(number)}</deviceID><dial>{escape(_dial(number))}</dial>"
            f"<language>{LANGUAGE_EN}</language></getUserAccountRequest>"
        )
        return self._selfcare(tokens, "user/account", "req", request)

    def usage(self, tokens: Tokens, line: EandLine) -> str:
        request = (
            f"<dialAndLanguageRequest><subscriberNumber>{escape(_dial(line.number))}</subscriberNumber>"
            f"<language>{LANGUAGE_EN}</language><shortCode>{escape(line.short_code or '')}</shortCode>"
            "</dialAndLanguageRequest>"
        )
        return self._selfcare(tokens, "servicemanagement/getGenericConsumptionsV2", "requestParam", request)

    def history(self, tokens: Tokens, line: EandLine, start: dt.date, end: dt.date) -> str:
        request = (
            f"<getCallHistoryV2Request><fromDate>{start.isoformat()}</fromDate><toDate>{end.isoformat()}</toDate>"
            f"<subscriberNumber>{escape(_dial(line.number))}</subscriberNumber><type>0</type></getCallHistoryV2Request>"
        )
        return self._selfcare(tokens, "services/accountHistoryV2", "req", request)

    def open_amount(self, tokens: Tokens, account_number: str) -> str:
        request = (
            f"<getOpenAmountRequest><getListOfBills>true</getListOfBills><language>{LANGUAGE_EN}</language>"
            f"<accountNumber>{escape(account_number)}</accountNumber></getOpenAmountRequest>"
        )
        return self._selfcare(tokens, "customerprofile/openAmount", "getOpenAmountRequest", request)

    def bill_history(self, tokens: Tokens, account_number: str, count: int = 6) -> str:
        request = (
            f"<getCustomerBillHistoryRequest><numberOfBills>{count}</numberOfBills>"
            f"<accountNumber>{escape(account_number)}</accountNumber></getCustomerBillHistoryRequest>"
        )
        return self._selfcare(tokens, "customerprofile/customerbillV2", "getCustomerBillHistoryRequest", request)


def _lower(items) -> dict[str, str]:
    return {name.lower(): value for name, value in items}


class EandEg(Provider):
    id = "eand_eg"
    name = "e& (Etisalat) Egypt"
    country = "EG"
    status = "stable"
    unit_base = EAND_UNIT
    currency = "EGP"
    fields = (
        Field(
            "number",
            "Home landline number",
            kind="tel",
            placeholder="0221234567",
            help="The landline your home internet is on, starting with 0.",
        ),
    )
    notes = "e& texts a sign-in code to the mobile number on the line's contract. Every line on that contract shows up."
    docs = "docs/providers/eand_eg.md"
    has_daily = True
    has_billing = True

    def __init__(self, client: EandClient | None = None):
        self.client = client or EandClient()
        self.code_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def account_name(self, values: dict[str, str]) -> str:
        return normalise_number(values.get("number", ""))

    def start_sign_in(self, values: dict[str, str]) -> Challenge:
        number = self.account_name(values)
        with self._lock:
            if time.time() - self.code_sent.get(number, 0) < CODE_INTERVAL_SECONDS:
                raise EandError("A code was sent less than a minute ago. Wait for the SMS, or try again in a minute.")
            self.client.send_code(number)
            self.code_sent[number] = time.time()
        return Challenge(
            "code",
            f"e& texted a code to the mobile number on {number}'s contract. Type it here.",
            state={"number": number},
        )

    def answer(self, values: dict[str, str], challenge: Challenge, answer: str) -> dict:
        number = challenge.state["number"]
        tokens = self.client.sign_in(number, re.sub(r"\s", "", answer))
        self.code_sent.pop(number, None)
        return {"number": number, "access": tokens.access, "refresh": tokens.refresh}

    def refresh(self, credentials: dict) -> dict:
        fresh = self.client.refresh(_tokens(credentials))
        return {**credentials, "access": fresh.access, "refresh": fresh.refresh}

    def lines(self, credentials: dict) -> list[Line]:
        profile = self.client.profile(_tokens(credentials), credentials["number"])
        found = [line for line in parse_lines(profile) if is_home_line(line.number)]
        if not found:
            raise EandError("This e& account has no home internet line")
        return [
            Line(
                line.number,
                plan=line.plan,
                account=line.account_number,
                meta={"short_code": line.short_code, "bill_cycle": line.bill_cycle},
            )
            for line in found
        ]

    def usage(self, credentials: dict, line: Line) -> Usage:
        return parse_usage(self.client.usage(_tokens(credentials), _eand_line(line)))

    def daily(self, credentials: dict, line: Line, start: dt.date, end: dt.date) -> dict[str, int]:
        return parse_history(self.client.history(_tokens(credentials), _eand_line(line), start, end))

    def billing(self, credentials: dict, line: Line) -> dict | None:
        if not line.account:
            return None
        tokens = _tokens(credentials)
        bills = parse_bills(self.client.open_amount(tokens, line.account), self.client.bill_history(tokens, line.account))
        return {**bills, "bill_cycle": line.meta.get("bill_cycle")}

    @classmethod
    def match_wan_login(cls, login: str, line_ids: list[str]) -> str | None:
        """e&'s router logins carry the landline, e.g. ETIS_0221234567."""
        number = number_in(login)
        return next((line_id for line_id in line_ids if same_number(number, line_id)), None)


def _tokens(credentials: dict) -> Tokens:
    return Tokens(credentials["access"], credentials["refresh"])


def _eand_line(line: Line) -> EandLine:
    return EandLine(line.id, line.meta.get("short_code"), line.plan, line.account, line.meta.get("bill_cycle"))


def _main(argv: list[str]) -> int:
    """Print e&'s raw answers for every signed-in e& account, to see fields Tattle doesn't read yet."""
    from .. import db
    from ..config import load_settings

    if argv != ["dump"]:
        print(__doc__, file=sys.stderr)
        return 2
    conn = db.connect(load_settings().db_path)
    client = EandClient()
    for row in conn.execute("SELECT identifier, credentials FROM provider_accounts WHERE provider = ?", (EandEg.id,)):
        tokens = _tokens(json.loads(row["credentials"]))
        try:
            profile = client.profile(tokens, row["identifier"])
        except SessionExpired:
            print(f"=== {row['identifier']}: session expired; let Tattle read once to refresh it", file=sys.stderr)
            continue
        print(f"=== profile for {row['identifier']}\n{profile}\n")
        for line in [line for line in parse_lines(profile) if is_home_line(line.number)]:
            print(f"=== usage of {line.number} ({line.short_code})\n{client.usage(tokens, line)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
