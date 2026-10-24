"""Shared test helpers."""

from __future__ import annotations

import os
import time
from typing import Callable

from tattle.config import Settings


def host_clock(settings: Settings) -> Callable[[], float]:
    """The fake host's "now": when its latest counter snapshot was written, so a
    collector under test always finds the gateway's snapshot fresh."""

    def clock() -> float:
        try:
            return os.stat(settings.nft_snapshot_path).st_mtime
        except OSError:
            return time.time()

    return clock
