"""Client-side rate limiter for NVIDIA API (default 40 requests/minute)."""

from __future__ import annotations

import os
import time
from collections import deque


class RateLimiter:
    """Sliding-window limiter: at most `max_per_minute` acquires per 60s."""

    def __init__(self, max_per_minute: int | None = None) -> None:
        self.max_per_minute = int(
            max_per_minute
            if max_per_minute is not None
            else os.getenv("NIM_MAX_REQUESTS_PER_MINUTE", "40")
        )
        if self.max_per_minute < 1:
            self.max_per_minute = 1
        self._min_interval = 60.0 / self.max_per_minute
        self._times: deque[float] = deque()
        self._last_acquire = 0.0

    def wait(self) -> float:
        """Block until a request slot is available. Returns seconds waited."""
        waited = 0.0
        while True:
            now = time.monotonic()
            # drop timestamps outside the window
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()

            since_last = now - self._last_acquire if self._last_acquire else self._min_interval
            need_spacing = max(0.0, self._min_interval - since_last)

            if len(self._times) < self.max_per_minute and need_spacing <= 0:
                self._times.append(now)
                self._last_acquire = now
                return waited

            # sleep until either spacing elapsed or oldest timestamp exits window
            sleep_for = need_spacing
            if len(self._times) >= self.max_per_minute:
                sleep_for = max(sleep_for, 60.0 - (now - self._times[0]) + 0.02)
            sleep_for = max(sleep_for, 0.02)
            time.sleep(sleep_for)
            waited += sleep_for
