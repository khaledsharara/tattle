from __future__ import annotations

import socket
import struct

import pytest

from tattle.gateway import probe

MAC = probe.mac_bytes("00:00:5e:00:53:10")
ROGUE_MAC = probe.mac_bytes("00:00:5e:00:53:66")


def run_bpf(program, packet: bytes) -> int:
    """Just enough of a classic BPF interpreter for the filter we attach."""
    a = x = pc = 0
    while True:
        code, jt, jf, k = program[pc]
        if code == 0x28:  # ldh [k]
            a = int.from_bytes(packet[k : k + 2], "big") if k + 2 <= len(packet) else None
        elif code == 0x30:  # ldb [k]
            a = packet[k] if k < len(packet) else None
        elif code == 0x48:  # ldh [x + k]
            a = int.from_bytes(packet[x + k : x + k + 2], "big") if x + k + 2 <= len(packet) else None
        elif code == 0xB1:  # ldxb 4*([k]&0xf)
            x = (packet[k] & 0x0F) * 4
        elif code == 0x15:  # jeq #k
            pc += jt if a == k else jf
        elif code == 0x45:  # jset #k
            pc += jt if a & k else jf
        elif code == 0x06:  # ret #k
            return k
        else:
            raise AssertionError(f"opcode {code:#x}")
        if a is None:
            return 0  # out-of-bounds load: the kernel drops the packet
        pc += 1


def dhcp_reply(kind: int, server_id: str | None = "192.0.2.1", src: str = "192.0.2.1", vlan: bool = False) -> bytes:
    """A server's broadcast reply, as a rogue DHCP server would send it."""
    bootp = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        2, 1, 6, 0, 0x01020304, 0, 0x8000,
        b"\0" * 4, socket.inet_aton("192.0.2.77"), socket.inet_aton(src), b"\0" * 4,
        MAC.ljust(16, b"\0"), b"\0" * 64, b"\0" * 128,
    )  # fmt: skip
    options = probe.DHCP_MAGIC + bytes([0, 53, 1, kind])  # a pad byte, then the message type
    if server_id:
        options += bytes([54, 4]) + socket.inet_aton(server_id)
    options += bytes([255])
    ip = probe.udp_ipv4_packet(src, "255.255.255.255", 67, 68, (bootp + options).ljust(300, b"\0"))
    tag = struct.pack("!HH", probe.ETH_P_8021Q, 7) if vlan else b""
    return b"\xff" * 6 + ROGUE_MAC + tag + struct.pack("!H", probe.ETH_P_IP) + ip


def test_discover_is_a_valid_broadcast_request():
    frame = probe.dhcp_discover(MAC, 0xDEADBEEF)
    assert frame[:6] == b"\xff" * 6 and frame[6:12] == MAC and frame[12:14] == b"\x08\x00"
    ip = frame[14:34]
    assert probe.checksum(ip) == 0  # the header checksum is right
    assert socket.inet_ntoa(ip[12:16]) == "0.0.0.0" and socket.inet_ntoa(ip[16:20]) == "255.255.255.255"
    udp = frame[34:]
    sport, dport, length, _ = struct.unpack("!HHHH", udp[:8])
    assert (sport, dport, length) == (68, 67, len(udp))
    pseudo = ip[12:20] + struct.pack("!BBH", 0, 17, length)
    assert probe.checksum(pseudo + udp) == 0  # the UDP checksum is right
    bootp = udp[8:]
    assert len(bootp) >= probe.BOOTP_MIN
    assert bootp[0] == 1 and struct.unpack("!I", bootp[4:8])[0] == 0xDEADBEEF
    assert struct.unpack("!H", bootp[10:12])[0] == 0x8000  # broadcast flag: replies reach us without an address
    assert bootp[28:34] == MAC
    options = probe.dhcp_options(bootp[240:])
    assert options[53] == b"\x01"  # DHCPDISCOVER; we never send a DHCPREQUEST
    assert 50 not in options and 61 not in options


def test_parse_offer():
    reply = probe.parse_dhcp_reply(dhcp_reply(probe.DHCP_OFFER))
    assert reply == probe.DhcpReply(
        kind=2, xid=0x01020304, server_id="192.0.2.1", src_ip="192.0.2.1", src_mac="00:00:5e:00:53:66", your_ip="192.0.2.77"
    )
    assert reply.server == "192.0.2.1"
    tagged = probe.parse_dhcp_reply(dhcp_reply(probe.DHCP_ACK, vlan=True))
    assert tagged is not None and tagged.kind == probe.DHCP_ACK
    no_id = probe.parse_dhcp_reply(dhcp_reply(probe.DHCP_OFFER, server_id=None, src="192.0.2.9"))
    assert no_id.server == "192.0.2.9"  # falls back to the sender's address


