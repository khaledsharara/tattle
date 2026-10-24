"""MikroTik RouterOS v7, through its REST API (https://<router>/rest/...).

Per-device totals come from Kid Control: /ip/kid-control/device keeps
`bytes-down` (what the device downloaded) and `bytes-up` for every device in
the ARP table, but only once at least one kid profile exists. A profile that
nobody is assigned to restricts nothing, so the setup guide adds one with
every hour allowed. These counters also include traffic between a device and
the router itself (DNS, the router's web pages), not only the internet.

The ARP table lists the modem or ISP equipment on the internet side too;
Tattle leaves out devices seen on the internet interface. REST answers every
value as a string, counters included. See docs/routers/mikrotik_rest.md.
"""

from __future__ import annotations

import base64
from typing import Any

from ..providers.base import Field, ProviderError
from ..providers.http import Http, Transport
from .base import DeviceCounter, RouterAdapter, RouterError

KID_SETUP = "/ip kid-control add name=tattle mon=0s-1d tue=0s-1d wed=0s-1d thu=0s-1d fri=0s-1d sat=0s-1d sun=0s-1d"


def _yes(value: str | None) -> bool:
    return (value or "").strip().lower() in ("yes", "y", "true", "1", "on")


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RouterError("The router sent a byte counter Tattle could not read") from None


