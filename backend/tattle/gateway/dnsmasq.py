"""dnsmasq: hands out addresses (naming this server as gateway and DNS) and answers DNS.

It runs in the foreground as a child of the gateway process, logging to its
stderr, which the gateway reads for DHCP fingerprints (see dhcplog.py).
"""

from __future__ import annotations

import subprocess

from .config import GatewayConfig
from .paths import Paths


def render(cfg: GatewayConfig, paths: Paths) -> str:
    server, router = cfg.server_ip, cfg.router_ip
    lines = [
        "# Written by the Tattle gateway on every start; edits here are lost.",
        "# --- where to listen",
        "port=53",
        "bind-interfaces",
        f"interface={cfg.lan_if}",
        f"listen-address={server}",
        "except-interface=lo",
        "# Stay root: switching to another user needs SETUID/SETGID, which the container doesn't",
        "# have. An empty group= also skips setgroups(), which needs SETGID even for root.",
        "user=root",
        "group=",
        "pid-file=",
        "# --- DNS",
        "no-resolv",
        "no-hosts",  # the container's /etc/hosts is the host's; don't serve it to the house
        *(f"server={upstream}" for upstream in cfg.dns),
        "cache-size=1000",
        "domain-needed",
        "bogus-priv",
        "stop-dns-rebind",
        f"address=/{cfg.local_name}/{server}",
        *(["# Devices only get IPv4 answers, so their traffic stays countable.", "filter-AAAA"] if cfg.filter_aaaa else []),
        "# --- DHCP",
        "dhcp-authoritative",
        "# Always broadcast replies: Wi-Fi extenders that translate MAC addresses (TP-Link RE",
        "# series and many others) lose replies sent straight to a phone's own MAC.",
        "dhcp-broadcast",
        f"dhcp-range={cfg.dhcp_start},{cfg.dhcp_end},{cfg.dhcp_lease}",
        f"dhcp-option=option:router,{server}",
        "# The router is the backup DNS, so names still resolve if this server is down.",
        f"dhcp-option=option:dns-server,{server},{router}",
        f"dhcp-leasefile={paths.leases}",
        "# --- logging: request details feed the device fingerprints (dhcplog.py)",
        "log-dhcp",
        "log-async=25",
        "log-facility=-",
    ]
    return "\n".join(lines) + "\n"


def start(conf_path: str) -> subprocess.Popen:
    """`--keep-in-foreground` so we supervise it; stdout and stderr share one pipe."""
    return subprocess.Popen(
        ["dnsmasq", "--keep-in-foreground", f"--conf-file={conf_path}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
