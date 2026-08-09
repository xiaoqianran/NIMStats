"""Round-robin NVIDIA API-key pool with an independent limiter per key."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass

from rate_limiter import RateLimiter


def load_api_keys() -> list[str]:
    """Load a deduplicated key list without ever logging key material."""
    candidates: list[str] = []
    multi = os.getenv("NIM_API_KEYS", "")
    if multi:
        candidates.extend(part.strip() for part in re.split(r"[\s,]+", multi))
    candidates.extend(
        value.strip()
        for value in (
            os.getenv("NIM_API_KEY", ""),
            os.getenv("NVIDIA_API_KEY", ""),
        )
        if value.strip()
    )

    unique: list[str] = []
    seen: set[str] = set()
    for key in candidates:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


@dataclass(frozen=True)
class ApiSlot:
    key: str
    limiter: RateLimiter


class ApiKeyPool:
    """Assign requests round-robin; every key retains its own 40 RPM window."""

    def __init__(self, keys: list[str], max_per_minute: int | None = None) -> None:
        if not keys:
            raise ValueError("At least one NVIDIA API key is required")
        self._slots = [ApiSlot(key, RateLimiter(max_per_minute)) for key in keys]
        self._lock = threading.Lock()
        self._next = 0
        self._acquires = 0

    @property
    def key_count(self) -> int:
        return len(self._slots)

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._acquires

    def acquire_with_index(
        self, preferred_indexes: list[int] | None = None
    ) -> tuple[int, str]:
        """Return ``(index, key)`` while preferring keys that listed the model."""
        with self._lock:
            allowed = {
                index
                for index in (preferred_indexes or range(len(self._slots)))
                if 0 <= index < len(self._slots)
            }
            if not allowed:
                allowed = set(range(len(self._slots)))
            index = self._next
            for _ in self._slots:
                if index in allowed:
                    break
                index = (index + 1) % len(self._slots)
            slot = self._slots[index]
            self._next = (index + 1) % len(self._slots)
            self._acquires += 1
        slot.limiter.wait()
        return index, slot.key

    def acquire(self, preferred_indexes: list[int] | None = None) -> str:
        """Return a rate-limited key. The key value must never be logged."""
        return self.acquire_with_index(preferred_indexes)[1]