class MikrotikRest(RouterAdapter):
    id = "mikrotik_rest"
    name = "MikroTik RouterOS 7 (REST API)"
    status = "beta"
    capabilities = frozenset({"device_counters", "wan_counters", "wan_login"})
    fields = (
        Field("url", "Router address", placeholder="https://192.168.88.1", help="https:// needs the www-ssl service; http:// works from RouterOS 7.9 but sends the password unencrypted."),
        Field("user", "User name", placeholder="tattle", help="A read-only user; the setup guide shows how to make one."),
        Field("password", "Password", kind="password"),
        Field("wan_interface", "Internet interface", placeholder="ether1", optional=True, help="The interface facing the modem or ISP: ether1, or pppoe-out1 when the router dials PPPoE itself."),
        Field("verify_tls", "Check the router's certificate", placeholder="no", optional=True, help="yes only if the router's certificate is one this computer trusts; RouterOS makes self-signed ones."),
    )
    notes = "Per-device totals from Kid Control (one profile must exist; it can allow everything). Includes traffic to the router itself."
    docs = "docs/routers/mikrotik_rest.md"

    def __init__(self, values: dict[str, str], transport: Transport | None = None):
        super().__init__(values)
        url = (values.get("url") or "https://192.168.88.1").strip().rstrip("/")
        self.verify_tls = _yes(values.get("verify_tls"))
        self.http = Http(
            url if "://" in url else f"https://{url}",
            timeout=15.0, transport=transport, verify_tls=self.verify_tls, who="the router", error=RouterError,
        )  # fmt: skip
        credentials = f"{values.get('user') or ''}:{values.get('password') or ''}".encode()
        self.auth = {"Authorization": "Basic " + base64.b64encode(credentials).decode()}
        self.wan = (values.get("wan_interface") or "ether1").strip()

    def _get(self, path: str, what: str, **params: str) -> list[dict]:
        """GET /rest/<path>; RouterOS lists come back as JSON arrays."""
        response = self.http.get(f"/rest/{path}", params=params or None, headers=self.auth)
        try:
            body = response.json()
        except ProviderError:
            body = None
        detail = str(body.get("detail") or body.get("message") or "") if isinstance(body, dict) else ""
        if response.status == 401:
            raise RouterError("The router refused the user name or password")
        if response.status == 404:
            raise RouterError("The router has no REST API at /rest: it needs RouterOS 7.1 or newer with the www-ssl service on")
        if response.status != 200:
            if "permission" in detail or "not allowed" in detail:
                raise RouterError(f"The router account may not read {what}: give its group the read, api and rest-api policies")
            if "no such command" in detail:
                raise RouterError(f"This RouterOS version cannot show {what} (/{path})")
            raise RouterError(f"The router answered status {response.status} for {what}: {detail or 'no details'}")
        if not isinstance(body, list):
            raise RouterError(f"The router's answer for {what} was not what Tattle expected")
        return [entry for entry in body if isinstance(entry, dict)]

    def device_counters(self) -> list[DeviceCounter]:
        devices = self._get(
            "ip/kid-control/device", "the Kid Control devices", **{".proplist": "mac-address,ip-address,bytes-down,bytes-up,name"}
        )
        if not devices and not self._get("ip/kid-control", "the Kid Control profiles"):
            raise RouterError(f"RouterOS keeps per-device counters only once Kid Control has a profile. On the router, run: {KID_SETUP}")
        outside = self._internet_side_macs()
        names = self._lease_names()
        counters: dict[str, DeviceCounter] = {}
        for device in devices:
            mac = str(device.get("mac-address") or "").lower()
            if not mac or mac in outside or mac in counters:  # a MAC twice: keep the first entry
                continue
            # ip-address can hold several, comma-separated, IPv6 ones included.
            addresses = [a.strip() for a in str(device.get("ip-address") or "").split(",")]
            ip = next((a for a in addresses if "." in a), None)
            counters[mac] = DeviceCounter(
                mac,
                rx=_count(device.get("bytes-down")),
                tx=_count(device.get("bytes-up")),
                ip=ip,
                hostname=str(device.get("name") or "") or names.get(mac),
            )
        return list(counters.values())

    def _internet_side_macs(self) -> set[str]:
        """MACs the router sees on its internet side (the modem, the ISP's gear).
        Behind PPPoE that is the Ethernet port the PPPoE client dials over."""
        interfaces = {self.wan}
        if self.wan.startswith("pppoe"):
            interfaces |= {
                str(client.get("interface"))
                for client in self._get("interface/pppoe-client", "the PPPoE clients", **{".proplist": "name,interface"})
                if client.get("name") == self.wan
            }
        neighbours = self._get("ip/arp", "the ARP table", **{".proplist": "mac-address,interface"})
        try:  # the ISP's IPv6 router shows up here only
            neighbours += self._get("ipv6/neighbor", "the IPv6 neighbours", **{".proplist": "mac-address,interface"})
        except RouterError:
            pass
        return {str(entry.get("mac-address") or "").lower() for entry in neighbours if entry.get("interface") in interfaces}

    def _lease_names(self) -> dict[str, str]:
        leases = self._get("ip/dhcp-server/lease", "the DHCP leases", **{".proplist": "mac-address,host-name"})
        return {
            str(lease.get("mac-address")).lower(): str(lease["host-name"])
            for lease in leases
            if lease.get("mac-address") and lease.get("host-name")
        }

    def wan_counters(self) -> tuple[int, int]:
        """The internet interface's own counters (64-bit, so they do not wrap);
        rx-byte is what came in from the internet."""
        found = self._get("interface", "the interfaces", name=self.wan, **{".proplist": "name,rx-byte,tx-byte"})
        if not found:
            raise RouterError(f"The router has no interface called {self.wan!r}: set 'Internet interface' to the one facing the modem")
        return _count(found[0].get("rx-byte")), _count(found[0].get("tx-byte"))

    def wan_login(self) -> str | None:
        """The PPPoE user name, when the router dials PPPoE itself. A read-only
        account sees the user name; RouterOS hides the password from it."""
        clients = self._get("interface/pppoe-client", "the PPPoE clients", **{".proplist": "name,user,running,disabled"})
        chosen = (
            next((c for c in clients if c.get("name") == self.wan), None)
            or next((c for c in clients if c.get("running") == "true"), None)
            or next((c for c in clients if c.get("disabled") != "true"), None)
        )
        return (str(chosen.get("user") or "") or None) if chosen else None
