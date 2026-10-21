"""Tests for the per-tenant rate limiter (src/heatbeat/ratelimit.py), scenario S17."""

from heatbeat.ratelimit import TokenBucketLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_s17_one_tenant_runs_out_others_are_unaffected():
    limiter = TokenBucketLimiter(per_minute=3, clock=Clock())

    tenant_a = [limiter.acquire("a") for _ in range(4)]
    tenant_b = limiter.acquire("b")

    assert tenant_a[:3] == [0, 0, 0]
    assert tenant_a[3] > 0  # the 4th request has to wait
    assert tenant_b == 0  # another customer still gets through


def test_tokens_refill_over_time():
    clock = Clock()
    limiter = TokenBucketLimiter(per_minute=60, burst=1, clock=clock)
    limiter.acquire("a")

    assert limiter.acquire("a") > 0
    clock.now += 1.0  # 60 per minute = one token per second
    assert limiter.acquire("a") == 0


def test_wait_time_says_when_to_retry():
    limiter = TokenBucketLimiter(per_minute=6, burst=1, clock=Clock())  # one token every 10 s
    limiter.acquire("a")

    assert 9.9 < limiter.acquire("a") <= 10.0
