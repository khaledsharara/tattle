"""Run the device linker on demand and explain what it sees.

    python -m tattle.linkcli explain
    python -m tattle.linkcli run
    python -m tattle.linkcli run --ignore-time-gates

`run` does exactly what the collector does every 10 minutes. With
--ignore-time-gates it skips only the "watched for 12 h" and "30 min active"
waits (for a live test); fingerprints, no-overlap and clean hand-overs are
still required.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import db, identity
from .collector import Collector, LiveState
from .config import load_settings


def label(item: identity.Identity) -> str:
    if item.custom_name:
        return item.custom_name
    readable = [v for n, v in item.names.items() if identity.name_strength(n) != "token"]
    if readable:
        return readable[0]
    return item.mac or f"#{item.id}"


def explain(collector: Collector, now: int) -> None:
    sessions = collector.load_sessions(now)
    items = list(collector.load_identities(sessions).values())
    evidence = identity.handover_evidence(items)
    missing = [i for i in items if i.private and not i.fingerprint and i.sessions]
    if missing:
        print("Private-address devices still waiting for a DHCP fingerprint (next time they renew or rejoin):")
        for item in missing:
            print(f"  - {label(item)} ({item.mac})")
        print()
    pairs = 0
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            shared = set(a.names) & set(b.names)
            if not ((a.private and b.private and identity.same_kind(a, b)) or shared):
                continue
            pairs += 1
            overlap = identity.overlap_seconds(a.sessions, b.sessions)
            weight, moves = evidence.get((min(a.id, b.id), max(a.id, b.id)), (0.0, 0))
            watched_h = (now - max(a.first_seen, b.first_seen)) / 3600
            active_m = min(a.active_seconds, b.active_seconds) / 60
            gaps = []
            if overlap > identity.OVERLAP_TOLERANCE:
                gaps.append(f"online together for {overlap}s -> different devices")
            if weight < identity.AUTO_EVIDENCE:
                gaps.append(f"hand-over evidence {weight:.1f}/{identity.AUTO_EVIDENCE:.0f}")
            if watched_h * 3600 < identity.MIN_WATCHED_SECONDS:
                gaps.append(f"watched {watched_h:.1f}h/{identity.MIN_WATCHED_SECONDS / 3600:.0f}h")
            if active_m * 60 < identity.MIN_ACTIVE_FOR_AUTO:
                gaps.append(f"active {active_m:.0f}/{identity.MIN_ACTIVE_FOR_AUTO / 60:.0f} min")
            verdict = "WOULD MERGE" if not gaps else "not yet: " + "; ".join(gaps)
            print(f"{label(a)}  <->  {label(b)}")
            print(f"    {moves} hand-overs, no-overlap={'yes' if overlap <= identity.OVERLAP_TOLERANCE else 'NO'}, "
                  f"shared names={sorted(shared) or '-'}")
            print(f"    {verdict}")
    if not pairs:
        print("No candidate pairs yet (same kind of device, private addresses).")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m tattle.linkcli")
    parser.add_argument("command", choices=("explain", "run"))
    parser.add_argument("--ignore-time-gates", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings()
    db.init(settings.db_path)
    collector = Collector(settings, LiveState())
    now = int(time.time())
    if args.ignore_time_gates:
        identity.MIN_WATCHED_SECONDS = 0
        identity.MIN_ACTIVE_FOR_AUTO = 0
    if args.command == "explain":
        explain(collector, now)
        return 0
    before = collector.conn.execute("SELECT COALESCE(MAX(id), 0) FROM identity_events").fetchone()[0]
    collector.link_identities()
    events = collector.conn.execute(
        "SELECT kind, device_id, other_id, reason FROM identity_events WHERE id > ? ORDER BY id", (before,)
    ).fetchall()
    if not events:
        print("No merges or splits this run.")
    for event in events:
        print(f"{event['kind']}: identity {event['device_id']} {'into' if event['kind'] == 'merged' else 'from'} "
              f"{event['other_id']} - {event['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