@pytest.mark.parametrize(
    "frame",
    [
        b"",
        b"\xff" * 13,
        dhcp_reply(probe.DHCP_OFFER)[:60],  # truncated
        probe.dhcp_discover(MAC, 1),  # a client's request, not a server's reply
        b"\xff" * 12 + b"\x08\x06" + b"\0" * 28,  # ARP
    ],
)
def test_parse_rejects_everything_else(frame):
    assert probe.parse_dhcp_reply(frame) is None


def test_bpf_filter_only_passes_dhcp_server_packets():
    accept = probe.DHCP_SERVER_FILTER[-2][3]
    assert run_bpf(probe.DHCP_SERVER_FILTER, dhcp_reply(probe.DHCP_OFFER)) == accept
    assert run_bpf(probe.DHCP_SERVER_FILTER, probe.dhcp_discover(MAC, 1)) == 0  # source port 68
    tcp = bytearray(dhcp_reply(probe.DHCP_OFFER))
    tcp[23] = 6
    assert run_bpf(probe.DHCP_SERVER_FILTER, bytes(tcp)) == 0
    fragment = bytearray(dhcp_reply(probe.DHCP_OFFER))
    fragment[20:22] = struct.pack("!H", 0x0010)  # a later fragment: no UDP header to look at
    assert run_bpf(probe.DHCP_SERVER_FILTER, bytes(fragment)) == 0
    assert run_bpf(probe.DHCP_SERVER_FILTER, b"\xff" * 12 + b"\x08\x06" + b"\0" * 28) == 0


def test_bpf_program_is_accepted_by_the_kernel():
    """SO_ATTACH_FILTER works on any socket, so this checks the struct packing without privileges."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        probe.attach_filter(sock, probe.DHCP_SERVER_FILTER)


def test_router_solicitation():
    rs = probe.router_solicitation(MAC)
    assert rs[0] == 133 and rs[1] == 0 and len(rs) == 16
    assert rs[8:10] == b"\x01\x01" and rs[10:] == MAC
    assert len(probe.router_solicitation(None)) == 8


def test_parse_router_advert():
    prefix = struct.pack("!BBBBIII16s", 3, 4, 64, 0xC0, 86400, 14400, 0, socket.inet_pton(socket.AF_INET6, "2001:db8:1::"))
    mtu = struct.pack("!BBHI", 5, 1, 0, 1500)
    message = struct.pack("!BBHBBHII", 134, 0, 0, 64, 0x40, 1800, 0, 0) + mtu + prefix
    advert = probe.parse_router_advert(message)
    assert advert == probe.RouterAdvert(lifetime=1800, managed=False, other=True, prefixes=["2001:db8:1::/64"])
    assert probe.parse_router_advert(message[:10]) is None
    assert probe.parse_router_advert(b"\x87" + message[1:]) is None  # not an advert
    looping = struct.pack("!BBHBBHII", 134, 0, 0, 64, 0, 0, 0, 0) + bytes([3, 0]) + b"\0" * 30
    assert probe.parse_router_advert(looping).prefixes == []  # zero-length option: stop, don't spin


def test_icmp6_filter_passes_only_adverts():
    words = struct.unpack("=8I", probe.icmp6_filter(134))
    passing = [t for t in range(256) if not words[t >> 5] & (1 << (t & 31))]
    assert passing == [134]


def test_ipv6_disabled(tmp_path):
    conf = tmp_path / "sys" / "net" / "ipv6" / "conf"
    for name, value in (("all", "0"), ("eth0", "1"), ("eth1", "0")):
        (conf / name).mkdir(parents=True)
        (conf / name / "disable_ipv6").write_text(value + "\n")
    assert probe.ipv6_disabled("eth0", str(tmp_path))
    assert not probe.ipv6_disabled("eth1", str(tmp_path))
    assert probe.ipv6_disabled("eth1", str(tmp_path / "nowhere"))  # kernel without IPv6


def test_mac_helpers():
    assert probe.mac_text(probe.mac_bytes("00-00-5E-00-53-01")) == "00:00:5e:00:53:01"
    with pytest.raises(ValueError):
        probe.mac_bytes("00:00:5e")
