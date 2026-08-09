"""Thread-safe client-side limiter for NVIDIA API requests."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Sliding-window limiter: at most ``max_per_minute`` starts per 60s.

    Requests are also evenly spaced.  Even spacing avoids a burst at the start
    of a workflow and lets many worker threads keep slow requests in flight
    without exceeding NVIDIA's per-key 40 RPM limit.
    """

    def __init__(
        self,
        max_per_minute: int | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_per_minute = int(
            max_per_minute
            if max_per_minute is not None
            else os.getenv("NIM_MAX_REQUESTS_PER_MINUTE", "40")
        )
        if self.max_per_minute < 1:
            self.max_per_minute = 1
        self._min_interval = 60.0 / self.max_per_minute
        self._times: deque[float] = deque()
        self._last_acquire: float | None = None
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep

    def wait(self) -> float:
        """Block until a request slot is available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()

                since_last = (
                    now - self._last_acquire
                    if self._last_acquire is not None
                    else self._min_interval
                )
                need_spacing = max(0.0, self._min_interval - since_last)

                if len(self._times) < self.max_per_minute and need_spacing <= 0:
                    self._times.append(now)
                    self._last_acquire = now
                    return waited

                sleep_for = need_spacing
                if len(self._times) >= self.max_per_minute:
                    sleep_for = max(
                        sleep_for,
                        60.0 - (now - self._times[0]) + 0.001,
                    )

            # Never sleep while holding the lock: another worker may have an
            # earlier reservation on a different wake-up cycle.
            sleep_for = max(sleep_for, 0.001)
            self._sleep(sleep_for)
            waited += sleep_for
