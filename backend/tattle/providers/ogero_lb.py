"""Ogero (Lebanon): DSL quota from the My Ogero pages the Ogero app shows.

Signs in at ogero.gov.lb/API/Login.php (user name + password, answered with a
SessionID), then reads three HTML pages with that session: the dashboard
(every phone line with its DSL account), the consumption page of a DSL account
(bundle, quota, consumption so far, last update) and the bill page of a phone
line. When the session runs out the pages say "You are required to login";
Tattle then signs in again with the stored password.

Ogero shows sizes in GB and gives no cycle dates; quotas are monthly, so the
cycle is taken to be the calendar month (unverified).
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable
from zoneinfo import ZoneInfo

from .base import Bundle, Field, Line, Provider, ProviderError, SessionExpired, Usage
from .http import Http, Transport

LOGIN_URL = "https://ogero.gov.lb/API/Login.php"
MYOGERO_URL = "https://ogero.gov.lb/myogero"
BEIRUT = ZoneInfo("Asia/Beirut")
OGERO_UNIT = 1000  # unverified
LOGIN_REQUIRED = "You are required to login"
_UNIT_POWERS = {"KB": 1, "MB": 2, "GB": 3, "TB": 4}
_VOID = {"br", "img", "input", "meta", "link", "hr", "wbr", "source", "col"}


class OgeroError(ProviderError):
    """Something Ogero refused or could not answer; fit to show."""


class Rejected(OgeroError):
    """Ogero turned the user name or password down."""


# --- a small HTML tree (standard library only) ---------------------------------------
@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)

    def text(self) -> str:
        parts = [c if isinstance(c, str) else c.text() for c in self.children]
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def iter(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None, **attrs: str) -> list[Node]:
        return [
            n
            for n in self.iter()
            if (tag is None or n.tag == tag)
            and (cls is None or cls in n.attrs.get("class", "").split())
            and all(n.attrs.get(k) == v for k, v in attrs.items())
        ]

    def elements(self) -> list[Node]:
        return [c for c in self.children if isinstance(c, Node)]


class _TreeBuilder(HTMLParser):
    """Comments are dropped (Ogero comments out old rows), unclosed tags are tolerated."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].children.append(data)


