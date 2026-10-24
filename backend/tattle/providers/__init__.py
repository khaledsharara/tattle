"""Internet provider plugins. Add yours to MODULES (see docs/adding-a-provider.md)."""

from __future__ import annotations

import importlib
import logging

from .base import Bundle, Challenge, Field, Line, Provider, ProviderError, SessionExpired, Usage

log = logging.getLogger("tattle.providers")

# Module names under tattle.providers, each defining one Provider subclass.
MODULES = [
    "eand_eg",
    "we_eg",
    "vodafone_eg",
    "aussie_bb",
    "telenet_be",
    "ogero_lb",
    "xfinity_us",
    "startca",
]

_registry: dict[str, type[Provider]] | None = None


def registry() -> dict[str, type[Provider]]:
    global _registry
    if _registry is None:
        found: dict[str, type[Provider]] = {}
        for name in MODULES:
            try:
                module = importlib.import_module(f"{__name__}.{name}")
            except Exception:  # one broken plugin must not take the dashboard down
                log.exception("provider %s failed to load", name)
                continue
            for value in vars(module).values():
                if isinstance(value, type) and issubclass(value, Provider) and value is not Provider and getattr(value, "id", None):
                    found[value.id] = value
        _registry = found
    return _registry


def get(provider_id: str) -> type[Provider]:
    try:
        return registry()[provider_id]
    except KeyError:
        raise ProviderError(f"Unknown provider {provider_id!r}") from None


def catalog() -> list[dict]:
    """Every provider, stable ones first, then by country and name."""
    return sorted(
        (cls.describe() for cls in registry().values()),
        key=lambda p: (p["status"] != "stable", p["country"], p["name"]),
    )


__all__ = [
    "Bundle",
    "Challenge",
    "Field",
    "Line",
    "Provider",
    "ProviderError",
    "SessionExpired",
    "Usage",
    "catalog",
    "get",
    "registry",
]
