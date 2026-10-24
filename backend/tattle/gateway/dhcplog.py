"""DHCP fingerprints from dnsmasq's log, for telling devices apart.

With ``log-dhcp``, dnsmasq logs every request's parameter request list, vendor
class and the name the client gives. The dashboard uses these to tell an iPhone
from an Android phone and to link a phone's different private addresses
(``identity.fingerprint`` / ``identity.os_hint``).

With ``log-facility=-`` and ``--keep-in-foreground`` each line is
``dnsmasq-dhcp[PID]: <text>``; without --keep-in-foreground dnsmasq adds a
timestamp first (``Oct 24 10:00:00 dnsmasq-dhcp[42]: ``), and debug mode drops
the PID. Every variant is accepted. The DHCP text itself starts with the
transaction id, one per client exchange::

    3221225473 vendor class: android-dhcp-14
    3221225473 client provides name: example-phone
    3221225473 DHCPREQUEST(eth0) 192.0.2.50 00:00:5e:00:53:01
    3221225473 DHCPACK(eth0) 192.0.2.50 00:00:5e:00:53:01 example-phone
    3221225473 requested options: 1:netmask, 3:router, 6:dns-server,
    3221225473 requested options: 15:domain-name, 26:mtu, 28:broadcast

Results go to ``<state>/dhcp/<mac without colons>.json`` with the keys mac,
ip, hostname, client_id, options, vendor_class and updated, the files
``Collector.ingest_identity_hints`` reads. Everything in them comes from the
network, so it is only ever stored as JSON data, length-capped.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass

from .paths import atomic_write, ensure_dir

PREFIX = re.compile(r"^(?:<\d+>)?(?:[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d )?dnsmasq(?:-[a-z]+)?(?:\[\d+\])?: ")
LINE = re.compile(r"^(\d+) (.*)$")
MAC = r"((?:[0-9a-f]{2}:){5}[0-9a-f]{2})"
PACKET = re.compile(r"^DHCP(DISCOVER|REQUEST|INFORM)\([^)]*\)(?: \S+)? " + MAC)
MAX_TRANSACTIONS = 2000
_VALID_MAC = re.compile(r"^" + MAC + r"$")


def strip_prefix(line: str) -> str:
    """dnsmasq's own `dnsmasq-dhcp[42]: ` (and optional timestamp) removed, if present."""
    line = line.rstrip("\r\n")
    match = PREFIX.match(line)
    return line[match.end() :] if match else line


@dataclass(frozen=True)
class Fingerprint:
    mac: str
    options: list[str]
    vendor: str | None
    name: str | None


class FingerprintParser:
    """Turns log lines into a Fingerprint once a request's option list is complete.

    Memory is bounded: transactions that never finish are dropped wholesale
    once there are too many in flight.
    """

    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}  # xid -> vendor/name seen before the packet line
        self.current: dict[str, dict] = {}  # xid -> packet being described

    def feed(self, line: str) -> Fingerprint | None:
        match = LINE.match(strip_prefix(line))
        if not match:
            return None
        xid, text = match.groups()
        result = None
        if text.startswith("vendor class: "):
            self.pending.setdefault(xid, {})["vendor"] = text[len("vendor class: ") :]
        elif text.startswith("client provides name: "):
            self.pending.setdefault(xid, {})["name"] = text[len("client provides name: ") :]
        elif packet := PACKET.match(text):
            seen = self.pending.pop(xid, {})
            self.current[xid] = {"mac": packet.group(2), "options": [], **seen}
        elif text.startswith("requested options: ") and xid in self.current:
            item = self.current[xid]
            body = text[len("requested options: ") :]
            item["options"] += [part.split(":")[0].strip() for part in body.split(",") if part.strip()]
            if not body.rstrip().endswith(","):  # last line of the list
                self.current.pop(xid, None)
                result = Fingerprint(item["mac"], item["options"], item.get("vendor"), item.get("name"))
        if len(self.current) > MAX_TRANSACTIONS or len(self.pending) > MAX_TRANSACTIONS:
            self.current.clear()
            self.pending.clear()
        return result


@dataclass(frozen=True)
class LeaseLine:
    expires: int
    mac: str
    ip: str
    hostname: str | None
    client_id: str | None


def read_leases(path: str) -> list[LeaseLine]:
    """dnsmasq's lease file: `<expiry> <mac> <ip> <hostname|*> <client-id|*>` per line."""
    result = []
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4 or parts[0] == "duid" or not _VALID_MAC.match(parts[1].lower()):
                    continue
                try:
                    expires = int(parts[0])
                except ValueError:
                    continue
                hostname = None if parts[3] == "*" else parts[3]
                client_id = parts[4] if len(parts) > 4 and parts[4] != "*" else None
                result.append(LeaseLine(expires, parts[1].lower(), parts[2], hostname, client_id))
    except OSError:
        pass
    return result


def active_leases(leases: list[LeaseLine], now: float) -> int:
    return sum(1 for lease in leases if lease.expires == 0 or lease.expires > now)


class FingerprintStore:
    """The per-MAC JSON files. Each update merges into what is there, so the log
    (fingerprints) and the lease file (address, client id) both contribute."""

    def __init__(self, directory: str):
        self.directory = directory
        self.lock = threading.Lock()  # the log worker and the lease sync both write

    def path(self, mac: str) -> str:
        return os.path.join(self.directory, mac.replace(":", "") + ".json")

    def merge(self, mac: str, fields: dict[str, str], defaults: dict[str, str] | None = None) -> bool:
        """Set `fields`, and `defaults` only where the file has no value yet.

        True if the file changed. Unchanged files keep their mtime, so the
        dashboard doesn't re-read them. Empty values never erase anything:
        dnsmasq replays leases at start-up without the request details.
        """
        mac = mac.lower()
        if not _VALID_MAC.match(mac):
            return False
        new = {"mac": mac, **{k: v for k, v in fields.items() if v}}
        with self.lock:
            ensure_dir(self.directory)
            path = self.path(mac)
            try:
                with open(path) as fh:
                    record = json.load(fh)
                if not isinstance(record, dict):
                    record = {}
            except (OSError, ValueError):
                record = {}
            new.update({k: v for k, v in (defaults or {}).items() if v and not record.get(k)})
            if all(record.get(k) == v for k, v in new.items()):
                return False
            record.update(new)
            record["updated"] = int(time.time())
            atomic_write(path, json.dumps(record))
            return True

    def add_fingerprint(self, fp: Fingerprint) -> bool:
        return self.merge(
            fp.mac,
            {
                "options": ",".join(fp.options)[:200],
                "vendor_class": (fp.vendor or "")[:200],
                "hostname": (fp.name or "")[:64],
            },
        )

    def sync_leases(self, leases: list[LeaseLine]) -> int:
        """Address and client id from the lease file (what dnsmasq's lease hook
        used to pass); returns how many files changed.

        The lease's hostname is only a fallback: dnsmasq stores a cleaned-up
        copy, and the name as the client sent it (from the log) is better.
        """
        changed = 0
        for lease in leases:
            changed += self.merge(
                lease.mac,
                {"ip": lease.ip[:45], "client_id": (lease.client_id or "")[:200]},
                {"hostname": (lease.hostname or "")[:64]},
            )
        return changed
