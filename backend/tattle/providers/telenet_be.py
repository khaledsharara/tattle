"""Telenet and BASE (Belgium): internet usage from the My Telenet / My BASE API.

Signs in the way the customer website does, through Telenet's Okta sign-in
(identity engine, "idx"): the authorization page carries a state token; it is
introspected into a state handle, the user name is identified, the password
authenticator is challenged and answered, and the success link lands back on
the API with session cookies. Those cookies are kept between readings; when
they stop working Tattle signs in again with the stored password.

Readings: the internet products of the plan, the current bill cycle, the usage
counter for that cycle (GB) and the per-day split (GB). Telenet's older API
converted its KB counters to GB by 1024 twice, so 1 GB = 1024 MB here.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from .base import Bundle, Field, Line, Provider, ProviderError, SessionExpired, Usage
from .http import Http, Response, Transport

TELENET_UNIT = 1024
BRANDS: dict[str, dict[str, str]] = {
    "telenet": {
        "name": "Telenet",
        "api": "https://api.prd.telenet.be",
        "secure": "https://secure.telenet.be",
        "authorization": "https://api.prd.telenet.be/ocapi/login/authorization/telenet_be"
        "?lang=nl&style_hint=care&targetUrl=https://www2.telenet.be/residential/nl/mytelenet/",
        "origin": "https://www2.telenet.be",
        "alt_referer": "https://www2.telenet.be/residential/nl/mijn-telenet/",
    },
    "base": {
        "name": "BASE",
        "api": "https://api.prd.base.be",
        "secure": "https://secure.base.be",
        "authorization": "https://api.prd.base.be/ocapi/login/authorization/base_be"
        "?lang=nl&style_hint=care&targetUrl=https://www.base.be/",
        "origin": "https://www.base.be",
        "alt_referer": "https://www.base.be/",
    },
}
PRODUCTS_PATH = "/ocapi/public/api/product-service/v1/product-subscriptions?producttypes=PLAN"
_STATE_TOKEN = re.compile(r'"stateToken"\s*:\s*"((?:[^"\\]|\\.){1,8192})"')


class TelenetError(ProviderError):
    """Something Telenet or BASE refused or could not answer; fit to show."""


class Rejected(TelenetError):
    """The user name or password was turned down."""


def brand_of(raw: str | None) -> str:
    brand = (raw or "telenet").strip().lower()
    if brand not in BRANDS:
        raise ValueError("Type telenet or base (or leave it empty for Telenet)")
    return brand


def state_token(page: str) -> str | None:
    """The Okta state token in the sign-in page, a JavaScript string with \\x escapes."""
    match = _STATE_TOKEN.search(page or "")
    if not match:
        return None
    escaped = re.sub(r"\\x([0-9a-fA-F]{2})", r"\\u00\1", match.group(1))
    try:
        token = json.loads(f'"{escaped}"')
    except ValueError:
        return None
    return token or None


def _gb(value) -> int | None:
    try:
        return int(round(float(value) * TELENET_UNIT**3))
    except (TypeError, ValueError):
        return None


def _date(text) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def internet_products(plans) -> list[dict]:
    """Internet products, also the ones inside a bundle (internet + TV + mobile)."""
    found: list[dict] = []
    for plan in plans if isinstance(plans, list) else []:
        if not isinstance(plan, dict):
            continue
        kind = str(plan.get("productType", "")).lower()
        children = plan.get("products") if kind == "bundle" else [plan]
        for product in children or []:
            if isinstance(product, dict) and str(product.get("productType", "")).lower() == "internet" and product.get("identifier"):
                found.append(product)
    return found


def current_cycle(data) -> tuple[dt.date, dt.date] | None:
    """The first bill cycle is the current one; its endDate is its last day."""
    cycles = data.get("billCycles") if isinstance(data, dict) else None
    first = cycles[0] if isinstance(cycles, list) and cycles and isinstance(cycles[0], dict) else {}
    start, end = _date(first.get("startDate")), _date(first.get("endDate"))
    return (start, end) if start and end else None


def parse_usage(data, cycle: tuple[dt.date, dt.date], label: str | None) -> Usage:
    """CAP plans stop or charge past the allocation; FUP and TURBO plans slow
    down past it; UNLIMITED plans have none. Sizes are in GB."""
    internet = data.get("internet") if isinstance(data, dict) and isinstance(data.get("internet"), dict) else {}
    category = str(internet.get("category") or "").upper() or None
    used = _gb((internet.get("totalUsage") or {}).get("units"))
    allocated = _gb((internet.get("allocatedUsage") or {}).get("units"))
    renews = cycle[1] + dt.timedelta(days=1)
    if category == "UNLIMITED" or not allocated:
        bundle = Bundle("plan", label, None, None, renews=renews, label=category or "No quota")
    else:
        remaining = max(allocated - used, 0) if used is not None else None
        bundle = Bundle("plan", label, allocated, remaining, renews=renews, label=category)
    return Usage(bundles=[bundle], renews=renews, cycle_start=cycle[0])


def parse_daily(data, start: dt.date, end: dt.date) -> dict[str, int]:
    """Per-day totals (peak + off-peak), {"2026-10-10": bytes}."""
    usage = data.get("internetUsage") if isinstance(data, dict) else None
    first = usage[0] if isinstance(usage, list) and usage and isinstance(usage[0], dict) else {}
    days: dict[str, int] = {}
    for row in first.get("dailyUsages") or []:
        day = _date(row.get("date")) if isinstance(row, dict) else None
        size = _gb(row.get("total")) if isinstance(row, dict) else None
        if day and size is not None and start <= day <= end:
            days[day.isoformat()] = days.get(day.isoformat(), 0) + size
    return days


def _okta_message(data) -> str | None:
    """Okta's own words in a refused step, e.g. 'Password is incorrect'."""
    messages = (data.get("messages") or {}).get("value") if isinstance(data, dict) else None
    texts = [m.get("message") for m in messages or [] if isinstance(m, dict) and m.get("message")]
    return " ".join(texts) or None


