"""Looking for other DHCP servers and IPv6 routers on the home network.

Either one quietly undoes gateway mode. A second DHCP server (the router's,
switched back on, or a Wi-Fi extender set to "Auto") hands some devices the
router as their gateway, and their traffic is never counted. A router
advertising IPv6 lets devices send IPv6 straight to it, past Tattle, which
only counts IPv4.

So every ~10 minutes we broadcast a DHCPDISCOVER (never a DHCPREQUEST, so
nobody hands out a lease) and an ICMPv6 Router Solicitation, and note who
answers. A socket that only receives packets from UDP port 67 (filtered in the
kernel, so Python never sees the house's other traffic) also catches other
servers' offers and acks between probes.

Packet building and parsing are pure functions over bytes, tested without a
network.
"""

from __future__ import annotations

import ctypes
import fcntl
import random
import socket
import struct
from dataclasses import dataclass, field

ETH_P_IP = 0x0800
ETH_P_8021Q = 0x8100
SO_ATTACH_FILTER = 26
PACKET_OUTGOING = 4
ICMP6_FILTER = 1
SIOCGIFHWADDR = 0x8927
DHCP_MAGIC = b"\x63\x82\x53\x63"
BOOTP_MIN = 300  # some servers ignore shorter requests
DHCP_OFFER, DHCP_ACK, DHCP_NAK = 2, 5, 6
RS, RA = 133, 134

# Classic BPF, as `tcpdump -dd 'ip and udp src port 67'` would compile it:
# IPv4, UDP, not a later fragment, source port 67. (code, jt, jf, k)
DHCP_SERVER_FILTER: list[tuple[int, int, int, int]] = [
    (0x28, 0, 0, 12),  # ldh [12]            ethertype
    (0x15, 0, 8, ETH_P_IP),  # jeq #0x800 else drop
    (0x30, 0, 0, 23),  # ldb [23]            IP protocol
    (0x15, 0, 6, 17),  # jeq #17 (UDP) else drop
    (0x28, 0, 0, 20),  # ldh [20]            flags + fragment offset
    (0x45, 4, 0, 0x1FFF),  # jset #0x1fff -> drop (later fragment)
    (0xB1, 0, 0, 14),  # ldxb 4*([14]&0xf)  IP header length
    (0x48, 0, 0, 14),  # ldh [x+14]          UDP source port
    (0x15, 0, 1, 67),  # jeq #67 else drop
    (0x06, 0, 0, 0x40000),  # ret: accept the whole packet
    (0x06, 0, 0, 0),  # ret: drop
]


# --- checksums and headers -------------------------------------------------------------


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def mac_bytes(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return raw


def mac_text(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def udp_ipv4_packet(src: str, dst: str, sport: int, dport: int, payload: bytes, ident: int = 0) -> bytes:
    """An IPv4 header plus UDP datagram, both checksums filled in."""
    s, d = socket.inet_aton(src), socket.inet_aton(dst)
    length = 8 + len(payload)
    pseudo = s + d + struct.pack("!BBH", 0, socket.IPPROTO_UDP, length)
    udp_sum = checksum(pseudo + struct.pack("!HHHH", sport, dport, length, 0) + payload) or 0xFFFF
    udp = struct.pack("!HHHH", sport, dport, length, udp_sum) + payload
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), ident, 0, 64, socket.IPPROTO_UDP, 0, s, d)
    return header[:10] + struct.pack("!H", checksum(header)) + header[12:] + udp


# --- DHCP ------------------------------------------------------------------------------


def dhcp_discover(mac: bytes, xid: int) -> bytes:
    """A complete Ethernet frame: broadcast DHCPDISCOVER from `mac`, asking for broadcast replies.

    Using the server's own MAC as the client hardware address means switches and
    extenders deliver the replies to us; the broadcast flag means they arrive
    even though we have no address to receive them on.
    """
    bootp = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        1,  # BOOTREQUEST
        1,  # Ethernet
        6,
        0,
        xid,
        0,
        0x8000,  # broadcast flag
        b"\0" * 4,
        b"\0" * 4,
        b"\0" * 4,
        b"\0" * 4,
        mac.ljust(16, b"\0"),
        b"\0" * 64,
        b"\0" * 128,
    )
    options = DHCP_MAGIC + bytes([53, 1, 1]) + bytes([55, 4, 1, 3, 6, 54]) + bytes([255])
    payload = (bootp + options).ljust(BOOTP_MIN, b"\0")
    ip = udp_ipv4_packet("0.0.0.0", "255.255.255.255", 68, 67, payload, ident=xid & 0xFFFF)
    return b"\xff" * 6 + mac + struct.pack("!H", ETH_P_IP) + ip


def dhcp_options(data: bytes) -> dict[int, bytes]:
    result: dict[int, bytes] = {}
    i = 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        length = data[i + 1]
        result.setdefault(code, data[i + 2 : i + 2 + length])
        i += 2 + length
    return result


@dataclass(frozen=True)
class DhcpReply:
    kind: int  # DHCP message type: 2 offer, 5 ack, 6 nak
    xid: int
    server_id: str | None  # option 54
    src_ip: str
    src_mac: str
    your_ip: str

    @property
    def server(self) -> str:
        return self.server_id or self.src_ip


