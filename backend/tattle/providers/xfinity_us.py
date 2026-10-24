"""Comcast Xfinity (USA): internet data usage from the Xfinity mobile app's API.

Xfinity signs in through its own hosted login (Xerxes), so the person signs in
in their own browser and pastes back where they land: Tattle builds the
authorize URL with a PKCE challenge, the person opens it, signs in, and the
page redirects to xfinitydigitalhome://auth?code=... which the browser can't
follow — they copy that code (or the whole redirected URL) back. Tattle trades
it for tokens and keeps refreshing with the refresh token.

Usage comes from the Galileo GraphQL API (operation InternetDataUsage): the
current month's used and allowed data, the days left, and whether the plan is
unlimited (then there is no quota, only what was used).

Xfinity reports data in GB/TB; many newer plans are unlimited.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import re
import secrets
import time
import urllib.parse

from .base import Bundle, Challenge, Line, Provider, ProviderError, SessionExpired, Usage
from .http import Http, Transport

AUTHORIZE_URL = "https://xerxes-sub.xerxessecure.com/xerxes-ctrl/oauth/authorize"
TOKEN_URL = "https://xerxes-sub.xerxessecure.com/xerxes-ctrl/oauth/token"
GRAPHQL_URL = "https://gw.api.dh.comcast.com/galileo/graphql"
CLIENT_ID = "xfinity-android-application"
REDIRECT_URI = "xfinitydigitalhome://auth"
# The GraphQL operation the Android app sends, with the id the server matches it by.
USAGE_OPERATION_ID = "61994c6016ac8c0ebcca875084919e5e01cb3b116a86aaf9646e597c3a1fbd06"
USAGE_QUERY = (
    "query InternetDataUsage { accountByServiceAccountId { internet { usage { monthlyUsage { "
    "policy startDate endDate daysRemaining currentUsage { value unit } allowableUsage { value unit } "
    "overage } } } } }"
)
GRAPHQL_HEADERS = {
    "user-agent": "Digital Home / Samsung SM-G991B / Android 14",
    "client": "digital-home-android",
    "client-detail": "MOBILE;Samsung;SM-G991B;Android 14;v5.38.0",
    "accept-language": "en-US",
    "content-type": "application/json",
}
_UNIT_POWERS = {"MB": 2, "GB": 3, "TB": 4}
UNIT_BASE = 1000  # Xfinity's data meter is decimal


class XfinityError(ProviderError):
    """Something Xfinity refused or could not answer; fit to show."""


# --- PKCE ----------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def code_verifier() -> str:
    return _b64(os.urandom(32))


def code_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode()).digest())


def authorize_url(verifier: str, state: str, activity_id: str) -> str:
    http = Http()
    return http.url(
        AUTHORIZE_URL,
        {
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "response_type": "code",
            "prompt": "select_account",
            "state": state,
            "scope": "profile",
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
            "activity_id": activity_id,
            "active_x1_account_count": "true",
            "rm_hint": "true",
            "partner_id": "comcast",
            "mso_partner_hint": "true",
        },
    )


def code_in(pasted: str) -> str | None:
    """The oi-<32 hex> code, from a pasted redirect URL or pasted on its own.
    The intermediate 0.ac... code is not it, so it is ignored."""
    text = (pasted or "").strip()
    if "code=" in text:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
        text = (query.get("code") or [text])[0]
    match = re.search(r"\b[a-z]{2}-[0-9a-f]{32}\b", text)
    return match.group(0) if match else None


# --- the usage answer ----------------------------------------------------------------
def _bytes(value: dict | None) -> int | None:
    if not isinstance(value, dict):
        return None
    power = _UNIT_POWERS.get(str(value.get("unit", "")).upper())
    try:
        return int(round(float(value["value"]) * UNIT_BASE**power)) if power is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _date(text) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def parse_usage(payload: dict, plan: str | None) -> Usage:
    """The most recent monthlyUsage entry is the current cycle."""
    _raise_graphql_errors(payload)
    data = payload.get("data") if isinstance(payload, dict) else None
    usage = (((data or {}).get("accountByServiceAccountId") or {}).get("internet") or {}).get("usage") or {}
    months = usage.get("monthlyUsage")
    if not isinstance(months, list) or not months:
        raise XfinityError("Xfinity returned no internet usage for this account")
    month = months[-1]
    used = _bytes(month.get("currentUsage"))
    allowed = _bytes(month.get("allowableUsage"))
    start, end = _date(month.get("startDate")), _date(month.get("endDate"))
    renews = end + dt.timedelta(days=1) if end else None
    unlimited = str(month.get("policy", "")).lower() == "unlimited" or not allowed
    if unlimited:
        bundle = Bundle("plan", plan, None, None, renews=renews, label="No quota (unlimited plan)")
    else:
        remaining = max(allowed - used, 0) if used is not None else None
        bundle = Bundle("plan", plan, allowed, remaining, renews=renews)
    return Usage(bundles=[bundle], renews=renews, cycle_start=start)


def _raise_graphql_errors(payload) -> None:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        messages = [e.get("message") for e in errors if isinstance(e, dict) and e.get("message")]
        raise XfinityError("Xfinity refused the request: " + (messages[0] if messages else "unknown error"))


# --- the provider --------------------------------------------------------------------
class XfinityUs(Provider):
    id = "xfinity_us"
    name = "Xfinity (Comcast)"
    country = "US"
    status = "beta"
    unit_base = UNIT_BASE
    currency = "USD"
    fields = ()  # the whole sign-in is the paste step below
    notes = "Sign in to Xfinity in your own browser, then paste back where it sends you. New Xfinity plans are often unlimited."
    docs = "docs/providers/xfinity_us.md"

    def __init__(self, transport: Transport | None = None):
        self.transport = transport

    def account_name(self, values: dict[str, str]) -> str:
        return "xfinity"

    # --- signing in -----------------------------------------------------------------
    def start_sign_in(self, values: dict[str, str]) -> Challenge:
        verifier = code_verifier()
        return Challenge(
            "paste",
            "Open this link, sign in to Xfinity, then paste the address the page ends on (it starts with "
            "xfinitydigitalhome://), or the code in it.",
            state={"code_verifier": verifier},
            url=authorize_url(verifier, secrets.token_urlsafe(16), secrets.token_hex(16)),
        )

    def answer(self, values: dict[str, str], challenge: Challenge, answer: str) -> dict:
        code = code_in(answer)
        if not code:
            raise XfinityError(
                "That did not contain an Xfinity sign-in code. Paste the whole address the page ended on "
                "(it starts with xfinitydigitalhome://)."
            )
        return self._exchange(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": challenge.state["code_verifier"],
            }
        )

    def refresh(self, credentials: dict) -> dict:
        if not credentials.get("refresh"):
            raise XfinityError("Signed out: sign in to Xfinity again from Settings", signed_out=True)
        try:
            return self._exchange({"grant_type": "refresh_token", "refresh_token": credentials["refresh"]})
        except XfinityError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                raise XfinityError("Xfinity signed Tattle out: sign in again from Settings", signed_out=True, status=exc.status) from exc
            raise

    # --- reading ------------------------------------------------------------------
    def lines(self, credentials: dict) -> list[Line]:
        return [Line("xfinity", plan="Xfinity Internet")]

    def usage(self, credentials: dict, line: Line) -> Usage:
        if time.time() > float(credentials.get("expires_at") or 0) - 30:
            raise SessionExpired("The Xfinity session ran out")
        response = self._http().post(
            GRAPHQL_URL,
            json={"operationName": "InternetDataUsage", "variables": {}, "query": USAGE_QUERY},
            headers={
                **GRAPHQL_HEADERS,
                "authorization": f"{credentials.get('token_type', 'Bearer')} {credentials['access']}",
                "x-id-token": credentials.get("id_token", ""),
                "x-apollo-operation-id": USAGE_OPERATION_ID,
                "x-apollo-operation-name": "InternetDataUsage",
                "accept": "multipart/mixed; deferSpec=20220824, application/json",
            },
        )
        if response.status in (401, 403):
            raise SessionExpired("The Xfinity session ran out")
        if response.status != 200:
            raise XfinityError(f"Xfinity answered {response.status}. Tattle will try again.", status=response.status)
        return parse_usage(response.json(), line.plan)

    # --- talking to Xfinity ---------------------------------------------------------
    def _http(self) -> Http:
        return Http(transport=self.transport, who="Xfinity", error=XfinityError)

    def _exchange(self, form: dict) -> dict:
        """Trade a code or refresh token for a fresh token set."""
        response = self._http().post(
            TOKEN_URL,
            form={
                **form,
                "client_id": CLIENT_ID,
                "scope": "profile",
                "partner_id": "comcast",
                "mso_partner_hint": "true",
                "active_x1_account_count": "true",
                "rm_hint": "true",
            },
            headers={"Accept": "application/json"},
        )
        data = response.json() if response.text else None
        if response.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            message = data.get("error_description") or data.get("error") if isinstance(data, dict) else None
            raise XfinityError(message or "Xfinity did not accept the sign-in. Try the paste step again.", status=response.status)
        return _tokens(data)


def _tokens(data: dict) -> dict:
    return {
        "access": data["access_token"],
        "refresh": data.get("refresh_token") or "",
        "id_token": data.get("id_token") or "",
        "token_type": data.get("token_type") or "Bearer",
        "expires_at": time.time() + float(data.get("expires_in") or 3600),
    }
