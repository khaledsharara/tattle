"""python -m tattle.gateway run|off|status|render"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import config, dnsmasq, dockerfw, nft, preflight, status
from .paths import Paths

STOP_SERVICE = "docker compose --profile gateway rm -sf gateway"
FRESH_SECONDS = 90  # gateway.json is rewritten every 30 s while the service runs


def load_config() -> config.GatewayConfig | None:
    try:
        return config.load()
    except config.ConfigError as exc:
        print(f"Tattle gateway: {exc}", file=sys.stderr)
        return None


def service_running(paths: Paths) -> bool:
    current = status.read(paths.status)
    return bool(current and current.get("state") == "running" and time.time() - current.get("updated", 0) < FRESH_SECONDS)


def cmd_run(paths: Paths) -> int:
    from .service import Gateway

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        paths.create()
        report = status.GatewayStatus(paths.status)
        report.set(state="refused", preflight=[preflight.Check("config", False, str(exc))])
        report.write()
        print(f"Tattle gateway can't start: {exc}", file=sys.stderr)
        return 2
    for label, value in config.describe(cfg):
        logging.getLogger("tattle.gateway").info("%s: %s", label, value)
    return Gateway(cfg, paths).run()


def cmd_off(paths: Paths, force: bool) -> int:
    if service_running(paths) and not force:
        print(
            "The gateway service is still running, and it would put its rules back within a minute.\n"
            f"Stop it first:\n\n    {STOP_SERVICE}\n\nthen run this again.",
            file=sys.stderr,
        )
        return 1
    removed = nft.remove_table()
    docker = dockerfw.apply(None)
    current = status.read(paths.status) or {}
    if os.path.isdir(paths.run_dir):
        report = status.GatewayStatus(paths.status, current.get("lan_if", ""), current.get("server_ip", ""), current.get("router_ip", ""))
        report.set(state="off")
        report.write()
    router = current.get("router_ip") or os.environ.get("TATTLE_ROUTER_IP") or "your router's address"
    lease = os.environ.get("TATTLE_DHCP_LEASE", config.DEFAULT_LEASE)
    print("Tattle's gateway is off." if removed or docker.ok else "Tattle's gateway was already off.")
    if docker.ok is False:
        print(f"Warning: {docker.detail}")
    print(
        f"""
Devices still have addresses from Tattle that name this server as their gateway.
To give the house its normal internet back:

  1. Open the router's settings (usually http://{router} in a browser) and turn
     its DHCP server back on. It is often under LAN, Network or DHCP settings.
  2. Turn Wi-Fi off and on again on each phone, laptop and TV (or unplug and
     replug wired ones). They then get an address from the router.
     Devices you don't touch switch over on their own within {lease}.

If you also turned off IPv6 or a Wi-Fi extender's DHCP for Tattle, you can turn
those back on now."""
    )
    return 0


def cmd_status(paths: Paths) -> int:
    cfg = load_config()
    if cfg is None:
        return 1
    running = service_running(paths)
    print("Tattle gateway" + (" (running)" if running else " (not running)"))
    for label, value in config.describe(cfg):
        print(f"  {label + ':':24} {value}")

    checks = preflight.run_checks(cfg, ports=not running)
    print("\nChecks" + (" (port checks skipped: the gateway itself holds the ports)" if running else ""))
    for check in checks:
        print(f"  [{'ok' if check.ok else 'FAIL'}] {check.detail}")

    listing = nft.Table(cfg).listing()
    _, wanted = nft.render(cfg)
    if listing is None:
        table = "missing"
    elif nft.live_hash(listing) == wanted:
        table = "in place"
    else:
        table = "in place, but older rules (restarting the gateway updates them)"
    plan = dockerfw.plan(dockerfw.read_saves(), cfg.lan_if)
    print(f"\nFirewall table:  {table}")
    print(f"Docker firewall: {plan.detail}" + (" (rule missing)" if plan.commands else ""))

    current = status.read(paths.status)
    if current:
        age = int(time.time() - current.get("updated", 0))
        dhcp, ipv6, fwd = current.get("dhcp", {}), current.get("ipv6_adverts", {}), current.get("forwarding", {})
        print(f"\nLast report ({age}s ago, state {current.get('state')}):")
        print(f"  Forwarding: {fwd.get('detail')}")
        print(f"  DHCP:       {dhcp.get('leases', 0)} devices hold an address. {dhcp.get('detail', '')}")
        print(f"  IPv6:       {ipv6.get('detail', '')}")
    return 0 if all(c.ok for c in checks) else 1


def cmd_render() -> int:
    cfg = load_config()
    if cfg is None:
        return 1
    text, _ = nft.render(cfg)
    print("# --- nftables (nft -f) ---")
    print(text)
    print("# --- dnsmasq.conf ---")
    print(dnsmasq.render(cfg, Paths.from_env()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tattle.gateway", description="Tattle's gateway mode.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="be the network's DHCP server and gateway (the container's job)")
    off = sub.add_parser("off", help="remove the firewall rules; run after stopping the service")
    off.add_argument("--force", action="store_true", help="even if the service looks like it is running")
    sub.add_parser("status", help="checks and the last health report")
    sub.add_parser("render", help="print the nftables rules and dnsmasq config")
    args = parser.parse_args(argv)

    os.umask(0o022)  # everything we write must be readable by the dashboard (uid 1000)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    paths = Paths.from_env()
    if args.command == "run":
        return cmd_run(paths)
    if args.command == "off":
        return cmd_off(paths, args.force)
    if args.command == "status":
        return cmd_status(paths)
    return cmd_render()


if __name__ == "__main__":
    sys.exit(main())