class TelenetBe(Provider):
    id = "telenet_be"
    name = "Telenet / BASE"
    country = "BE"
    status = "beta"
    unit_base = TELENET_UNIT
    currency = "EUR"
    fields = (
        Field("username", "My Telenet or My BASE user name", placeholder="user@example.com"),
        Field("password", "Password", kind="password"),
        Field(
            "brand",
            "Telenet or BASE",
            placeholder="telenet",
            help="Type base if you are a BASE customer; leave it empty for Telenet.",
            optional=True,
        ),
    )
    notes = "Signs in like the My Telenet / My BASE website. Accounts that sign in with a code instead of a password are not supported yet."
    docs = "docs/providers/telenet_be.md"
    has_daily = True

    def __init__(self, transport: Transport | None = None):
        self.transport = transport

    # --- signing in -----------------------------------------------------------------
    def start_sign_in(self, values: dict[str, str]) -> dict:
        brand = brand_of(values.get("brand"))
        username, password = values.get("username", "").strip(), values.get("password", "")
        http = self._http(brand)
        self._sign_in(http, brand, username, password)
        return {"brand": brand, "username": username, "password": password, "cookies": http.export_cookies()}

    def refresh(self, credentials: dict) -> dict:
        brand = brand_of(credentials.get("brand"))
        http = self._http(brand)
        try:
            self._sign_in(http, brand, credentials["username"], credentials.get("password", ""))
        except Rejected as exc:
            raise TelenetError(f"{BRANDS[brand]['name']} signed Tattle out: {exc}", signed_out=True) from exc
        return {**credentials, "cookies": http.export_cookies()}

    # --- reading ------------------------------------------------------------------
    def lines(self, credentials: dict) -> list[Line]:
        products = internet_products(self._get(credentials, PRODUCTS_PATH))
        if not products:
            raise TelenetError("This account has no internet product Tattle can read")
        return [Line(str(p["identifier"]), plan=(str(p.get("label") or "").split("/")[0].strip() or None)) for p in products]

    def usage(self, credentials: dict, line: Line) -> Usage:
        cycle = self._cycle(credentials, line)
        data = self._get(credentials, _product_path(line, "usage", cycle))
        return parse_usage(data, cycle, line.plan)

    def daily(self, credentials: dict, line: Line, start: dt.date, end: dt.date) -> dict[str, int]:
        """Telenet keeps the per-day split of the current bill cycle only."""
        cycle = self._cycle(credentials, line)
        data = self._get(credentials, _product_path(line, "dailyusage", cycle, current=True))
        return parse_daily(data, start, end)

    # --- talking to Telenet ---------------------------------------------------------
    def _http(self, brand: str, cookies: list[dict] | None = None) -> Http:
        http = Http(BRANDS[brand]["api"], transport=self.transport, who=BRANDS[brand]["name"], error=TelenetError)
        http.import_cookies(cookies)
        return http

    @staticmethod
    def _headers(brand: str) -> dict[str, str]:
        config = BRANDS[brand]
        return {
            "x-alt-referer": config["alt_referer"],
            "X-Requested-With": "XMLHttpRequest",
            "Origin": config["origin"],
            "Referrer": config["origin"],  # sic: the website sends this spelling
        }

    def _cycle(self, credentials: dict, line: Line) -> tuple[dt.date, dt.date]:
        path = (
            f"/ocapi/public/api/billing-service/v1/account/products/{line.id}/billcycle-details"
            "?producttype=internet&count=3"
        )
        cycle = current_cycle(self._get(credentials, path))
        if cycle is None:
            raise TelenetError("Telenet sent no bill cycle for this internet product")
        return cycle

    def _get(self, credentials: dict, path: str) -> Any:
        brand = brand_of(credentials.get("brand"))
        http = self._http(brand, credentials.get("cookies"))
        response = http.get(path, headers=self._headers(brand))
        credentials["cookies"] = http.export_cookies()
        if response.status in (401, 403):
            raise SessionExpired(f"The {BRANDS[brand]['name']} session ran out")
        if response.status != 200:
            raise TelenetError(f"{BRANDS[brand]['name']} answered {response.status}. Tattle will try again.", status=response.status)
        return response.json()

    def _step(self, http: Http, brand: str, path: str, payload: dict) -> dict:
        """One Okta identity-engine step on the secure host."""
        response = http.post(BRANDS[brand]["secure"] + "/idp/idx/" + path, json=payload, headers=self._headers(brand))
        data = response.json() if response.text else {}
        if response.status >= 500:
            raise TelenetError(f"{BRANDS[brand]['name']}'s sign-in is having trouble. Try again later.", status=response.status)
        if response.status >= 400 or not isinstance(data, dict):
            message = _okta_message(data)
            raise Rejected(message or f"{BRANDS[brand]['name']} did not accept the user name or password.", status=response.status)
        return data

    def _sign_in(self, http: Http, brand: str, username: str, password: str) -> None:
        config = BRANDS[brand]
        headers = self._headers(brand)
        page = http.get(config["authorization"], headers={**headers, "Accept": "text/html"})
        token = state_token(page.text)
        if token is None:
            raise TelenetError(f"{config['name']}'s sign-in page did not load as expected. Try again later.", status=page.status)
        handle = self._step(http, brand, "introspect", {"stateToken": token}).get("stateHandle")
        # The website fetches these two before identifying; Okta may expect them.
        http.get(config["secure"] + "/auth/services/devicefingerprint", headers=headers)
        http.post(config["secure"] + "/api/v1/internal/device/nonce", json={}, headers=headers)
        identified = self._step(http, brand, "identify", {"identifier": username, "stateHandle": handle})
        authenticators = (identified.get("authenticators") or {}).get("value") or []
        password_id = next((a.get("id") for a in authenticators if isinstance(a, dict) and a.get("type") == "password"), None)
        if not password_id:
            raise Rejected(_okta_message(identified) or "This account does not sign in with a password, which Tattle can't do yet.")
        challenged = self._step(
            http, brand, "challenge", {"authenticator": {"id": password_id}, "stateHandle": identified.get("stateHandle")}
        )
        answered = self._step(
            http,
            brand,
            "challenge/answer",
            {"credentials": {"passcode": password}, "stateHandle": challenged.get("stateHandle")},
        )
        href = (answered.get("success") or {}).get("href") if isinstance(answered.get("success"), dict) else None
        if not href:
            raise Rejected(_okta_message(answered) or f"{config['name']} did not accept the password.")
        _finish(http.get(href, headers=headers), config["name"])


def _product_path(line: Line, what: str, cycle: tuple[dt.date, dt.date], current: bool = False) -> str:
    dates = f"fromDate={cycle[0].isoformat()}&toDate={cycle[1].isoformat()}"
    extra = "billcycle=CURRENT&" if current else ""
    return f"/ocapi/public/api/product-service/v1/products/internet/{line.id}/{what}?{extra}{dates}"


def _finish(response: Response, name: str) -> None:
    if response.status >= 400:
        raise TelenetError(f"{name} accepted the password but did not finish signing in. Try again.", status=response.status)
