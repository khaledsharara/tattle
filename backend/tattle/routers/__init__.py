"""Router adapters. Add yours to MODULES (see docs/adding-a-router.md)."""

from __future__ import annotations

import importlib
import logging

from .base import DeviceCounter, RouterAdapter, RouterError

log = logging.getLogger("tattle.routers")

# Module names under tattle.routers, each defining one RouterAdapter subclass.
MODULES = [
    "dlink_dsl245ge",
    "openwrt_nlbwmon",
    "mikrotik_rest",
    "unifi",
]

_registry: dict[str, type[RouterAdapter]] | None = None


def registry() -> dict[str, type[RouterAdapter]]:
    global _registry
    if _registry is None:
        found: dict[str, type[RouterAdapter]] = {}
        for name in MODULES:
            try:
                module = importlib.import_module(f"{__name__}.{name}")
            except Exception:  # one broken plugin must not take the dashboard down
                log.exception("router adapter %s failed to load", name)
                continue
            for value in vars(module).values():
                if (
                    isinstance(value, type)
                    and issubclass(value, RouterAdapter)
                    and value is not RouterAdapter
                    and getattr(value, "id", None)
                ):
                    found[value.id] = value
        _registry = found
    return _registry


def create(adapter_id: str, values: dict[str, str]) -> RouterAdapter:
    try:
        cls = registry()[adapter_id]
    except KeyError:
        raise RouterError(f"Unknown router type {adapter_id!r}") from None
    return cls(values)


def catalog() -> list[dict]:
    return sorted((cls.describe() for cls in registry().values()), key=lambda r: (r["status"] != "stable", r["name"]))


__all__ = ["DeviceCounter", "RouterAdapter", "RouterError", "catalog", "create", "registry"]
