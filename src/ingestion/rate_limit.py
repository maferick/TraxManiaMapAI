"""Token-bucket rate limiter.

Single-threaded by default (ingestion is serial). The lock makes it
safe to share across threads if a future concurrent ingestion worker
needs it without requiring an API change.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_second: float,
        *,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._capacity = capacity if capacity is not None else max(rate_per_second, 1.0)
        if self._capacity <= 0:
            raise ValueError("capacity must be positive")
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

    def acquire(self, tokens: float = 1.0) -> None:
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        if tokens > self._capacity:
            raise ValueError(f"cannot acquire {tokens} tokens; bucket capacity is {self._capacity}")
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait_s = deficit / self._rate
            self._sleep(wait_s)
