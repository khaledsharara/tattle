"""The nftables table that makes this server a gateway and counts every device.

One table, ``inet tattle``, and nothing else in the host's firewall is touched:

* NAT: devices' internet traffic leaves on the same interface it came in on
  (a "one-armed" router), masqueraded so the replies come back through us too;
* counters: bytes per LAN IP in each direction (sets ``dev_up``/``dev_down``)
  plus the server's own traffic (``server_up``/``server_down``), which is what
  the dashboard reads from the snapshot;
* ICMP redirects out of the LAN interface are dropped, or the kernel would
  helpfully tell every device to skip us and talk to the router directly;
* a forwarding self-test: the same LAN->internet packets are counted once
  before every other firewall (``fwd_seen``) and once after them
  (``fwd_passed``). Packets that reach the first but not the second are being
  dropped by something else on this server.

The rendered ruleset's hash is stored in the table comment. A container restart
or an upgrade that renders the same rules leaves the live table, and so every
counter, alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .config import GatewayConfig

log = logging.getLogger("tattle.gateway.nft")

TABLE = "tattle"
FAMILY = "inet"
RULESET_VERSION = 1  # bump when the rendered rules change meaning, not just text
_HASH = re.compile(r"sha256=([0-9a-f]{16})")


def set_size(cfg: GatewayConfig) -> int:
    """Room for every address in the subnet (a /24 has 254), within nftables' practical limits."""
    return max(64, min(cfg.lan.num_addresses - 2, 65536))


def render_body(cfg: GatewayConfig) -> str:
    lan_if, lan, server, size = json.dumps(cfg.lan_if), cfg.lan, cfg.server_ip, set_size(cfg)
    outbound = f'iifname {lan_if} oifname {lan_if} ip saddr {lan} ip daddr != {lan}'
    inbound = f'iifname {lan_if} oifname {lan_if} ip daddr {lan} ip saddr != {lan}'
    return f"""\
	# Per-device byte counters, keyed by LAN IPv4 address. "up" = device -> internet.
	set dev_up {{
		type ipv4_addr
		size {size}
		flags dynamic
		counter
	}}
	set dev_down {{
		type ipv4_addr
		size {size}
		flags dynamic
		counter
	}}

	# The server's own internet traffic (host processes and containers).
	counter server_up {{
	}}
	counter server_down {{
	}}

	# Forwarding self-test: devices' internet packets before and after every other firewall.
	counter fwd_seen {{
	}}
	counter fwd_passed {{
	}}

	chain forward_seen {{
		type filter hook forward priority filter - 100; policy accept;
		{outbound} counter name "fwd_seen"
	}}

	chain nat_postrouting {{
		type nat hook postrouting priority srcnat + 10; policy accept;
		oifname {lan_if} ip saddr {lan} ip saddr != {server} ip daddr != {lan} masquerade
	}}

	# Devices must keep using this server as their gateway even though the
	# router is on the same network; never tell them otherwise.
	chain no_redirects {{
		type filter hook output priority filter; policy accept;
		oifname {lan_if} icmp type redirect drop
	}}

	# Accounting runs after iptables (priority filter) and firewalld (filter + 10),
	# so only traffic that is really forwarded gets counted.
	chain account_forward {{
		type filter hook forward priority filter + 20; policy accept;
		{outbound} counter name "fwd_passed" update @dev_up {{ ip saddr }}
		{inbound} update @dev_down {{ ip daddr }}
		iifname != {lan_if} oifname {lan_if} ip daddr != {lan} counter name "server_up"
		iifname {lan_if} oifname != {lan_if} ip saddr != {lan} counter name "server_down"
	}}

	chain account_output {{
		type filter hook output priority filter + 20; policy accept;
		oifname {lan_if} ip daddr != {lan} counter name "server_up"
	}}

	chain account_input {{
		type filter hook input priority filter + 20; policy accept;
		iifname {lan_if} ip saddr != {lan} counter name "server_down"
	}}
"""


def ruleset_hash(body: str) -> str:
    return hashlib.sha256(f"v{RULESET_VERSION}\n{body}".encode()).hexdigest()[:16]


def render(cfg: GatewayConfig) -> tuple[str, str]:
    """(table definition, its hash)."""
    body = render_body(cfg)
    digest = ruleset_hash(body)
    return f'table {FAMILY} {TABLE} {{\n\tcomment "tattle-gateway sha256={digest}"\n\n{body}}}\n', digest


