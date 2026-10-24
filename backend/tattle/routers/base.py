"""What a router adapter implements.

Routers know different things. An adapter declares what it can read in
`capabilities` and implements those methods:

* "device_counters": bytes each device sent and received (router mode: Tattle
  counts per device without becoming the network's gateway)
* "wan_counters": the router's internet-side byte counters (a cross-check)
* "wan_login": the user name the router signs in to the internet with (PPPoE);
  with two lines on one account it tells Tattle which line is in use
* "link_status": DSL/fibre sync state and speeds
* "dhcp_enabled": whether the router's own DHCP server is on (gateway mode
  needs it off)

See docs/adding-a-router.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import ClassVar, Literal

from ..providers.base import Field

Capability = Literal["device_counters", "wan_counters", "wan_login", "link_status", "dhcp_enabled"]


class RouterError(Exception):
    """The router refused or could not answer; the message is shown to people."""


@dataclass(frozen=True)
class DeviceCounter:
    """Running totals for one device, as the router keeps them. They may restart
    (router reboot, device reconnect); Tattle treats a drop as a restart."""

    mac: str
    rx: int  # bytes the device downloaded
    tx: int  # bytes the device uploaded
    ip: str | None = None
    hostname: str | None = None


class RouterAdapter:
    id: ClassVar[str]
    name: ClassVar[str]  # e.g. "OpenWrt (nlbwmon)"
    status: ClassVar[Literal["stable", "beta"]] = "beta"
    capabilities: ClassVar[frozenset[str]] = frozenset()
    fields: ClassVar[tuple[Field, ...]] = ()
    notes: ClassVar[str] = ""
    docs: ClassVar[str] = ""
    # Counters that wrap around (2**32 on many routers); None when they don't.
    wan_modulus: ClassVar[int | None] = None
    device_modulus: ClassVar[int | None] = None
    # How often device counters are worth reading.
    poll_seconds: ClassVar[int] = 60

    def __init__(self, values: dict[str, str]):
        self.values = values

    def device_counters(self) -> list[DeviceCounter]:
        raise NotImplementedError

    def wan_counters(self) -> tuple[int, int]:
        """(received, sent) bytes on the internet side."""
        raise NotImplementedError

    def wan_login(self) -> str | None:
        raise NotImplementedError

    def link_status(self) -> dict:
        """{"state": "up", "down_kbps": int|None, "up_kbps": int|None}"""
        raise NotImplementedError

    def dhcp_enabled(self) -> bool:
        raise NotImplementedError

    def test(self) -> list[str]:
        """Read everything the adapter can, for the "Save and test" button."""
        found = []
        if "device_counters" in self.capabilities:
            found.append(f"{len(self.device_counters())} devices with traffic counters")
        if "wan_counters" in self.capabilities:
            rx, tx = self.wan_counters()
            found.append(f"internet counters {rx:,} B received / {tx:,} B sent")
        if "wan_login" in self.capabilities:
            found.append(f"internet login {self.wan_login() or 'not set'}")
        if "link_status" in self.capabilities:
            status = self.link_status()
            found.append(f"line {status.get('state')}, {status.get('down_kbps')} / {status.get('up_kbps')} kbps")
        if "dhcp_enabled" in self.capabilities:
            found.append(f"router DHCP {'on' if self.dhcp_enabled() else 'off'}")
        return found

    @classmethod
    def describe(cls) -> dict:
        return {
            "id": cls.id,
            "name": cls.name,
            "status": cls.status,
            "capabilities": sorted(cls.capabilities),
            "fields": [asdict(f) for f in cls.fields],
            "notes": cls.notes,
            "docs": cls.docs,
        }
