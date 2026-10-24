"""MAC address -> manufacturer, from Wireshark's `manuf` database."""

from __future__ import annotations


def is_private_mac(mac: str) -> bool:
    """Locally administered addresses: phones' per-network random MACs, extenders' NAT MACs."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


class VendorLookup:
    def __init__(self, path: str):
        # prefix length in bits -> {hex prefix -> vendor}
        self._tables: dict[int, dict[str, str]] = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip() or line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 2:
                        continue
                    prefix, _, bits = parts[0].partition("/")
                    nbits = int(bits) if bits else 24
                    hexdigits = prefix.replace(":", "").replace("-", "").lower()[: nbits // 4]
                    name = (parts[2] if len(parts) > 2 and parts[2] else parts[1]).strip()
                    self._tables.setdefault(nbits, {})[hexdigits] = name
        except OSError:
            pass

    def lookup(self, mac: str | None) -> str | None:
        if not mac or is_private_mac(mac):
            return None
        hexdigits = mac.replace(":", "").lower()
        for nbits in sorted(self._tables, reverse=True):
            name = self._tables[nbits].get(hexdigits[: nbits // 4])
            if name:
                return name
        return None
