"""Telling physical devices apart when their addresses change.

Phones use a different private MAC for every Wi-Fi network name, so one phone
on the router and on each extender looks like several devices. Three signals
link those identities back together:

* names a device announces about itself (DHCP hostname, Bonjour host and
  service names), compared after normalising;
* what kind of device it is, from the shape of its DHCP request;
* timing: one Wi-Fi radio is on one network at a time, so two identities that
  are ever online together are different devices, and a phone moving around
  the house shows up as one identity going quiet as another appears.

A shared specific name merges identities automatically; behaviour alone only
produces suggestions for the admin to confirm. Nothing here looks at what
anyone browses.
"""

from __future__ import annotations

import bisect
import logging
from collections import defaultdict
import queue
import re
import socket
import struct
import threading
import time
import unicodedata
from dataclasses import dataclass, field

from .oui import is_private_mac

log = logging.getLogger("tattle.identity")

# --- names -----------------------------------------------------------------------

_GENERIC = {
    "iphone", "ipad", "ipod", "android", "localhost", "unknown", "mac", "macbook", "macbook-air",
    "macbook-pro", "imac", "mac-mini", "apple-tv", "galaxy", "samsung", "windows", "desktop",
    "laptop", "pc", "phone", "tablet", "esp", "espressif", "linux", "ubuntu", "raspberrypi", "host",
}
# Names that describe a model rather than an owner: shared by every unit sold.
_MODEL_LIKE = re.compile(
    r"^(oppo|samsung|galaxy|redmi|xiaomi|mi|poco|huawei|honor|realme|vivo|oneplus|nokia|motorola|moto|"
    r"infinix|tecno|itel|pixel|sm|iphone|ipad|desktop|laptop|android)(-[a-z0-9]+)*$"
)
# Service IDs some Apple devices announce over Bonjour: never shown as a name,
# but the same ID under two addresses is the same device.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RANDOM_LIKE = re.compile(r"^(android-)?[0-9a-f]{8,}$|^(hostname|esp|tasmota|shelly)-|([0-9a-f]{2}-){2,}[0-9a-f]{2}$")


