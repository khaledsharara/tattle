"""Letting devices' traffic through Docker's iptables firewall.

When Docker manages iptables it sets the FORWARD policy to DROP, which would
silently cut every device off. One rule in DOCKER-USER (Docker's chain for
exactly this) accepts traffic that comes in and goes out on the LAN interface;
it is tagged with a comment so it can be found and replaced later, even after
the interface changes.

The host may run iptables-nft or iptables-legacy, and this container has both.
Rules must go through the one Docker itself uses, found the way the Kubernetes
iptables-wrappers project does it: compare what each backend already holds.
Docker restarts wipe DOCKER-USER, so `ensure` is re-run every minute.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

COMMENT = "tattle-gateway"
BACKENDS = ("nft", "legacy")
WAIT = ["-w", "5"]  # wait for the xtables lock (bind-mounted from the host) instead of failing

Runner = Callable[[list[str]], tuple[int, str]]  # argv -> (exit code, stdout)


def run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


# --- iptables-save parsing (pure) --------------------------------------------------------


@dataclass
class SavedTable:
    policies: dict[str, str] = field(default_factory=dict)  # chain -> policy ("-" for user chains)
    rules: list[str] = field(default_factory=list)  # "-A CHAIN ..." lines


def parse_save(text: str) -> dict[str, SavedTable]:
    tables: dict[str, SavedTable] = {}
    current: SavedTable | None = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("*"):
            current = tables.setdefault(line[1:], SavedTable())
        elif current is None or not line or line.startswith("#"):
            continue
        elif line.startswith(":"):
            name, _, rest = line[1:].partition(" ")
            current.policies[name] = rest.split(" ")[0] if rest else "-"
        elif line.startswith("-A "):
            current.rules.append(line)
    return tables


def docker_weight(tables: dict[str, SavedTable]) -> int:
    """How much of Docker's setup a backend holds (our own leftovers don't count)."""
    chains = sum(name.startswith("DOCKER") for t in tables.values() for name in t.policies)
    return chains + sum("DOCKER" in rule and not ours(rule) for t in tables.values() for rule in t.rules)


def choose_backend(saves: dict[str, str]) -> str | None:
    """The backend Docker uses; else the one with rules at all; None if both are empty."""
    parsed = {b: parse_save(saves.get(b, "")) for b in BACKENDS}
    weights = {b: docker_weight(t) for b, t in parsed.items()}
    if any(weights.values()):
        return max(BACKENDS, key=lambda b: weights[b])  # ties go to nft, listed first
    counts = {b: sum(len(t.rules) for t in parsed[b].values()) for b in BACKENDS}
    if not any(counts.values()) and not any("filter" in parsed[b] for b in BACKENDS):
        return None
    return max(BACKENDS, key=lambda b: counts[b])


def ours(rule: str) -> bool:
    try:
        words = shlex.split(rule)
    except ValueError:
        return False
    return any(a == "--comment" and b == COMMENT for a, b in zip(words, words[1:], strict=False))


def wanted_rule(chain: str, lan_if: str) -> list[str]:
    return ["-A", chain, "-i", lan_if, "-o", lan_if, "-m", "comment", "--comment", COMMENT, "-j", "ACCEPT"]


@dataclass
class Plan:
    backend: str | None
    chain: str | None  # where the accept rule belongs; None when nothing drops forwarded traffic
    commands: list[list[str]]  # iptables argv lists to run, in order
    detail: str


def plan(saves: dict[str, str], lan_if: str | None) -> Plan:
    """What to run so exactly one correct accept rule exists (none at all if `lan_if` is None)."""
    backend = choose_backend(saves) if lan_if else None
    commands: list[list[str]] = []
    keep: tuple[str, str] | None = None
    chain = None
    if backend is not None:
        filt = parse_save(saves.get(backend, "")).get("filter", SavedTable())
        if "DOCKER-USER" in filt.policies:
            chain = "DOCKER-USER"
        elif filt.policies.get("FORWARD") == "DROP":
            chain = "FORWARD"  # Docker isn't running yet, or another tool set the drop policy
    for name in BACKENDS:
        filt = parse_save(saves.get(name, "")).get("filter", SavedTable())
        for rule in filt.rules:
            if not ours(rule):
                continue
            words = shlex.split(rule)
            if keep is None and name == backend and chain is not None and words == wanted_rule(chain, lan_if or ""):
                keep = (name, rule)
                continue
            commands.append([f"iptables-{name}", *WAIT, "-D", *words[1:]])
    if chain is not None and keep is None:
        commands.append([f"iptables-{backend}", *WAIT, "-I", chain, "1", *wanted_rule(chain, lan_if or "")[2:]])
    if backend is None:
        detail = "Docker's iptables firewall isn't in use here, so nothing needs opening."
    elif chain is None:
        detail = f"iptables-{backend} doesn't block forwarded traffic, so nothing needs opening."
    else:
        detail = f"Devices' traffic is allowed through Docker's firewall ({chain}, iptables-{backend})."
    return Plan(backend, chain, commands, detail)


# --- applying ----------------------------------------------------------------------------


def read_saves(runner: Runner = run) -> dict[str, str]:
    """Both backends' rules. Never `-t filter`: that would load legacy modules on an nft-only host."""
    saves = {}
    for name in BACKENDS:
        code, out = runner([f"iptables-{name}-save"])
        saves[name] = out if code == 0 else ""
    return saves


@dataclass
class ForwardState:
    ok: bool | None = None
    backend: str | None = None
    chain: str | None = None
    detail: str = "Not checked yet."

    def as_dict(self) -> dict:
        return {"ok": self.ok, "backend": self.backend, "chain": self.chain, "detail": self.detail}


def apply(lan_if: str | None, runner: Runner = run) -> ForwardState:
    """Make the rules match `plan`; with lan_if=None, remove every rule we ever added."""
    steps = plan(read_saves(runner), lan_if)
    for argv in steps.commands:
        code, out = runner(argv)
        if code != 0:
            return ForwardState(
                False,
                steps.backend,
                steps.chain,
                f"Couldn't update Docker's firewall ({' '.join(argv[:1] + argv[3:5])}): {out.strip()[:200]}",
            )
    return ForwardState(True, steps.backend, steps.chain, steps.detail)
