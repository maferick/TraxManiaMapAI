from __future__ import annotations

import pytest

from src.ingestion.rate_limit import TokenBucket


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def tick(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_rejects_nonpositive_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=0.0)
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=-1.0)


def test_acquire_under_capacity_does_not_block() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(
        rate_per_second=2.0, capacity=2.0, clock=clock.tick, sleep=clock.sleep
    )
    bucket.acquire()
    bucket.acquire()
    assert clock.slept == []


def test_acquire_at_empty_bucket_waits_for_refill() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(
        rate_per_second=2.0, capacity=1.0, clock=clock.tick, sleep=clock.sleep
    )
    bucket.acquire()
    bucket.acquire()
    assert len(clock.slept) == 1
    assert clock.slept[0] == pytest.approx(0.5)


def test_rejects_request_exceeding_capacity() -> None:
    bucket = TokenBucket(rate_per_second=1.0, capacity=1.0)
    with pytest.raises(ValueError, match="capacity"):
        bucket.acquire(tokens=2.0)


def test_rejects_nonpositive_acquire() -> None:
    bucket = TokenBucket(rate_per_second=1.0)
    with pytest.raises(ValueError):
        bucket.acquire(tokens=0)
    with pytest.raises(ValueError):
        bucket.acquire(tokens=-1.0)


def test_bucket_does_not_exceed_capacity_on_refill() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(
        rate_per_second=1.0, capacity=1.0, clock=clock.tick, sleep=clock.sleep
    )
    clock.now = 1000.0  # simulate long idle
    bucket.acquire()
    # If capacity cap worked, the second acquire here would need to wait.
    bucket.acquire()
    assert clock.slept  # second acquire blocked