def normalise_name(name: str) -> str:
    """'Maya’s iPhone', 'Mayas-iPhone.local' and 'MAYAS IPHONE' all become 'mayas-iphone'."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"\.local\.?$", "", text)
    text = re.sub(r"['`’]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-\d+$", "", text) if re.search(r"[a-z]-\d$", text) else text  # Bonjour "-2" suffix
    return text


def name_strength(norm: str) -> str:
    """'personal' names and 'token' IDs identify one device; 'model' names one product line."""
    if _UUID.match(norm):
        return "token"
    if not norm or norm in _GENERIC or _RANDOM_LIKE.search(norm):
        return "none"
    if _MODEL_LIKE.match(norm):
        return "model"
    return "personal"


# --- what kind of device -------------------------------------------------------


def os_hint(options: str | None, vendor_class: str | None, model: str | None) -> str | None:
    """Best guess at the kind of device from its DHCP request and Bonjour model."""
    if model:
        for prefix, label in (
            ("iPhone", "iPhone"),
            ("iPad", "iPad"),
            ("Watch", "Apple Watch"),
            ("AppleTV", "Apple TV"),
            ("AudioAccessory", "HomePod"),
            ("Mac", "Mac"),
            ("iMac", "Mac"),
        ):
            if model.startswith(prefix):
                return label
    vendor = (vendor_class or "").lower()
    if vendor.startswith("android-dhcp"):
        return "Android"
    if vendor.startswith("msft"):
        return "Windows"
    if vendor.startswith("dhcpcd"):
        return "Linux"
    if vendor.startswith("udhcp"):
        return "Embedded device"
    opts = [o for o in (options or "").split(",") if o]
    if opts[:5] == ["1", "121", "3", "6", "15"] and "252" in opts:
        return "Mac" if "95" in opts or ("44" in opts and "46" in opts) else "iPhone or iPad"
    if "249" in opts and "43" in opts and "252" in opts:
        return "Windows"
    return None


def fingerprint(options: str | None, vendor_class: str | None) -> str | None:
    if not options and not vendor_class:
        return None
    return f"{options or ''}|{vendor_class or ''}"


# --- Bonjour (mDNS) ------------------------------------------------------------

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
# Service types whose instance names are usually the device's own name.
_NAMED_SERVICES = ("_companion-link._tcp.local", "_device-info._tcp.local", "_dosvc._tcp.local", "_rdlink._tcp.local")
QUERY_SERVICES = ("_companion-link._tcp.local", "_device-info._tcp.local", "_rdlink._tcp.local", "_dosvc._tcp.local")


@dataclass
class MdnsFacts:
    names: set[str] = field(default_factory=set)
    model: str | None = None


def _read_name(data: bytes, offset: int, depth: int = 0) -> tuple[str, int]:
    labels: list[str] = []
    jumped_end: int | None = None
    while True:
        if offset >= len(data) or depth > 20:
            raise ValueError("bad name")
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("bad pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if jumped_end is None:
                jumped_end = offset + 2
            offset = pointer
            depth += 1
            continue
        offset += 1
        labels.append(data[offset : offset + length].decode("utf-8", "replace"))
        offset += length
    return ".".join(labels), (jumped_end if jumped_end is not None else offset)


def parse_mdns(data: bytes, source_ip: str) -> MdnsFacts:
    """Names and model a device announces about itself in one mDNS response."""
    facts = MdnsFacts()
    if len(data) < 12:
        return facts
    _, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    if not flags & 0x8000:  # only responses carry a device's own records
        return facts
    offset = 12
    try:
        for _ in range(qd):
            _, offset = _read_name(data, offset)
            offset += 4
        for _ in range(an + ns + ar):
            name, offset = _read_name(data, offset)
            rtype, _, _, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
            offset += 10
            rdata_start, offset = offset, offset + rdlength
            lower = name.lower()
            if rtype == 1 and rdlength == 4 and lower.endswith(".local"):
                if socket.inet_ntoa(data[rdata_start:offset]) == source_ip:
                    facts.names.add(name[: -len(".local")])
            elif rtype == 12 and lower in _NAMED_SERVICES:
                instance, _ = _read_name(data, rdata_start)
                facts.names.add(instance.split("._")[0])
            elif rtype == 16 and lower.endswith("._device-info._tcp.local"):
                facts.names.add(name.split("._")[0])
                position = rdata_start
                while position < offset:
                    size = data[position]
                    entry = data[position + 1 : position + 1 + size].decode("utf-8", "replace")
                    position += 1 + size
                    if entry.lower().startswith("model="):
                        facts.model = entry[6:]
    except (ValueError, struct.error, OSError):
        pass
    facts.names = {n for n in facts.names if n and len(n) <= 64}
    return facts


def build_query(services: tuple[str, ...]) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0, len(services), 0, 0, 0)
    body = b""
    for service in services:
        for label in service.split("."):
            body += bytes([len(label)]) + label.encode()
        body += b"\x00" + struct.pack("!HH", 12, 1)  # PTR, IN
    return header + body


class MdnsListener(threading.Thread):
    """Listens to Bonjour announcements on the LAN and asks for names now and then.

    Results go to `results` as (source_ip, MdnsFacts).
    """

    QUERY_INTERVAL = 600

    def __init__(self, interface_ip: str):
        super().__init__(name="mdns", daemon=True)
        self.interface_ip = interface_ip
        self.results: queue.Queue[tuple[str, MdnsFacts]] = queue.Queue(maxsize=5000)
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._listen()
            except OSError as exc:
                log.warning("mDNS listener stopped (%s); retrying in 60s", exc)
                self.stop_event.wait(60)

    def _listen(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", MDNS_PORT))
        membership = socket.inet_aton(MDNS_GROUP) + socket.inet_aton(self.interface_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.interface_ip))
        sock.settimeout(5)
        next_query = 0.0
        query = build_query(QUERY_SERVICES)
        try:
            while not self.stop_event.is_set():
                if time.time() >= next_query:
                    sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
                    next_query = time.time() + self.QUERY_INTERVAL
                try:
                    data, (source_ip, _) = sock.recvfrom(9000)
                except socket.timeout:
                    continue
                if source_ip == self.interface_ip:
                    continue
                facts = parse_mdns(data, source_ip)
                if facts.names or facts.model:
                    try:
                        self.results.put_nowait((source_ip, facts))
                    except queue.Full:
                        pass
        finally:
            sock.close()


# --- linking identities ----------------------------------------------------------
#
# Evidence model. Upload traffic marks when an identity is present (a device
# only sends from the network it is on). For two identities of the same kind:
#
# * any real overlap in presence  -> different devices (never merged; an
#   automatic merge that later overlaps is split);
# * a hand-over: one goes silent and the other starts sending shortly after,
#   with no other same-kind identity starting in that window. Tight hand-overs
#   (re-joining Wi-Fi and getting an address takes seconds) weigh more.
#
# Automatic merges need enough exclusive hand-overs AND enough time watching
# both identities without them ever overlapping. Two different phones of the
# same model would sooner or later be online together, and are then kept apart.

LOOKBACK_SECONDS = 14 * 86400
OVERLAP_TOLERANCE = 20  # seconds: tick boundaries at a hand-over
SPLIT_OVERLAP = 60  # seconds online together that undo an automatic merge
TIGHT_GAP = 45
LOOSE_GAP = 180
TIGHT_WEIGHT = 3.0
LOOSE_WEIGHT = 1.0
AUTO_EVIDENCE = 8.0  # e.g. three clean, tight hand-overs
SUGGEST_EVIDENCE = 2.0
MIN_WATCHED_SECONDS = 12 * 3600  # both identities known at least this long
MIN_ACTIVE_FOR_AUTO = 1800  # and each seen sending for at least this long


@dataclass
class Identity:
    id: int
    mac: str | None
    custom_name: str | None
    first_seen: int
    os_hint: str | None
    fingerprint: str | None
    names: dict[str, str] = field(default_factory=dict)  # norm -> as announced
    sessions: list[tuple[int, int]] = field(default_factory=list)

    @property
    def private(self) -> bool:
        return bool(self.mac and is_private_mac(self.mac))

    @property
    def active_seconds(self) -> int:
        return sum(end - start for start, end in self.sessions)


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def overlap_seconds(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    total, i, j = 0, 0, 0
    while i < len(a) and j < len(b):
        start, end = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if end > start:
            total += end - start
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def same_kind(a: Identity, b: Identity) -> bool:
    """Same DHCP request shape (preferred) or, failing that, the same recognised kind."""
    if a.fingerprint and b.fingerprint:
        return a.fingerprint == b.fingerprint
    return bool(a.os_hint and a.os_hint == b.os_hint)


def handover_evidence(identities: list[Identity]) -> dict[tuple[int, int], tuple[float, int]]:
    """(id, id) -> (weight, number of hand-overs) for same-kind private identities.

    A hand-over is shared between all candidates that started in the window, so
    a coincidence in a busy moment counts for little.
    """
    candidates = [x for x in identities if x.private and x.sessions]
    arrivals = sorted((start, x.id) for x in candidates for start, _ in x.sessions)
    starts = [start for start, _ in arrivals]
    by_id = {x.id: x for x in candidates}
    evidence: dict[tuple[int, int], list[float]] = defaultdict(lambda: [0.0, 0])
    for leaving in candidates:
        for _, end in leaving.sessions:
            lo, hi = bisect.bisect_left(starts, end), bisect.bisect_right(starts, end + LOOSE_GAP)
            window = [
                (start - end, other)
                for start, other in arrivals[lo:hi]
                if other != leaving.id and same_kind(leaving, by_id[other])
            ]
            if not window:
                continue
            for gap, other in window:
                weight = (TIGHT_WEIGHT if gap <= TIGHT_GAP else LOOSE_WEIGHT) / len(window)
                key = (min(leaving.id, other), max(leaving.id, other))
                evidence[key][0] += weight
                evidence[key][1] += 1
    return {key: (value[0], int(value[1])) for key, value in evidence.items()}


@dataclass
class Link:
    a: Identity
    b: Identity
    reason: str
    score: float
    automatic: bool


def find_links(identities: list[Identity], now: int) -> list[Link]:
    links: list[Link] = []
    evidence = handover_evidence(identities)
    for i, a in enumerate(identities):
        for b in identities[i + 1 :]:
            if overlap_seconds(a.sessions, b.sessions) > OVERLAP_TOLERANCE:
                continue  # online at the same time: two different devices
            if a.os_hint and b.os_hint and a.os_hint != b.os_hint:
                continue
            shared = set(a.names) & set(b.names)
            personal = [n for n in shared if name_strength(n) == "personal"]
            tokens = [n for n in shared if name_strength(n) == "token"]
            if (personal or tokens) and (a.private or b.private):
                reason = (
                    f"Both call themselves “{a.names.get(personal[0]) or b.names[personal[0]]}”"
                    if personal
                    else "Both announce the same Apple device ID"
                )
                links.append(Link(a, b, reason, 1.0, automatic=True))
                continue
            if not (a.private and b.private and same_kind(a, b)):
                continue
            weight, moves = evidence.get((min(a.id, b.id), max(a.id, b.id)), (0.0, 0))
            if any(name_strength(n) == "model" for n in shared):
                weight += TIGHT_WEIGHT  # the same model name is one more clue
            watched = now - max(a.first_seen, b.first_seen)
            kind = a.os_hint or "device"
            reason = (
                f"Same kind of device ({kind}), never online together, and it moved from one to the "
                f"other {moves} {'time' if moves == 1 else 'times'}"
            )
            if (
                weight >= AUTO_EVIDENCE
                and watched >= MIN_WATCHED_SECONDS
                and min(a.active_seconds, b.active_seconds) >= MIN_ACTIVE_FOR_AUTO
            ):
                links.append(Link(a, b, reason, min(1.0, weight / (2 * AUTO_EVIDENCE) + 0.5), automatic=True))
            elif weight >= SUGGEST_EVIDENCE:
                links.append(Link(a, b, reason, min(0.9, weight / (2 * AUTO_EVIDENCE)), automatic=False))
    return links


def merge_target(a: Identity, b: Identity) -> tuple[Identity, Identity]:
    """(keep, fold): keep the one you named, else the older one."""
    if bool(a.custom_name) != bool(b.custom_name):
        return (a, b) if a.custom_name else (b, a)
    return (a, b) if (a.first_seen, a.id) <= (b.first_seen, b.id) else (b, a)
