"""Sliding-window limits per client address for the public endpoints (demo shops, sign-ups).

In-process: each API replica limits on its own, which bounds abuse per replica; the daily caps are counted in
Postgres, so they hold across replicas.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class Window:
    def __init__(self, limit: Callable[[], int], seconds: int = 3600) -> None:
        self._limit = limit  # read per check, so a settings change applies at once
        self._seconds = seconds
        self._hits: dict[str, deque[float]] = {}

    def wait(self, client: str) -> int | None:
        """Seconds until `client` may act again, or None if it may now."""
        now = time.monotonic()
        q = self._hits.setdefault(client, deque())
        while q and now - q[0] > self._seconds:
            q.popleft()
        return None if len(q) < self._limit() else int(self._seconds - (now - q[0])) + 1

    def hit(self, client: str) -> None:
        self._hits.setdefault(client, deque()).append(time.monotonic())

    def clear(self) -> None:
        self._hits.clear()
