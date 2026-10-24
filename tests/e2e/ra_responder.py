"""Answers IPv6 Router Solicitations on one interface with a Router Advertisement.

Stands in for a home router that announces IPv6, so the gateway e2e test can
check that Tattle notices. Run it in a network namespace that forwards IPv6
(as a real router does), or the kernel never joins ff02::2 and drops the
solicitations before they reach this socket.
"""

import socket
import struct
import sys

RS, RA = 133, 134


def main(ifname: str) -> None:
    index = socket.if_nametoindex(ifname)
    sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
    blocked = [0xFFFFFFFF] * 8  # struct icmp6_filter: a set bit blocks that type
    blocked[RS >> 5] &= ~(1 << (RS & 31))
    sock.setsockopt(socket.IPPROTO_ICMPV6, 1, struct.pack("=8I", *blocked))  # ICMP6_FILTER
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, index)
    prefix = struct.pack(
        "!BBBBIII16s", 3, 4, 64, 0xC0, 86400, 14400, 0, socket.inet_pton(socket.AF_INET6, "2001:db8:99::")
    )  # documentation prefix (RFC 3849), on-link + autonomous
    advert = struct.pack("!BBHBBHII", RA, 0, 0, 64, 0, 1800, 0, 0) + prefix
    while True:
        data, _ = sock.recvfrom(2048)
        if data[:1] == bytes([RS]):
            sock.sendto(advert, ("ff02::1", 0, 0, index))


if __name__ == "__main__":
    main(sys.argv[1])