def parse_dhcp_reply(frame: bytes) -> DhcpReply | None:
    """A DHCP server's reply inside an Ethernet frame, or None for anything else."""
    if len(frame) < 14:
        return None
    offset, ethertype = 14, struct.unpack("!H", frame[12:14])[0]
    if ethertype == ETH_P_8021Q and len(frame) >= 18:
        offset, ethertype = 18, struct.unpack("!H", frame[16:18])[0]
    if ethertype != ETH_P_IP or len(frame) < offset + 20:
        return None
    ip = frame[offset:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[0] >> 4 != 4 or ihl < 20 or ip[9] != socket.IPPROTO_UDP or len(ip) < ihl + 8:
        return None
    if struct.unpack("!H", ip[6:8])[0] & 0x1FFF:
        return None
    sport = struct.unpack("!H", ip[ihl : ihl + 2])[0]
    bootp = ip[ihl + 8 :]
    if sport != 67 or len(bootp) < 240 or bootp[0] != 2 or bootp[236:240] != DHCP_MAGIC:
        return None
    options = dhcp_options(bootp[240:])
    kind = options.get(53, b"\0")[:1]
    server_id = options.get(54)
    return DhcpReply(
        kind=kind[0] if kind else 0,
        xid=struct.unpack("!I", bootp[4:8])[0],
        server_id=socket.inet_ntoa(server_id[:4]) if server_id and len(server_id) >= 4 else None,
        src_ip=socket.inet_ntoa(ip[12:16]),
        src_mac=mac_text(frame[6:12]),
        your_ip=socket.inet_ntoa(bootp[16:20]),
    )


# --- IPv6 router discovery ------------------------------------------------------------------


def router_solicitation(mac: bytes | None) -> bytes:
    """ICMPv6 type 133; the kernel fills in the checksum on raw ICMPv6 sockets."""
    message = struct.pack("!BBHI", RS, 0, 0, 0)
    return message + (bytes([1, 1]) + mac if mac else b"")  # source link-layer address option


@dataclass(frozen=True)
class RouterAdvert:
    lifetime: int  # seconds; 0 = "I'm not a default router"
    managed: bool  # M flag: addresses come from DHCPv6
    other: bool  # O flag
    prefixes: list[str] = field(default_factory=list)


def parse_router_advert(message: bytes) -> RouterAdvert | None:
    if len(message) < 16 or message[0] != RA or message[1] != 0:
        return None
    flags, lifetime = message[5], struct.unpack("!H", message[6:8])[0]
    prefixes = []
    i = 16
    while i + 2 <= len(message):
        kind, length = message[i], message[i + 1] * 8
        if length == 0 or i + length > len(message):
            break
        if kind == 3 and length >= 32:  # prefix information
            prefix = socket.inet_ntop(socket.AF_INET6, message[i + 16 : i + 32])
            prefixes.append(f"{prefix}/{message[i + 2]}")
        i += length
    return RouterAdvert(lifetime, bool(flags & 0x80), bool(flags & 0x40), prefixes)


def icmp6_filter(*passing: int) -> bytes:
    """struct icmp6_filter: a set bit blocks that type."""
    words = [0xFFFFFFFF] * 8
    for kind in passing:
        words[kind >> 5] &= ~(1 << (kind & 31))
    return struct.pack("=8I", *words)


# --- sockets ---------------------------------------------------------------------------------


def attach_filter(sock: socket.socket, program: list[tuple[int, int, int, int]]) -> None:
    """SO_ATTACH_FILTER takes a struct sock_fprog {unsigned short len; struct sock_filter *filter};
    the kernel copies the program, so the buffer only has to outlive this call."""
    blob = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *ins) for ins in program))
    fprog = struct.pack("HL", len(program), ctypes.addressof(blob))
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, fprog)


def open_dhcp_listener(ifname: str, timeout: float = 1.0) -> socket.socket:
    """Receives only DHCP server packets on `ifname`.

    Created with protocol 0 (receives nothing), filtered, then bound: no
    unfiltered packet can slip in before the filter is attached.
    """
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
    try:
        attach_filter(sock, DHCP_SERVER_FILTER)
        sock.bind((ifname, ETH_P_IP))
        sock.settimeout(timeout)
    except OSError:
        sock.close()
        raise
    return sock


def send_frame(ifname: str, frame: bytes) -> None:
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0) as sock:
        sock.bind((ifname, 0))
        sock.send(frame)


def interface_mac(ifname: str) -> bytes:
    """SIOCGIFHWADDR rather than /sys, which can describe another network namespace."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        info = fcntl.ioctl(sock.fileno(), SIOCGIFHWADDR, struct.pack("256s", ifname.encode()[:15]))
    return info[18:24]


def new_xid() -> int:
    return random.SystemRandom().getrandbits(32)


def ipv6_disabled(ifname: str, proc: str = "/proc") -> bool:
    for name in ("all", ifname):
        try:
            with open(f"{proc}/sys/net/ipv6/conf/{name}/disable_ipv6") as fh:
                if fh.read().strip() == "1":
                    return True
        except OSError:
            return True  # no IPv6 at all in this kernel
    return False


def solicit_routers(ifname: str, mac: bytes | None, wait: float, clock, recv_timeout: float = 0.5) -> dict[str, RouterAdvert]:
    """Send one Router Solicitation and collect adverts for `wait` seconds: source -> advert."""
    ifindex = socket.if_nametoindex(ifname)
    found: dict[str, RouterAdvert] = {}
    with socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6) as sock:
        sock.setsockopt(socket.IPPROTO_ICMPV6, ICMP6_FILTER, icmp6_filter(RA))
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, ifindex)
        sock.settimeout(recv_timeout)
        sock.sendto(router_solicitation(mac), ("ff02::2", 0, 0, ifindex))
        deadline = clock() + wait
        while clock() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except TimeoutError:
                continue
            if len(addr) >= 4 and addr[3] not in (0, ifindex):
                continue  # an advert on another interface
            advert = parse_router_advert(data)
            if advert is not None:
                found[addr[0].split("%")[0]] = advert
    return found