def load_script(table: str) -> str:
    """Replace the table in one transaction: create-if-missing, delete, define."""
    return f"table {FAMILY} {TABLE}\ndelete table {FAMILY} {TABLE}\n{table}"


# --- reading a live table (pure) ---------------------------------------------------


def table_info(listing: dict | None) -> dict | None:
    for item in (listing or {}).get("nftables", []):
        if "table" in item and item["table"].get("name") == TABLE:
            return item["table"]
    return None


def live_hash(listing: dict | None) -> str | None:
    info = table_info(listing)
    match = _HASH.search(str((info or {}).get("comment", "")))
    return match.group(1) if match else None


def named_counters(listing: dict | None) -> dict[str, tuple[int, int]]:
    """Named counter -> (packets, bytes)."""
    result = {}
    for item in (listing or {}).get("nftables", []):
        counter = item.get("counter") if isinstance(item, dict) else None
        if counter and counter.get("table") in (TABLE, None) and "name" in counter:
            result[counter["name"]] = (int(counter.get("packets", 0)), int(counter.get("bytes", 0)))
    return result


# --- talking to nft ------------------------------------------------------------------


class NftError(Exception):
    pass


def run_nft(args: list[str], stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(["nft", *args], input=stdin, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NftError(str(exc)) from exc
    if proc.returncode != 0:
        raise NftError(proc.stderr.strip() or f"nft exited with {proc.returncode}")
    return proc.stdout


class Table:
    """Keeps the live table equal to the rendered one, without needless reloads.

    `lock` serialises everything that runs nft, so the snapshot taken just
    before a reload really is the last one with the old counters.
    """

    def __init__(self, cfg: GatewayConfig, nft: Callable[..., str] = run_nft):
        self.text, self.hash = render(cfg)
        self.nft = nft
        self.lock = threading.Lock()

    def listing(self) -> dict | None:
        """The live table as JSON, or None if it isn't there."""
        try:
            return json.loads(self.nft(["-j", "list", "table", FAMILY, TABLE]))
        except NftError:
            return None
        except ValueError as exc:
            raise NftError(f"unreadable nft output: {exc}") from exc

    def ensure(self, before_reload: Callable[[dict], None] | None = None) -> str:
        """'kept', 'created' or 'reloaded'. `before_reload` gets the old table's listing."""
        with self.lock:
            live = self.listing()
            if live is not None and live_hash(live) == self.hash:
                return "kept"
            if live is not None and before_reload is not None:
                before_reload(live)
            self.nft(["-f", "-"], load_script(self.text))
            state = "created" if live is None else "reloaded"
            log.info("nftables table %s (rules %s)", state, self.hash)
            return state

    def snapshot(self) -> tuple[str, dict] | None:
        """Raw `nft -j list table` output and its parsed form, for the exporter."""
        with self.lock:
            try:
                raw = self.nft(["-j", "list", "table", FAMILY, TABLE])
            except NftError:
                return None
        try:
            return raw, json.loads(raw)
        except ValueError:
            return None


def remove_table(nft: Callable[..., str] = run_nft) -> bool:
    try:
        nft(["delete", "table", FAMILY, TABLE])
        return True
    except NftError:
        return False


# --- forwarding self-test ----------------------------------------------------------------


@dataclass
class ForwardingTest:
    """Compares fwd_seen and fwd_passed between two readings."""

    min_packets: int = 20
    last: tuple[int, int] | None = None
    ok: bool | None = None
    detail: str = "No device has used this server as its gateway yet."

    def update(self, counters: dict[str, tuple[int, int]]) -> None:
        if "fwd_seen" not in counters or "fwd_passed" not in counters:
            return
        now = (counters["fwd_seen"][0], counters["fwd_passed"][0])
        previous, self.last = self.last, now
        if previous is None or now[0] < previous[0] or now[1] < previous[1]:
            return  # first reading, or the table was reloaded
        seen, passed = now[0] - previous[0], now[1] - previous[1]
        if seen < self.min_packets:
            return  # too quiet to judge; keep the last verdict
        if passed * 2 < seen:
            self.ok = False
            self.detail = (
                "Another firewall on this server (firewalld, ufw or Docker) is dropping devices' internet traffic before "
                "Tattle can pass it on. Allow forwarding on the home network interface (the installer can do this), "
                "or see 'Troubleshooting' in docs/gateway-mode.md."
            )
        else:
            self.ok = True
            self.detail = "Devices' internet traffic is passing through this server."
