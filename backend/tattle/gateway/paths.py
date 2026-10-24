"""The files the gateway shares with the dashboard container, and how it writes them.

The dashboard mounts both directories read-only and runs as uid 1000, so every
directory is 0755 and every file 0644. Formats are a contract with
``tattle/sources.py`` and ``Collector.ingest_identity_hints``.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Paths:
    run_dir: str = "/run/tattle"  # lost on restart is fine: rewritten every few seconds
    state_dir: str = "/var/lib/tattle"  # kept across restarts and upgrades

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Paths:
        return cls(env.get("TATTLE_RUN_DIR", cls.run_dir), env.get("TATTLE_STATE_DIR", cls.state_dir))

    @property
    def nft_snapshot(self) -> str:
        return os.path.join(self.run_dir, "nft.json")

    @property
    def status(self) -> str:
        return os.path.join(self.run_dir, "gateway.json")

    @property
    def dnsmasq_conf(self) -> str:
        return os.path.join(self.run_dir, "dnsmasq.conf")

    @property
    def leases(self) -> str:
        return os.path.join(self.state_dir, "dnsmasq.leases")

    @property
    def dhcp_dir(self) -> str:
        return os.path.join(self.state_dir, "dhcp")

    def create(self) -> None:
        for path in (self.run_dir, self.state_dir, self.dhcp_dir):
            ensure_dir(path)


def ensure_dir(path: str) -> None:
    os.makedirs(path, mode=0o755, exist_ok=True)
    os.chmod(path, 0o755)  # makedirs' mode is filtered by the umask, and the volume may predate us


def atomic_write(path: str, data: str | bytes) -> None:
    """Readers never see a half-written file: write a temporary file, then rename it.

    The temporary name ends in ``.tmp`` so the dashboard (which looks for
    ``*.json``) never picks it up, and is unique so two threads can't collide.
    """
    directory, name = os.path.split(path)
    fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
