"""UniFi Network: UniFi OS consoles (UDM, UDR, UCG, Cloud Key Gen2+) and
self-hosted UniFi Network servers.

The two sign in differently. A console answers / with its own sign-in page
(200), takes POST /api/auth/login and serves the Network API under
/proxy/network, with an x-csrf-token header to send back. A self-hosted
server redirects / to /manage (302), takes POST /api/login and serves the
API at the root. A self-hosted server answers /api/auth/login with 401
api.err.LoginRequired, not 404, so Tattle asks / first instead of guessing
from a failed sign-in.

Per-client totals come from stat/sta and are counted from the network's
side: `tx_bytes` is what the access point sent to the client, so it is the
client's download (Ubiquiti's own client list shows it in the "Down" column).
Wired clients use `wired-tx_bytes` / `wired-rx_bytes`, which the UI shows as
their Down/Up. The access points and switches count everything a device
sends or receives, so traffic that stays in the house is included. Totals
restart when a client reconnects. See docs/routers/unifi.md.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ..providers.base import Field, ProviderError
from ..providers.http import Http, Response, Transport
from .base import DeviceCounter, RouterAdapter, RouterError

CONSOLE_PREFIX = "/proxy/network"


def _yes(value: str | None) -> bool:
    return (value or "").strip().lower() in ("yes", "y", "true", "1", "on")


def _flag(value: Any) -> bool:
    """UniFi sends booleans as true/false or as "true"/"false"."""
    return value is True or str(value).lower() == "true"


def _count(value: Any) -> int | None:
    """UniFi sends numbers as numbers or as numeric strings."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _body(response: Response) -> dict:
    try:
        body = response.json()
    except ProviderError:
        return {}
    return body if isinstance(body, dict) else {}


def _meta_msg(response: Response) -> str:
    meta = _body(response).get("meta")
    return str(meta.get("msg") or "") if isinstance(meta, dict) else ""


