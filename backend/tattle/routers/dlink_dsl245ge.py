"""D-Link DSL-245GE, the VDSL modem-router e& Egypt hands out (firmware ET_1.02b01).

Its web UI keeps no per-device counters, but it tells Tattle which internet
login (and so which e& line) is in use, how fast the DSL line synced, its own
internet byte counters, and whether its DHCP server is on.

The router tracks logins per client IP; there is no session cookie. Loading
/cgi-bin/Login.asp without parameters (the web UI's frameset does it) ends the
login, so Tattle signs in again whenever a page comes back without its data.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse

from ..providers.base import Field
from ..providers.http import Http, Transport
from .base import RouterAdapter, RouterError


class DlinkDsl245ge(RouterAdapter):
    id = "dlink_dsl245ge"
    name = "D-Link DSL-245GE (e& Egypt)"
    status = "stable"
    capabilities = frozenset({"wan_counters", "wan_login", "link_status", "dhcp_enabled"})
    fields = (
        Field("url", "Router address", placeholder="http://192.168.1.1"),
        Field("user", "User name", placeholder="user", help="The account on the router's sign-in page."),
        Field("password", "Password", kind="password"),
    )
    notes = "Shows which e& line the router is signed in with, the DSL speeds and the router's own counters. No per-device counts."
    docs = "docs/routers/dlink_dsl245ge.md"
    wan_modulus = 2**32  # the WAN byte counters are 32-bit and wrap around

    def __init__(self, values: dict[str, str], transport: Transport | None = None):
        super().__init__(values)
        url = (values.get("url") or "http://192.168.1.1").strip()
        self.http = Http(url if "://" in url else f"http://{url}", timeout=10.0, transport=transport, who="the router", error=RouterError)
        self.user = values.get("user") or "user"
        self.password = values.get("password") or ""

    def _login(self) -> None:
        query = urllib.parse.urlencode({"User": self.user, "Pwd": hashlib.md5(self.password.encode()).hexdigest()})
        if self.http.get(f"/cgi-bin/Login.asp?{query}").text.strip() != "1":
            raise RouterError("The router refused the user name or password")

    def _page(self, path: str, marker: str) -> str:
        """A page's text, signing in first if the answer is not the real page."""
        text = self.http.get(path).text
        if marker not in text:
            self._login()
            text = self.http.get(path).text
            if marker not in text:
                raise RouterError(f"The router's answer from {path} was not what Tattle expected")
        return text

    def wan_counters(self) -> tuple[int, int]:
        text = self._page("/cgi-bin/get/New_GUI/Statistics.asp", '"EthRxTk00"')
        rx = re.search(r'"EthRxTk00"\s*:\s*"(\d+)"', text)
        tx = re.search(r'"EthTxTk00"\s*:\s*"(\d+)"', text)
        if not rx or not tx:
            raise RouterError("The internet counters are missing from the router's statistics page")
        return int(rx.group(1)), int(tx.group(1))

    def dhcp_enabled(self) -> bool:
        text = self._page("/cgi-bin/New_GUI/Network.asp", "lan_value")
        match = re.search(r"var lan_value\s*=\s*(\{.*?\});", text, re.S)
        if not match:
            raise RouterError("The LAN settings are missing from the router's network page")
        return json.loads(match.group(1)).get("lan_dhcp_type") != "0"

    def wan_login(self) -> str | None:
        """The PPPoE user name, e.g. ETIS_0221234567 (it carries the e& landline)."""
        text = self._page("/cgi-bin/get/New_GUI/Internet_setup.asp", "pppLists")
        match = re.search(r"pppLists\s*=\s*(\[.*?\])", text)
        if not match:
            raise RouterError("The internet login is missing from the router's setup page")
        fields = json.loads(match.group(1))
        return (fields[2] or None) if len(fields) > 2 else None

    def link_status(self) -> dict:
        """The router answers like 'up|5119 kbps|32766 kbps|N/A' (state, up, down)."""
        text = self._page("/cgi-bin/get/New_GUI/home_DSL_Status.asp", "|")
        parts = [part.strip() for part in text.strip().split("|")]
        if len(parts) < 3:
            raise RouterError("The router's DSL status was not what Tattle expected")

        def kbps(value: str) -> int | None:
            match = re.match(r"(\d+)\s*kbps", value)
            return int(match.group(1)) if match else None

        return {"state": parts[0], "up_kbps": kbps(parts[1]), "down_kbps": kbps(parts[2])}
