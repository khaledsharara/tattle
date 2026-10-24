#!/usr/bin/env python3
"""Fail when a tracked file looks like it holds someone's real data.

Tattle's tests and docs are full of MAC addresses; real ones identify real
devices (and their maker). Made-up ones come from the documentation range
00:00:5e:00:53:xx (RFC 7042) or are locally administered (the "private Wi-Fi
address" kind, second-lowest bit of the first byte set). Databases and .env
files never belong in the repository.

    python scripts/check-privacy.py
"""

from __future__ import annotations

import re
import subprocess
import sys

MAC = re.compile(r"(?<![0-9a-fA-F:])((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})(?![0-9a-fA-F:])")
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".svg")
SKIP_FILES = {"frontend/package-lock.json"}
FORBIDDEN = re.compile(r"(^|/)(\.env|.*\.db|.*\.db-wal|.*\.db-shm|setup-code\.txt)$")


def made_up(mac: str) -> bool:
    mac = mac.lower()
    first = int(mac[:2], 16)
    return (
        mac.startswith("00:00:5e:00:53:")  # RFC 7042 documentation range
        or bool(first & 0x02)  # locally administered: private addresses, examples like aa:bb:..
        or mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff")
    )


def main() -> int:
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    problems = []
    for path in files:
        if FORBIDDEN.search(path):
            problems.append(f"{path}: this kind of file must not be committed")
            continue
        if path in SKIP_FILES or path.endswith(SKIP_SUFFIXES):
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for mac in MAC.findall(line):
                if not made_up(mac):
                    problems.append(f"{path}:{number}: {mac} looks like a real device's MAC; use 00:00:5e:00:53:xx")
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s). See CONTRIBUTING.md, 'Personal data'.")
        return 1
    print(f"Checked {len(files)} files: nothing that looks like real data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