def parse_html(text: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(text or "")
    builder.close()
    return builder.root


# --- Ogero's pages -------------------------------------------------------------------
def page_error(root: Node) -> str | None:
    """Ogero puts errors in a script that redirects with ?error=<message>."""
    for script in root.find_all("script", language="javascript"):
        found = re.search(r"error=([^&;\"']+)", script.text())
        if found:
            return urllib.parse.unquote_plus(found.group(1)).strip()
    return None


def parse_accounts(root: Node) -> list[tuple[str, str]]:
    """(phone line, DSL account) pairs from the dashboard's line selector."""
    select = root.find_all("select", id="changnumber")
    if not select:
        raise OgeroError("Ogero's dashboard did not list any line")
    return [
        (o.attrs.get("value", "").strip(), o.attrs.get("value2", "").strip())
        for o in select[0].find_all("option")
        if o.attrs.get("value2", "").strip()
    ]


def size(text: str) -> int | None | float:
    """'800 GB FUP' -> bytes; 'Unlimited' -> inf; unreadable -> None."""
    if text.strip().lower().startswith("unlimited"):
        return float("inf")
    match = re.match(r"\s*([\d.,]+)\s*([KMGT]B)?", text, re.IGNORECASE)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(round(number * OGERO_UNIT ** _UNIT_POWERS[(match.group(2) or "GB").upper()]))


def month_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    """(first day of this month, first day of next month)."""
    start = today.replace(day=1)
    days = calendar.monthrange(today.year, today.month)[1]
    return start, start + dt.timedelta(days=days)


def parse_consumption(root: Node, today: dt.date) -> Usage:
    rows: dict[str, str] = {}
    for grid in root.find_all("div", cls="MyConsumptionGrid"):
        spans = [c for c in grid.elements() if c.tag == "span"]
        if len(spans) >= 2:
            rows[spans[0].text()] = " ".join(s.text() for s in spans[1:] if s.text())
    if not rows:
        raise OgeroError("Ogero's consumption page had no figures")
    quota = size(rows.get("Total Quota", ""))
    used = size(rows.get("Total Consumption", ""))
    start, renews = month_bounds(today)
    if quota == float("inf") or quota is None:
        total = remaining = None
    else:
        total = int(quota)
        remaining = max(total - int(used), 0) if isinstance(used, int) else None
    bundle = Bundle(
        "plan",
        rows.get("Current Bundle") or None,
        total_bytes=total,
        remaining_bytes=remaining,
        renews=renews,
        label=rows.get("Total Quota") or None,
    )
    return Usage(bundles=[bundle], renews=renews, cycle_start=start)


def _amount(text: str) -> float | None:
    match = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    try:
        return float(match.group(0).replace(",", "")) if match else None
    except ValueError:
        return None


def parse_bills(root: Node) -> dict:
    """Outstanding amount and the bill table (LBP). Ogero names bills by month only."""
    outstanding = root.find_all("div", cls="BillOutstandingSection1")
    open_amount = None
    if outstanding:
        spans = outstanding[0].find_all("span")
        open_amount = _amount(spans[0].text()) if spans else None
    bills, unpaid = [], []
    for row in root.find_all("tr"):
        dates, amounts = row.find_all(cls="BillDate"), row.find_all(cls="BillAmount")
        if not dates or not amounts:
            continue
        try:
            month = dt.datetime.strptime(dates[0].text().replace(" ", ""), "%b%Y").date()
        except ValueError:
            continue
        value = _amount(amounts[0].text())
        cells = [c for c in row.elements() if c.tag == "td"]
        status = cells[2].text().lower() if len(cells) > 2 else ""
        paid = True if status == "paid" else False if status == "not paid" else None
        start, end = month_bounds(month)
        bills.append(
            {"from": start.isoformat(), "to": (end - dt.timedelta(days=1)).isoformat(), "issued": None, "due": None, "value": value, "paid": paid}
        )
        if paid is False:
            unpaid.append({"due": None, "value": value})
    return {"open_amount": open_amount, "unpaid": unpaid, "bills": bills, "currency": "LBP"}


# --- the provider --------------------------------------------------------------------
class OgeroLb(Provider):
    id = "ogero_lb"
    name = "Ogero"
    country = "LB"
    status = "beta"
    unit_base = OGERO_UNIT
    currency = "LBP"
    fields = (
        Field("username", "My Ogero user name", placeholder="user@example.com"),
        Field("password", "My Ogero password", kind="password"),
    )
    notes = "Reads the DSL quota and bills shown in the Ogero app. Every DSL line on the account shows up."
    docs = "docs/providers/ogero_lb.md"
    has_billing = True

    def __init__(self, transport: Transport | None = None, today: Callable[[], dt.date] | None = None):
        self.transport = transport
        self.today = today or (lambda: dt.datetime.now(BEIRUT).date())

    def start_sign_in(self, values: dict[str, str]) -> dict:
        username, password = values.get("username", "").strip(), values.get("password", "")
        return {"username": username, "password": password, "session": self._sign_in(username, password)}

    def refresh(self, credentials: dict) -> dict:
        try:
            session = self._sign_in(credentials["username"], credentials.get("password", ""))
        except Rejected as exc:
            raise OgeroError(f"Ogero signed Tattle out: {exc}", signed_out=True) from exc
        return {**credentials, "session": session}

    def lines(self, credentials: dict) -> list[Line]:
        accounts = parse_accounts(self._page(credentials, "mobileapp.dashboard.php", nbr="", dsl=""))
        if not accounts:
            raise OgeroError("This Ogero account has no DSL line")
        return [Line(dsl, account=phone or None, meta={"phone": phone}) for phone, dsl in accounts]

    def usage(self, credentials: dict, line: Line) -> Usage:
        return parse_consumption(self._page(credentials, "consumption.php", dsl=line.id), self.today())

    def billing(self, credentials: dict, line: Line) -> dict | None:
        phone = line.meta.get("phone")
        return parse_bills(self._page(credentials, "bill.php", nbr=phone)) if phone else None

    # --- talking to Ogero -----------------------------------------------------------
    def _http(self) -> Http:
        return Http(transport=self.transport, who="Ogero", error=OgeroError)

    def _sign_in(self, username: str, password: str) -> str:
        response = self._http().post(
            LOGIN_URL, form={"Username": username, "Password": password}, headers={"Accept": "application/json", "Cache-Control": "no-cache"}
        )
        data = response.json() if response.text else None
        if response.status == 400 or (isinstance(data, dict) and data.get("error")):
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise Rejected(message or "Ogero did not accept the user name or password.", status=response.status)
        if response.status != 200 or not isinstance(data, dict) or not data.get("SessionID"):
            raise OgeroError(f"Ogero could not sign in right now (it answered {response.status}). Try again later.", status=response.status)
        return str(data["SessionID"])

    def _page(self, credentials: dict, page: str, **params: str | None) -> Node:
        # "AppRequest" goes without a value, as the app sends it.
        query = "&".join(
            [f"SessionID={urllib.parse.quote(credentials.get('session', ''))}", f"Username={urllib.parse.quote(credentials['username'])}", "AppRequest"]
            + [f"{k}={urllib.parse.quote(v or '')}" for k, v in params.items()]
        )
        response = self._http().get(f"{MYOGERO_URL}/{page}?{query}", headers={"Accept": "text/html"})
        if response.status != 200:
            raise OgeroError(f"Ogero answered {response.status}. Tattle will try again.", status=response.status)
        root = parse_html(response.text)
        error = page_error(root)
        if error and error.startswith(LOGIN_REQUIRED):
            raise SessionExpired("The Ogero session ran out")
        if error:
            raise OgeroError(f"Ogero said: {error}")
        return root