class Unifi(RouterAdapter):
    id = "unifi"
    name = "UniFi Network (console or self-hosted)"
    status = "beta"
    capabilities = frozenset({"device_counters"})
    fields = (
        Field("url", "Controller address", placeholder="https://192.168.1.1", help="The console's address, or your UniFi Network server's with its port, e.g. https://192.168.1.10:8443."),
        Field("user", "User name", optional=True, help="A local account with View Only access to UniFi Network (on consoles: Restrict to Local Access Only)."),
        Field("password", "Password", kind="password", optional=True),
        Field("api_key", "API key", kind="password", optional=True, help="Instead of a user name and password, on consoles with UniFi Network 9 or newer: Settings > Control Plane > Integrations."),
        Field("site", "Site", placeholder="default", optional=True, help="The site's short name, as in the address bar (/manage/<site>/...). Most homes have only 'default'."),
        Field("verify_tls", "Check the certificate", placeholder="no", optional=True, help="yes only if the controller has a certificate this computer trusts; UniFi makes self-signed ones."),
    )
    notes = "Per-device totals from your access points and switches. They include traffic that stays in the house."
    docs = "docs/routers/unifi.md"

    def __init__(self, values: dict[str, str], transport: Transport | None = None):
        super().__init__(values)
        url = (values.get("url") or "https://192.168.1.1").strip().rstrip("/")
        self.verify_tls = _yes(values.get("verify_tls"))
        self.http = Http(
            url if "://" in url else f"https://{url}",
            timeout=15.0, transport=transport, verify_tls=self.verify_tls, who="the UniFi controller", error=RouterError,
        )  # fmt: skip
        self.user = (values.get("user") or "").strip()
        self.password = values.get("password") or ""
        self.api_key = (values.get("api_key") or "").strip()
        self.site = (values.get("site") or "default").strip() or "default"
        self.prefix: str | None = None  # CONSOLE_PREFIX or "", once known
        self.csrf: str | None = None
        self.signed_in = False

    # --- signing in --------------------------------------------------------------------
    def _is_console(self) -> bool:
        if self.prefix is None:
            if self.api_key:  # API keys exist only on consoles
                self.prefix = CONSOLE_PREFIX
            else:
                self.prefix = CONSOLE_PREFIX if self.http.get("/", follow_redirects=False).status == 200 else ""
        return self.prefix == CONSOLE_PREFIX

    def _login(self) -> None:
        if not self.user or not self.password:
            raise RouterError("Fill in the user name and password, or an API key")
        self.http.jar.clear()
        self.csrf = None
        credentials = {"username": self.user, "password": self.password}
        kinds = [CONSOLE_PREFIX, ""] if self._is_console() else ["", CONSOLE_PREFIX]
        for prefix in kinds:
            response = self.http.post("/api/auth/login" if prefix else "/api/login", json=credentials, follow_redirects=False)
            if response.status == 200:
                self.prefix = prefix
                self.csrf = response.headers.get("x-updated-csrf-token") or response.headers.get("x-csrf-token")
                self.signed_in = True
                return
            # The other kind of controller: no such page, or (self-hosted) "log in first".
            if response.status in (301, 302, 303, 307, 308, 404) or _meta_msg(response) == "api.err.LoginRequired":
                continue
            raise RouterError(self._login_failure(response))
        raise RouterError("No UniFi sign-in at this address: check the controller address (self-hosted servers usually need :8443)")

    @staticmethod
    def _login_failure(response: Response) -> str:
        text = response.text.upper()
        if response.status == 429:
            return "The console refuses sign-ins for a while after failed attempts: wait a few minutes, then test again"
        if response.status == 499 or "MFA" in text or "2FA" in text:
            return "This account needs two-factor sign-in. Make a local account for Tattle (Restrict to Local Access Only)"
        if response.status in (400, 401, 403):
            return "The controller refused the user name or password"
        return f"The controller's sign-in failed with status {response.status}"

    def _headers(self) -> dict[str, str]:
        if self.api_key:
            return {"X-API-Key": self.api_key}
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    # --- reading -----------------------------------------------------------------------
    def _get(self, path: str) -> list[dict]:
        """GET a Network API list, signing in again once when the session ran out."""
        response = None
        for attempt in range(2):
            if not self.api_key and not self.signed_in:
                self._login()
            self._is_console()
            response = self.http.get(f"{self.prefix}{path}", headers=self._headers(), follow_redirects=False)
            token = response.headers.get("x-updated-csrf-token")
            if token:
                self.csrf = token
            if _meta_msg(response) == "api.err.NoSiteContext":
                raise RouterError(self._no_site())
            if response.status == 401 and not self.api_key and attempt == 0:
                self.signed_in = False  # the session ran out (or the controller restarted)
                continue
            break
        assert response is not None
        if response.status == 401:
            if self.api_key:
                raise RouterError("The console refused the API key")
            raise RouterError("The controller signed Tattle out right after signing in: check that the account may use UniFi Network")
        if response.status == 404:
            raise RouterError(f"The controller has no {path}: check the address and the site name")
        body = _body(response)
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        if response.status != 200 or meta.get("rc", "ok") != "ok":
            raise RouterError(f"The controller answered status {response.status}: {meta.get('msg') or 'no details'}")
        data = body.get("data")
        if not isinstance(data, list):
            raise RouterError("The controller's answer was not what Tattle expected")
        return [entry for entry in data if isinstance(entry, dict)]

    def _no_site(self) -> str:
        message = f"The controller has no site called {self.site!r}"
        try:
            response = self.http.get(f"{self.prefix}/api/self/sites", headers=self._headers(), follow_redirects=False)
            names = [str(s.get("name")) for s in _body(response).get("data") or [] if isinstance(s, dict) and s.get("name")]
        except RouterError:
            names = []
        return f"{message}; it has: {', '.join(names)}" if names else message

    def device_counters(self) -> list[DeviceCounter]:
        clients = self._get(f"/api/s/{urllib.parse.quote(self.site, safe='')}/stat/sta")
        counters = []
        for client in clients:
            mac = str(client.get("mac") or "").lower()
            if not mac:
                continue
            if _flag(client.get("is_wired")) and "wired-tx_bytes" in client:
                down, up = client.get("wired-tx_bytes"), client.get("wired-rx_bytes")
            else:
                down, up = client.get("tx_bytes"), client.get("rx_bytes")
            rx, tx = _count(down), _count(up)
            if rx is None or tx is None:  # skip rather than book a made-up restart
                continue
            name = str(client.get("name") or client.get("hostname") or "") or None
            counters.append(DeviceCounter(mac, rx=rx, tx=tx, ip=str(client.get("ip") or "") or None, hostname=name))
        return counters

    def test(self) -> list[str]:
        found = super().test()
        if self.api_key:
            how = "with an API key"
        else:
            how = "signed in to a UniFi OS console" if self.prefix == CONSOLE_PREFIX else "signed in to a UniFi Network server"
        return [*found, f"site {self.site}, {how}"]
